"""JSON report tests: `mirror`/`check` --json on stdout and --report to a file.

Drives cli.main() with a fake client and a fake Mirror (same seams as
tests/test_cli_features.py) so no network call is made. The point under test is
that a monitor always gets exactly one complete, parseable document - on success,
on file failures (exit 1) and on a refusal/bad config (exit 2).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake import cli  # noqa: E402


class FakeClient:
    """Only what preflight()/check touch: probe() for storage, account for the label."""

    account = {}

    def probe(self):
        return {"storage": {"free_human": "1 TB", "used_human": "0 B", "max_human": "1 TB"}}


class FakeStats:
    def __init__(self, failures=()):
        self.uploaded = 3
        self.unchanged = 10
        self.bytes = 1234
        self.verified = 3
        self.trashed = 0
        self.deleted = 0
        self.failures = list(failures)

    def failed(self):
        return len(self.failures)


def fake_mirror(stats):
    class FakeMirror:
        def __init__(self, client, local, remote, **kwargs):
            self.local, self.remote = local, remote

        def run(self):
            return stats

    return FakeMirror


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def run(argv, client=None, mirror=None):
    """cli.main() with build_client/Mirror stubbed; returns (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(cli, "build_client",
                                              return_value=client or FakeClient()))
        if mirror is not None:
            stack.enter_context(mock.patch.object(cli, "Mirror", mirror))
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class JsonMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.txt"), "w") as handle:
            handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def mirror_argv(self, *extra):
        # --no-upload-journal keeps the run off coldsnake.state entirely.
        return ["mirror", "--local", self.src, "--remote", "R",
                "--no-upload-journal", *extra]

    def test_success_emits_exact_schema(self):
        code, out, _ = run(self.mirror_argv("--json"), mirror=fake_mirror(FakeStats()))
        self.assertEqual(code, 0)
        doc = json.loads(out)                       # stdout is exactly one document
        self.assertEqual(doc["command"], "mirror")
        self.assertEqual(doc["version"], cli.__version__)
        self.assertIsInstance(doc["started"], str)
        self.assertIsInstance(doc["finished"], str)
        self.assertIsInstance(doc["duration_s"], (int, float))
        self.assertIs(doc["ok"], True)
        self.assertEqual(doc["failures"], 0)
        self.assertEqual(len(doc["mirrors"]), 1)
        entry = doc["mirrors"][0]
        self.assertEqual(entry, {"local": self.src, "remote": "R", "uploaded": 3,
                                 "unchanged": 10, "bytes": 1234, "verified": 3,
                                 "trashed": 0, "deleted": 0, "failed": 0, "failures": []})

    def test_failed_file_flips_ok_and_exit_code(self):
        stats = FakeStats(failures=[("a/b.txt", "boom")])
        code, out, _ = run(self.mirror_argv("--json"), mirror=fake_mirror(stats))
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertIs(doc["ok"], False)
        self.assertEqual(doc["failures"], 1)
        self.assertEqual(doc["mirrors"][0]["failed"], 1)
        self.assertEqual(doc["mirrors"][0]["failures"],
                         [{"path": "a/b.txt", "error": "boom"}])

    def test_report_writes_identical_document(self):
        path = os.path.join(self.tmp.name, "report.json")
        code, out, _ = run(self.mirror_argv("--json", "--report", path),
                           mirror=fake_mirror(FakeStats()))
        self.assertEqual(code, 0)
        with open(path, "rb") as handle:
            written = handle.read()
        self.assertEqual(written.decode(), out)     # byte-identical to stdout
        self.assertEqual(json.loads(written)["command"], "mirror")
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "644")

    def test_human_lines_go_to_stderr_stdout_stays_json(self):
        code, out, err = run(self.mirror_argv("--json"), mirror=fake_mirror(FakeStats()))
        self.assertEqual(code, 0)
        self.assertIn("pre-flight ok", err)
        self.assertIn("done: 0 failure(s)", err)
        self.assertEqual(out.strip()[0], "{")        # stdout is only the document
        json.loads(out)                              # still parses
        self.assertNotIn("pre-flight ok", out)

    def test_report_without_json_keeps_human_stdout(self):
        path = os.path.join(self.tmp.name, "report.json")
        code, out, _ = run(self.mirror_argv("--report", path),
                           mirror=fake_mirror(FakeStats()))
        self.assertEqual(code, 0)
        self.assertIn("pre-flight ok", out)          # no --json: output unchanged
        self.assertTrue(os.path.exists(path))
        self.assertEqual(read_json(path)["ok"], True)


