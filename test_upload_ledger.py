"""Offline unit tests for the SoundCloud upload ledger.

Hermetic: no network, no browser, no Playwright, and nothing outside the
standard library, so the suite adds no dependency to ``requirements.txt``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from upload_ledger import (
    UPLOAD_VERSION,
    UploadLedger,
    set_key,
    track_key,
)

# The failure-path tests drive the module into its warning logs on purpose;
# the assertions, not the log lines, are what prove the behaviour.
logging.disable(logging.CRITICAL)


class LedgerTestCase(unittest.TestCase):
    """Base case giving each test its own throwaway album folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "Example Album"
        self.folder.mkdir()
        self.addCleanup(self._tmp.cleanup)

    @property
    def ledger_path(self) -> Path:
        # Ledgers now live in a sibling directory, so a test writing one by hand
        # has to create it the way the store would.
        path = UploadLedger(self.folder).path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class KeyTests(unittest.TestCase):
    def test_set_key_is_namespaced_and_trimmed(self):
        self.assertEqual(set_key("  Example Album "), "set:Example Album")

    def test_track_key_drops_the_directory(self):
        self.assertEqual(track_key("/a/b/01 - INTRO.mp3"), "track:01 - INTRO.mp3")

    def test_set_and_track_keys_cannot_collide(self):
        self.assertNotEqual(set_key("x"), track_key("x"))


class RoundTripTests(LedgerTestCase):
    def test_record_then_flush_survives_a_reload(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("Example Album"), set_url="https://snd.sc/set", note="first")
        ledger.record(track_key("01 - INTRO.mp3"))
        ledger.flush()

        reloaded = UploadLedger(self.folder)
        self.assertEqual(len(reloaded), 2)
        self.assertTrue(reloaded.has(set_key("Example Album")))
        entry = reloaded.get(set_key("Example Album"))
        self.assertEqual(entry["set_url"], "https://snd.sc/set")
        self.assertEqual(entry["note"], "first")
        self.assertTrue(entry["uploaded_at"].endswith("Z"))

    def test_record_writes_through_without_an_explicit_flush(self):
        # An interrupted run must resume, so a record that is only in memory is
        # worthless: the file has to be current the moment record() returns.
        UploadLedger(self.folder).record(set_key("A"), set_url="u")
        payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], UPLOAD_VERSION)
        self.assertIn("set:A", payload["entries"])

    def test_missing_file_loads_as_empty(self):
        ledger = UploadLedger(self.folder)
        self.assertEqual(len(ledger), 0)
        self.assertFalse(ledger.has(set_key("A")))
        self.assertIsNone(ledger.get(set_key("A")))

    def test_get_returns_a_copy(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"), set_url="u")
        ledger.get(set_key("A"))["set_url"] = "tampered"
        self.assertEqual(ledger.get(set_key("A"))["set_url"], "u")

    def test_contains_and_len_reflect_the_recorded_keys(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"))
        self.assertIn(set_key("A"), ledger)
        self.assertNotIn(set_key("B"), ledger)
        self.assertNotIn(123, ledger)
        self.assertEqual(len(ledger), 1)

    def test_legacy_bare_url_value_is_read_as_the_set_url(self):
        self.ledger_path.write_text(
            json.dumps({"set:A": "https://snd.sc/old"}), encoding="utf-8"
        )
        ledger = UploadLedger(self.folder)
        self.assertEqual(ledger.get("set:A")["set_url"], "https://snd.sc/old")

    def test_record_overwrites_an_earlier_entry(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"), set_url="first")
        ledger.record(set_key("A"), set_url="second")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger.get(set_key("A"))["set_url"], "second")


class CorruptionTests(LedgerTestCase):
    def test_corrupt_json_loads_as_empty_and_the_file_is_kept(self):
        self.ledger_path.write_text("{not json at all", encoding="utf-8")
        ledger = UploadLedger(self.folder)
        self.assertEqual(len(ledger), 0)
        self.assertTrue(self.ledger_path.is_file())
        self.assertEqual(self.ledger_path.read_text(encoding="utf-8"), "{not json at all")

    def test_json_that_is_not_an_object_loads_as_empty(self):
        self.ledger_path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(len(UploadLedger(self.folder)), 0)
        self.assertTrue(self.ledger_path.is_file())

    def test_malformed_entries_section_loads_as_empty(self):
        self.ledger_path.write_text(
            json.dumps({"version": 1, "entries": "nope"}), encoding="utf-8"
        )
        self.assertEqual(len(UploadLedger(self.folder)), 0)

    def test_unusable_individual_entries_are_dropped_but_the_rest_survive(self):
        self.ledger_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {"set:A": {"set_url": "u"}, "set:B": 17, "set:C": None},
                }
            ),
            encoding="utf-8",
        )
        ledger = UploadLedger(self.folder)
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger.has("set:A"))

    def test_a_corrupt_ledger_is_replaced_only_on_the_next_write(self):
        self.ledger_path.write_text("garbage", encoding="utf-8")
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"), set_url="u")
        payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(list(payload["entries"]), ["set:A"])


