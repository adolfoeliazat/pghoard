"""
pghoard - main pghoard daemon

Copyright (c) 2016 Ohmu Ltd
See LICENSE for details
"""
from contextlib import closing
from pghoard import config, logutil, statsd, version, wal
from pghoard.basebackup import PGBaseBackup
from pghoard.common import (
    create_alert_file,
    get_object_storage_config,
    replication_connection_string_using_pgpass,
    write_json_file,
)
from pghoard.compressor import CompressorThread
from pghoard.rohmu.inotify import InotifyWatcher
from pghoard.transfer import TransferAgent
from pghoard.receivexlog import PGReceiveXLog
from pghoard.rohmu import get_transfer
from pghoard.rohmu.errors import FileNotFoundFromStorageError, InvalidConfigurationError
from pghoard.webserver import WebServer
from queue import Empty, Queue
import argparse
import datetime
import dateutil.parser
import json
import logging
import os
import psycopg2
import random
import signal
import socket
import subprocess
import sys
import time


class PGHoard:
    def __init__(self, config_path):
        self.stats = None
        self.log = logging.getLogger("pghoard")
        self.log_level = None
        self.running = True
        self.config_path = config_path
        self.compression_queue = Queue()
        self.transfer_queue = Queue()
        self.syslog_handler = None
        self.config = {}
        self.site_transfers = {}
        self.state = {
            "backup_sites": {},
            "startup_time": datetime.datetime.utcnow().isoformat(),
        }
        self.load_config()

        if not os.path.exists(self.config["backup_location"]):
            os.makedirs(self.config["backup_location"])

        signal.signal(signal.SIGHUP, self.load_config)
        signal.signal(signal.SIGINT, self.quit)
        signal.signal(signal.SIGTERM, self.quit)
        self.time_of_last_backup = {}
        self.time_of_last_backup_check = {}
        self.basebackups = {}
        self.basebackups_callbacks = {}
        self.receivexlogs = {}
        self.compressors = []
        self.transfer_agents = []
        self.requested_basebackup_sites = set()

        self.inotify = InotifyWatcher(self.compression_queue)
        self.webserver = WebServer(
            self.config,
            self.requested_basebackup_sites,
            self.compression_queue,
            self.transfer_queue)

        for _ in range(self.config["compression"]["thread_count"]):
            compressor = CompressorThread(
                config=self.config,
                compression_queue=self.compression_queue,
                transfer_queue=self.transfer_queue,
                stats=self.stats)
            self.compressors.append(compressor)

        compressor_state = {}  # shared among transfer agents
        for _ in range(self.config["transfer"]["thread_count"]):
            ta = TransferAgent(
                config=self.config,
                compression_queue=self.compression_queue,
                transfer_queue=self.transfer_queue,
                stats=self.stats,
                shared_state_dict=compressor_state)
            self.transfer_agents.append(ta)

        logutil.notify_systemd("READY=1")
        self.log.info("pghoard initialized, own_hostname: %r, cwd: %r", socket.gethostname(), os.getcwd())

    def check_pg_versions_ok(self, pg_version_server, command):
        if pg_version_server is None:
            # remote pg version not available, don't create version alert in this case
            return False
        if not pg_version_server or pg_version_server <= 90200:
            self.log.error("pghoard does not support versions earlier than 9.2, found: %r", pg_version_server)
            create_alert_file(self.config, "version_unsupported_error")
            return False
        pg_version_client = self.config[command + "_version"]
        if pg_version_server // 100 != pg_version_client // 100:
            self.log.error("Server version: %r does not match %s version: %r",
                           pg_version_server, self.config[command + "_path"], pg_version_client)
            create_alert_file(self.config, "version_mismatch_error")
            return False
        return True

    def create_basebackup(self, site, connection_string, basebackup_path, callback_queue=None):
        pg_version_server = self.check_pg_server_version(connection_string)
        if pg_version_server:
            self.config["backup_sites"][site]["pg_version"] = pg_version_server
        if not self.check_pg_versions_ok(pg_version_server, "pg_basebackup"):
            if callback_queue:
                callback_queue.put({"success": False})
            return None

        # Note that this xlog file value will only be correct if no other basebackups are run
        # in parallel. PGHoard itself will never do this itself but if the user starts
        # one on his own, and if tablespaces are set to False we'll get an incorrect
        # start-wal-time since the pg_basebackup from pghoard will not generate a
        # new checkpoint. This means that this xlog information would not be the oldest
        # required to restore from this basebackup.
        current_xlog = wal.get_current_wal_from_identify_system(connection_string)

        thread = PGBaseBackup(
            config=self.config,
            site=site,
            connection_string=connection_string,
            basebackup_path=basebackup_path,
            compression_queue=self.compression_queue,
            transfer_queue=self.transfer_queue,
            callback_queue=callback_queue,
            start_wal_segment=current_xlog,
            pg_version_server=pg_version_server)
        thread.start()
        self.basebackups[site] = thread

    def check_pg_server_version(self, connection_string):
        pg_version = None
        try:
            with closing(psycopg2.connect(connection_string)) as c:
                pg_version = c.server_version
        except psycopg2.OperationalError as ex:
            self.log.warning("%s (%s) connecting to DB at: %r",
                             ex.__class__.__name__, ex, connection_string)
            if "password authentication" in str(ex) or "authentication failed" in str(ex):
                create_alert_file(self.config, "authentication_error")
            else:
                create_alert_file(self.config, "configuration_error")
        except Exception as ex:  # log all errors and return None; pylint: disable=broad-except
            self.log.exception("Problem in getting PG server version")
            self.stats.unexpected_exception(ex, where="check_pg_server_version")
        return pg_version

    def receivexlog_listener(self, site, xlog_location, connection_string, slot):
        pg_version_server = self.check_pg_server_version(connection_string)
        if pg_version_server:
            self.config["backup_sites"][site]["pg_version"] = pg_version_server
        if not self.check_pg_versions_ok(pg_version_server, "pg_receivexlog"):
            return

        self.inotify.add_watch(xlog_location)
        thread = PGReceiveXLog(
            config=self.config,
            connection_string=connection_string,
            xlog_location=xlog_location,
            slot=slot,
            pg_version_server=pg_version_server)
        thread.start()
        self.receivexlogs[site] = thread

    def create_backup_site_paths(self, site):
        site_path = os.path.join(self.config["backup_location"], self.config["path_prefix"], site)
        xlog_path = os.path.join(site_path, "xlog")
        basebackup_path = os.path.join(site_path, "basebackup")

        paths_to_create = [
            site_path,
            xlog_path,
            xlog_path + "_incoming",
            basebackup_path,
            basebackup_path + "_incoming",
        ]

        for path in paths_to_create:
            if not os.path.exists(path):
                os.makedirs(path)

        return xlog_path, basebackup_path

    def delete_remote_wal_before(self, wal_segment, site, pg_version):
        self.log.debug("Starting WAL deletion from: %r before: %r, pg_version: %r",
                       site, wal_segment, pg_version)
        storage = self.site_transfers.get(site)
        valid_timeline = True
        tli, log, seg = wal.name_to_tli_log_seg(wal_segment)
        while True:
            if valid_timeline:
                # Decrement one segment if we're on a valid timeline
                if seg == 0 and log == 0:
                    break
                seg, log = wal.get_previous_wal_on_same_timeline(seg, log, pg_version)
            wal_path = os.path.join(self.config["path_prefix"], site, "xlog",
                                    wal.name_for_tli_log_seg(tli, log, seg))
            self.log.debug("Deleting wal_file: %r", wal_path)
            try:
                storage.delete_key(wal_path)
                valid_timeline = True
            except FileNotFoundFromStorageError:
                if not valid_timeline or tli <= 1:
                    # if we didn't find any WALs to delete on this timeline or we're already at
                    # timeline 1 there's no need or possibility to try older timelines, break.
                    self.log.info("Could not delete wal_file: %r, returning", wal_path)
                    break
                # let's try the same segment number on a previous timeline, but flag that timeline
                # as "invalid" until we're able to delete at least one segment on it.
                valid_timeline = False
                tli -= 1
                self.log.info("Could not delete wal_file: %r, trying the same segment on a previous "
                              "timeline (%s)", wal_path, wal.name_for_tli_log_seg(tli, log, seg))
            except Exception as ex:  # FIXME: don't catch all exceptions; pylint: disable=broad-except
                self.log.exception("Problem deleting: %r", wal_path)
                self.stats.unexpected_exception(ex, where="delete_remote_wal_before")

    def delete_remote_basebackup(self, site, basebackup):
        storage = self.site_transfers.get(site)
        obj_key = os.path.join(self.config["path_prefix"], site, "basebackup", basebackup)
        try:
            storage.delete_key(obj_key)
        except FileNotFoundFromStorageError:
            self.log.info("Tried to delete non-existent basebackup %r", obj_key)
        except Exception as ex:  # FIXME: don't catch all exceptions; pylint: disable=broad-except
            self.log.exception("Problem deleting: %r", obj_key)
            self.stats.unexpected_exception(ex, where="delete_remote_basebackup")

    def get_remote_basebackups_info(self, site):
        storage = self.site_transfers.get(site)
        if not storage:
            storage_config = get_object_storage_config(self.config, site)
            storage = get_transfer(storage_config)
            self.site_transfers[site] = storage

        results = storage.list_path(os.path.join(self.config["path_prefix"], site, "basebackup"))
        for entry in results:
            # drop path from resulting list and convert timestamps
            entry["name"] = os.path.basename(entry["name"])
            entry["metadata"]["start-time"] = dateutil.parser.parse(entry["metadata"]["start-time"])

        results.sort(key=lambda entry: entry["metadata"]["start-time"])
        return results

    def check_backup_count_and_state(self, site):
        """Look up basebackups from the object store, prune any extra
        backups and return the datetime of the latest backup."""
        basebackups = self.get_remote_basebackups_info(site)
        self.log.debug("Found %r basebackups", basebackups)

        if basebackups:
            last_backup_time = basebackups[-1]["metadata"]["start-time"]
        else:
            last_backup_time = None

        allowed_basebackup_count = self.config["backup_sites"][site]["basebackup_count"]
        if allowed_basebackup_count is None:
            allowed_basebackup_count = len(basebackups)

        while len(basebackups) > allowed_basebackup_count:
            self.log.warning("Too many basebackups: %d > %d, %r, starting to get rid of %r",
                             len(basebackups), allowed_basebackup_count, basebackups, basebackups[0]["name"])
            basebackup_to_be_deleted = basebackups.pop(0)
            pg_version = basebackup_to_be_deleted["metadata"].get("pg-version")
            last_wal_segment_still_needed = 0
            if basebackups:
                last_wal_segment_still_needed = basebackups[0]["metadata"]["start-wal-segment"]

            if last_wal_segment_still_needed:
                self.delete_remote_wal_before(last_wal_segment_still_needed, site, pg_version)
            self.delete_remote_basebackup(site, basebackup_to_be_deleted["name"])
        self.state["backup_sites"][site]["basebackups"] = basebackups

        return last_backup_time

    def set_state_defaults(self, site):
        if site not in self.state["backup_sites"]:
            self.state["backup_sites"][site] = {"basebackups": []}

    def startup_walk_for_missed_files(self):
        """Check xlog and xlog_incoming directories for files that receivexlog has received but not yet
        compressed as well as the files we have compressed but not yet uploaded and process them."""
        for site in self.config["backup_sites"]:
            compressed_xlog_path, _ = self.create_backup_site_paths(site)
            uncompressed_xlog_path = compressed_xlog_path + "_incoming"

            # Process uncompressed files (ie WAL pg_receivexlog received)
            for filename in os.listdir(uncompressed_xlog_path):
                full_path = os.path.join(uncompressed_xlog_path, filename)
                if not wal.XLOG_RE.match(filename) and not wal.TIMELINE_RE.match(filename):
                    self.log.warning("Found invalid file %r from incoming xlog directory", full_path)
                    continue
                compression_event = {
                    "delete_file_after_compression": True,
                    "full_path": full_path,
                    "site": site,
                    "type": "CLOSE_WRITE",
                }
                self.log.debug("Found: %r when starting up, adding to compression queue", compression_event)
                self.compression_queue.put(compression_event)

            # Process compressed files (ie things we've processed but not yet uploaded)
            for filename in os.listdir(compressed_xlog_path):
                if filename.endswith(".metadata"):
                    continue  # silently ignore .metadata files, they're expected and processed below
                full_path = os.path.join(compressed_xlog_path, filename)
                metadata_path = full_path + ".metadata"
                is_xlog = wal.XLOG_RE.match(filename)
                is_timeline = wal.TIMELINE_RE.match(filename)
                if not ((is_xlog or is_timeline) and os.path.exists(metadata_path)):
                    self.log.warning("Found invalid file %r from compressed xlog directory", full_path)
                    continue
                with open(metadata_path, "r") as fp:
                    metadata = json.load(fp)

                transfer_event = {
                    "file_size": os.path.getsize(full_path),
                    "filetype": "xlog" if is_xlog else "timeline",
                    "local_path": full_path,
                    "metadata": metadata,
                    "site": site,
                    "type": "UPLOAD",
                }
                self.log.debug("Found: %r when starting up, adding to transfer queue", transfer_event)
                self.transfer_queue.put(transfer_event)

    def start_threads_on_startup(self):
        # Startup threads
        self.inotify.start()
        self.webserver.start()
        for compressor in self.compressors:
            compressor.start()
        for ta in self.transfer_agents:
            ta.start()

    def handle_site(self, site, site_config):
        self.set_state_defaults(site)
        xlog_path, basebackup_path = self.create_backup_site_paths(site)

        if not site_config["active"]:
            return  # If a site has been marked inactive, don't bother checking anything

        chosen_backup_node = random.choice(site_config["nodes"])

        if site not in self.receivexlogs and site_config["active_backup_mode"] == "pg_receivexlog":
            connection_string, slot = replication_connection_string_using_pgpass(chosen_backup_node)
            self.receivexlog_listener(site, xlog_path + "_incoming", connection_string, slot)

        if site not in self.time_of_last_backup_check or \
                time.monotonic() - self.time_of_last_backup_check[site] > 300:
            self.time_of_last_backup[site] = self.check_backup_count_and_state(site)
            self.time_of_last_backup_check[site] = time.monotonic()

        # check if a basebackup is running, or if a basebackup has just completed
        if site in self.basebackups:
            try:
                result = self.basebackups_callbacks[site].get(block=False)
            except Empty:
                # previous basebackup (or its compression and upload) still in progress
                return
            if self.basebackups[site].is_alive():
                self.basebackups[site].join()
            del self.basebackups[site]
            del self.basebackups_callbacks[site]
            self.log.debug("Basebackup has finished for %r: %r", site, result)
            self.time_of_last_backup[site] = self.check_backup_count_and_state(site)
            self.time_of_last_backup_check[site] = time.monotonic()

        new_backup_needed = False
        if site in self.requested_basebackup_sites:
            self.log.info("Creating a new basebackup for %r due to request", site)
            self.requested_basebackup_sites.discard(site)
            new_backup_needed = True
        elif site_config["basebackup_interval_hours"] is None:
            # Basebackups are disabled for this site (but they can still be requested over the API.)
            pass
        elif self.time_of_last_backup.get(site) is None:
            self.log.info("Creating a new basebackup for %r because there are currently none", site)
            new_backup_needed = True
        else:
            delta_since_last_backup = datetime.datetime.now(datetime.timezone.utc) - self.time_of_last_backup[site]
            if delta_since_last_backup >= datetime.timedelta(hours=site_config["basebackup_interval_hours"]):
                self.log.info("Creating a new basebackup for %r by schedule (%s from previous)",
                              site, delta_since_last_backup)
                new_backup_needed = True

        if new_backup_needed:
            connection_string, slot = replication_connection_string_using_pgpass(chosen_backup_node)
            self.basebackups_callbacks[site] = Queue()
            self.create_basebackup(site, connection_string, basebackup_path, self.basebackups_callbacks[site])

    def run(self):
        self.start_threads_on_startup()
        self.startup_walk_for_missed_files()
        while self.running:
            try:
                for site, site_config in self.config["backup_sites"].items():
                    self.handle_site(site, site_config)
                self.write_backup_state_to_json_file()
            except subprocess.CalledProcessError as ex:
                self.log.error("%s: %s", ex.__class__.__name__, ex)
            except Exception as ex:  # pylint: disable=broad-except
                self.log.exception("Unexpected exception in PGHoard main loop")
                self.stats.unexpected_exception(ex, where="pghoard_run")
            time.sleep(5.0)

    def write_backup_state_to_json_file(self):
        """Periodically write a JSON state file to disk"""
        start_time = time.time()
        state_file_path = self.config["json_state_file_path"]
        self.state["pg_receivexlogs"] = {
            key: {"latest_activity": value.latest_activity, "running": value.running}
            for key, value in self.receivexlogs.items()
        }
        self.state["pg_basebackups"] = {
            key: {"latest_activity": value.latest_activity, "running": value.running}
            for key, value in self.basebackups.items()
        }
        self.state["compressors"] = [compressor.state for compressor in self.compressors]
        self.state["transfer_agents"] = [ta.state for ta in self.transfer_agents]
        self.state["queues"] = {
            "compression_queue": self.compression_queue.qsize(),
            "transfer_queue": self.transfer_queue.qsize(),
        }
        self.log.debug("Writing JSON state file to %r", state_file_path)
        write_json_file(state_file_path, self.state)
        self.log.debug("Wrote JSON state file to disk, took %.4fs", time.time() - start_time)

    def load_config(self, _signal=None, _frame=None):  # pylint: disable=unused-argument
        self.log.debug("Loading JSON config from: %r, signal: %r", self.config_path, _signal)
        try:
            new_config = config.read_json_config_file(self.config_path)
        except (InvalidConfigurationError, subprocess.CalledProcessError, UnicodeDecodeError) as ex:
            self.log.exception("Invalid config file %r: %s: %s", self.config_path, ex.__class__.__name__, ex)
            # if we were called by a signal handler we'll ignore (and log)
            # the error and hope the user fixes the configuration before
            # restarting pghoard.
            if _signal is not None:
                return
            if isinstance(ex, InvalidConfigurationError):
                raise
            raise InvalidConfigurationError(self.config_path)

        self.config = new_config
        if self.config.get("syslog") and not self.syslog_handler:
            self.syslog_handler = logutil.set_syslog_handler(
                address=self.config.get("syslog_address", "/dev/log"),
                facility=self.config.get("syslog_facility", "local2"),
                logger=logging.getLogger(),
            )
        # NOTE: getLevelName() also converts level names to numbers
        self.log_level = logging.getLevelName(self.config["log_level"])
        try:
            logging.getLogger().setLevel(self.log_level)
        except ValueError:
            self.log.exception("Problem with log_level: %r", self.log_level)

        # statsd settings may have changed
        stats = self.config.get("statsd", {})
        self.stats = statsd.StatsClient(host=stats.get("host"), port=stats.get("port"),
                                        tags=stats.get("tags"))

        self.log.debug("Loaded config: %r from: %r", self.config, self.config_path)

    def quit(self, _signal=None, _frame=None):  # pylint: disable=unused-argument
        self.log.warning("Quitting, signal: %r", _signal)
        self.running = False
        self.inotify.running = False
        all_threads = [self.webserver]
        all_threads.extend(self.basebackups.values())
        all_threads.extend(self.receivexlogs.values())
        all_threads.extend(self.compressors)
        all_threads.extend(self.transfer_agents)
        for t in all_threads:
            t.running = False
        for t in all_threads:
            if t.is_alive():
                t.join()


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="pghoard",
        description="postgresql automatic backup daemon")
    parser.add_argument("--version", action="version", help="show program version",
                        version=version.__version__)
    parser.add_argument("-s", "--short-log", help="use non-verbose logging format", action="store_true")
    parser.add_argument("--config", help="configuration file path", default=os.environ.get("PGHOARD_CONFIG"))
    parser.add_argument("config_file", help="configuration file path (for backward compatibility)",
                        nargs="?")
    arg = parser.parse_args(args)

    config_path = arg.config or arg.config_file
    if not config_path:
        print("pghoard: config file path must be given with --config or via env PGHOARD_CONFIG")
        return 1

    if not os.path.exists(config_path):
        print("pghoard: {!r} doesn't exist".format(config_path))
        return 1

    logutil.configure_logging(short_log=arg.short_log)

    try:
        pghoard = PGHoard(config_path)
    except InvalidConfigurationError as ex:
        print("pghoard: failed to load config {}: {}".format(config_path, ex))
        return 1

    return pghoard.run()


if __name__ == "__main__":
    sys.exit(main())
