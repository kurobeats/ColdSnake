"""Command line interface: coldsnake {login,account,ls,trash,restore,versions,check,mirror,download}."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib

from . import __version__
from .client import AuthError, Client, IcedriveError, TransientError
from .sync import Mirror, PreflightError, preflight

# --json moves the human log lines off stdout so a monitor reading stdout sees
# exactly one document; reset on every main() call so runs cannot leak into each other.
_LOG_ON_STDERR = False


def log(message: str) -> None:
    """Unbuffered logging: under systemd/journald a block-buffered stdout hides
    progress until the process exits, which makes a stalled run look dead."""
    print(message, file=sys.stderr if _LOG_ON_STDERR else sys.stdout, flush=True)


class Report:
    """The --json / --report run summary.

    Assembled during the run and written once on the way out of main(), so even
    an abort (exit 2/3) emits one complete object instead of a half document.
    """

    def __init__(self, command: str, to_stdout: bool, path: str | None):
        self.command = command
        self.to_stdout = to_stdout
        self.path = path
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._start = time.time()
        self.ok = True
        self.failures = 0
        self.error: str | None = None
        self.storage: dict | None = None
        self.mirrors: list[dict] = []

    def document(self) -> dict:
        doc = {"command": self.command, "version": __version__, "started": self.started,
               "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "duration_s": round(time.time() - self._start, 1),
               "ok": self.ok, "failures": self.failures}
        if self.command == "mirror":
            doc["mirrors"] = self.mirrors
        if self.storage is not None:
            doc["storage"] = self.storage
        if self.error is not None:
            doc["error"] = self.error
        return doc

    def failed(self, error: str) -> None:
        self.ok = False
        self.error = error

    def emit(self) -> None:
        text = json.dumps(self.document())
        if self.to_stdout:
            print(text, flush=True)
        if self.path:
            # A bad --report path must not turn a finished run into a crash: the
            # run's own exit code is the signal, the report is a convenience.
            try:
                with open(self.path, "w") as handle:
                    handle.write(text + "\n")
                os.chmod(self.path, 0o644)
            except OSError as exc:                      # noqa: BLE001
                log(f"warning: could not write report {self.path}: {exc}")


def mirror_document(local: str, remote: str, stats) -> dict:
    """One mirrors[] entry; failure paths/errors are truncated as the logs truncate them."""
    return {"local": local, "remote": remote, "uploaded": stats.uploaded,
            "unchanged": stats.unchanged, "bytes": stats.bytes, "verified": stats.verified,
            "trashed": stats.trashed, "deleted": stats.deleted, "failed": stats.failed(),
            "failures": [{"path": path, "error": str(error)[:200]}
                         for path, error in stats.failures]}


DEFAULT_CONFIG = os.path.expanduser("~/.config/coldsnake/config.toml")
TOKEN_CACHE = os.path.expanduser("~/.config/coldsnake/token")
LEGACY_CREDS = os.path.expanduser("~/.config/icedrive/credentials")


def load_config(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PreflightError(f"bad config {path}: {exc}") from exc


def resolve_device_id(config: dict) -> str:
    """Stable per-install id, as the official clients send (ICEDRIVE_DEVICE_ID overrides)."""
    env = os.environ.get("ICEDRIVE_DEVICE_ID")
    if env:
        return env
    path = os.path.join(os.path.dirname(DEFAULT_CONFIG), "device-id")
    if os.path.exists(path):
        with open(path) as handle:
            value = handle.read().strip()
            if value:
                return value
    import uuid
    value = str(uuid.uuid4())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)
    return value


def resolve_credentials(config: dict) -> tuple[str, str]:
    """env vars -> config [auth] -> legacy credentials file."""
    email = os.environ.get("ICEDRIVE_EMAIL")
    password = os.environ.get("ICEDRIVE_PASSWORD")
    auth = config.get("auth", {})
    email = email or auth.get("email")
    password = password or auth.get("password")
    if not (email and password) and os.path.exists(LEGACY_CREDS):
        with open(LEGACY_CREDS) as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("ICEDRIVE_EMAIL="):
                    email = email or line.split("=", 1)[1]
                elif line.startswith("ICEDRIVE_PASSWORD="):
                    password = password or line.split("=", 1)[1]
    if not email or not password:
        raise PreflightError(
            "no credentials: set ICEDRIVE_EMAIL / ICEDRIVE_PASSWORD, add an [auth] "
            f"section to {DEFAULT_CONFIG}, or create {LEGACY_CREDS} (0600)")
    return email, password


def build_client(args, config, use_cache: bool = True, journal=None) -> Client:
    """Cached bearer token when possible, otherwise proof-of-work login."""
    email, password = resolve_credentials(config)
    client = Client(email, password, verbose=args.verbose,
                    device_id=resolve_device_id(config), log=log, journal=journal)
    if use_cache and not getattr(args, "no_token_cache", False):
        client._token_path = TOKEN_CACHE
        if client.load_token(TOKEN_CACHE):
            return client
    client.login()
    return client


def resolve_pairs(args, config: dict) -> list[tuple[str, str]]:
    """Pairs from --local/--remote, else from the config's [[mirror]] entries."""
    if getattr(args, "local", None):
        if not args.remote:
            raise PreflightError("--local requires --remote")
        return [(args.local, args.remote)]
    pairs = parse_pairs(config)
    if not pairs:
        raise PreflightError(
            f"no mirrors: pass --local/--remote or add [[mirror]] entries to {args.config}")
    return pairs


