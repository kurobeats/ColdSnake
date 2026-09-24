"""Client-side move/rename tests: the exact probe-verified request shapes.

Network is faked: a Recorder subclass replaces the transport, so the assertions
are about what would go on the wire, not about the API.
"""
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coldsnake.client import (AuthError, Client, IcedriveError,  # noqa: E402
                              TransientError)


class Recorder(Client):
    """Client with the transport replaced: records each call, serves canned bodies."""

    def __init__(self, responses=None):
        super().__init__("e@example.com", "pw")
        self.responses = responses or {}
        self.calls = []
        self.bodies = []

    def call(self, path, body=None, content_type=None, method="GET", auth=True):
        self.calls.append({"path": path, "method": method,
                           "form": urllib.parse.parse_qs(body.decode()) if body else {}})
        self.bodies.append(body.decode() if body else "")
        result = self.responses.get(path, {})
        if isinstance(result, Exception):
            raise result
        return result


class MoveTests(unittest.TestCase):
    def test_one_id_request_shape(self):
        client = Recorder()
        self.assertEqual(client.move_files([123], 42), 1)
        self.assertEqual(client.calls, [{"path": "/api", "method": "POST",
                                         "form": {"request": ["move"], "items": ["file-123"],
                                                  "folderId": ["42"]}}])

    def test_several_ids_are_comma_joined_on_items(self):
        client = Recorder()
        self.assertEqual(client.move_files([1, 2, 3], 7), 3)
        self.assertEqual(len(client.calls), 1, "one batch call, not one call per file")
        self.assertEqual(client.calls[0]["form"]["items"], ["file-1,file-2,file-3"])
        self.assertEqual(client.bodies[0], "request=move&items=file-1%2Cfile-2%2Cfile-3&folderId=7")

    def test_folder_id_passthrough_and_returned_count(self):
        client = Recorder()
        self.assertEqual(client.move_files([9, 8], 555), 2)
        self.assertEqual(client.calls[0]["form"]["folderId"], ["555"])

    def test_empty_list_is_a_noop(self):
        client = Recorder()
        self.assertEqual(client.move_files([], 42), 0)
        self.assertEqual(client.calls, [], "an empty move must issue no call")

    def test_transient_error_is_not_swallowed(self):
        client = Recorder({"/api": TransientError("service down")})
        with self.assertRaises(TransientError):
            client.move_files([1, 2], 42)
        self.assertEqual(len(client.calls), 1, "the failure must surface, not be retried away")

    def test_auth_error_propagates_unchanged(self):
        client = Recorder({"/api": AuthError("bad token")})
        with self.assertRaises(AuthError):
            client.move_files([1], 42)


class RenameTests(unittest.TestCase):
    def test_rename_request_shape(self):
        client = Recorder()
        client.rename_file(123, "new.txt")
        self.assertEqual(client.calls, [{"path": "/api", "method": "POST",
                                         "form": {"request": ["file-rename"], "id": ["123"],
                                                  "filename": ["new.txt"]}}])
        self.assertEqual(client.bodies, ["request=file-rename&id=123&filename=new.txt"])

    def test_returned_name_comes_from_the_response(self):
        response = {"/api": {"error": False, "message": "File Renamed",
                             "filename": "server.txt", "id": 123}}
        client = Recorder(response)
        self.assertEqual(client.rename_file(123, "asked.txt"), "server.txt")

    def test_missing_response_name_falls_back_to_the_request(self):
        client = Recorder()                      # every response is {} by default
        self.assertEqual(client.rename_file(123, "asked.txt"), "asked.txt")

    def test_empty_name_is_refused_before_any_request(self):
        client = Recorder()
        with self.assertRaises(IcedriveError):
            client.rename_file(123, "   ")
        self.assertEqual(client.calls, [], "a bad name must never reach the API")

    def test_transient_error_is_not_swallowed(self):
        client = Recorder({"/api": TransientError("service down")})
        with self.assertRaises(TransientError):
            client.rename_file(123, "new.txt")
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
