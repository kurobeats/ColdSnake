"""Client-side feature tests: trash/restore, versions, batch delete, the
download engine split, and the cross-run upload journal. Network is faked."""
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.client import (Client, IcedriveError, TransientError,  # noqa: E402
                              upload_id_for)
from coldsnake.state import UploadJournal                              # noqa: E402


class Recorder(Client):
    """Client with the transport replaced: records each call, serves canned bodies."""

    def __init__(self, responses=None):
        super().__init__("e@example.com", "pw")
        self.responses = responses or {}
        self.calls = []
        self.erased = []
        self.raise_on_erase = None

    def call(self, path, body=None, content_type=None, method="GET", auth=True):
        self.calls.append({"path": path, "method": method,
                           "form": urllib.parse.parse_qs(body.decode()) if body else {}})
        if path == "/erase" and self.raise_on_erase is not None:
            raise self.raise_on_erase
        return self.responses.get(path, {})

    def delete_file(self, file_id):
        self.erased.append(file_id)


class TrashTests(unittest.TestCase):
    def test_trash_request_shape(self):
        client = Recorder()
        client.trash(123)
        self.assertEqual(client.calls, [{"path": "/api", "method": "POST",
                                         "form": {"request": ["trash-add"], "items": ["file-123"]}}])

    def test_trash_folder_prefix(self):
        client = Recorder()
        client.trash(9, is_folder=True)
        self.assertEqual(client.calls[0]["form"]["items"], ["folder-9"])

    def test_restore_request_shape(self):
        client = Recorder()
        client.restore(123)
        self.assertEqual(client.calls, [{"path": "/api", "method": "POST",
                                         "form": {"request": ["trash-restore"], "items": ["file-123"]}}])

    def test_restore_folder_prefix(self):
        client = Recorder()
        client.restore(9, is_folder=True)
        self.assertEqual(client.calls[0]["form"]["request"], ["trash-restore"])
        self.assertEqual(client.calls[0]["form"]["items"], ["folder-9"])

    def test_trash_listing_unwraps_data(self):
        response = {"/collection?type=trash&folderId=0": {"data": [{"id": 1, "filename": "x"}]}}
        client = Recorder(response)
        self.assertEqual(client.trash_listing(), [{"id": 1, "filename": "x"}])


class VersionTests(unittest.TestCase):
    def test_versions_assigns_index_and_passes_url(self):
        raw = {"filename": "f.bin", "versions": [
            {"current": True, "timestamp": 1790248953, "filesize": 108006, "url": "https://a/1"},
            {"current": False, "timestamp": 1790248946, "filesize": 108006, "url": "https://a/2"},
        ]}
        client = Recorder({"/version-list?id=7": raw})
        result = client.versions(7)
        self.assertEqual([v["index"] for v in result], [0, 1])
        self.assertEqual(result[1]["url"], "https://a/2")     # url passthrough for download
        self.assertEqual([v["timestamp"] for v in result], [1790248953, 1790248946])


class BatchDeleteTests(unittest.TestCase):
    def test_batch_success_is_one_call(self):
        client = Recorder()
        self.assertEqual(client.delete_files([1, 2]), 2)
        self.assertEqual(client.erased, [], "batch success must not fall back to per-file")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["path"], "/erase")
        self.assertEqual(client.calls[0]["form"]["items"], ["file-1,file-2"])

    def test_batch_rejected_raises_without_a_second_attempt(self):
        # Failure isolation belongs to the caller (Mirror._erase): a partly deleted
        # batch must not be re-erased by a second fallback layer here.
        client = Recorder()
        client.raise_on_erase = IcedriveError("invalid request")
        with self.assertRaises(IcedriveError):
            client.delete_files([1, 2, 3])
        self.assertEqual(client.erased, [])
        self.assertEqual(len(client.calls), 1, "one batch attempt, no per-file retry")

    def test_transient_error_does_not_fall_back(self):
        client = Recorder()
        client.raise_on_erase = TransientError("service down")
        with self.assertRaises(TransientError):
            client.delete_files([1, 2])
        self.assertEqual(client.erased, [], "an outage must never become a silent partial success")

    def test_empty_list_is_a_noop(self):
        client = Recorder()
        self.assertEqual(client.delete_files([]), 0)
        self.assertEqual(client.calls, [])


