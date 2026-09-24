"""Tests for the pure logic: proof-of-work grading and mirror decisions."""
import base64
import hashlib
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.client import (AuthError, Client, IcedriveError, TransientError,  # noqa: E402
                              check_payload, chunk_ranges, leading_zero_bits, solve_pow,
                              upload_id_for)
from coldsnake.sync import Mirror, PreflightError, preflight         # noqa: E402


class FakeClient:
    """In-memory stand-in for the API: one flat folder, name -> entry."""

    def __init__(self):
        self.tree = {0: []}
        self.uploads = []
        self.deleted = []
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
        stat = os.stat(path)
        name = os.path.basename(path)
        self.uploads.append(name)
        self.tree[folder_id] = [e for e in self.tree.get(folder_id, []) if e["filename"] != name]
        self.tree[folder_id].append({"filename": name, "filesize": stat.st_size,
                                     "moddate": int(stat.st_mtime), "isFolder": 0})
        return {"error": False}

    def download_url(self, file_id):
        return f"fake://file-{file_id}"

    def download(self, file_id, dest):
        # fake payload keyed by id: lets tests assert content round-trips
        data = f"payload-{file_id}".encode()
        with open(dest + ".tmp", "wb") as handle:
            handle.write(data)
        os.replace(dest + ".tmp", dest)
        return len(data)

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        for fid, entries in self.tree.items():
            self.tree[fid] = [e for e in entries if e.get("id") != file_id]


class ProofOfWorkTests(unittest.TestCase):
    def test_leading_zero_bits(self):
        self.assertEqual(leading_zero_bits(b"\x00"), 8)
        self.assertEqual(leading_zero_bits(b"\x00\x80"), 8)
        self.assertEqual(leading_zero_bits(b"\xff"), 0)
        self.assertEqual(leading_zero_bits(b"\x01"), 7)

    def test_solution_satisfies_requested_difficulty(self):
        challenge = {"challenge": base64.urlsafe_b64encode(b"seed-bytes").decode().rstrip("="),
                     "token": "tok", "exp": 1, "difficultyBits": 12, "scope": "login"}
        proof = solve_pow(challenge)
        seed = base64.urlsafe_b64decode(proof["challenge"] + "==")
        nonce = base64.urlsafe_b64decode(proof["nonce"] + "==")
        digest = hashlib.sha256(seed + nonce).digest()
        self.assertGreaterEqual(leading_zero_bits(digest), 12)
        self.assertEqual(digest.hex(), proof["hash"])
        self.assertEqual(proof["ver"], "1")


class ChunkingTests(unittest.TestCase):
    """Ranged chunks keyed by unique_upload_id are what make a stall cheap."""

    def test_ranges_cover_the_file_exactly(self):
        self.assertEqual(chunk_ranges(10, 4), [(0, 4), (4, 4), (8, 2)])
        self.assertEqual(chunk_ranges(8, 4), [(0, 4), (4, 4)])
        self.assertEqual(chunk_ranges(3, 4), [(0, 3)])
        sizes = [length for _, length in chunk_ranges(8388608 * 3 + 17, 8388608)]
        self.assertEqual(sum(sizes), 8388608 * 3 + 17)
        self.assertEqual(len(sizes), 4)

    def test_upload_id_is_stable_for_the_same_content(self):
        first = upload_id_for(42, "/data/big.bin", 1024, 1700000000)
        self.assertEqual(first, upload_id_for(42, "/other/dir/big.bin", 1024, 1700000000))
        self.assertNotEqual(first, upload_id_for(42, "/data/big.bin", 1025, 1700000000))
        self.assertNotEqual(first, upload_id_for(43, "/data/big.bin", 1024, 1700000000))


