"""Scrub: rotating coverage, drift detection, and one-listing folder resolution."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.scrub import ScrubStats, folder_id_for, scrub_mirror  # noqa: E402
from coldsnake.state import SyncDb  # noqa: E402


def file_entry(entry_id: int, name: str, size: int) -> dict:
    return {"id": entry_id, "filename": name, "filesize": size, "isFolder": 0, "moddate": 0}


def folder_entry(entry_id: int, name: str) -> dict:
    return {"id": entry_id, "filename": name, "isFolder": 1}


class FakeClient:
    """Just enough of Client for the scrub: listing by folder id, call counting."""

    def __init__(self, tree: dict[int, list[dict]]):
        self.tree = tree
        self.calls: list[int] = []
        self.control_interval = 0

    def listing(self, folder_id: int = 0) -> list[dict]:
        self.calls.append(folder_id)
        return self.tree.get(folder_id, [])


def make_db(rows: list[tuple[str, int]], checked: dict[str, int] | None = None) -> SyncDb:
    db = SyncDb(":memory:")
    for rel, size in rows:
        db.put("archive", rel, size, 0)
    for directory, at in (checked or {}).items():
        db.mark_checked("archive", directory, at)
    return db


class FolderResolutionTests(unittest.TestCase):
    def test_walks_from_the_root_and_caches_every_component(self):
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(101, "a")],
                             101: [folder_entry(102, "b")]})
        db = SyncDb(":memory:")
        self.assertEqual(folder_id_for(client, db, "archive", "a/b"), 102)
        self.assertEqual(client.calls, [0, 100, 101])
        self.assertEqual(folder_id_for(client, db, "archive", "a/b"), 102)
        self.assertEqual(client.calls, [0, 100, 101])       # all cached, no new calls

    def test_missing_component_means_the_folder_is_gone(self):
        client = FakeClient({0: [folder_entry(100, "archive")], 100: []})
        self.assertIsNone(folder_id_for(client, SyncDb(":memory:"), "archive", "gone"))


class ScrubTests(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def test_rotates_oldest_first_and_only_the_requested_count(self):
        db = make_db([("a/x", 10), ("b/y", 20), ("c/z", 30)],
                     checked={"a": 300, "b": 100, "c": 200})
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "a"), folder_entry(2, "b"), folder_entry(3, "c")],
                             1: [file_entry(11, "x", 10)],
                             2: [file_entry(12, "y", 20)],
                             3: [file_entry(13, "z", 30)]})
        scrub_mirror(client, db, "archive", limit=2, log=self.log)

        checked = db.checked_at("archive")
        self.assertEqual(checked["a"], 300)                 # newest: still not due
        self.assertGreater(checked["b"], 300)               # b (oldest) and c were checked
        self.assertGreater(checked["c"], 300)
        self.assertNotIn(1, client.calls)                   # so a's folder was not listed

    def test_a_vanished_file_is_forgotten_so_the_next_mirror_run_reattaches_it(self):
        db = make_db([("a/x", 10), ("a/kept", 5)])
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "a")],
                             1: [file_entry(11, "kept", 5)]})
        stats = scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual((stats.files, stats.ok, stats.requeued), (2, 1, 1))
        self.assertIsNone(db.get("archive", "a/x"))
        self.assertIsNotNone(db.get("archive", "a/kept"))

    def test_a_size_mismatch_is_requeued(self):
        db = make_db([("a/x", 10)])
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "a")],
                             1: [file_entry(11, "x", 999)]})
        stats = scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual(stats.requeued, 1)
        self.assertIsNone(db.get("archive", "a/x"))

    def test_a_mangled_name_with_one_matching_size_still_verifies(self):
        db = make_db([("a/\U0001f311 ogg", 10)])
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "a")],
                             1: [file_entry(11, "?.ogg", 10)]})
        stats = scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual((stats.ok, stats.requeued), (1, 0))
        self.assertIsNotNone(db.get("archive", "a/\U0001f311 ogg"))

    def test_a_gone_folder_requeues_all_of_its_files(self):
        db = make_db([("gone/x", 10), ("gone/y", 20)])
        client = FakeClient({0: [folder_entry(100, "archive")], 100: []})
        stats = scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual((stats.missing_folders, stats.requeued), (1, 2))
        self.assertEqual(db.rows("archive"), [])

    def test_child_folder_ids_from_a_listing_are_cached_for_free(self):
        # a before a/b, so a's listing is the one that teaches us b's id
        db = make_db([("a/x", 10), ("a/b/y", 20)], checked={"a": 1, "a/b": 2})
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "a")],
                             1: [file_entry(11, "x", 10), folder_entry(2, "b")],
                             2: [file_entry(21, "y", 20)]})
        scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual(db.folder_id("archive", "a/b"), 2)   # learned from a's listing
        self.assertEqual(client.calls.count(1), 1)            # b never re-resolved
        self.assertEqual(client.calls, [0, 100, 1, 2])        # one call per folder, root aside

    def test_files_under_a_hidden_folder_are_unverifiable_not_lost(self):
        """The API never lists dot-prefixed folders, so .sync/.dtrash read as gone
        even when they exist; requeueing them would just fail an upload each run."""
        db = make_db([("Photos/.dtrash/files/a", 10), ("Photos/kept", 5)])
        client = FakeClient({0: [folder_entry(100, "archive")],
                             100: [folder_entry(1, "Photos")],
                             1: [file_entry(11, "kept", 5)]})
        stats = scrub_mirror(client, db, "archive", limit=None, log=self.log)

        self.assertEqual((stats.unverifiable, stats.requeued, stats.ok), (1, 0, 1))
        self.assertIsNotNone(db.get("archive", "Photos/.dtrash/files/a"))
        self.assertEqual(stats.folders, 1)                  # the hidden folder was skipped

    def test_nothing_recorded_is_a_no_op(self):
        client = FakeClient({})
        stats = scrub_mirror(client, SyncDb(":memory:"), "archive", limit=None, log=self.log)
        self.assertEqual(stats, ScrubStats())
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
