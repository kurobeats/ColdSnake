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
# Socket timeouts are inactivity timeouts: as long as bytes flow, a large upload
# is fine. They are deliberately short so a stalled server surfaces as a retryable
# failure instead of hanging the run.
CONTROL_TIMEOUT = 60
UPLOAD_TIMEOUT = 120
STREAM_CHUNK = 1024 * 1024
# Files larger than this are uploaded as ranged chunks (verified against the API:
# chunks are keyed by unique_upload_id, idempotent when re-sent, and a different
# id cannot resume a partial upload). A stall therefore costs one chunk, not the
# whole file.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
RETRY_STATUS = (429, 500, 502, 503, 504, 522, 524)


class IcedriveError(RuntimeError):
    """Any unrecoverable API failure."""


class AuthError(IcedriveError):
    """Credentials rejected."""


class TransientError(IcedriveError):
    """Server is degraded or throttling: retry slowly, do not hammer."""


def now() -> str:
    return time.strftime("%F %T")


# The API reports failures with HTTP 200 and an error payload, so responses must
# be checked or a failed listing looks like an empty folder.
AUTH_ERROR_CODES = (1001, 1010, 2001)
# The API reports its own outages inside HTTP 200 payloads: "code": 0 "Fatal
# error encountered" or "Service temporarily unavailable". Retry these slowly
# rather than hammering.
TRANSIENT_ERROR_CODES = (0, 429, 500, 502, 503, 504)
TRANSIENT_MESSAGES = ("fatal error", "temporarily unavailable", "try again")


def chunk_ranges(size: int, chunk: int):
    """[(offset, length)] covering size in chunk-sized pieces."""
    return [(offset, min(chunk, size - offset)) for offset in range(0, size, chunk)]


def upload_id_for(folder_id: int, path: str, size: int, mtime: int) -> str:
    """Stable id for a destination+content pair: a retry (even in a later run)
    continues the same partial upload instead of creating a second one."""
    seed = f"{folder_id}/{os.path.basename(path)}/{size}/{mtime}"
    return hashlib.sha1(seed.encode()).hexdigest()


