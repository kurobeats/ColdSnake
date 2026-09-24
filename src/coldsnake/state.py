"""Cross-run upload journal: which chunk offsets of an upload already landed.

The API keys ranged chunks by unique_upload_id, so the id is the natural journal
key: a run killed mid-file re-sends only the offsets missing for *that* id, and
a changed file (new id) starts clean instead of resuming into a stale upload.

The journal is best-effort by design: a missing, empty, corrupt or unwritable
file is treated as "nothing known" rather than failing the backup it exists to
speed up.
"""
from __future__ import annotations

import json
import os


class UploadJournal:
    """Remembers which chunks of an upload landed, so a run killed mid-file
    resumes instead of re-sending the whole file."""

    def __init__(self, path: str | None):
        self._path = path
        self._uploads = self._load()

    def path(self) -> str | None:
        return self._path

    def done(self, upload_id: str) -> set[int]:
        """Offsets already accepted by the server for this upload id."""
        if self._path is None:
            return set()
        return set(self._uploads.get(upload_id, ()))

    def mark(self, upload_id: str, offset: int) -> None:
        if self._path is None:
            return
        self._uploads.setdefault(upload_id, set()).add(offset)
        self._save()

    def clear(self, upload_id: str) -> None:
        """Drop one upload's offsets, once the whole file is complete."""
        if self._path is None or self._uploads.pop(upload_id, None) is None:
            return
        self._save()

    def _load(self) -> dict[str, set[int]]:
        if self._path is None:
            return {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                uploads = json.load(handle)["uploads"]
            # int() on load: a hand-edited or truncated file must not push non-offset
            # values into range()/set arithmetic at the call site.
            return {str(key): {int(v) for v in values} for key, values in uploads.items()}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

    def _save(self) -> None:
        # ponytail: whole file rewritten, no fsync, no lock per mark. A chunk is
        # megabytes, so the JSON cost is noise and a torn final write just loses
        # resume info (the chunk is re-sent), never data.
        tmp = f"{self._path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"uploads": {key: sorted(value) for key, value in self._uploads.items()}},
                          handle)
            os.chmod(tmp, 0o600)  # before replace: never a world-readable window
            os.replace(tmp, self._path)
        except OSError:  # noqa: BLE001 - an unwritable journal only costs re-uploads
            pass