class _FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body
        self._pos = 0

    def read(self, n=-1):
        if n < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadTests(unittest.TestCase):
    def test_download_delegates_to_download_from_url(self):
        client = Client("e@example.com", "pw")
        with mock.patch.object(client, "download_url", return_value="https://node/x") as url, \
                mock.patch.object(client, "download_from_url", return_value=99) as engine:
            written = client.download(5, "/dest/f.bin", size=99)
        self.assertEqual(written, 99)
        url.assert_called_once_with(5)
        engine.assert_called_once_with("https://node/x", "/dest/f.bin", 99)

    def test_download_from_url_creates_parent_dirs(self):
        payload = b"0123456789"
        client = Client("e@example.com", "pw")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "nested", "sub", "f.bin")   # parent dirs do not exist
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResponse(200, {"Content-Length": str(len(payload))},
                                                       payload)):
                written = client.download_from_url("https://node/x", dest, size=len(payload))
            self.assertTrue(os.path.isdir(os.path.dirname(dest)), "parent dirs must be created")
            self.assertEqual(written, len(payload))
            with open(dest, "rb") as handle:
                self.assertEqual(handle.read(), payload)
            self.assertFalse(os.path.exists(dest + ".tmp"), "the .tmp must be renamed away")

    def test_download_from_url_resumes_seeded_partial(self):
        payload = b"0123456789"
        ranges = []

        def fake_urlopen(request, **kwargs):
            ranges.append(request.get_header("Range"))
            return _FakeResponse(206, {"Content-Range": "bytes 5-9/10", "Content-Length": "5"},
                                 payload[5:])

        client = Client("e@example.com", "pw")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "f.bin")
            with open(dest + ".tmp", "wb") as handle:     # seeded partial from an earlier run
                handle.write(payload[:5])
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                written = client.download_from_url("https://node/x", dest, size=len(payload))
            self.assertEqual(written, len(payload))
            self.assertEqual(ranges, ["bytes=5-"], "a seeded partial must resume via Range")
            with open(dest, "rb") as handle:
                self.assertEqual(handle.read(), payload)
            self.assertFalse(os.path.exists(dest + ".tmp"), "the .tmp must be renamed away")


class _JournalClient(Client):
    """Client whose chunk sender is faked: records offsets, can fail on one."""

    def __init__(self, journal, fail_at=None, listing_rows=None):
        super().__init__("e@example.com", "pw", chunk_size=4, journal=journal)
        self.sent = []
        self.fail_at = fail_at
        self.listing_rows = listing_rows or []

    def listing(self, folder_id=0):
        return list(self.listing_rows)

    def _send(self, path, preamble, trailer, length, headers, offset, size):
        self.sent.append(offset)
        if offset == self.fail_at:
            raise IcedriveError("boom")
        return {"message": "Upload Successful"}


