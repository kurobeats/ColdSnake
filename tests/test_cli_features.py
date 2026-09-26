"""CLI feature tests: trash/restore/versions/version-download/prune-delete.

Every test drives cli.main() with a fake client (build_client patched) so no
network call is ever made; the fake tree mirrors the real entry shape:
root -> "Pics"/"a.txt".
"""
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake import cli  # noqa: E402


class FakeClient:
    """Fixed tree: root/Pics/a.txt, one trash entry, two version rows."""

    def __init__(self):
        self.tree = {
            0: [{"id": 10, "filename": "Pics", "isFolder": 1}],
            10: [{"id": 11, "filename": "a.txt", "isFolder": 0,
                  "filesize": 100, "moddate": 1700000000}],
        }
        self.trash = [{"id": 5, "filename": "old.txt", "filesize": 12,
                       "moddate": 1700000000}]
        self.version_rows = {
            11: [{"index": 0, "current": 0, "date": "2024-01-01", "filesize": 90, "url": "u0"},
                 {"index": 1, "current": 1, "date": "2024-02-02", "filesize": 100, "url": "u1"}],
        }
        self.restored = []
        self.restore_errors = set()
        self.version_ids = []
        self.downloads = []

    def listing(self, folder_id=0):
        return self.tree.get(folder_id, [])

    def trash_listing(self):
        return self.trash

    def restore(self, item_id, is_folder=False):
        if item_id in self.restore_errors:
            raise getattr(self, "restore_error_type", cli.IcedriveError)(f"cannot restore {item_id}")
        self.restored.append((item_id, is_folder))

    def versions(self, file_id):
        self.version_ids.append(file_id)
        return self.version_rows.get(file_id, [])

    def download_from_url(self, url, dest, size=None):
        self.downloads.append((url, dest, size))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"x" * (size or 3))
        return size or 3

    def download(self, file_id, dest, size=None):
        self.downloads.append((file_id, dest, size))
        return size or 0

    def probe(self):
        return {"storage": {"free_human": "1 TB", "used_human": "0 B", "max_human": "1 TB"}}


def run(fake, argv):
    """cli.main() with build_client stubbed; returns (exit code, stdout)."""
    out = io.StringIO()
    with mock.patch.object(cli, "build_client", return_value=fake), redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class FakeStats:
    uploaded = unchanged = skipped = bytes = verified = trashed = deleted = 0

    def failed(self):
        return 0