def format_date(entry: dict) -> str:
    """Listing/version entries carry a preformatted date, else a unix moddate."""
    date = entry.get("date")
    if date:
        return str(date)
    stamp = entry.get("moddate") or entry.get("timestamp")
    try:
        return time.strftime("%F %T", time.localtime(int(stamp))) if stamp else "-"
    except (TypeError, ValueError, OverflowError, OSError):  # not our data: print it
        return str(stamp)


def find_remote_root(client, remote: str) -> int | None:
    """Id of the mirror root folder at the drive root, or None."""
    return next((e["id"] for e in client.listing(0)
                 if e.get("filename") == remote and e.get("isFolder")), None)


def find_file(client, root_id: int, relpath: str) -> dict | None:
    """Resolve root_id/relpath to its file entry, or None if any part is missing."""
    folder_id, rel = root_id, relpath
    while True:
        head, sep, rel = rel.partition("/")
        if not sep:                                       # head is the file name
            return next((e for e in client.listing(folder_id)
                         if not e.get("isFolder") and e.get("filename") == head), None)
        sub = next((e["id"] for e in client.listing(folder_id)
                    if e.get("isFolder") and e.get("filename") == head), None)
        if sub is None:
            return None
        folder_id = sub


def parse_pairs(config: dict) -> list[tuple[str, str]]:
    """Return (local, remote) pairs from config [[mirror]] entries."""
    pairs = []
    for entry in config.get("mirror", []):
        local, remote = entry.get("local"), entry.get("remote")
        if not local or not remote:
            raise PreflightError("each [[mirror]] entry needs 'local' and 'remote'")
        pairs.append((local, remote))
    return pairs


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="log per-file decisions")
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help=f"config file (default {DEFAULT_CONFIG})")
    common.add_argument("--no-token-cache", action="store_true", default=argparse.SUPPRESS,
                        help="always log in (ignore the cached token)")
    parser = argparse.ArgumentParser(prog="coldsnake", description="Icedrive client (no WebDAV)", parents=[common])
    parser.add_argument("--version", action="version", version=f"coldsnake {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", parents=[common], help="verify credentials and print account info")
    sub.add_parser("account", parents=[common], help="show plan, storage quota and bandwidth usage")
    sub.add_parser("ls", parents=[common], help="list a remote folder").add_argument(
        "--folder-id", type=int, default=0)
    sub.add_parser("trash", parents=[common], help="list trashed items (id, name, size, date)")
    restore = sub.add_parser("restore", parents=[common], help="restore trashed items by id")
    restore.add_argument("--id", type=int, action="append", default=[], required=True,
                         metavar="ID", help="trashed item id (repeatable)")
    restore.add_argument("--folder", action="store_true", help="the ids are folder ids")
    versions = sub.add_parser("versions", parents=[common], help="list a file's version history")
    versions.add_argument("--remote", required=True,
                          help="remote folder name (as in [[mirror]] remote)")
    versions.add_argument("--file", required=True, metavar="RELPATH",
                          help="relative path under the mirror root")
    check = sub.add_parser("check", parents=[common],
                           help="pre-flight only: service health + mirror sources (no uploads)")
    check.add_argument("--local", help="local directory to check (with --remote)")
    check.add_argument("--remote", help="remote folder name (with --local)")
    check.add_argument("--allow-empty", action="store_true", help="tolerate empty sources")
    check.add_argument("--json", action="store_true",
                       help="emit one JSON summary on stdout (progress goes to stderr)")
    check.add_argument("--report", metavar="PATH", help="also write the JSON summary to PATH")

    mirror = sub.add_parser("mirror", parents=[common], help="upload-only mirror local dirs into Icedrive")
    mirror.add_argument("--local", help="local directory (with --remote)")
    mirror.add_argument("--remote", help="remote folder name (with --local)")
    mirror.add_argument("--dry-run", action="store_true", help="report what would upload, change nothing")
    mirror.add_argument("--allow-empty", action="store_true",
                        help="do not refuse when a source directory is empty")
    mirror.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip files/dirs matching GLOB (repeatable)")
    mirror.add_argument("--prune", action="store_true",
                        help="delete remote files that no longer exist locally")
    mirror.add_argument("--prune-force", action="store_true",
                        help="allow prune to exceed the sanity threshold")
    mirror.add_argument("--prune-delete", action="store_true",
                        help="permanently delete pruned files instead of trashing them")
    mirror.add_argument("--no-upload-journal", action="store_true",
                        help="do not resume a partial upload from a previous run")
    mirror.add_argument("--json", action="store_true",
                        help="emit one JSON summary on stdout (progress goes to stderr)")
    mirror.add_argument("--report", metavar="PATH", help="also write the JSON summary to PATH")

    dl = sub.add_parser("download", parents=[common], help="download from Icedrive to a local directory")
    dl.add_argument("--remote", required=True, help="remote folder name (as in [[mirror]] remote)")
    dl.add_argument("--local", required=True, help="destination directory")
    dl.add_argument("--file", action="append", default=[], metavar="RELPATH",
                    help="relative path under the mirror root (repeatable; default: whole tree)")
    dl.add_argument("--version", type=int, metavar="N",
                    help="download version index N of --file (needs exactly one --file)")

    args = parser.parse_args(argv)
    # argparse subparsers overwrite main-parser values with their own defaults,
    # so the shared flags parse with SUPPRESS and are filled in here - this is
    # what makes them work before or after the sub-command.
    args.verbose = getattr(args, "verbose", False)
    args.config = getattr(args, "config", DEFAULT_CONFIG)
    args.no_token_cache = getattr(args, "no_token_cache", False)

    # --json / --report only exist on mirror and check; absent everywhere else.
    global _LOG_ON_STDERR
    json_flag = getattr(args, "json", False)
    report_path = getattr(args, "report", None)
    report = (Report(args.command, to_stdout=json_flag, path=report_path)
              if args.command in ("mirror", "check") and (json_flag or report_path) else None)
    _LOG_ON_STDERR = bool(json_flag)

    try:
        config = load_config(args.config)
        if args.command == "login":
            client = build_client(args, config)
            log("login ok")
            return 0

        if args.command == "account":
            client = build_client(args, config)
            stats = client.user_stats()
            storage, bandwidth = stats.get("storage", {}), stats.get("bandwidth", {})
            user = client.account or {}
            log(f"account : {user.get('email')} ({user.get('plan')})")
            log(f"storage : {storage.get('used_human')} of {storage.get('max_human')} used, "
                f"{storage.get('free_human')} free ({storage.get('pcent')}%)")
            log(f"bandwidth: {bandwidth.get('used_human')} of {bandwidth.get('max_human')} used")
            return 0

        if args.command == "check":
            pairs = resolve_pairs(args, config)
            client = build_client(args, config)
            info = preflight(client, pairs, allow_empty=args.allow_empty)
            storage = info["storage"]
            user = client.account or {}
            log(f"service : ok ({user.get('email')}, plan {user.get('plan')})")
            log(f"storage : {storage.get('used_human')} of {storage.get('max_human')} used, "
                f"{storage.get('free_human')} free")
            for local, remote in pairs:
                entries = len(os.listdir(os.path.expanduser(local)))
                log(f"source  : {local} -> {remote} ({entries} entries)")
            log("pre-flight ok")
            if report is not None:
                report.storage = storage
            return 0

        if args.command == "ls":
            client = build_client(args, config)
            for entry in client.listing(args.folder_id):
                kind = "DIR " if entry.get("isFolder") else "    "
                size = "" if entry.get("isFolder") else f"{entry.get('filesize', 0):>12}"
                print(f"{kind}{size}  {entry['filename']}")
            return 0

        if args.command == "trash":
            client = build_client(args, config)
            for entry in client.trash_listing():
                size = "" if entry.get("isFolder") else str(int(entry.get("filesize") or 0))
                print(f"{entry.get('id'):>10}  {format_date(entry):<19}  {size:>12}  {entry['filename']}")
            return 0

        if args.command == "restore":
            client = build_client(args, config)
            failures = 0
            for item_id in args.id:
                try:
                    client.restore(item_id, is_folder=args.folder)
                    log(f"restored {item_id}")
                except (TransientError, AuthError):
                    raise                                   # outage/credentials: not one item's fault
                except Exception as exc:                        # noqa: BLE001 - keep restoring the rest
                    failures += 1
                    log(f"FAILED restore {item_id}: {str(exc)[:200]}")
            return 1 if failures else 0

        if args.command == "versions":
            client = build_client(args, config)
            root = find_remote_root(client, args.remote)
            if root is None:
                raise PreflightError(f"remote folder {args.remote!r} not found at the drive root")
            entry = find_file(client, root, args.file)
            if entry is None:
                raise PreflightError(f"{args.file!r} not found under {args.remote}")
            for version in client.versions(entry["id"]):
                current = "current" if version.get("current") else ""
                print(f"{version.get('index'):>4}  {format_date(version):<19}  "
                      f"{int(version.get('filesize') or 0):>12}  {current}")
            return 0

        if args.command == "download":
            if not args.remote:
                raise PreflightError("download needs --remote (the mirror root folder name)")
            if not args.file and not os.path.isdir(os.path.expanduser(args.local)):
                raise PreflightError(f"destination does not exist: {args.local}")
            client = build_client(args, config)
            root = find_remote_root(client, args.remote)
            if root is None:
                raise PreflightError(f"remote folder {args.remote!r} not found at the drive root")
            dest_root = os.path.expanduser(args.local)

            if args.version is not None:
                if len(args.file) != 1:
                    raise PreflightError("--version needs exactly one --file")
                entry = find_file(client, root, args.file[0])
                if entry is None:
                    raise PreflightError(f"{args.file[0]!r} not found under {args.remote}")
                chosen = next((v for v in client.versions(entry["id"])
                               if v.get("index") == args.version), None)
                if chosen is None:                             # absent index: a failure, not a crash
                    log(f"FAILED {args.file[0]}: no version {args.version}")
                    return 1
                size = client.download_from_url(chosen["url"], os.path.join(dest_root, args.file[0]),
                                                size=int(chosen.get("filesize") or 0) or None)
                log(f"downloaded {args.file[0]} version {args.version} ({size} bytes)")
                return 0

            wanted = set(args.file)
            failures = 0

            # flat, explicit walk: download every file under the mirror root
            stack = [(root, "")]
            found: set[str] = set()
            while stack:
                folder_id, rel = stack.pop()
                for entry in client.listing(folder_id):
                    name, child_rel = entry["filename"], (os.path.join(rel, entry["filename"]) if rel else entry["filename"])
                    if entry.get("isFolder"):
                        stack.append((entry["id"], child_rel))
                        if not wanted:
                            os.makedirs(os.path.join(dest_root, child_rel), exist_ok=True)
                        continue
                    if wanted and child_rel not in wanted:
                        continue
                    found.add(child_rel)
                    try:
                        size = client.download(entry["id"], os.path.join(dest_root, child_rel),
                                               size=int(entry.get("filesize") or 0) or None)
                        log(f"downloaded {child_rel} ({size} bytes)")
                    except Exception as exc:                    # noqa: BLE001 - isolate per file
                        failures += 1
                        log(f"FAILED {child_rel}: {str(exc)[:200]}")
            if wanted:
                for missing in sorted(wanted - found):
                    failures += 1
                    log(f"FAILED {missing}: not found under {args.remote}")
            log(f"done: {len(found)} downloaded, {failures} failure(s)")
            return 1 if failures else 0

        # mirror
        pairs = resolve_pairs(args, config)
        journal = None
        if not args.no_upload_journal:
            from .state import UploadJournal               # lazy: the CLI must not need state to import
            journal = UploadJournal(os.path.join(os.path.dirname(DEFAULT_CONFIG), "upload-journal.json"))
        client = build_client(args, config, journal=journal)
        excludes = list(args.exclude) + list(config.get("exclude", []))

        # Nothing long runs until the service answers and the sources are sane.
        info = preflight(client, pairs, allow_empty=args.allow_empty)
        storage = info["storage"]
        log(f"pre-flight ok: service up, {storage.get('free_human')} free on the account")

        def quota_check(needed: int) -> None:
            storage = client.user_stats().get("storage", {})
            free = int(storage.get("free", 0))
            log(f"storage : {storage.get('used_human')} of {storage.get('max_human')} used, "
                f"{storage.get('free_human')} free; this run needs {needed / 1e6:.1f} MB")
            if needed > free * 0.99:
                raise RuntimeError(
                    f"not enough space: {needed / 1e9:.1f} GB needed, "
                    f"{free / 1e9:.1f} GB free")

        failures = 0
        for local, remote in pairs:
            mirrorer = Mirror(client, local, remote, dry_run=args.dry_run, verbose=args.verbose,
                              log=log, quota_check=quota_check, excludes=excludes,
                              prune=args.prune, prune_force=args.prune_force,
                              prune_delete=args.prune_delete)
            try:
                stats = mirrorer.run()
            except (TransientError, AuthError):
                # A run that aborted on an outage must exit 3 (2 for credentials),
                # not be counted as file failures the way a bad file is.
                raise
            except Exception as exc:                            # noqa: BLE001 - one dir must not stop the rest
                log(f"{remote}: ABORTED: {str(exc)[:200]}")
                failures += 1
                if report is not None:
                    report.mirrors.append({"local": local, "remote": remote, "uploaded": 0,
                                           "unchanged": 0, "bytes": 0, "verified": 0,
                                           "trashed": 0, "deleted": 0, "failed": 1,
                                           "failures": [{"path": remote, "error": str(exc)[:200]}]})
                continue
            log(f"{remote}: uploaded {stats.uploaded} ({stats.bytes / 1e6:.1f} MB), "
                  f"unchanged {stats.unchanged}, verified {stats.verified}, "
                  f"trashed {stats.trashed}, deleted {stats.deleted}, failed {stats.failed()}")
            failures += stats.failed()
            if report is not None:
                report.mirrors.append(mirror_document(local, remote, stats))
        log(f"done: {failures} failure(s)")
        if report is not None:
            report.failures = failures
            report.ok = failures == 0
        return 1 if failures else 0

    except AuthError as exc:
        log(f"authentication failed: {exc}")
        if report is not None:
            report.failed(str(exc))
        return 2
    except TransientError as exc:
        log(f"service unavailable, run aborted: {exc}")
        if report is not None:
            report.failed(str(exc))
        return 3
    except PreflightError as exc:
        log(f"pre-flight refused to start: {exc}")
        if report is not None:
            report.failed(str(exc))
        return 2
    except IcedriveError as exc:
        log(f"error: {exc}")
        if report is not None:
            report.failed(str(exc))
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        if report is not None:
            report.failed("interrupted")
        return 130
    except Exception as exc:                    # noqa: BLE001 - a CLI never tracebacks at the user
        # Anything unforeseen (a permission error reading the config, a bug) is still
        # a failure: log it with its type, mark the report failed, exit 1. Otherwise
        # the report would claim ok:true while the process died.
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        log(f"error: {detail}")
        if report is not None:
            report.failed(detail)
        return 1
    finally:
        # One emit point: an abort on any path above still writes the whole document.
        if report is not None:
            report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