class JournalTests(unittest.TestCase):
    def test_marks_then_skips_then_clears_across_two_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.bin")
            with open(path, "wb") as handle:
                handle.write(b"0123456789")               # 10 bytes -> offsets 0, 4, 8
            journal = UploadJournal(os.path.join(tmp, "journal.json"))
            upload_id = upload_id_for(0, path, 10, int(os.stat(path).st_mtime))

            first = _JournalClient(journal, fail_at=8)    # dies on the last chunk
            with self.assertRaises(IcedriveError):
                first._upload_chunked(0, path, os.stat(path))
            self.assertEqual(first.sent, [0, 4, 8])
            self.assertEqual(journal.done(upload_id), {0, 4}, "accepted chunks are journalled")
            self.assertFalse(os.path.exists(path + ".tmp"))

            second = _JournalClient(journal)              # same file: same upload id
            second._upload_chunked(0, path, os.stat(path))
            self.assertEqual(second.sent, [8], "already-landed offsets must be skipped")
            self.assertEqual(journal.done(upload_id), set(), "clear once the file completes")

    def test_none_journal_sends_every_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.bin")
            with open(path, "wb") as handle:
                handle.write(b"0123456789")
            client = _JournalClient(None)
            client._upload_chunked(0, path, os.stat(path))
            self.assertEqual(client.sent, [0, 4, 8])

    def test_all_offsets_journalled_is_believed_only_if_the_server_agrees(self):
        """A run killed between the last mark and the clear must not just claim success."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.bin")
            with open(path, "wb") as handle:
                handle.write(b"0123456789")
            journal = UploadJournal(os.path.join(tmp, "journal.json"))
            upload_id = upload_id_for(0, path, 10, int(os.stat(path).st_mtime))
            for offset in (0, 4, 8):
                journal.mark(upload_id, offset)

            agreed = _JournalClient(journal, listing_rows=[{"filename": "f.bin", "filesize": 10}])
            agreed._upload_chunked(0, path, os.stat(path))
            self.assertEqual(agreed.sent, [], "no chunk needs re-sending")
            self.assertEqual(journal.done(upload_id), set(), "believed: journal cleared")

            for offset in (0, 4, 8):                # seed again for the disagreeing server
                journal.mark(upload_id, offset)
            short = _JournalClient(journal, listing_rows=[{"filename": "f.bin", "filesize": 4}])
            with self.assertRaises(IcedriveError):
                short._upload_chunked(0, path, os.stat(path))
            self.assertEqual(journal.done(upload_id), set(),
                             "a stale journal must be dropped so the file is re-sent")

            missing = _JournalClient(journal, listing_rows=[])
            for offset in (0, 4, 8):
                journal.mark(upload_id, offset)
            with self.assertRaises(IcedriveError):
                missing._upload_chunked(0, path, os.stat(path))


class FolderCreateTests(unittest.TestCase):
    """A 2006 "Folder exists" from folder-create must not abort a mirror:
    the listing can lag a folder a previous run just created."""

    def test_folder_exists_returns_none(self):
        client = Recorder(responses={"/folder-create":
                                     {"error": True, "code": 2006, "message": "Folder exists"}})
        self.assertIsNone(client.create_folder(0, "music"))

    def test_ensure_folder_recovers_by_relisting(self):
        client = Recorder(responses={"/folder-create":
                                     {"error": True, "code": 2006, "message": "Folder exists"}})
        listings = [[], [{"id": 7, "filename": "music", "isFolder": 1}]]
        client.listing = lambda folder_id=0: listings.pop(0)
        self.assertEqual(client.ensure_folder(0, "music"), 7)

    def test_transient_error_still_propagates(self):
        client = Recorder()
        with mock.patch.object(client, "call", side_effect=TransientError("HTTP 503")):
            with self.assertRaises(TransientError):
                client.create_folder(0, "music")

    def test_uncreateable_folder_still_fails_loudly(self):
        client = Recorder(responses={"/folder-create":
                                     {"error": True, "code": 2001, "message": "Missing data"}})
        client.listing = lambda folder_id=0: []
        with self.assertRaises(IcedriveError):
            client.ensure_folder(0, "music")


if __name__ == "__main__":
    unittest.main()


class EnsureFolderNormalizationTests(unittest.TestCase):
    def test_ensure_folder_reuses_normalized_existing(self):
        client = Recorder()
        client.responses["/collection?type=cloud&folderId=0"] = {
            "data": [{"id": 9, "filename": "Dante Mars Ajeto!", "isFolder": 1}]}
        self.assertEqual(client.ensure_folder(0, "Dante Mars Ajeto\uff01"), 9)
        self.assertFalse(any(c["path"] == "/folder-create" for c in client.calls))  # no create call


class RetryBackoffTests(unittest.TestCase):
    """429 is the throttle answer: retry slowly or a long listing pass aborts."""

    def _client_with_429s(self, times):
        client = Recorder()
        responses = [urllib.error.HTTPError("url", 429, "Too Many Requests",
                                            {"Retry-After": "60"}, None)] * times
        calls = {"n": 0}

        def operation():
            index = calls["n"]
            calls["n"] += 1
            if index < times:
                raise responses[index]
            return "ok"

        with mock.patch("time.sleep") as slept:
            result = client._retry(operation, auth=False)
        return result, [c.args[0] for c in slept.call_args_list]

    def test_429_waits_retry_after_not_exponential(self):
        result, sleeps = self._client_with_429s(times=2)
        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [60, 60])

    def test_429_without_header_waits_60s(self):
        client = Recorder()
        state = {"n": 0}

        def operation():
            state["n"] += 1
            if state["n"] == 1:
                raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            return "ok"

        with mock.patch("time.sleep") as slept:
            result = client._retry(operation, auth=False)
        self.assertEqual(result, "ok")
        self.assertEqual(slept.call_args_list[0].args[0], 60)
