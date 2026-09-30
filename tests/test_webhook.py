"""Webhook notification tests: one POST on run start, one on run finish."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake import cli  # noqa: E402, F401  (patch target)
from coldsnake.client import TransientError  # noqa: E402


class Probe:
    """Minimal client: healthy service, empty remote listing, forced upload error."""

    def __init__(self, upload_raises=None):
        self.upload_raises = upload_raises

    def probe(self):
        return {"storage": {"used_human": "1 GB", "max_human": "10 GB",
                            "free_human": "9 GB", "pcent": 10, "free": 9_000_000_000,
                            "used": 1_000_000_000, "max": 10_000_000_000}}

    def listing(self, folder_id):
        return []

    def ensure_folder(self, parent_id, name):
        return 1

    def upload(self, folder_id, path):
        if self.upload_raises:
            raise self.upload_raises

    def user_stats(self):
        return self.probe()


def make_config(tmp, with_file=False, error=None):
    """Config with a webhook, plus the source tree; returns (config path, client)."""
    src = os.path.join(tmp, "src")
    os.makedirs(src, exist_ok=True)
    if with_file:
        with open(os.path.join(src, "a.txt"), "w") as handle:
            handle.write("x")
    path = os.path.join(tmp, "config.toml")
    with open(path, "w") as handle:
        handle.write(f'[webhook]\nurl = "http://hook/{int(time.time())}"\n\n'
                     f'[[mirror]]\nlocal = "{src}"\nremote = "R"\n')
    return path, Probe(upload_raises=error)


class Webhook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_start_and_finish_posted(self):
        posts = []
        config, client = make_config(self.tmp)
        args = ["mirror", "--config", config, "--allow-empty",
                "--no-token-cache", "--no-upload-journal", "--no-sync-db"]
        with mock.patch.object(cli, "build_client", return_value=client), \
             mock.patch.object(cli, "post_webhook", side_effect=lambda u, p: posts.append((u, p))):
            code = cli.main(args)
        self.assertEqual(code, 0)
        self.assertEqual([p[1]["event"] for p in posts], ["started", "finished"])
        self.assertTrue(posts[1][1]["ok"])
        self.assertEqual(posts[1][1]["failures"], 0)

    def test_file_failure_reported(self):
        posts = []
        config, client = make_config(self.tmp, with_file=True, error=Exception("boom"))
        args = ["mirror", "--config", config,
                "--no-token-cache", "--no-upload-journal", "--no-sync-db"]
        with mock.patch.object(cli, "build_client", return_value=client), \
             mock.patch.object(cli, "post_webhook", side_effect=lambda u, p: posts.append((u, p))):
            code = cli.main(args)
        self.assertEqual(code, 1)
        finished = posts[1][1]
        self.assertEqual(finished["event"], "finished")
        self.assertFalse(finished["ok"])
        self.assertEqual(finished["failures"], 1)
        self.assertEqual(finished["mirrors"][0]["failures"][0]["path"], "a.txt")
        self.assertEqual(finished["mirrors"][0]["failures"][0]["error"], "boom")

    def test_abort_error_reported(self):
        posts = []
        config, client = make_config(self.tmp, with_file=True, error=TransientError("service down"))
        args = ["mirror", "--config", config,
                "--no-token-cache", "--no-upload-journal", "--no-sync-db"]
        with mock.patch.object(cli, "build_client", return_value=client), \
             mock.patch.object(cli, "post_webhook", side_effect=lambda u, p: posts.append((u, p))):
            code = cli.main(args)
        self.assertEqual(code, 3)
        finished = posts[1][1]
        self.assertFalse(finished["ok"])
        self.assertIn("service down", finished["error"])

    def test_bad_webhook_never_fails_the_run(self):
        config, client = make_config(self.tmp)
        args = ["mirror", "--config", config, "--allow-empty",
                "--no-token-cache", "--no-upload-journal", "--no-sync-db"]
        # real post_webhook against a dead endpoint must not raise out of main
        with mock.patch.object(cli, "build_client", return_value=client):
            code = cli.main(args)
        self.assertEqual(code, 0)

    def test_no_webhook_config_no_report_object(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        config = os.path.join(self.tmp, "plain.toml")
        with open(config, "w") as handle:
            handle.write(f'[[mirror]]\nlocal = "{src}"\nremote = "R"\n')
        args = ["mirror", "--config", config, "--allow-empty",
                "--no-token-cache", "--no-upload-journal", "--no-sync-db"]
        with mock.patch.object(cli, "build_client", return_value=Probe()):
            code = cli.main(args)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

class DiscordPayload(unittest.TestCase):
    def test_shape(self):
        payload = {"event": "finished", "command": "mirror", "ok": False, "failures": 1,
                   "started": "s", "finished": "f", "error": "service down",
                   "mirrors": [{"remote": "R", "failures": [{"path": "a.txt", "error": "boom"}]}]}
        body = cli.discord_payload(payload)
        embed = body["embeds"][0]
        self.assertIn("FAILED", embed["title"])
        self.assertEqual(embed["color"], 0xe74c3c)
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("R/a.txt", embed["fields"][-2]["value"])
        self.assertIn("error", names)
        self.assertIn("file failures (1)", names)

    def test_caps(self):
        payload = {"event": "finished", "ok": False, "mirrors": [
            {"remote": "R", "failures": [{"path": f"f{i}", "error": "x"} for i in range(500)]}]}
        embed = cli.discord_payload(payload)["embeds"][0]
        self.assertEqual(len(embed["fields"]), 1)         # all 500 collapse into one field
        self.assertLessEqual(len(embed["fields"][0]["value"]), 1024)
