"""Upload-only mirror: local directory -> Icedrive folder.

Safety properties, deliberately:
  * additive - never deletes or truncates anything remote
  * uploads only when size or mtime differ; remote mtimes are preserved by the
    API, so repeat runs are cheap
  * streams file bodies; the process footprint does not depend on file size
  * per-file failure isolation, and a post-upload verification pass that
    re-lists each folder written to and compares sizes
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .client import AuthError, Client, TransientError, name_key

MTIME_TOLERANCE = 2          # seconds; inside this a file counts as unchanged


class PreflightError(RuntimeError):
    """The run must not start (bad config, missing source, nothing to mirror)."""


@dataclass
class Stats:
    uploaded: int = 0
    unchanged: int = 0
    skipped: int = 0
    bytes: int = 0
    verified: int = 0
    trashed: int = 0
    deleted: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def failed(self) -> int:
        return len(self.failures)


def preflight(client, pairs, allow_empty: bool = False):
    """Check everything that would make a long run pointless before starting it.

    * service health - a degraded API is what made a 25h run look like a mystery
      earlier; failing here costs seconds instead of hours of failed uploads
    * sources - a missing, unreadable or empty local directory means the mirror is
      about to do nothing (or, once pruning exists, delete things). Refuse.
    * config sanity - the same local path mirrored twice is a mistake.
    """
    stats = client.probe()                      # TransientError/AuthError propagate
    storage = stats.get("storage", {})

    seen = {}
    for local, remote in pairs:
        if not local or not remote:
            raise PreflightError("mirror needs both a local path and a remote name")
        resolved = os.path.realpath(os.path.expanduser(local))
        if resolved in seen:
            raise PreflightError(f"{local} is mirrored twice (also as {seen[resolved]!r})")
        seen[resolved] = remote
        if not os.path.exists(resolved):
            raise PreflightError(f"source does not exist: {local}")
        if not os.path.isdir(resolved):
            raise PreflightError(f"source is not a directory: {local}")
        if not os.access(resolved, os.R_OK | os.X_OK):
            raise PreflightError(f"source is not readable: {local}")
        if not os.listdir(resolved) and not allow_empty:
            raise PreflightError(
                f"source is empty: {local} (refusing to mirror nothing; "
                f"pass --allow-empty if this is intended)")
    return {"storage": storage}


class Mirror:
    """Mirror one local directory tree into one remote folder."""

    def __init__(self, client: Client, root: str, remote: str,
                 dry_run: bool = False, verbose: bool = False, log=print, quota_check=None,
                 excludes: list[str] | None = None, prune: bool = False,
                 prune_force: bool = False, prune_delete: bool = False):
        self.client = client
        self.root = root
        self.remote = remote
        self.dry_run = dry_run
        self.verbose = verbose
        self.log = log
        self.quota_check = quota_check      # callable(needed_bytes) -> None, may raise
        self.excludes = excludes or []
        self.prune = prune
        self.prune_force = prune_force
        self.prune_delete = prune_delete
        self.stats = Stats()
        self._folder_ids: dict[str, int] = {}
        self._listings: dict[str, list[dict]] = {}
        self._uploaded: dict[str, list[tuple[str, int]]] = {}
        self._local_files: set[str] = set()
        self._local_name_keys: set[str] = set()
        self._walked_dirs: list[str] = []

    def excluded(self, rel: str) -> bool:
        """True when a relative path matches any exclusion glob (rel or basename)."""
        base = os.path.basename(rel)
        return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat) for pat in self.excludes)

    # --- remote tree -----------------------------------------------------
    def entries(self, rel: str) -> list[dict]:
        if rel not in self._listings:
            folder_id = self.folder_id(rel)
            self._listings[rel] = [] if folder_id < 0 else self.client.listing(folder_id)
        return self._listings[rel]

    def folder_id(self, rel: str) -> int:
        """Remote folder id for a local relative dir. '' is the mirror root
        (a folder named after the remote name, created at the drive root)."""
        if rel in self._folder_ids:
            return self._folder_ids[rel]
        if rel == "":
            parent_id, name = 0, self.remote
        else:
            parent_id, name = self.folder_id(os.path.dirname(rel)), os.path.basename(rel)
        parent_entries = [] if parent_id < 0 else self.client.listing(parent_id)
        existing = next((e for e in parent_entries
                         if e.get("isFolder")
                         and name_key(e.get("filename", "")) == name_key(name)), None)
        if existing is not None:
            if existing.get("filename") != name and self.verbose:
                self.log(f"  {name!r} uses the existing folder {existing['filename']!r} "
                         f"(the server normalizes names)")
            self._folder_ids[rel] = existing["id"]
            return existing["id"]
        if self.dry_run:
            self.log(f"  would create remote folder {name if rel == '' else self.remote + '/' + rel}")
            self._folder_ids[rel] = -1
            return -1
        self._folder_ids[rel] = self.client.ensure_folder(parent_id, name)
        parent_rel = os.path.dirname(rel)
        self._listings.pop(parent_rel, None)
        return self._folder_ids[rel]

    # --- decisions -------------------------------------------------------
    def needs_upload(self, rel: str, stat: os.stat_result) -> tuple[bool, str]:
        entry = next((e for e in self.entries(os.path.dirname(rel))
                      if name_key(e.get("filename", "")) == name_key(os.path.basename(rel))
                      and not e.get("isFolder")), None)
        if entry is None:
            return True, "new"
        if int(entry.get("filesize", -1)) != stat.st_size:
            return True, f"size {entry.get('filesize')}->{stat.st_size}"
        if abs(int(entry.get("moddate", 0)) - int(stat.st_mtime)) > MTIME_TOLERANCE:
            return True, "mtime"
        return False, "same"

    # --- run -------------------------------------------------------------
    def run(self) -> Stats:
        if not os.path.isdir(self.root):
            raise NotADirectoryError(f"{self.root} is not a directory")
        dirs: list[str] = []
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir
            # do not even descend into excluded directories
            dirnames[:] = [d for d in dirnames if not self.excluded(os.path.join(rel_dir, d) if rel_dir else d)]
            dirs.append(rel_dir)
            kept = [name for name in filenames if not self.excluded(os.path.join(rel_dir, name) if rel_dir else name)]
            files.extend(os.path.join(rel_dir, name) if rel_dir else name for name in kept)
        self._walked_dirs = dirs
        self._local_files = set(files)
        self._local_name_keys = {name_key(f) for f in files}
        self.log(f"[{self.remote}] {len(dirs)} dirs / {len(files)} files under {self.root}")

        for rel_dir in sorted(dirs, key=lambda d: (d.count(os.sep), d)):
            if rel_dir:
                self.folder_id(rel_dir)

        # Plan first: needed bytes are known before anything is uploaded, so a
        # full drive (or a bad path) fails fast instead of after thousands of
        # failed uploads.
        plan = []
        for rel in sorted(files):
            path = os.path.join(self.root, rel)
            try:
                stat = os.stat(path)
                if stat.st_size == 0:
                    # The API refuses empty uploads; a legit empty file (e.g. a
                    # Syncthing .stignore marker) must not count as a failure.
                    self.stats.skipped += 1
                    self.log(f"  skipped {rel}: empty file")
                    continue
                needed, reason = self.needs_upload(rel, stat)
                if not needed:
                    self.stats.unchanged += 1
                    continue
                plan.append((rel, stat, reason))
            except (TransientError, AuthError):
                raise                                       # outage/credentials: abort the run
            except Exception as exc:                            # noqa: BLE001 - isolate per file
                self.stats.failures.append((rel, str(exc)[:200]))
                self.log(f"  FAILED {rel}: {str(exc)[:200]}")

        to_upload = sum(stat.st_size for _, stat, _ in plan)
        if plan:
            self.log(f"[{self.remote}] {len(plan)} file(s) to upload, {to_upload / 1e6:.1f} MB")
            if self.quota_check and not self.dry_run:
                self.quota_check(to_upload)

        for rel, stat, reason in plan:
            path = os.path.join(self.root, rel)
            try:
                if self.dry_run:
                    self.log(f"  would upload {rel} ({reason}, {stat.st_size} bytes)")
                    continue
                self.client.upload(self.folder_id(os.path.dirname(rel)), path)
                self.stats.uploaded += 1
                self.stats.bytes += stat.st_size
                parent = os.path.dirname(rel)
                self._uploaded.setdefault(parent, []).append((os.path.basename(rel), stat.st_size))
                self._listings.setdefault(parent, []).append(
                    {"filename": os.path.basename(rel), "filesize": stat.st_size,
                     "moddate": int(stat.st_mtime), "isFolder": 0})
                if self.verbose:
                    self.log(f"  uploaded {rel} ({reason}, {stat.st_size} bytes)")
            except (TransientError, AuthError):
                raise                                       # outage/credentials: abort the run
            except Exception as exc:                            # noqa: BLE001 - isolate per file
                self.stats.failures.append((rel, str(exc)[:200]))
                self.log(f"  FAILED {rel}: {str(exc)[:200]}")

        if not self.dry_run and self._uploaded:
            self.verify()
        if self.prune and not self.dry_run:
            self.prune_remote()
        return self.stats

    def prune_remote(self) -> None:
        """Remove remote files that no longer exist locally.

        Trashes them by default (recoverable); prune_delete erases instead.
        Guards (see README): a complete fresh remote listing is taken first,
        only files are removed (the API cannot delete folders), everything stays
        inside this mirror's own subtree, and a large wipe must be forced.
        """
        deletions: list[tuple[int, int, str]] = []
        remote_total = 0
        for rel_dir in sorted(self._walked_dirs):
            folder_id = self._folder_ids.get(rel_dir)
            if folder_id is None or folder_id < 0:      # never created, nothing to prune
                continue
            for entry in self.client.listing(folder_id):
                if entry.get("isFolder"):
                    continue
                remote_total += 1
                name = entry["filename"]
                rel = os.path.join(rel_dir, name) if rel_dir else name
                # names are compared through name_key: a locally NFKC-different
                # name may have been stored normalized by the server
                if name_key(rel) not in self._local_name_keys and not self.excluded(rel):
                    deletions.append((folder_id, entry["id"], rel))
        if not deletions:
            self.log(f"[{self.remote}] prune: nothing to remove")
            return
        threshold = max(50, remote_total // 4)
        if len(deletions) > threshold and not self.prune_force:
            raise PreflightError(
                f"prune would remove {len(deletions)} file(s) in {self.remote} "
                f"(threshold {threshold}); pass --prune-force if this is intended")
        if self.prune_delete:
            self._erase(deletions)
        else:
            for folder_id, file_id, rel in deletions:
                try:
                    self.client.trash(file_id)
                    self.stats.trashed += 1
                    self.log(f"  trashed {rel}")
                except (TransientError, AuthError):
                    raise                                   # outage: report it, do not blame the file
                except Exception as exc:                    # noqa: BLE001 - isolate per file
                    self.stats.failures.append((rel, str(exc)[:200]))
                    self.log(f"  FAILED prune {rel}: {str(exc)[:200]}")
        self.log(f"[{self.remote}] pruned {len(deletions)} stale file(s) "
                 f"({self.stats.trashed} trashed, {self.stats.deleted} deleted)")

    def _erase(self, deletions: list[tuple[int, int, str]]) -> None:
        """Permanently delete pruned files: one batch call, falling back to
        per-file deletes (with per-file isolation) only if the batch is refused.

        This is the single place prune handles delete failures. On the fallback
        path an id the batch had already removed is asked for again and its error
        is counted as a failure - harmless, since /erase does not half-apply in
        practice, but it does mean stats.deleted can undercount.
        """
        file_ids = [file_id for _, file_id, _ in deletions]
        try:
            self.stats.deleted += self.client.delete_files(file_ids)
            for _, _, rel in deletions:
                self.log(f"  deleted {rel}")
            return
        except (TransientError, AuthError):
            raise                                       # outage/credentials: not a batch problem
        except Exception as exc:                            # noqa: BLE001 - fall back per file
            self.log(f"  batch delete failed ({str(exc)[:200]}); retrying per file")
        for _, file_id, rel in deletions:
            try:
                self.client.delete_file(file_id)
                self.stats.deleted += 1
                self.log(f"  deleted {rel}")
            except (TransientError, AuthError):
                raise
            except Exception as exc:                        # noqa: BLE001 - isolate per file
                self.stats.failures.append((rel, str(exc)[:200]))
                self.log(f"  FAILED prune {rel}: {str(exc)[:200]}")

    def verify(self) -> None:
        """Re-list every folder written to and check the sizes we uploaded."""
        for rel_dir, uploaded in self._uploaded.items():
            try:
                listing = {name_key(e["filename"]): e for e in self.client.listing(self.folder_id(rel_dir))}
            except (TransientError, AuthError):
                raise                                       # cannot verify while the service is down
            except Exception as exc:                            # noqa: BLE001
                self.stats.failures.append((rel_dir or "/", f"verify listing failed: {exc}"))
                continue
            for name, size in uploaded:
                entry = listing.get(name_key(name))
                target = f"{rel_dir}/{name}" if rel_dir else name
                if entry is None:
                    self.stats.failures.append((target, "missing after upload"))
                elif int(entry.get("filesize", -1)) != size:
                    self.stats.failures.append((target, f"size {entry.get('filesize')} != {size}"))
                else:
                    self.stats.verified += 1
        self.log(f"[{self.remote}] verified {self.stats.verified} uploaded file(s) "
                 f"across {len(self._uploaded)} folder(s)")
