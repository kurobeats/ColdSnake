"""Command line interface: coldsnake {login,ls,mirror}."""
from __future__ import annotations

import argparse
import os
import sys
import tomllib

from . import __version__
from .client import AuthError, Client, IcedriveError, TransientError
from .sync import Mirror, PreflightError, preflight

def log(message: str) -> None:
    """Unbuffered logging: under systemd/journald a block-buffered stdout hides
    progress until the process exits, which makes a stalled run look dead."""
    print(message, flush=True)


DEFAULT_CONFIG = os.path.expanduser("~/.config/coldsnake/config.toml")
TOKEN_CACHE = os.path.expanduser("~/.config/coldsnake/token")
LEGACY_CREDS = os.path.expanduser("~/.config/icedrive/credentials")


def load_config(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "rb") as handle:
        return tomllib.load(handle)


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
        sys.exit(
            "no credentials: set ICEDRIVE_EMAIL / ICEDRIVE_PASSWORD, add an [auth] "
            f"section to {DEFAULT_CONFIG}, or create {LEGACY_CREDS} (0600)")
    return email, password


def build_client(args, config, use_cache: bool = True) -> Client:
    """Cached bearer token when possible, otherwise proof-of-work login."""
    email, password = resolve_credentials(config)
    client = Client(email, password, verbose=args.verbose,
                    device_id=resolve_device_id(config), log=log)
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
            sys.exit("--local requires --remote")
        return [(args.local, args.remote)]
    pairs = parse_pairs(config)
    if not pairs:
        sys.exit(f"no mirrors: pass --local/--remote or add [[mirror]] entries to {args.config}")
    return pairs


def parse_pairs(config: dict) -> list[tuple[str, str]]:
    """Return (local, remote) pairs from config [[mirror]] entries."""
    pairs = []
    for entry in config.get("mirror", []):
        local, remote = entry.get("local"), entry.get("remote")
        if not local or not remote:
            sys.exit("each [[mirror]] entry needs 'local' and 'remote'")
        pairs.append((local, remote))
    return pairs


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", help="log per-file decisions")
    common.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")

    parser = argparse.ArgumentParser(prog="coldsnake", description="Icedrive client (no WebDAV)", parents=[common])
    parser.add_argument("--version", action="version", version=f"coldsnake {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", parents=[common], help="verify credentials and print account info")
    sub.add_parser("account", parents=[common], help="show plan, storage quota and bandwidth usage")
    sub.add_parser("ls", parents=[common], help="list a remote folder").add_argument(
        "--folder-id", type=int, default=0)
    check = sub.add_parser("check", parents=[common],
                           help="pre-flight only: service health + mirror sources (no uploads)")
    check.add_argument("--local", help="local directory to check (with --remote)")
    check.add_argument("--remote", help="remote folder name (with --local)")
    check.add_argument("--allow-empty", action="store_true", help="tolerate empty sources")

    mirror = sub.add_parser("mirror", parents=[common], help="upload-only mirror local dirs into Icedrive")
    mirror.add_argument("--local", help="local directory (with --remote)")
    mirror.add_argument("--remote", help="remote folder name (with --local)")
    mirror.add_argument("--dry-run", action="store_true", help="report what would upload, change nothing")
    mirror.add_argument("--allow-empty", action="store_true",
                        help="do not refuse when a source directory is empty")
    parser.add_argument("--no-token-cache", action="store_true", help="always log in (ignore the cached token)")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
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
            return 0

        if args.command == "ls":
            client = build_client(args, config)
            for entry in client.listing(args.folder_id):
                kind = "DIR " if entry.get("isFolder") else "    "
                size = "" if entry.get("isFolder") else f"{entry.get('filesize', 0):>12}"
                print(f"{kind}{size}  {entry['filename']}")
            return 0

        # mirror
        pairs = resolve_pairs(args, config)
        client = build_client(args, config)

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
                              log=log, quota_check=quota_check)
            try:
                stats = mirrorer.run()
            except Exception as exc:                            # noqa: BLE001 - one dir must not stop the rest
                log(f"{remote}: ABORTED: {str(exc)[:200]}")
                failures += 1
                continue
            log(f"{remote}: uploaded {stats.uploaded} ({stats.bytes / 1e6:.1f} MB), "
                  f"unchanged {stats.unchanged}, verified {stats.verified}, failed {stats.failed()}")
            failures += stats.failed()
        log(f"done: {failures} failure(s)")
        return 1 if failures else 0

    except AuthError as exc:
        log(f"authentication failed: {exc}")
        return 2
    except TransientError as exc:
        log(f"service unavailable, nothing attempted: {exc}")
        return 3
    except PreflightError as exc:
        log(f"pre-flight refused to start: {exc}")
        return 2
    except IcedriveError as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
