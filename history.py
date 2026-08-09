"""Resume ledger for downloaded tracks.

One JSON file per album under ``<output_dir>/.download_history/``, recording
which tracks already landed on disk so a re-run skips them instead of paying
for the download again.

Unlike the upload ledger this one is written from worker threads while a
playlist is still in flight, and it batches: :meth:`HistoryStore.add`
autoflushes every :data:`AUTOFLUSH_EVERY` adds rather than on every one. A
crash therefore loses at most that many entries, which costs a few redundant
downloads - cheap enough not to fsync forty times for a forty-track album.

Paths are stored relative to the album folder where possible, so moving the
whole folder does not invalidate the ledger.

Storage, atomicity and the never-raise policy live in :mod:`json_store`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from json_store import JsonStore
from models import HISTORY_DIR

__all__ = [
    "HISTORY_VERSION",
    "AUTOFLUSH_EVERY",
    "HistoryStore",
]

HISTORY_VERSION = 1

#: ``add()`` writes through once this many un-flushed adds pile up, so a crash
#: halfway through a long playlist loses at most this many entries.
AUTOFLUSH_EVERY = 5


class HistoryStore(JsonStore):
    """What has already been downloaded, per album folder.

    Loads eagerly and flushes on exit from a ``with`` block::

        with HistoryStore(playlist_dir) as history:
            if not history.has(key):
                ...
                history.add(key, mp3_path, "Some Title")
    """

    directory = HISTORY_DIR
    version = HISTORY_VERSION
    label = "history"

    @staticmethod
    def _coerce_entry(value: Any) -> Optional[Dict[str, Any]]:
        """Normalise one stored value; a bare string is a legacy path entry."""
        if isinstance(value, str):
            return {"path": value, "title": None, "downloaded_at": None}
        if isinstance(value, dict):
            raw_path = value.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                return None
            return {
                "path": raw_path,
                "title": value.get("title"),
                "downloaded_at": value.get("downloaded_at"),
            }
        return None

    def _abs(self, raw: str) -> Path:
        """Expand a stored path back to absolute; relative entries hang off the
        album folder."""
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return self.folder / candidate

    def _store_path(self, path: Path) -> str:
        """Render ``path`` for storage, preferring an album-relative form so the
        user can move or rename the folder without invalidating the ledger."""
        target = Path(path)
        try:
            resolved = target.resolve()
            base = self.folder.resolve()
            rel = Path(os.path.relpath(str(resolved), str(base)))
        except (OSError, ValueError):  # pragma: no cover - exotic filesystems
            return str(target)
        first = rel.parts[0] if rel.parts else ""
        if first == os.pardir:
            return str(resolved)
        return rel.as_posix()

    def resolve_path(self, key: str) -> Optional[Path]:
        """Absolute path recorded for ``key``, which is not guaranteed to still
        exist on disk."""
        entry = self.get(key)
        if entry is None:
            return None
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            return None
        return self._abs(raw)

    def add(self, key: str, path: Path, title: Optional[str] = None) -> None:
        """Record a successful download, autoflushing every
        :data:`AUTOFLUSH_EVERY` adds.

        The flush happens after the lock is released, so a plain non-reentrant
        lock cannot deadlock here.
        """
        entry = {
            "path": self._store_path(path),
            "title": title,
            "downloaded_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        with self._lock:
            self._entries[key] = entry
            self._unflushed += 1
            due = self._unflushed >= AUTOFLUSH_EVERY
        if due:
            self.flush()

    def remove(self, key: str) -> None:
        """Forget ``key``, so a track whose file has vanished downloads again."""
        with self._lock:
            if self._entries.pop(key, None) is not None:
                self._unflushed += 1