class AtomicWriteTests(LedgerTestCase):
    def test_no_temp_files_are_left_behind(self):
        ledger = UploadLedger(self.folder)
        for index in range(5):
            ledger.record(track_key("%02d.mp3" % index))
        ledger.flush()
        self.assertEqual(
            sorted(p.name for p in UploadLedger(self.folder).path.parent.iterdir()),
            [self.folder.name + ".json"]
        )

    def test_a_failed_replace_leaves_no_temp_file_and_no_exception(self):
        ledger = UploadLedger(self.folder)
        with mock.patch("json_store.os.replace", side_effect=OSError("nope")):
            ledger.record(set_key("A"))
        ledger_dir = UploadLedger(self.folder).path.parent
        leftovers = [p.name for p in ledger_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_the_previous_ledger_survives_a_failed_write(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"), set_url="good")
        with mock.patch("json_store.os.replace", side_effect=OSError("nope")):
            ledger.record(set_key("B"), set_url="lost")
        payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(list(payload["entries"]), ["set:A"])

    def test_the_write_is_fsynced_before_the_rename(self):
        # Without the fsync the rename can land before the bytes do, which on a
        # power cut is exactly the empty-but-present ledger we are avoiding.
        order = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        with mock.patch("json_store.os.fsync", spy_fsync), mock.patch(
            "json_store.os.replace", spy_replace
        ):
            UploadLedger(self.folder).record(set_key("A"))
        self.assertEqual(order, ["fsync", "replace"])


class ConcurrencyTests(LedgerTestCase):
    def test_records_from_several_threads_all_land(self):
        ledger = UploadLedger(self.folder)
        start = threading.Barrier(8)

        def worker(index):
            start.wait(timeout=5)
            for step in range(10):
                ledger.record(track_key("%d-%d.mp3" % (index, step)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(ledger), 80)
        ledger.flush()
        payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["entries"]), 80)
        self.assertEqual(len(UploadLedger(self.folder)), 80)

    def test_a_concurrent_reader_never_sees_a_half_written_file(self):
        ledger = UploadLedger(self.folder)
        ledger.record(set_key("A"))
        stop = threading.Event()
        broken = []

        def reader():
            while not stop.is_set():
                try:
                    json.loads(self.ledger_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    pass
                except ValueError as exc:
                    broken.append(str(exc))
                    return

        watcher = threading.Thread(target=reader)
        watcher.start()
        try:
            for index in range(60):
                ledger.record(track_key("%d.mp3" % index))
        finally:
            stop.set()
            watcher.join(timeout=10)
        self.assertEqual(broken, [])


class ContextManagerTests(LedgerTestCase):
    def test_exiting_the_block_flushes(self):
        with UploadLedger(self.folder) as ledger:
            with mock.patch.object(UploadLedger, "flush"):  # keep record() in memory
                ledger.record(set_key("A"), set_url="u")
            self.assertFalse(self.ledger_path.exists())
        self.assertTrue(self.ledger_path.is_file())
        self.assertIn("set:A", json.loads(self.ledger_path.read_text(encoding="utf-8"))["entries"])

    def test_an_exception_inside_the_block_still_flushes_and_propagates(self):
        with self.assertRaises(RuntimeError):
            with UploadLedger(self.folder) as ledger:
                with mock.patch.object(UploadLedger, "flush"):
                    ledger.record(set_key("A"))
                raise RuntimeError("boom")
        self.assertTrue(self.ledger_path.is_file())


class UnwritableTests(LedgerTestCase):
    def test_an_unwritable_folder_logs_and_does_not_raise(self):
        if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
            self.skipTest("root can write to a read-only directory")
        # The ledger directory, not the album folder: that is where flush writes.
        ledger_dir = UploadLedger(self.folder).path.parent
        ledger_dir.mkdir(parents=True, exist_ok=True)
        mode = ledger_dir.stat().st_mode
        os.chmod(ledger_dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, ledger_dir, mode)

        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        ledger = UploadLedger(self.folder)
        with self.assertLogs("upload_ledger", level="WARNING") as captured:
            ledger.record(set_key("A"), set_url="u")  # must not raise
            ledger.flush()  # must not raise either
        self.assertTrue(any("could not write" in line for line in captured.output))
        # The record is still in memory, so the run it belongs to can carry on.
        self.assertTrue(ledger.has(set_key("A")))

    def test_a_missing_ledger_directory_is_created_on_flush(self):
        nested = self.folder / "nope" / "deeper"
        ledger = UploadLedger(nested)
        ledger.record(set_key("A"))
        self.assertTrue(UploadLedger(nested).path.is_file())


if __name__ == "__main__":
    unittest.main()