class JsonRefusalTests(unittest.TestCase):
    def test_bad_config_still_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.toml")
            with open(cfg, "w") as handle:
                handle.write("this is not = toml [")
            path = os.path.join(tmp, "report.json")
            code, out, err = run(["check", "--config", cfg, "--json", "--report", path])
            self.assertEqual(code, 2)
            doc = json.loads(out)
            self.assertIs(doc["ok"], False)
            self.assertEqual(doc["command"], "check")
            self.assertTrue(doc["error"])
            # --report is written even on a non-zero exit
            self.assertEqual(read_json(path), doc)
            self.assertIn("pre-flight refused", err)

    def test_outage_still_emits_json(self):
        class DownClient(FakeClient):
            def probe(self):
                raise cli.TransientError("Service temporarily unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "w") as handle:
                handle.write("x")
            code, out, _ = run(["mirror", "--local", tmp, "--remote", "R",
                                "--no-upload-journal", "--json"], client=DownClient())
        self.assertEqual(code, 3)
        doc = json.loads(out)
        self.assertIs(doc["ok"], False)
        self.assertEqual(doc["command"], "mirror")
        self.assertTrue(doc["error"])


class JsonCheckTests(unittest.TestCase):
    def test_check_success_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "w") as handle:
                handle.write("x")
            code, out, _ = run(["check", "--local", tmp, "--remote", "R", "--json"])
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["command"], "check")
            self.assertIs(doc["ok"], True)
            self.assertEqual(doc["failures"], 0)
            self.assertEqual(doc["storage"]["free_human"], "1 TB")


class JsonRobustnessTests(unittest.TestCase):
    """The report must survive a bad path, an interrupted run and a buggy client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.txt"), "w") as handle:
            handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def mirror_argv(self, *extra):
        return ["mirror", "--local", self.src, "--remote", "R",
                "--no-upload-journal", *extra]

    def test_interrupt_still_emits_one_document(self):
        class Interrupted:
            def __init__(self, client, local, remote, **kwargs):
                pass

            def run(self):
                raise KeyboardInterrupt

        code, out, _ = run(self.mirror_argv("--json"), mirror=Interrupted)
        self.assertEqual(code, 130)
        doc = json.loads(out)
        self.assertIs(doc["ok"], False)
        self.assertEqual(doc["error"], "interrupted")

    def test_report_written_on_the_outage_path(self):
        class DownClient(FakeClient):
            def probe(self):
                raise cli.TransientError("Service temporarily unavailable")

        path = os.path.join(self.tmp.name, "report.json")
        code, out, _ = run(self.mirror_argv("--json", "--report", path), client=DownClient())
        self.assertEqual(code, 3)
        self.assertEqual(read_json(path), json.loads(out), "file must match stdout, even on exit 3")

    def test_unexpected_error_is_reported_as_a_failure_not_a_success(self):
        class BrokenClient(FakeClient):
            def probe(self):
                raise ValueError("something unforeseen")

        code, out, err = run(self.mirror_argv("--json"), client=BrokenClient())
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertIs(doc["ok"], False, "a crash must never be reported as ok")
        self.assertIn("ValueError", doc["error"])
        self.assertIn("error:", err)

    def test_bad_report_path_does_not_break_a_good_run(self):
        missing = os.path.join(self.tmp.name, "nope", "report.json")
        code, out, err = run(self.mirror_argv("--json", "--report", missing),
                             mirror=fake_mirror(FakeStats()))
        self.assertEqual(code, 0, "a bad --report path is not a run failure")
        self.assertIs(json.loads(out)["ok"], True)
        self.assertIn("could not write report", err)

    def test_json_flag_does_not_leak_into_the_next_run(self):
        first, out, _ = run(self.mirror_argv("--json"), mirror=fake_mirror(FakeStats()))
        self.assertEqual(first, 0)
        json.loads(out)                                  # pure JSON on stdout
        second, out2, _ = run(self.mirror_argv(), mirror=fake_mirror(FakeStats()))
        self.assertEqual(second, 0)
        self.assertIn("R: uploaded 3", out2, "human output must be back on stdout")
        self.assertRaises(ValueError, json.loads, out2)


if __name__ == "__main__":
    unittest.main()
