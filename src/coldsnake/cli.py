"""Command line interface: coldsnake {login,ls,mirror}."""
from __future__ import annotations

import argparse
import os
import sys
import tomllib

from . import __version__
from .client import AuthError, Client, IcedriveError
from .sync import Mirror

DEFAULT_CONFIG = os.path.expanduser("~/.config/coldsnake/config.toml")
LEGACY_CREDS = os.path.expanduser("~/.config/icedrive/credentials")


def load_config(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "rb") as handle:
        return tomllib.load(handle)


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


def build_client(args, config) -> Client:
    email, password = resolve_credentials(config)
    client = Client(email, password, verbose=args.verbose)
    client.login()
    return client


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
    parser = argparse.ArgumentParser(prog="coldsnake", description="Icedrive client (no WebDAV)")
    parser.add_argument("--version", action="version", version=f"coldsnake {__version__}")
    parser.add_argument("--verbose", action="store_true", help="log per-file decisions")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="verify credentials and print account info")
    sub.add_parser("ls", help="list a remote folder").add_argument("--folder-id", type=int, default=0)

    mirror = sub.add_parser("mirror", help="upload-only mirror local dirs into Icedrive")
    mirror.add_argument("--local", help="local directory (with --remote)")
    mirror.add_argument("--remote", help="remote folder name (with --local)")
    mirror.add_argument("--dry-run", action="store_true", help="report what would upload, change nothing")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
        if args.command == "login":
            client = build_client(args, config)
            print("login ok")
            return 0

        if args.command == "ls":
            client = build_client(args, config)
            for entry in client.listing(args.folder_id):
                kind = "DIR " if entry.get("isFolder") else "    "
                size = "" if entry.get("isFolder") else f"{entry.get('filesize', 0):>12}"
                print(f"{kind}{size}  {entry['filename']}")
            return 0

        # mirror
        if args.local:
            if not args.remote:
                sys.exit("--local requires --remote")
            pairs = [(args.local, args.remote)]
        else:
            pairs = parse_pairs(config)
            if not pairs:
                sys.exit(f"no mirrors: pass --local/--remote or add [[mirror]] entries to {args.config}")

        client = build_client(args, config)
        failures = 0
        for local, remote in pairs:
            mirrorer = Mirror(client, local, remote, dry_run=args.dry_run, verbose=args.verbose)
            try:
                stats = mirrorer.run()
            except Exception as exc:                            # noqa: BLE001 - one dir must not stop the rest
                print(f"{remote}: ABORTED: {str(exc)[:200]}")
                failures += 1
                continue
            print(f"{remote}: uploaded {stats.uploaded} ({stats.bytes / 1e6:.1f} MB), "
                  f"unchanged {stats.unchanged}, verified {stats.verified}, failed {stats.failed()}")
            failures += stats.failed()
        print(f"done: {failures} failure(s)")
        return 1 if failures else 0

    except AuthError as exc:
        print(f"authentication failed: {exc}")
        return 2
    except IcedriveError as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
