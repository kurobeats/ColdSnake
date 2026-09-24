"""Icedrive v3 mobile API client: proof-of-work login, folders, streaming upload.

Protocol (recovered from Icedrive's own clients):
  POST /api  {app:ios, request:pow-new, scope:login}     -> PoW challenge
  POST /api  {password, email, pow_proof, request:login} -> bearer token
  GET  /collection?type=cloud&folderId=<id>              -> folder listing
  POST /folder-create (multipart)                        -> create a folder
  GET  /geo-fileserver-list?app=ios&pow_proof=<b64>      -> signed /deposit endpoints
  POST <deposit endpoint> multipart {folderId, moddate, files[]} -> store the file

PoW: sha256(challenge_bytes || nonce12 || counter_be32) must have at least
difficultyBits leading zero bits; pow_proof is base64(JSON of the challenge
plus the winning nonce and hash).

Re-uploading the same filename inside a folder overwrites server-side (same file
id, no duplicate), which is what makes a re-sync idempotent.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://apis.icedrive.net/v3/mobile"
USER_AGENT = "icedrive-ios/2.3.1"
TIMEOUT = 600
STREAM_CHUNK = 1024 * 1024
RETRY_STATUS = (429, 500, 502, 503, 504, 522, 524)


class IcedriveError(RuntimeError):
    """Any unrecoverable API failure."""


class AuthError(IcedriveError):
    """Credentials rejected."""


def now() -> str:
    return time.strftime("%F %T")


def leading_zero_bits(data: bytes) -> int:
    """Count leading zero bits, used to grade a proof-of-work hash."""
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
            continue
        for bit in range(7, -1, -1):
            if not (byte >> bit) & 1:
                count += 1
            else:
                return count
    return count


def solve_pow(challenge: dict) -> dict:
    """Solve a PoW challenge, returning the pow_proof payload."""
    challenge_bytes = base64.urlsafe_b64decode(challenge["challenge"] + "==")
    nonce = secrets.token_bytes(12)
    buf = bytearray(challenge_bytes + nonce + b"\x00\x00\x00\x00")
    offset = len(challenge_bytes) + 12
    for counter in range(1 << 32):
        buf[offset : offset + 4] = counter.to_bytes(4, "big")
        digest = hashlib.sha256(bytes(buf)).digest()
        if leading_zero_bits(digest) >= challenge["difficultyBits"]:
            return {
                "client_id": "",
                "token": challenge["token"],
                "challenge": challenge["challenge"],
                "ver": "1",
                "hash": digest.hex(),
                "nonce": base64.urlsafe_b64encode(nonce + counter.to_bytes(4, "big")).decode().rstrip("="),
                "exp": challenge["exp"],
                "difficultyBits": challenge["difficultyBits"],
                "scope": challenge["scope"],
            }
    raise IcedriveError("proof-of-work counter exhausted")


def _urlencode(data: dict) -> tuple[bytes, str]:
    return urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"


def _multipart(fields: dict) -> tuple[bytes, str]:
    boundary = "----geckoformboundary" + uuid.uuid4().hex
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    ) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class Client:
    """Minimal Icedrive client. Log in once, then list/create/upload."""

    def __init__(self, email: str, password: str, verbose: bool = False,
                 timeout: int = TIMEOUT, retries: int = 3, log=print):
        self.email = email
        self.password = password
        self.verbose = verbose
        self.timeout = timeout
        self.retries = retries
        self.log = log
        self.token: str | None = None
        self._context = ssl.create_default_context()
        self._endpoints: list[str] = []
        self._endpoints_at = 0.0

    # --- transport -------------------------------------------------------
    def _request(self, url, body=None, content_type=None, method="GET", auth=True):
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept", "*/*")
        if content_type:
            request.add_header("Content-Type", content_type)
        if auth and self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(request, context=self._context, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw.lstrip().startswith(b"{") else raw

    def _retry(self, operation, auth: bool):
        """Run operation, retrying 5xx/429/network errors; re-login once on 401/403."""
        last = None
        for attempt in range(self.retries + 1):
            try:
                return operation()
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code} {exc.reason}"
                if exc.code in (401, 403) and auth:
                    self.log(f"[{now()}] auth error {exc.code}; logging in again")
                    self.login()
                    continue
                if exc.code not in RETRY_STATUS:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise IcedriveError(f"request failed after {self.retries + 1} attempts: {last}")

    def call(self, path, body=None, content_type=None, method="GET", auth=True):
        return self._retry(lambda: self._request(API + path, body, content_type, method, auth), auth)

    # --- auth ------------------------------------------------------------
    def login(self) -> dict:
        body, content_type = _urlencode({"app": "ios", "request": "pow-new", "scope": "login"})
        challenge = self._retry(
            lambda: self._request(API + "/api", body, content_type, "POST", auth=False), auth=False)
        proof = base64.b64encode(json.dumps(solve_pow(challenge)).encode()).decode()
        body, content_type = _urlencode({
            "password": self.password, "pow_proof": proof, "request": "login",
            "email": self.email, "no_token_check": "true", "app": "ios",
        })
        result = self._retry(
            lambda: self._request(API + "/api", body, content_type, "POST", auth=False), auth=False)
        if not isinstance(result, dict) or not result.get("token"):
            raise AuthError(f"login failed: {json.dumps(result)[:200]}")
        self.token = result["token"]
        self._endpoints, self._endpoints_at = [], 0.0
        if self.verbose:
            auth = result.get("auth_data", {})
            self.log(f"[{now()}] logged in as {auth.get('email')} "
                     f"(plan {auth.get('plan')}, id {auth.get('id')})")
        return result

    # --- folders ---------------------------------------------------------
    def listing(self, folder_id: int = 0) -> list[dict]:
        return self.call(f"/collection?type=cloud&folderId={folder_id}").get("data", [])

    def create_folder(self, parent_id: int, name: str) -> int | None:
        body, content_type = _multipart({"request": "folder-create", "type": "folder-create",
                                         "parentId": str(parent_id), "filename": name})
        result = self.call("/folder-create", body, content_type, "POST")
        if isinstance(result, dict) and not result.get("error"):
            return result.get("folderId") or result.get("id")
        return None

    def ensure_folder(self, parent_id: int, name: str) -> int:
        """Return the id of a child folder, creating it if needed."""
        for entry in self.listing(parent_id):
            if entry.get("filename") == name and entry.get("isFolder"):
                return entry["id"]
        created = self.create_folder(parent_id, name)
        if created:
            return created
        for entry in self.listing(parent_id):        # lost a race, or the name existed
            if entry.get("filename") == name and entry.get("isFolder"):
                return entry["id"]
        raise IcedriveError(f"cannot create remote folder {name!r}")

    # --- uploads ---------------------------------------------------------
    def upload_endpoints(self) -> list[str]:
        if self._endpoints and time.time() - self._endpoints_at < 240:
            return list(self._endpoints)
        body, content_type = _urlencode({"app": "ios", "request": "pow-new", "scope": "geo-fileserver-list"})
        challenge = self.call("/api", body, content_type, "POST")
        proof = base64.b64encode(json.dumps(solve_pow(challenge)).encode()).decode()
        result = self.call(f"/geo-fileserver-list?app=ios&pow_proof={urllib.parse.quote(proof)}")
        endpoints = result.get("upload_endpoints") or []
        if not endpoints:
            raise IcedriveError(f"no upload endpoints: {json.dumps(result)[:200]}")
        self._endpoints, self._endpoints_at = endpoints, time.time()
        return list(endpoints)

    def upload(self, folder_id: int, path: str) -> dict:
        """Stream one file to a signed /deposit endpoint. Constant memory."""
        stat = os.stat(path)
        name = os.path.basename(path).replace("\\", "\\\\").replace('"', '\\"')
        boundary = "----geckoformboundary" + uuid.uuid4().hex
        preamble = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="folderId"\r\n\r\n{folder_id}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="moddate"\r\n\r\n{int(stat.st_mtime)}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[]"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        trailer = f"\r\n--{boundary}--\r\n".encode()
        length = len(preamble) + stat.st_size + len(trailer)
        last = None

        for attempt in range(self.retries + 1):
            for endpoint in self.upload_endpoints():
                parsed = urllib.parse.urlsplit(endpoint)
                target = parsed.path + ("?" + parsed.query if parsed.query else "")
                conn = None
                try:
                    conn = http.client.HTTPSConnection(parsed.netloc, timeout=self.timeout, context=self._context)
                    conn.putrequest("POST", target)
                    conn.putheader("User-Agent", USER_AGENT)
                    conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                    conn.putheader("Content-Length", str(length))
                    conn.endheaders()
                    conn.send(preamble)
                    with open(path, "rb") as handle:
                        while True:
                            block = handle.read(STREAM_CHUNK)
                            if not block:
                                break
                            conn.send(block)
                    conn.send(trailer)
                    response = conn.getresponse()
                    raw = response.read()
                    if response.status >= 400:
                        last = f"HTTP {response.status}"
                        continue
                    result = json.loads(raw) if raw.lstrip().startswith(b"{") else {}
                    if result.get("error"):
                        last = json.dumps(result)[:200]
                        continue
                    return result
                except Exception as exc:                        # noqa: BLE001 - try the next mirror
                    last = exc
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:                       # noqa: BLE001
                            pass
            if attempt < self.retries:
                time.sleep(2 ** attempt)
                self._endpoints, self._endpoints_at = [], 0.0
        raise IcedriveError(f"upload failed for {path}: {last}")
