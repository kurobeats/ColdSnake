"""Tests for the cross-run upload journal (chunk offsets that already landed)."""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.state import UploadJournal  # noqa: E402


class UploadJournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "upload-journal.json")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_round_trip_across_instances(self):
        journal = UploadJournal(self.path)
        self.assertEqual(journal.path(), self.path)
        self.assertEqual(journal.done("up-1"), set())
        journal.mark("up-1", 0)
        journal.mark("up-1", 8 * 1024 * 1024)

        resumed = UploadJournal(self.path)
        self.assertEqual(resumed.done("up-1"), {0, 8 * 1024 * 1024})

    def test_missing_file_is_empty_and_still_writable(self):
        journal = UploadJournal(self.path)
        self.assertEqual(journal.done("up-1"), set())
        journal.mark("up-1", 5)
        self.assertEqual(UploadJournal(self.path).done("up-1"), {5})

    def test_empty_file_is_empty(self):
        self.write("")
        journal = UploadJournal(self.path)
        self.assertEqual(journal.done("up-1"), set())
        journal.mark("up-1", 5)
        self.assertEqual(self.read(), {"uploads": {"up-1": [5]}})

    def test_corrupt_file_is_empty_and_never_raises(self):
        for text in ("not json{", '{"uploads": {"up-1": [0]', '{"uploads": 3}',
                     '{"other": 1}', '{"uploads": {"up-1": ["x", 2]}}'):
            self.write(text)
            journal = UploadJournal(self.path)
            self.assertEqual(journal.done("up-1"), set(), text)
            journal.clear("up-1")  # must not raise on any of these
            journal.mark("up-2", 1)
            self.assertEqual(self.read(), {"uploads": {"up-2": [1]}}, text)

    def test_disabled_journal_is_a_no_op(self):
        journal = UploadJournal(None)
        self.assertIsNone(journal.path())
        journal.mark("up-1", 0)
        journal.clear("up-1")
        self.assertEqual(journal.done("up-1"), set())
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_clear_removes_only_that_upload(self):
        journal = UploadJournal(self.path)
        journal.mark("up-1", 0)
        journal.mark("up-2", 16)
        journal.clear("up-1")
        self.assertEqual(journal.done("up-1"), set())
        self.assertEqual(journal.done("up-2"), {16})
        journal.clear("up-2")  # clearing the last id may empty, never break
        self.assertEqual(self.read(), {"uploads": {}})

    def test_offsets_do_not_leak_between_uploads(self):
        journal = UploadJournal(self.path)
        journal.mark("up-a", 0)
        journal.mark("up-a", 1024)
        journal.mark("up-b", 2048)
        self.assertEqual(journal.done("up-a"), {0, 1024})
        self.assertEqual(journal.done("up-b"), {2048})
        self.assertEqual(journal.done("up-never-seen"), set())

    def test_on_disk_mode_is_0600(self):
        journal = UploadJournal(self.path)
        journal.mark("up-1", 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_file_is_valid_json_after_mark(self):
        journal = UploadJournal(self.path)
        journal.mark("up-1", 0)
        journal.mark("up-1", 4096)
        self.assertEqual(self.read(), {"uploads": {"up-1": [0, 4096]}})


if __name__ == "__main__":
    unittest.main()


class SyncDbTests(unittest.TestCase):
    def _db(self):
        import tempfile
        from coldsnake.state import SyncDb
        tmp = tempfile.mkdtemp()
        return SyncDb(os.path.join(tmp, "sync-db.sqlite")), tmp

    def test_roundtrip(self):
        db, _ = self._db()
        self.assertIsNone(db.get("R", "a.txt"))
        db.put("R", "a.txt", 100, 1700000000)
        self.assertEqual(db.get("R", "a.txt"), (100, 1700000000))
        db.put("R", "a.txt", 200, 1700000001)               # replace, not duplicate
        self.assertEqual(db.get("R", "a.txt"), (200, 1700000001))
        self.assertIsNone(db.get("Other", "a.txt"))          # keyed per mirror

    def test_none_path_and_corrupt_file_are_best_effort(self):
        from coldsnake.state import SyncDb
        self.assertIsNone(SyncDb(None).get("R", "a.txt"))
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "db.sqlite")
        with open(path, "w") as handle:
            handle.write("not a database")
        db = SyncDb(path)                                    # must not raise
        self.assertIsNone(db.get("R", "a.txt"))
        db.put("R", "a.txt", 1, 1)                           # no-op, no crash

    def test_directory_is_created(self):
        import tempfile
        from coldsnake.state import SyncDb
        tmp = tempfile.mkdtemp()
        db = SyncDb(os.path.join(tmp, "sub", "dir", "sync-db.sqlite"))
        db.put("R", "a", 1, 2)
        self.assertEqual(db.get("R", "a"), (1, 2))
