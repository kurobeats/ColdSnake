"""Tests for the pure logic: proof-of-work grading and mirror decisions."""
import base64
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.client import (AuthError, Client, IcedriveError, TransientError,  # noqa: E402
                              check_payload, chunk_ranges, leading_zero_bits, solve_pow,
                              upload_id_for)
from coldsnake.sync import Mirror                                  # noqa: E402


class FakeClient:
    """In-memory stand-in for the API: one flat folder, name -> entry."""

    def __init__(self):
        self.tree = {0: []}
        self.uploads = []
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


if __name__ == "__main__":
    unittest.main()
