"""CLI mv/rename tests: path resolution, destination preflight, failure isolation.

Every test drives cli.main() with a fake client (build_client patched), so no
network call is ever made. The fake tree is

    root -> Pics/ -> a.txt, b.txt, sub/ -> c.txt

and the fake move_files/rename_file implement the frozen client contract:
move_files(file_ids, folder_id) -> count, rename_file(file_id, name) -> name.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake import cli  # noqa: E402


class FakeClient:
    """Fixed tree; records moves as (ids, folder_id) and renames as (id, name), and
    actually relocates the entry so the post-move destination check can be tested."""

    def __init__(self):
        self.tree = {
            0: [{"id": 10, "filename": "Pics", "isFolder": 1, "parentId": 0},
                {"id": 20, "filename": "Docs", "isFolder": 1, "parentId": 0}],
            10: [{"id": 11, "filename": "a.txt", "isFolder": 0, "filesize": 1, "parentId": 10},
                 {"id": 12, "filename": "b.txt", "isFolder": 0, "filesize": 2, "parentId": 10},
                 {"id": 13, "filename": "sub", "isFolder": 1, "parentId": 10},
                 {"id": 16, "filename": "sub2", "isFolder": 1, "parentId": 10}],
            13: [{"id": 14, "filename": "c.txt", "isFolder": 0, "filesize": 3, "parentId": 13}],
            16: [],
        }
        self.moves = []
        self.renames = []
        self.move_errors = set()
        self.rename_error = None
        self.error_type = cli.IcedriveError
        # The API answers success for an id that no longer exists (verified live):
        # this models that, so the CLI's destination check can be exercised.
        self.move_vanishes = False

    def listing(self, folder_id=0):
        return self.tree.get(folder_id, [])

    def _pop(self, file_id):
        for entries in self.tree.values():
            for entry in list(entries):
                if entry.get("id") == file_id:
                    entries.remove(entry)
                    return entry
        return None

    def move_files(self, file_ids, folder_id):
        for file_id in file_ids:
            if file_id in self.move_errors:
                raise self.error_type(f"cannot move file {file_id}")
        self.moves.append((list(file_ids), folder_id))
        if not self.move_vanishes:
            for file_id in file_ids:
                entry = self._pop(file_id)
                if entry is not None:
                    self.tree.setdefault(folder_id, []).append(dict(entry, parentId=folder_id))
        return len(file_ids)

    def rename_file(self, file_id, filename):
        if self.rename_error is not None:
            raise self.rename_error
        self.renames.append((file_id, filename))
        return filename


def run(fake, argv):
    """cli.main() with build_client stubbed; returns (exit code, stdout)."""
    out = io.StringIO()
    with mock.patch.object(cli, "build_client", return_value=fake), redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


def ids_moved(fake):
    """Every file id the fake was asked to move, in call order."""
    return [file_id for ids, _ in fake.moves for file_id in ids]


class CliMvTests(unittest.TestCase):
    def test_each_file_resolves_to_its_own_id(self):
        fake = FakeClient()
        code, _ = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt",
                             "--file", "sub/c.txt", "--to", "sub2"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.moves, [([11], 16), ([14], 16)])

    def test_default_destination_is_the_remote_root(self):
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "sub/c.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.moves, [([14], 10)])
        self.assertEqual(out.splitlines(), ["moved sub/c.txt -> /"])

    def test_nested_destination_resolves_to_that_folder_id(self):
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--to", "sub"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.moves, [([11], 13)])
        self.assertEqual(out.splitlines(), ["moved a.txt -> sub"])

    def test_one_line_per_file(self):
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt",
                               "--file", "b.txt", "--to", "sub"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["moved a.txt -> sub", "moved b.txt -> sub"])

    def test_missing_destination_exits_2_and_moves_nothing(self):
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--to", "nope"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.moves, [])
        self.assertIn("nope", out)

    def test_destination_may_not_be_a_file(self):
        fake = FakeClient()
        code, _ = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--to", "b.txt"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.moves, [])

    def test_missing_file_exits_2_before_moving_anything(self):
        fake = FakeClient()
        code, _ = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--file", "gone.txt"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.moves, [])

    def test_unknown_remote_exits_2(self):
        fake = FakeClient()
        code, _ = run(fake, ["mv", "--remote", "Nowhere", "--file", "a.txt"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.moves, [])

    def test_one_bad_file_exits_1_and_the_others_still_move(self):
        fake = FakeClient()
        fake.move_errors.add(12)
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt",
                               "--file", "b.txt", "--file", "sub/c.txt", "--to", "sub2"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.moves, [([11], 16), ([14], 16)])
        self.assertIn("FAILED move b.txt", out)
        self.assertIn("moved a.txt -> sub2", out)
        self.assertIn("moved sub/c.txt -> sub2", out)

    def test_outage_exits_3_and_counts_no_file_failure(self):
        fake = FakeClient()
        fake.move_errors.add(12)
        fake.error_type = cli.TransientError
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt",
                               "--file", "b.txt", "--to", "sub"])
        self.assertEqual(code, 3, "an outage must exit 3, not be counted per file")
        self.assertNotIn("FAILED", out)

    def test_auth_error_exits_2_and_counts_no_file_failure(self):
        fake = FakeClient()
        fake.move_errors.add(14)
        fake.error_type = cli.AuthError
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "sub/c.txt"])
        self.assertEqual(code, 2, "bad credentials are exit 2, not a failed file")
        self.assertNotIn("FAILED", out)


class CliRenameTests(unittest.TestCase):
    def test_rename_uses_the_resolved_id_and_prints_the_line(self):
        fake = FakeClient()
        code, out = run(fake, ["rename", "--remote", "Pics", "--file", "sub/c.txt",
                               "--name", "d.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.renames, [(14, "d.txt")])
        self.assertEqual(out.splitlines(), ["renamed sub/c.txt -> d.txt"])

    def test_rename_prints_the_name_the_server_reports(self):
        fake = FakeClient()

        def renamed(file_id, filename):
            return "server.txt"

        fake.rename_file = renamed
        code, out = run(fake, ["rename", "--remote", "Pics", "--file", "a.txt", "--name", "x.txt"])
        self.assertEqual(code, 0)
        self.assertIn("renamed a.txt -> server.txt", out)

    def test_slash_in_name_exits_2(self):
        fake = FakeClient()
        code, out = run(fake, ["rename", "--remote", "Pics", "--file", "a.txt",
                               "--name", "sub/a.txt"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.renames, [])
        self.assertIn("sub/a.txt", out)

    def test_renaming_a_folder_exits_2(self):
        fake = FakeClient()
        code, _ = run(fake, ["rename", "--remote", "Pics", "--file", "sub", "--name", "s2"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.renames, [])

    def test_missing_file_exits_2(self):
        fake = FakeClient()
        code, _ = run(fake, ["rename", "--remote", "Pics", "--file", "gone.txt", "--name", "x"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.renames, [])

    def test_rename_outage_exits_3_and_auth_error_exits_2(self):
        for error, expected in ((cli.TransientError("down"), 3), (cli.AuthError("bad"), 2)):
            fake = FakeClient()
            fake.rename_error = error
            code, _ = run(fake, ["rename", "--remote", "Pics", "--file", "a.txt", "--name", "x"])
            self.assertEqual(code, expected)
            self.assertEqual(fake.renames, [])

    def test_blank_name_exits_2_without_calling(self):
        fake = FakeClient()
        code, _ = run(fake, ["rename", "--remote", "Pics", "--file", "a.txt", "--name", "  "])
        self.assertEqual(code, 2, "a blank name is a bad invocation, not a failed rename")
        self.assertEqual(fake.renames, [])

    def test_duplicate_file_arguments_move_it_once(self):
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt",
                               "--file", "a.txt", "--to", "sub"])
        self.assertEqual(code, 0)
        self.assertEqual(ids_moved(fake), [11], "a repeated path must not be moved twice")
        self.assertEqual(out.count("moved a.txt"), 1)

    def test_a_file_wins_over_a_same_named_folder(self):
        """download/versions take a path to a FILE: a folder of the same name must not win."""
        fake = FakeClient()
        fake.tree[10].insert(0, {"id": 15, "filename": "a.txt", "isFolder": 1, "parentId": 10})
        code, _ = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--to", "sub"])
        self.assertEqual(code, 0)
        self.assertEqual(ids_moved(fake), [11], "the file, not the folder")

    def test_file_already_in_the_destination_is_skipped_not_failed(self):
        """The API refuses a move into the current folder (5105); that no-op is not a failure."""
        fake = FakeClient()
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "sub/c.txt", "--to", "sub"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.moves, [], "no pointless call for a file that is already there")
        self.assertIn("already in sub", out)

    def test_a_file_that_vanishes_is_not_reported_as_moved(self):
        """The API answers 200 for an unknown id, so the destination is checked."""
        fake = FakeClient()
        fake.move_vanishes = True
        code, out = run(fake, ["mv", "--remote", "Pics", "--file", "a.txt", "--to", "sub"])
        self.assertEqual(code, 1)
        self.assertIn("FAILED move a.txt", out)
        self.assertNotIn("moved a.txt", out)


if __name__ == "__main__":
    unittest.main()
