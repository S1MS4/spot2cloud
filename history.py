"""Resume ledger for downloaded tracks.

Each album gets one ledger under ``<output_dir>/.download_history/`` (:data:`models.HISTORY_DIR`)
recording which tracks already landed on disk, so a re-run of the same playlist
skips them instead of paying for the download again.

The file is written from worker threads while a playlist is in flight, so every
mutation is guarded by a lock and every write to disk is atomic (write a temp
file in the same directory, ``fsync``, then ``os.replace``). A reader therefore
never observes a half-written ledger, and a crash mid-write leaves the previous
good copy intact.

Failure policy: this module never raises for an I/O problem. Losing the ledger
costs a redundant download on the next run; raising would throw away downloads
that already succeeded. Corruption is likewise tolerated - a broken file is
logged and treated as empty, and is *not* deleted so the user can inspect it.

On-disk shape::

    {
      "version": 1,
      "entries": {
        "spotify:EXAMPLETRACKID00000001": {
          "path": "Artist Name - Example Track.mp3",
          "title": "Example Track",
          "downloaded_at": "2026-08-07T18:04:11Z"
        }
      }
    }

A legacy flat ``{key: path}`` mapping is upgraded in memory on load and written
back in the current shape at the next flush.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from models import HISTORY_DIR

__all__ = [
    "HISTORY_VERSION",
    "AUTOFLUSH_EVERY",
    "HistoryStore",
]

logger = logging.getLogger(__name__)

#: Schema version stamped into the JSON file.
HISTORY_VERSION = 1

#: ``add()`` writes the ledger to disk once this many un-flushed adds pile up,
#: so a crash halfway through a long playlist loses at most this many entries.
AUTOFLUSH_EVERY = 5


class HistoryStore:
    """Thread-safe, crash-tolerant record of what has already been downloaded.

    Instances are cheap to create and load their backing file eagerly. Usable as
    a context manager, which flushes on exit::

        with HistoryStore(playlist_dir) as history:
            if not history.has(key):
                ...
                history.add(key, mp3_path, "Some Title")

    Concurrency: all state lives behind a single :class:`threading.Lock`.
    :meth:`flush` acquires that lock for the whole write, so concurrent flushes
    are serialised rather than racing on the temp file. :meth:`add` therefore
    never calls :meth:`flush` while holding the lock - it releases first - which
    is what keeps a non-reentrant ``Lock`` from deadlocking on autoflush.

    Attributes:
        playlist_dir: Folder holding both the MP3s and the ledger.
    """

    def __init__(self, playlist_dir: Path) -> None:
        """Load the ledger for ``playlist_dir``, if one exists.

        Args:
            playlist_dir: Directory that contains (or will contain) the
                playlist's MP3s and its ``download_history.json``.

        Raises:
            Nothing. An unreadable or malformed file yields an empty store.
        """
        self.playlist_dir = Path(playlist_dir)
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._unflushed = 0
        self._entries = self._read_from_disk()

    # --- paths ------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path of the JSON ledger."""
        return (
            self.playlist_dir.parent
            / HISTORY_DIR
            / (self.playlist_dir.name + ".json")
        )

    def _abs(self, raw: str) -> Path:
        """Expand a stored path string back into an absolute path.

        Args:
            raw: Path as stored in the ledger; relative entries are relative to
                :attr:`playlist_dir`.

        Returns:
            An absolute :class:`~pathlib.Path`.
        """
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return self.playlist_dir / candidate

    def _store_path(self, path: Path) -> str:
        """Render ``path`` for storage, preferring a playlist-relative form.

        Keeping the path relative means the user can move or rename the whole
        playlist folder without invalidating the ledger.

        Args:
            path: The file that was written.

        Returns:
            A relative POSIX-style path when ``path`` sits inside
            :attr:`playlist_dir`, otherwise the absolute path as a string.
        """
        target = Path(path)
        try:
            resolved = target.resolve()
            base = self.playlist_dir.resolve()
            rel = Path(os.path.relpath(str(resolved), str(base)))
        except (OSError, ValueError):  # pragma: no cover - exotic filesystems
            return str(target)
        first = rel.parts[0] if rel.parts else ""
        if first == os.pardir:
            return str(resolved)
        return rel.as_posix()

    # --- loading ----------------------------------------------------------

    def _read_from_disk(self) -> Dict[str, Dict[str, Any]]:
        """Parse the ledger from disk, tolerating every kind of damage.

        Returns:
            Mapping of history key to entry dict. Empty when the file is
            missing, unreadable, not JSON, not a mapping, or otherwise
            unrecognisable.
        """
        target = self.path
        if not target.is_file():
            return {}
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                "history: ignoring unreadable ledger %s (%s); starting empty. "
                "The file is left in place for inspection.",
                target,
                exc,
            )
            return {}

        if not isinstance(raw, dict):
            logger.warning(
                "history: ledger %s is a %s, expected an object; starting empty.",
                target,
                type(raw).__name__,
            )
            return {}

        payload = raw.get("entries", raw)
        if not isinstance(payload, dict):
            logger.warning(
                "history: ledger %s has a malformed 'entries' section; starting empty.",
                target,
            )
            return {}

        entries: Dict[str, Dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            entry = self._coerce_entry(value)
            if entry is not None:
                entries[key] = entry
        if entries:
            logger.debug("history: loaded %d entries from %s", len(entries), target)
        return entries

    @staticmethod
    def _coerce_entry(value: Any) -> Optional[Dict[str, Any]]:
        """Normalise one stored value into the current entry shape.

        Args:
            value: Either a modern entry dict or a legacy bare path string.

        Returns:
            A dict with at least a ``path`` key, or ``None`` if the value is
            unusable and should be dropped.
        """
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

    # --- reads ------------------------------------------------------------

    def has(self, key: str) -> bool:
        """Report whether ``key`` is recorded.

        Note this only checks the ledger; it does not stat the file. Callers
        that care whether the MP3 still exists should use :meth:`resolve_path`.

        Args:
            key: History key, e.g. ``Track.history_key()``.

        Returns:
            ``True`` if an entry exists for ``key``.
        """
        with self._lock:
            return key in self._entries

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the entry for ``key``.

        Args:
            key: History key to look up.

        Returns:
            A shallow copy of the entry dict (``path``/``title``/
            ``downloaded_at``), or ``None`` when the key is unknown. A copy is
            returned so callers cannot mutate store state without the lock.
        """
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def resolve_path(self, key: str) -> Optional[Path]:
        """Return the absolute path recorded for ``key``.

        Args:
            key: History key to look up.

        Returns:
            Absolute path of the recorded file, or ``None`` when the key is
            unknown. The file is not guaranteed to still exist.
        """
        entry = self.get(key)
        if entry is None:
            return None
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            return None
        return self._abs(raw)

    def __contains__(self, key: object) -> bool:
        """Support ``key in history``."""
        return isinstance(key, str) and self.has(key)

    def __len__(self) -> int:
        """Return the number of recorded entries."""
        with self._lock:
            return len(self._entries)

    # --- writes -----------------------------------------------------------

    def add(self, key: str, path: Path, title: Optional[str] = None) -> None:
        """Record a successful download.

        Autoflushes every :data:`AUTOFLUSH_EVERY` adds. The flush deliberately
        happens after the lock is released, so a plain (non-reentrant) lock
        cannot deadlock here.

        Args:
            key: History key, e.g. ``Track.history_key()``.
            path: Path of the file that was written. Stored relative to
                :attr:`playlist_dir` when it lives inside it.
            title: Resolved track title, kept for human-readable reporting.

        Returns:
            None.

        Raises:
            Nothing. A failing autoflush is logged, not raised.
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
        """Drop ``key`` from the ledger if present.

        Used when a recorded file has vanished, so the track downloads again.

        Args:
            key: History key to forget.

        Returns:
            None.
        """
        with self._lock:
            if self._entries.pop(key, None) is not None:
                self._unflushed += 1

    def prune_missing(self) -> int:
        """Forget every entry whose file no longer exists on disk.

        Lets a user delete an MP3 and have the next run fetch it again.

        Returns:
            How many entries were dropped.
        """
        with self._lock:
            gone = []
            for key, entry in self._entries.items():
                raw = entry.get("path")
                if not isinstance(raw, str) or not raw or not self._abs(raw).exists():
                    gone.append(key)
            for key in gone:
                del self._entries[key]
            if gone:
                self._unflushed += len(gone)
        if gone:
            logger.info("history: pruned %d entr(y/ies) whose file is gone", len(gone))
        return len(gone)

    def flush(self) -> None:
        """Write the ledger to disk atomically.

        Writes a temp file alongside the target, ``fsync``s it, then
        :func:`os.replace`s it over the real file - an atomic rename on POSIX
        and on Windows - so readers see either the old ledger or the new one and
        never a truncated one. The whole operation holds the lock, which
        serialises concurrent flushes.

        Returns:
            None.

        Raises:
            Nothing. An :class:`OSError` is logged as a warning and swallowed:
            a ledger that cannot be written must never invalidate downloads that
            already succeeded.
        """
        with self._lock:
            payload = {
                "version": HISTORY_VERSION,
                "entries": {key: dict(value) for key, value in self._entries.items()},
            }
            target = self.path
            tmp_name = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.playlist_dir.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(self.playlist_dir),
                    prefix=".%s." % HISTORY_DIR,
                    suffix=".tmp",
                    delete=False,
                )
                tmp_name = handle.name
                try:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    handle.close()
                os.replace(tmp_name, str(target))
                tmp_name = None
                self._unflushed = 0
                logger.debug("history: wrote %d entries to %s", len(self._entries), target)
            except OSError as exc:
                logger.warning("history: could not write %s (%s); continuing", target, exc)
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        logger.debug("history: could not remove temp file %s", tmp_name)

    # --- context manager --------------------------------------------------

    def __enter__(self) -> "HistoryStore":
        """Enter a ``with`` block; returns ``self``."""
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Flush on the way out, whether or not the block raised.

        Returns:
            ``False``, so any in-flight exception keeps propagating.
        """
        self.flush()
        return False

    def __repr__(self) -> str:
        return "HistoryStore(%r, entries=%d)" % (str(self.playlist_dir), len(self))
