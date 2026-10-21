"""Verify already-uploaded files still exist, remote, at the size we recorded.

Why this exists: the mirror trusts `sync-db.sqlite` and never re-reads the remote
for a file it last saw as synced. A file lost on Icedrive's side (trash, provider
hiccup, a manual cleanup) would therefore stay invisible forever - the nightly run
would keep reporting "unchanged". Uploads are verified, but nothing verifies them
again later.

Cost model (measured, see tools/throttle_probe.py): there is no whole-tree listing,
so verification is one `/collection` call per folder, and the account 429s above
~2-4 calls/s. This pass therefore walks a bounded number of folders per run,
oldest-checked first, which is a deterministic rotating sweep: every folder is
verified once per cycle, nothing is sampled.

A row that fails the check is deleted from the db, which is the whole repair
mechanism - the next mirror run finds no row, lists the folder, and re-uploads.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .client import AuthError, TransientError, name_key


@dataclass
class ScrubStats:
    folders: int = 0
    files: int = 0
    ok: int = 0
    requeued: int = 0
    missing_folders: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"folders": self.folders, "files": self.files, "ok": self.ok,
                "requeued": self.requeued, "missing_folders": self.missing_folders,
                "failures": [{"path": path, "error": error} for path, error in self.failures]}


def folder_id_for(client, db, remote: str, rel: str) -> int | None:
    """Resolve (and remember) the remote id of `remote/rel`; None if it is gone.

    The db turns repeat runs into one `listing` call per folder instead of one per
    path component - the difference between a 20 min and a 3 h sweep."""
    cached = db.folder_id(remote, rel) if db else None
    if cached is not None:
        return cached
    parent, parts = 0, rel.split("/") if rel else []
    for depth in range(len(parts) + 1):
        path = "/".join(parts[:depth])
        known = db.folder_id(remote, path) if db else None
        if known is not None:
            parent = known
            continue
        name = remote if depth == 0 else parts[depth - 1]
        entry = next((e for e in client.listing(parent)
                      if e.get("isFolder") and name_key(e.get("filename", "")) == name_key(name)), None)
        if entry is None:
            return None
        parent = entry["id"]
        if db:
            db.put_folder_id(remote, path, parent)
    return parent


def scrub_mirror(client, db, remote: str, limit: int | None, log=print) -> ScrubStats:
    """Compare up to `limit` folders (oldest verified first) against the remote."""
    stats = ScrubStats()
    rows = db.rows(remote) if db else []
    by_dir: dict[str, list[tuple[str, int]]] = {}
    for rel, size, _ in rows:
        by_dir.setdefault(os.path.dirname(rel), []).append((rel, size))
    if not by_dir:
        log(f"[{remote}] scrub: nothing recorded to verify")
        return stats

    checked = db.checked_at(remote) if db else {}
    # Oldest first (never-checked folders sort first): a complete cycle, in order,
    # spread over as many runs as it takes. No randomness, so coverage is provable.
    todo = sorted(by_dir, key=lambda directory: checked.get(directory, 0))
    if limit is not None:
        todo = todo[:limit]
    # Process the chosen slice parent-first: a folder's listing hands back its
    # childrens' ids, so in path order each folder costs one call instead of one
    # per unresolved ancestor. Selection stays oldest-first; only the order of
    # the slice changes, which does not affect coverage.
    todo.sort()
    log(f"[{remote}] scrub: {len(todo)} of {len(by_dir)} folder(s), "
        f"{sum(len(by_dir[d]) for d in todo)} file(s)")

    for rel_dir in todo:
        label = rel_dir or "/"
        try:
            folder_id = folder_id_for(client, db, remote, rel_dir)
            if folder_id is None:
                for rel, _ in by_dir[rel_dir]:          # the folder itself is gone
                    if db:
                        db.delete(remote, rel)
                stats.missing_folders += 1
                stats.requeued += len(by_dir[rel_dir])
                log(f"  requeued {len(by_dir[rel_dir])} file(s): remote folder {label} is gone")
                if db:
                    db.mark_checked(remote, rel_dir, int(time.time()))
                continue
            entries = client.listing(folder_id)
            # Cache the child folder ids this listing just handed us: resolving
            # them later then costs nothing, which halves the calls a deep tree
            # needs (music is 5 364 folders).
            for child in entries:
                if child.get("isFolder") and db:
                    db.put_folder_id(remote, os.path.join(rel_dir, child["filename"]) if rel_dir
                                     else child["filename"], child["id"])
            listing = {name_key(e.get("filename", "")): e
                       for e in entries if not e.get("isFolder")}
        except (TransientError, AuthError):
            raise                                        # outage: do not blame the files
        except Exception as exc:                         # noqa: BLE001 - one folder must not stop the rest
            stats.failures.append((label, str(exc)[:200]))
            log(f"  FAILED scrub {label}: {str(exc)[:200]}")
            continue

        for rel, size in by_dir[rel_dir]:
            stats.files += 1
            entry = listing.get(name_key(os.path.basename(rel)))
            if entry is None:
                # The server mangles some names it dislikes; a unique same-size
                # entry is the same fallback verify() uses after an upload.
                matches = [e for e in listing.values()
                           if not e.get("isFolder") and int(e.get("filesize", -1)) == size]
                entry = matches[0] if len(matches) == 1 else None
            if entry is not None and int(entry.get("filesize", -1)) == size:
                stats.ok += 1
                continue
            if db:
                db.delete(remote, rel)                   # next mirror run re-uploads it
            stats.requeued += 1
            reason = "missing" if entry is None else f"size {entry.get('filesize')} != {size}"
            log(f"  requeued {rel}: {reason}")
        if db:
            db.mark_checked(remote, rel_dir, int(time.time()))
        stats.folders += 1
    return stats