def check_payload(data):
    """Raise if an API response body is an error document."""
    if isinstance(data, dict) and data.get("error"):
        code = data.get("code")
        message = data.get("message") or "unknown error"
        if code in AUTH_ERROR_CODES:
            raise AuthError(f"auth error {code}: {message}")
        if code in TRANSIENT_ERROR_CODES or any(m in message.lower() for m in TRANSIENT_MESSAGES):
            raise TransientError(f"API error {code}: {message}")
        raise IcedriveError(f"API error {code}: {message}")
    return data


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
                 timeout: int = CONTROL_TIMEOUT, upload_timeout: int = UPLOAD_TIMEOUT,
                 retries: int = 3, device_id: str | None = None,
                 chunk_size: int = DEFAULT_CHUNK_SIZE, log=print):
        self.email = email
        self.password = password
        self.verbose = verbose
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.retries = retries
        # The official clients identify themselves with a stable device id and an
        # X-App-Method header; matching that seems prudent when the API throttles.
        self.device_id = device_id
        self.chunk_size = chunk_size
        self.log = log
        self.token: str | None = None
        self.account: dict | None = None
        self._context = ssl.create_default_context()
        self._endpoints: list[str] = []
        self._endpoints_at = 0.0
        self._connections: dict[str, http.client.HTTPSConnection] = {}

    # --- transport -------------------------------------------------------
    def _identity(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "X-App-Method": "sync"}
        if self.device_id:
            headers["X-Icedrive-Device-Id"] = self.device_id
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _request(self, url, body=None, content_type=None, method="GET", auth=True):
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in self._identity().items():
            if key == "Authorization" and not auth:
                continue
            request.add_header(key, value)
        if content_type:
            request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, context=self._context, timeout=self.timeout) as response:
            raw = response.read()
        return check_payload(json.loads(raw)) if raw.lstrip().startswith(b"{") else raw

    def _retry(self, operation, auth: bool):
        """Run operation, retrying 5xx/429/network errors; re-login once on 401/403.

        If the last failure was a retryable one the error is reported as
        TransientError, so callers can tell "Icedrive is down" (retry later, exit 3)
        from "your request is wrong" (exit 1).
        """
        last = None
        transient = False
        for attempt in range(self.retries + 1):
            try:
                return operation()
            except TransientError as exc:
                last, transient = exc, True
                if attempt < self.retries:
                    time.sleep(30 * (attempt + 1))          # slow, deliberately
            except AuthError as exc:
                last = exc
                if auth:
                    self.log(f"[{now()}] auth error ({exc}); logging in again")
                    self.login()
                    continue
                raise
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code} {exc.reason}"
                transient = exc.code in RETRY_STATUS
                if exc.code in (401, 403) and auth:
                    self.log(f"[{now()}] auth error {exc.code}; logging in again")
                    self.login()
                    continue
                if exc.code not in RETRY_STATUS:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last, transient = exc, True
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        message = f"request failed after {self.retries + 1} attempts: {last}"
        raise TransientError(message) if transient else IcedriveError(message)

    def call(self, path, body=None, content_type=None, method="GET", auth=True):
        return self._retry(lambda: self._request(API + path, body, content_type, method, auth), auth)

    # --- auth ------------------------------------------------------------
    _token_path: str | None = None

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
        self.account = result.get("auth_data")
        self._endpoints, self._endpoints_at = [], 0.0
        if self._token_path:
            try:
                self.save_token(self._token_path)
            except OSError as exc:
                self.log(f"warning: could not cache token: {exc}")
        if self.verbose:
            auth = result.get("auth_data", {})
            self.log(f"[{now()}] logged in as {auth.get('email')} "
                     f"(plan {auth.get('plan')}, id {auth.get('id')})")
        return result

    def probe(self) -> dict:
        """Single-attempt health check: raises TransientError while the service is
        unavailable, AuthError if the account is not usable, returns stats if fine.
        Deliberately does not retry - the caller decides how long to wait."""
        try:
            result = self._request(API + "/user-stats")
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS:
                raise TransientError(f"HTTP {exc.code} {exc.reason}") from exc
            if exc.code in (401, 403):
                raise AuthError(f"HTTP {exc.code} {exc.reason}") from exc
            raise IcedriveError(f"HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            raise TransientError(str(exc)) from exc
        return check_payload(result)

    def user_stats(self) -> dict:
        """Storage/bandwidth usage: {storage: {used, max, free, pcent}, bandwidth: {...}}."""
        return self.call("/user-stats")

    # --- token cache -----------------------------------------------------
    def save_token(self, path: str) -> None:
        """Cache the bearer token plus the account info the login returned, so a
        scheduled run rarely needs to log in (login is the 2FA-protected step)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # create with 0600 directly: never a world-readable window
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"token": self.token, "account": self.account}, handle)

    def load_token(self, path: str, validate=None) -> bool:
        """validate: callable that raises if the token is not usable (defaults to
        a user-stats call, injected in tests to avoid network access)."""
        if not os.path.exists(path):
            return False
        with open(path) as handle:
            raw = handle.read().strip()
        if not raw:
            return False
        account = None
        try:
            cached = json.loads(raw)
            token, account = cached.get("token"), cached.get("account")
        except json.JSONDecodeError:                            # older bare-token cache
            token = raw
        if not token:
            return False
        self.token, self.account = token, account
        validate = validate or self.probe                        # single attempt, no retry storm
        try:
            validate()
        except TransientError:                                   # service down: nothing can run
            self.token, self.account = None, None
            raise
        except Exception:                                        # noqa: BLE001 - stale token: log in
            self.token, self.account = None, None
            return False
        if self.verbose:
            self.log(f"[{now()}] reusing cached bearer token")
        return True

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

    # --- connection reuse ------------------------------------------------
    def _connection(self, netloc: str) -> http.client.HTTPSConnection:
        """Keep-alive connection per storage node: a fresh TLS handshake per file
        is what makes many-small-file mirrors slow (measured ~0.5 files/s)."""
        conn = self._connections.get(netloc)
        if conn is not None and conn.sock is not None:
            return conn
        conn = http.client.HTTPSConnection(netloc, timeout=self.upload_timeout, context=self._context)
        self._connections[netloc] = conn
        return conn

    def _drop_connection(self, netloc: str) -> None:
        conn = self._connections.pop(netloc, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:                                   # noqa: BLE001
                pass

    def upload(self, folder_id: int, path: str) -> dict:
        """Upload a file, chunked when it is larger than chunk_size."""
        stat = os.stat(path)
        if stat.st_size == 0:
            raise IcedriveError(f"refusing to upload empty file {path}")
        if self.chunk_size and stat.st_size > self.chunk_size:
            return self._upload_chunked(folder_id, path, stat)
        return self._upload_single(folder_id, path, stat)

    def _upload_body(self, folder_id: int, path: str, stat, upload_id=None, size=None,
                     offset: int = 0):
        """Build the multipart request for a whole file or one ranged chunk."""
        size = size if size is not None else stat.st_size
        name = os.path.basename(path).replace("\\", "\\\\").replace('"', '\\"')
        boundary = "----geckoformboundary" + uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="folderId"\r\n\r\n{folder_id}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="moddate"\r\n\r\n{int(stat.st_mtime)}\r\n',
        ]
        if upload_id:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="unique_upload_id"'
                         f'\r\n\r\n{upload_id}\r\n')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files[]"; filename="{name}"'
                     f'\r\nContent-Type: application/octet-stream\r\n\r\n')
        preamble = "".join(parts).encode()
        trailer = f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Accept-Encoding": "identity", "Connection": "keep-alive"}
        if upload_id:
            headers["Content-Range"] = f"bytes {offset}-{offset + size - 1}/{stat.st_size}"
        length = len(preamble) + size + len(trailer)
        return preamble, trailer, length, headers

    def _send(self, path: str, preamble: bytes, trailer: bytes, length: int, headers: dict,
              offset: int, size: int):
        """POST one request, streaming [offset, offset+size) of the file."""
        last = None
        for attempt in range(self.retries + 1):
            for endpoint in self.upload_endpoints():
                parsed = urllib.parse.urlsplit(endpoint)
                target = parsed.path + ("?" + parsed.query if parsed.query else "")
                conn = None
                try:
                    conn = self._connection(parsed.netloc)
                    conn.putrequest("POST", target)
                    for key, value in self._identity().items():
                        conn.putheader(key, value)
                    for key, value in headers.items():
                        conn.putheader(key, value)
                    conn.putheader("Content-Length", str(length))
                    conn.endheaders()
                    conn.send(preamble)
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        remaining = size
                        while remaining > 0:
                            block = handle.read(min(STREAM_CHUNK, remaining))
                            if not block:
                                break
                            conn.send(block)
                            remaining -= len(block)
                    conn.send(trailer)
                    response = conn.getresponse()
                    raw = response.read()
                    if response.status >= 400:
                        last = f"HTTP {response.status}"
                        self._drop_connection(parsed.netloc)
                        continue
                    result = json.loads(raw) if raw.lstrip().startswith(b"{") else {}
                    if result.get("error"):
                        last = json.dumps(result)[:200]
                        self._drop_connection(parsed.netloc)
                        continue
                    return result
                except Exception as exc:                        # noqa: BLE001 - try the next mirror
                    last = exc
                    self._drop_connection(parsed.netloc)
                    continue
            if attempt < self.retries:
                time.sleep(2 ** attempt)
                self._endpoints, self._endpoints_at = [], 0.0
        raise IcedriveError(f"upload failed for {path}: {last}")

    def _upload_single(self, folder_id: int, path: str, stat) -> dict:
        if self.verbose:
            self.log(f"  uploading {os.path.basename(path)} ({stat.st_size} bytes)")
        preamble, trailer, length, headers = self._upload_body(folder_id, path, stat)
        return self._send(path, preamble, trailer, length, headers, 0, stat.st_size)

    def _upload_chunked(self, folder_id: int, path: str, stat) -> dict:
        upload_id = upload_id_for(folder_id, path, stat.st_size, int(stat.st_mtime))
        chunk = self.chunk_size
        if self.verbose:
            self.log(f"  uploading {os.path.basename(path)} ({stat.st_size} bytes) in "
                     f"{-(-stat.st_size // chunk)} chunks, id {upload_id[:8]}")
        result = {}
        for offset, size in chunk_ranges(stat.st_size, chunk):
            preamble, trailer, length, headers = self._upload_body(
                folder_id, path, stat, upload_id=upload_id, size=size, offset=offset)
            result = self._send(path, preamble, trailer, length, headers, offset, size)
        if result.get("message") != "Upload Successful":
            raise IcedriveError(f"chunked upload did not complete for {path}: {result}")
        return result

    # --- download / delete ------------------------------------------------
    # Wire formats verified against the live account (round-trip with sha256
    # match) on 2026-09-24: POST /download-multi {items: file-<id>, crypto: 0}
    # -> {urls: [{url}]} (url may be relative to https://apis.icedrive.net), and
    # POST /erase {items: file-<id>} deletes a file. Folder erase silently
    # does nothing, so only files are ever passed to /erase.

    def download_url(self, file_id: int) -> str:
        """Signed URL for one file, from the batch endpoint the desktop app uses."""
        body, content_type = _urlencode({"items": f"file-{file_id}", "crypto": "0"})
        result = self.call("/download-multi", body, content_type, "POST")
        urls = (result or {}).get("urls") or []
        if not urls or not urls[0].get("url"):
            raise IcedriveError(f"no download url for file {file_id}: {json.dumps(result)[:200]}")
        url = urls[0]["url"]
        return url if url.startswith("https://") else "https://apis.icedrive.net" + url

    def download(self, file_id: int, dest: str) -> int:
        """Stream one file to dest (via .tmp + rename). Returns bytes written.
        ponytail: no Range resume - a retry restarts the file. The official
        client chunks downloads; add resume if long-haul failures hurt."""
        last = None
        for attempt in range(self.retries + 1):
            tmp = dest + ".tmp"
            try:
                request = urllib.request.Request(self.download_url(file_id), method="GET")
                for key, value in self._identity().items():
                    if key != "Authorization":            # the URL itself carries the signature
                        request.add_header(key, value)
                with urllib.request.urlopen(request, context=self._context, timeout=self.timeout) as response:
                    with open(tmp, "wb") as handle:
                        while True:
                            block = response.read(STREAM_CHUNK)
                            if not block:
                                break
                            handle.write(block)
                os.replace(tmp, dest)
                return os.stat(dest).st_size
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in RETRY_STATUS:
                    raise IcedriveError(f"download failed for {dest}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise IcedriveError(f"download failed for {dest}: {last}")

    def delete_file(self, file_id: int) -> None:
        """Delete one remote file. Folders cannot be deleted through this API."""
        body, content_type = _urlencode({"items": f"file-{file_id}"})
        self.call("/erase", body, content_type, "POST")