class PreflightTests(unittest.TestCase):
    """Nothing long should start when the service is down or a source is bogus."""

    class ProbeClient:
        def __init__(self, error=None):
            self.error = error

        def probe(self):
            if self.error:
                raise self.error
            return {"storage": {"free_human": "2.00 TB", "used_human": "0 B", "max_human": "2.00 TB"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.txt"), "w") as handle:
            handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_unavailable_stops_the_run(self):
        with self.assertRaises(TransientError):
            preflight(self.ProbeClient(TransientError("Service temporarily unavailable")),
                      [(self.src, "Remote")])

    def test_missing_source_is_refused(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(self.ProbeClient(), [(os.path.join(self.tmp.name, "nope"), "Remote")])
        self.assertIn("does not exist", str(ctx.exception))

    def test_empty_source_is_refused_unless_allowed(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        with self.assertRaises(PreflightError) as ctx:
            preflight(self.ProbeClient(), [(empty, "Remote")])
        self.assertIn("empty", str(ctx.exception))
        info = preflight(self.ProbeClient(), [(empty, "Remote")], allow_empty=True)
        self.assertTrue(info["storage"])

    def test_same_source_twice_is_refused(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(self.ProbeClient(), [(self.src, "One"), (self.src + "/", "Two")])
        self.assertIn("mirrored twice", str(ctx.exception))

    def test_healthy_run_passes(self):
        info = preflight(self.ProbeClient(), [(self.src, "Remote")])
        self.assertIn("storage", info)


class PayloadValidationTests(unittest.TestCase):
    """The API signals failure with HTTP 200 + an error body."""

    def test_ok_payload_passes_through(self):
        self.assertEqual(check_payload({"error": False, "data": [1]}), {"error": False, "data": [1]})

    def test_error_payload_raises(self):
        with self.assertRaises(IcedriveError):
            check_payload({"error": True, "code": 2003, "message": "Invalid request"})

    def test_service_unavailable_is_transient(self):
        for payload in ({"error": True, "code": 503, "message": "Service temporarily unavailable"},
                        {"error": True, "code": 0, "message": "Fatal error encountered"}):
            with self.assertRaises(TransientError):
                check_payload(payload)

    def test_auth_error_payload_raises_auth_error(self):
        with self.assertRaises(AuthError):
            check_payload({"error": True, "code": 1001, "message": "Not authenticated"})

    def test_expired_token_does_not_load_from_cache(self):
        client = Client("a@b.c", "pw")
        client.token = "stale"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            client.save_token(path)

            def reject():
                raise AuthError("token expired")

            self.assertFalse(Client("a@b.c", "pw").load_token(path, validate=reject))


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "a.txt"), "w") as handle:
            handle.write("hello")
        with open(os.path.join(self.root, "sub", "b.txt"), "w") as handle:
            handle.write("nested")

    def tearDown(self):
        self.tmp.cleanup()

    def run_mirror(self):
        client = FakeClient()
        mirror = Mirror(client, self.root, "Remote", log=lambda *_: None)
        stats = mirror.run()
        return client, stats

    def test_uploads_everything_then_is_idempotent(self):
        client, stats = self.run_mirror()
        self.assertEqual(sorted(client.uploads), ["a.txt", "b.txt"])
        self.assertEqual((stats.uploaded, stats.unchanged, stats.failed()), (2, 0, 0))
        self.assertEqual(stats.verified, 2)

        again = Mirror(client, self.root, "Remote", log=lambda *_: None)
        stats2 = again.run()
        self.assertEqual((stats2.uploaded, stats2.unchanged), (0, 2))

    def test_files_land_inside_the_remote_folder_not_the_drive_root(self):
        client, _ = self.run_mirror()
        top = {e["filename"]: e for e in client.tree[0]}
        self.assertIn("Remote", top, "mirror root folder must be created at the drive root")
        self.assertNotIn("a.txt", top, "files must not be uploaded loose into the drive root")
        remote_id = top["Remote"]["id"]
        self.assertEqual(sorted(e["filename"] for e in client.tree[remote_id]), ["a.txt", "sub"])
        sub_id = next(e["id"] for e in client.tree[remote_id] if e["isFolder"])
        self.assertEqual([e["filename"] for e in client.tree[sub_id]], ["b.txt"])

    def test_changed_size_is_reuploaded(self):
        client, _ = self.run_mirror()
        with open(os.path.join(self.root, "a.txt"), "w") as handle:
            handle.write("hello world")
        stats = Mirror(client, self.root, "Remote", log=lambda *_: None).run()
        self.assertEqual(stats.uploaded, 1)
        self.assertIn("a.txt", client.uploads[-1:])

    def test_dry_run_touches_nothing(self):
        client = FakeClient()
        stats = Mirror(client, self.root, "Remote", dry_run=True, log=lambda *_: None).run()
        self.assertEqual(stats.uploaded, 0)
        self.assertEqual(client.uploads, [])
        self.assertEqual(client.tree[0], [])

    def test_excludes_skip_upload(self):
        with open(os.path.join(self.root, "skip.tmp"), "w") as handle:
            handle.write("junk")
        with open(os.path.join(self.root, "sub", "skip.log"), "w") as handle:
            handle.write("junk")
        client = FakeClient()
        Mirror(client, self.root, "Remote", log=lambda *_: None,
               excludes=["*.tmp", "*.log"]).run()
        self.assertEqual(sorted(client.uploads), ["a.txt", "b.txt"])

    def test_prune_deletes_remote_only_files(self):
        client, _ = self.run_mirror()
        remote_id = next(e["id"] for e in client.tree[0] if e["filename"] == "Remote")
        client.tree[remote_id].append({"id": 999, "filename": "gone.txt",
                                       "filesize": 1, "moddate": 0, "isFolder": 0})
        stats = Mirror(client, self.root, "Remote", log=lambda *_: None, prune=True).run()
        self.assertEqual(client.deleted, [999])
        self.assertEqual(stats.failed(), 0)

    def test_prune_aborts_above_threshold_without_force(self):
        client, _ = self.run_mirror()
        remote_id = next(e["id"] for e in client.tree[0] if e["filename"] == "Remote")
        for i in range(60):                       # 2 remote files + 60 stale > threshold 50
            client.tree[remote_id].append({"id": 1000 + i, "filename": f"stale{i}",
                                           "filesize": 1, "moddate": 0, "isFolder": 0})
        with self.assertRaises(PreflightError):
            Mirror(client, self.root, "Remote", log=lambda *_: None, prune=True).run()
        self.assertEqual(client.deleted, [])
        Mirror(client, self.root, "Remote", log=lambda *_: None,
               prune=True, prune_force=True).run()
        self.assertEqual(len(client.deleted), 60)

    def test_prune_keeps_excluded_files(self):
        client, _ = self.run_mirror()
        remote_id = next(e["id"] for e in client.tree[0] if e["filename"] == "Remote")
        client.tree[remote_id].append({"id": 777, "filename": "cache.tmp",
                                       "filesize": 1, "moddate": 0, "isFolder": 0})
        Mirror(client, self.root, "Remote", log=lambda *_: None,
               prune=True, excludes=["*.tmp"]).run()
        self.assertEqual(client.deleted, [], "never-uploaded excluded files must not be pruned")

    def test_download_roundtrip(self):
        client, _ = self.run_mirror()
        remote_id = next(e["id"] for e in client.tree[0] if e["filename"] == "Remote")
        entry = client.tree[remote_id][0]
        dest = os.path.join(self.root, "out.bin")
        size = client.download(entry["id"], dest)
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), f"payload-{entry['id']}".encode())
        self.assertEqual(size, len(f"payload-{entry['id']}"))


class QuotaGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "a.bin"), "wb") as handle:
            handle.write(b"x" * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quota_gate_blocks_upload_when_space_is_short(self):
        calls = []

        def gate(needed):
            calls.append(needed)
            raise RuntimeError("not enough space")

        mirror = Mirror(FakeClient(), self.tmp.name, "Remote", log=lambda *_: None, quota_check=gate)
        with self.assertRaises(RuntimeError):
            mirror.run()
        self.assertEqual(calls, [1024], "gate must be asked once, with the bytes needed")

    def test_quota_gate_sees_exactly_the_pending_bytes(self):
        client = FakeClient()
        seen = []
        mirror = Mirror(client, self.tmp.name, "Remote", log=lambda *_: None,
                        quota_check=lambda needed: seen.append(needed))
        mirror.run()
        self.assertEqual(seen, [1024])
        # second run: nothing pending, so the gate is not consulted
        seen.clear()
        Mirror(client, self.tmp.name, "Remote", log=lambda *_: None,
               quota_check=lambda needed: seen.append(needed)).run()
        self.assertEqual(seen, [])

    def test_token_cache_round_trip(self):
        path = os.path.join(self.tmp.name, "token")
        client = Client("a@b.c", "pw")
        client.token = "tok123"
        client.account = {"email": "a@b.c", "plan": "Pro"}
        client.save_token(path)
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600", "token cache must be 0600")

        same = Client("a@b.c", "pw")
        self.assertTrue(same.load_token(path, validate=lambda: None))
        self.assertEqual(same.token, "tok123")
        self.assertEqual(same.account["plan"], "Pro")


class CliExitCodeTests(unittest.TestCase):
    """README promises exit 2 for 'refused to start: bad config' - hold it to that."""

    def test_bad_toml_exits_2(self):
        from coldsnake.cli import main
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write("[auth\nbroken =")
            path = handle.name
        try:
            self.assertEqual(main(["--config", path, "account"]), 2)
        finally:
            os.unlink(path)

    def test_missing_credentials_exits_2(self):
        from coldsnake.cli import main
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write("[auth]\nemail = 'x'\n")
            path = handle.name
        env = {k: v for k, v in os.environ.items() if not k.startswith("ICEDRIVE_")}
        home = tempfile.mkdtemp()                      # no legacy ~/.config/icedrive/credentials
        env["HOME"] = env["USERPROFILE"] = home
        try:
            code = subprocess.run(
                [sys.executable, "-m", "coldsnake.cli", "--config", path, "account"],
                env=env, capture_output=True, cwd=os.path.join(os.path.dirname(__file__), "..", "src"))
            self.assertEqual(code.returncode, 2)
        finally:
            os.unlink(path)
            os.rmdir(home)


class DownloadResumeTests(unittest.TestCase):
    """Signed URLs honour Range, so a retry must continue the partial .tmp.

    Real HTTP against a local server: this is the one path where "did it resume?"
    cannot be faked by an in-memory client, and a wrong answer corrupts files.
    """

    payload = bytes(range(256)) * 800          # 204 800 bytes

    class Handler(http.server.BaseHTTPRequestHandler):
        payload = b""
        requests = []
        honour_range = True
        spoof_range_start = False
        cut_first = 0
        always_cut = False

        def log_message(self, *args):
            pass

        def do_GET(self):
            type(self).requests.append(self.headers.get("Range"))
            start = 0
            rng = self.headers.get("Range")
            if type(self).spoof_range_start and rng:
                # a 206 whose Content-Range claims a start we did not ask for
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-{len(type(self).payload) - 1}/{len(type(self).payload)}")
            elif rng and rng.startswith("bytes=") and type(self).honour_range:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(type(self).payload) - 1}/{len(type(self).payload)}")
            else:
                self.send_response(200)
            body = type(self).payload[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            cut = type(self).always_cut or (type(self).cut_first and len(type(self).requests) == 1)
            if cut:
                self.wfile.write(body[:type(self).cut_first or 1024])
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.wfile.write(body)

    class LocalClient(Client):
        def __init__(self, url, **kwargs):
            super().__init__("a@b.c", "pw", log=lambda *_: None, **kwargs)
            self.url = url

        def download_url(self, file_id):
            return self.url

    def setUp(self):
        handler = type(self).Handler
        handler.payload = self.payload
        handler.requests = []
        handler.honour_range = True
        handler.spoof_range_start = False
        handler.cut_first = 0
        handler.always_cut = False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/signed"
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self.tmp.name, "restored.bin")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_truncated_transfer_resumes_and_lands_complete(self):
        type(self).Handler.cut_first = 50_000
        with mock.patch("coldsnake.client.time.sleep"):
            size = self.LocalClient(self.url, retries=2).download(7, self.dest, size=len(self.payload))
        self.assertEqual(size, len(self.payload))
        with open(self.dest, "rb") as handle:
            self.assertEqual(handle.read(), self.payload, "resumed file must be byte-exact")
        self.assertEqual(type(self).Handler.requests[1], "bytes=50000-",
                         "retry must ask for exactly the missing range")

    def test_server_ignoring_range_starts_over_instead_of_duplicating(self):
        type(self).Handler.honour_range = False
        with open(self.dest + ".tmp", "wb") as handle:
            handle.write(b"junk" * 4)               # stale partial from an earlier run
        with mock.patch("coldsnake.client.time.sleep"):
            self.LocalClient(self.url).download(7, self.dest, size=len(self.payload))
        with open(self.dest, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)

    def test_complete_partial_is_renamed_without_a_request(self):
        with open(self.dest + ".tmp", "wb") as handle:
            handle.write(self.payload)
        size = self.LocalClient(self.url).download(7, self.dest, size=len(self.payload))
        self.assertEqual(size, len(self.payload))
        self.assertEqual(type(self).Handler.requests, [])

    def test_a_misreported_range_is_not_appended_to(self):
        type(self).Handler.spoof_range_start = True
        with open(self.dest + ".tmp", "wb") as handle:
            handle.write(b"junk" * 4)
        with mock.patch("coldsnake.client.time.sleep"):
            self.LocalClient(self.url).download(7, self.dest, size=len(self.payload))
        with open(self.dest, "rb") as handle:
            self.assertEqual(handle.read(), self.payload,
                             "a body from an unexpected offset must not be appended")

    def test_short_transfer_never_commits_a_bad_file(self):
        type(self).Handler.always_cut = True
        with mock.patch("coldsnake.client.time.sleep"):
            with self.assertRaises(IcedriveError) as ctx:
                self.LocalClient(self.url, retries=1).download(7, self.dest, size=len(self.payload))
        self.assertIn("download failed", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest), "a truncated download must not land")


if __name__ == "__main__":
    unittest.main()