class CliTrashTests(unittest.TestCase):
    def test_trash_lists_id_name_size_date(self):
        code, out = run(FakeClient(), ["trash"])
        self.assertEqual(code, 0)
        self.assertIn("5", out)
        self.assertIn("old.txt", out)
        self.assertIn("12", out)

    def test_restore_by_id_repeatable(self):
        fake = FakeClient()
        code, _ = run(fake, ["restore", "--id", "5", "--id", "6"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.restored, [(5, False), (6, False)])

    def test_restore_folder_flag(self):
        fake = FakeClient()
        code, _ = run(fake, ["restore", "--id", "9", "--folder"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.restored, [(9, True)])

    def test_format_date_survives_junk(self):
        """Bad server data must print, not traceback out of a listing command."""
        self.assertEqual(cli.format_date({"date": "2024-01-02 03:04:05"}), "2024-01-02 03:04:05")
        self.assertIsInstance(cli.format_date({"moddate": 1700000000}), str)
        for junk in ({"moddate": "not-a-number"}, {"moddate": "9" * 40},
                     {"timestamp": 1e400}, {"moddate": None}):
            self.assertIsInstance(cli.format_date(junk), str)

    def test_restore_partial_failure_exits_1(self):
        fake = FakeClient()
        fake.restore_errors.add(7)
        code, out = run(fake, ["restore", "--id", "5", "--id", "7"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.restored, [(5, False)])
        self.assertIn("FAILED restore 7", out)

    def test_restore_outage_exits_3_not_1(self):
        fake = FakeClient()
        fake.restore_errors.add(7)
        fake.restore_error_type = cli.TransientError
        code, _ = run(fake, ["restore", "--id", "7"])
        self.assertEqual(code, 3, "an outage must exit 3, not be counted per item")
        self.assertEqual(fake.restored, [])

        fake = FakeClient()
        fake.restore_errors.add(7)
        fake.restore_error_type = cli.AuthError
        code, _ = run(fake, ["restore", "--id", "7"])
        self.assertEqual(code, 2, "bad credentials are exit 2, not a failed item")


class CliVersionsTests(unittest.TestCase):
    def test_versions_lists_each_row(self):
        fake = FakeClient()
        code, out = run(fake, ["versions", "--remote", "Pics", "--file", "a.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.version_ids, [11])
        self.assertIn("2024-02-02", out)
        self.assertIn("current", out)

    def test_download_version_selects_that_url_and_size(self):
        fake = FakeClient()
        tmp = tempfile.mkdtemp()
        code, _ = run(fake, ["download", "--remote", "Pics", "--local", tmp,
                             "--file", "a.txt", "--version", "1"])
        self.assertEqual(code, 0)
        url, dest, size = fake.downloads[0]
        self.assertEqual(url, "u1")
        self.assertEqual(size, 100)                       # that version's filesize
        self.assertEqual(dest, os.path.join(tmp, "a.txt"))

    def test_absent_version_index_exits_1_without_raising(self):
        fake = FakeClient()
        tmp = tempfile.mkdtemp()
        code, out = run(fake, ["download", "--remote", "Pics", "--local", tmp,
                               "--file", "a.txt", "--version", "9"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.downloads, [])
        self.assertIn("no version 9", out)


class CliMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.txt"), "w") as handle:
            handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def fake_state(self, seen):
        module = types.ModuleType("coldsnake.state")

        class UploadJournal:
            def __init__(self, path):
                self._path = path
                seen.append(path)

            def path(self):
                return self._path

        module.UploadJournal = UploadJournal
        return module

    def mirror_run(self, argv, seen):
        calls = []

        def fake_build(args, config, use_cache=True, journal=None):
            calls.append(journal)
            return FakeClient()

        recorded = {}

        class FakeMirror:
            def __init__(self, client, local, remote, **kwargs):
                recorded.update(kwargs)

            def run(self):
                return FakeStats()

        with mock.patch.object(cli, "build_client", side_effect=fake_build), \
                mock.patch.object(cli, "Mirror", FakeMirror), \
                mock.patch.dict(sys.modules, {"coldsnake.state": self.fake_state(seen)}):
            code = cli.main(argv)
        return code, recorded, calls

    def test_mid_run_outage_exits_3(self):
        """An outage that aborts a run is a service problem (3), not file failures (1)."""
        seen = []

        class FakeMirror:
            def __init__(self, client, local, remote, **kwargs):
                pass

            def run(self):
                raise cli.TransientError("Service temporarily unavailable")

        with mock.patch.object(cli, "build_client", return_value=FakeClient()), \
                mock.patch.object(cli, "Mirror", FakeMirror), \
                mock.patch.dict(sys.modules, {"coldsnake.state": self.fake_state(seen)}):
            code = cli.main(["mirror", "--local", self.src, "--remote", "R"])
        self.assertEqual(code, 3)

    def test_prune_delete_forwarded_and_journal_built(self):
        seen = []
        code, recorded, calls = self.mirror_run(
            ["mirror", "--local", self.src, "--remote", "R", "--prune", "--prune-delete"], seen)
        self.assertEqual(code, 0)
        self.assertTrue(recorded["prune_delete"])
        self.assertTrue(recorded["prune"])
        # journal path sits next to DEFAULT_CONFIG
        self.assertEqual(seen, [os.path.join(os.path.dirname(cli.DEFAULT_CONFIG),
                                             "upload-journal.json")])
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])

    def test_no_upload_journal_disables_it(self):
        seen = []
        code, _, calls = self.mirror_run(
            ["mirror", "--local", self.src, "--remote", "R", "--no-upload-journal"], seen)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [None])
        self.assertEqual(seen, [])                       # state module never imported


if __name__ == "__main__":
    unittest.main()
