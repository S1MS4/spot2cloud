"""Resume ledger for uploaded sets.

One JSON file per album under ``<output_dir>/.upload_history/``, recording what
has already reached SoundCloud so a re-run does not publish a second copy.

This ledger matters more than the download one. A repeated download costs
bandwidth; a repeated upload puts a duplicate set on the user's public profile,
which they then have to find and delete by hand. So :meth:`record` writes
through to disk immediately rather than batching like ``history.HistoryStore``
does - the window between "uploaded" and "recorded" is the window in which a
crash produces a duplicate, and it is worth an fsync to keep it short.

Storage, atomicity and the never-raise policy live in :mod:`json_store`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from spot2cloud.store.json_store import JsonStore

__all__ = [
    "UPLOAD_HISTORY_DIR",
    "UPLOAD_VERSION",
    "UploadLedger",
    "set_key",
    "track_key",
]

UPLOAD_HISTORY_DIR = ".upload_history"
UPLOAD_VERSION = 1


def set_key(album_title: str) -> str:
    """Ledger key for a whole set, keyed on the title SoundCloud will show."""
    return "set:%s" % (album_title or "").strip()


def track_key(filename: str) -> str:
    """Ledger key for one track.

    The bare filename rather than the full path, so a track that moved within
    the album folder is still recognised as already uploaded.
    """
    return "track:%s" % Path(filename).name


class UploadLedger(JsonStore):
    """What has already been published, per album folder.

    Loads eagerly and flushes on exit from a ``with`` block::

        with UploadLedger(album_folder) as ledger:
            if not ledger.has(set_key(album.title)):
                url = upload(...)
                ledger.record(set_key(album.title), set_url=url)
    """

    directory = UPLOAD_HISTORY_DIR
    version = UPLOAD_VERSION
    label = "upload ledger"

    @staticmethod
    def _coerce_entry(value: Any) -> Optional[Dict[str, Any]]:
        """Normalise one stored value; a bare string is read as the set URL."""
        if isinstance(value, str):
            return {"set_url": value or None, "note": None, "uploaded_at": None}
        if isinstance(value, dict):
            return {
                "set_url": value.get("set_url"),
                "note": value.get("note"),
                "uploaded_at": value.get("uploaded_at"),
            }
        return None

    def record(self, key: str, set_url: Optional[str] = None, note: Optional[str] = None) -> None:
        """Record a successful upload and write it through to disk.

        ``set_url`` is the permalink SoundCloud returned, when there is one; a
        dry run has none and callers should simply not record one.
        """
        entry = {
            "set_url": set_url,
            "note": note,
            "uploaded_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        with self._lock:
            self._entries[key] = entry
        # Outside the lock: flush takes it, and a plain Lock is not reentrant.
        self.flush()
