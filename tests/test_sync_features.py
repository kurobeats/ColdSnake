"""Sync-side features: prune trashes by default, erases only with prune_delete.

Guards (threshold force, excludes, files only, own subtree) live unchanged in
tests/test_coldsnake.py; here the focus is *how* a stale file is removed.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.client import TransientError  # noqa: E402
from coldsnake.sync import Mirror, PreflightError  # noqa: E402


def remote_only(file_id, name):
    return {"id": file_id, "filename": name, "filesize": 1, "moddate": 0, "isFolder": 0}


class FakeClient:
    """In-memory API stand-in that records how prune removed each file."""

    def __init__(self, fail_trash=(), fail_batch=False, fail_delete=(),
                 fail_batch_transient=False, fail_trash_transient=(),
                 fail_upload_transient=False):
        self.tree = {0: []}
        self.trashed = []
        self.deleted = []
        self.batches = []                       # every delete_files([...]) call
        self.fail_trash = set(fail_trash)
        self.fail_batch = fail_batch
        self.fail_batch_transient = fail_batch_transient
        self.fail_trash_transient = set(fail_trash_transient)
        self.fail_upload_transient = fail_upload_transient
        self.fail_delete = set(fail_delete)
        self._next = 1

    def listing(self, folder_id=0):
        return list(self.tree.get(folder_id, []))

    def ensure_folder(self, parent_id, name):
        for entry in self.tree.get(parent_id, []):
            if entry["filename"] == name and entry["isFolder"]:
                return entry["id"]
        folder = {"id": self._next, "filename": name, "isFolder": 1}
        self._next += 1
        self.tree.setdefault(parent_id, []).append(folder)
        self.tree[folder["id"]] = []
        return folder["id"]

    def upload(self, folder_id, path):
        if self.fail_upload_transient:
            raise TransientError("service down")
        stat = os.stat(path)
        name = os.path.basename(path)
        self.tree[folder_id] = [e for e in self.tree.get(folder_id, [])
                                if e["filename"] != name]
        self.tree[folder_id].append({"id": self._next, "filename": name,
                                     "filesize": stat.st_size, "moddate": int(stat.st_mtime),
                                     "isFolder": 0})
        self._next += 1
        return {"error": False}

    def trash(self, file_id):
        if file_id in self.fail_trash_transient:
            raise TransientError("service down")
        if file_id in self.fail_trash:
            raise RuntimeError(f"trash {file_id} refused")
        self.trashed.append(file_id)
        self._remove(file_id)

    def delete_files(self, file_ids):
        self.batches.append(list(file_ids))
        if self.fail_batch_transient:
            raise TransientError("service down")
        if self.fail_batch:
            raise RuntimeError("batch erase refused")
        self.deleted.extend(file_ids)
        for file_id in file_ids:
            self._remove(file_id)
        return len(file_ids)

    def delete_file(self, file_id):
        if file_id in self.fail_delete:
            raise RuntimeError(f"delete {file_id} refused")
        self.deleted.append(file_id)
        self._remove(file_id)

    def _remove(self, file_id):
        for fid, entries in self.tree.items():
            self.tree[fid] = [e for e in entries if e.get("id") != file_id]


class PruneRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "sub"))
        for rel in ("a.txt", "sub/b.txt"):
            with open(os.path.join(self.root, rel), "w") as handle:
                handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def mirror(self, client, **kwargs):
        return Mirror(client, self.root, "Remote", log=lambda *_: None, **kwargs)

    def seed(self, client, entries):
        """Upload the local tree once, then plant extra remote entries at the root."""
        self.mirror(client).run()
        remote_id = next(e["id"] for e in client.tree[0] if e["filename"] == "Remote")
        client.tree[remote_id].extend(entries)
        return remote_id

    def test_default_prune_trashes_remote_only_files(self):
        client = FakeClient()
        self.seed(client, [remote_only(999, "gone.txt")])
        stats = self.mirror(client, prune=True).run()
        self.assertEqual(client.trashed, [999])
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.batches, [])
        self.assertEqual((stats.trashed, stats.deleted), (1, 0))
        self.assertEqual(stats.failed(), 0)

    def test_prune_delete_hard_deletes_in_one_batch_call(self):
        client = FakeClient()
        self.seed(client, [remote_only(901, "g1"), remote_only(902, "g2")])
        stats = self.mirror(client, prune=True, prune_delete=True).run()
        self.assertEqual(client.batches, [[901, 902]], "one batch call for the whole set")
        self.assertEqual(sorted(client.deleted), [901, 902])
        self.assertEqual(client.trashed, [])
        self.assertEqual((stats.trashed, stats.deleted), (0, 2))

    def test_prune_delete_falls_back_per_file_when_batch_fails(self):
        client = FakeClient(fail_batch=True, fail_delete={902})
        self.seed(client, [remote_only(901, "g1"), remote_only(902, "g2")])
        stats = self.mirror(client, prune=True, prune_delete=True).run()
        self.assertEqual(client.batches, [[901, 902]], "batch attempted once")
        self.assertEqual(client.deleted, [901], "the surviving file still deleted")
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.failed(), 1)

    def test_batch_transient_error_is_not_swallowed(self):
        client = FakeClient(fail_batch_transient=True)
        self.seed(client, [remote_only(901, "g1")])
        with self.assertRaises(TransientError):
            self.mirror(client, prune=True, prune_delete=True).run()
        self.assertEqual(client.deleted, [], "no silent per-file fallback on an outage")

    def test_threshold_aborts_without_force_and_touches_nothing(self):
        client = FakeClient()
        self.seed(client, [remote_only(1000 + i, f"stale{i}") for i in range(60)])
        with self.assertRaises(PreflightError):
            self.mirror(client, prune=True).run()
        self.assertEqual(client.trashed, [])
        self.assertEqual(client.deleted, [])

    def test_excluded_files_are_never_trashed(self):
        client = FakeClient()
        self.seed(client, [remote_only(777, "cache.tmp")])
        self.mirror(client, prune=True, excludes=["*.tmp"]).run()
        self.assertEqual(client.trashed, [])
        self.assertEqual(client.deleted, [])

    def test_folders_are_never_touched(self):
        client = FakeClient()
        remote_id = self.seed(client, [{"id": 555, "filename": "olddir", "isFolder": 1}])
        self.mirror(client, prune=True).run()
        self.assertEqual(client.trashed, [])
        self.assertEqual(client.deleted, [])
        self.assertTrue(any(e.get("id") == 555 for e in client.tree[remote_id]),
                        "remote folder must be left alone")

    def test_transient_error_during_upload_aborts_the_run(self):
        """An outage while uploading must abort (exit 3), not be counted per file."""
        client = FakeClient(fail_upload_transient=True)
        with self.assertRaises(TransientError):
            self.mirror(client).run()

    def test_transient_error_during_trash_is_not_swallowed(self):
        """An outage during a trashing prune is an outage (exit 3), not a bad file."""
        client = FakeClient(fail_trash_transient={901})
        self.seed(client, [remote_only(901, "g1"), remote_only(902, "g2")])
        with self.assertRaises(TransientError):
            self.mirror(client, prune=True).run()

    def test_per_file_trash_failure_is_isolated(self):
        client = FakeClient(fail_trash={901})
        self.seed(client, [remote_only(901, "g1"), remote_only(902, "g2")])
        stats = self.mirror(client, prune=True).run()      # must not raise
        self.assertEqual(client.trashed, [902], "the other file still removed")
        self.assertEqual(stats.trashed, 1)
        self.assertEqual(stats.failed(), 1)
        self.assertEqual(stats.failures[0][0], "g1")


if __name__ == "__main__":
    unittest.main()


class EmptyFileTests(unittest.TestCase):
    """A legit empty local file (Syncthing's .stignore) is skipped, not a failure."""

    def _mirror_with_empty_file(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, ".stignore"), "w"):
            pass
        with open(os.path.join(tmp, "real.txt"), "w") as handle:
            handle.write("data")
        return Mirror(FakeClient(), tmp, "Remote"), tmp

    def test_empty_file_is_skipped_not_failed(self):
        mirror, tmp = self._mirror_with_empty_file()
        stats = mirror.run()
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.failed(), 0)
        self.assertEqual(stats.uploaded, 1)

    def test_empty_file_repeats_cleanly(self):
        mirror, _ = self._mirror_with_empty_file()
        mirror.run()
        from coldsnake.sync import Stats
        mirror.stats = Stats()               # fresh counters, same fake remote
        stats = mirror.run()                 # every run must stay failure-free
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.failed(), 0)
