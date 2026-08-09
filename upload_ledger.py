"""Resume ledger for SoundCloud uploads.

Each album folder owns one ``upload_history.json`` next to its MP3s, recording
which sets and which tracks already reached SoundCloud. An upload is far more
expensive than a download and is *not* idempotent: re-running an interrupted
run without this file gives the user a second copy of the same set on their
profile, which they then have to delete by hand.

Design mirrors :mod:`history`: one :class:`threading.Lock` around all state,
atomic writes (temp file in the same directory, ``fsync``, ``os.replace``), a
corrupt file logged and treated as empty but never deleted, and no I/O error
ever raised - losing the ledger costs a duplicate set, raising would abandon
uploads that already succeeded.

Unlike :mod:`history` there is no autoflush counter: every :meth:`record`
writes through immediately. Records arrive minutes apart, so the write is free,
and the whole point is to survive a Ctrl-C between two sets.

On-disk shape::

    {
      "version": 1,
      "entries": {
        "set:Example Album": {
          "set_url": "https://soundcloud.com/user/sets/mixtape-vol-1",
          "note": null,
          "uploaded_at": "2026-08-09T12:31:07Z"
        },
        "track:01 - INTRO.mp3": {"set_url": null, "note": null, "uploaded_at": "..."}
      }
    }
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

__all__ = [
    "UPLOAD_HISTORY_DIR",
    "UPLOAD_VERSION",
    "set_key",
    "track_key",
    "UploadLedger",
]

logger = logging.getLogger(__name__)

#: Ledger directory, a sibling of the album folders; one JSON file per album.
UPLOAD_HISTORY_DIR = ".upload_history"

#: Schema version stamped into the JSON file.
UPLOAD_VERSION = 1


def set_key(album_title: str) -> str:
    """Ledger key for a whole set. Keyed on the title SoundCloud will show."""
    return "set:%s" % (album_title or "").strip()


def track_key(filename: str) -> str:
    """Ledger key for one track.

    The bare filename, not the full path, so moving or renaming the album
    folder does not resurrect an upload that already happened.
    """
    return "track:%s" % Path(filename).name


class UploadLedger:
    """Thread-safe, crash-tolerant record of what has already been uploaded.

    Loads eagerly and is usable as a context manager, which flushes on exit::

        with UploadLedger(album_folder) as ledger:
            if not ledger.has(set_key(album.title)):
                url = upload(...)
                ledger.record(set_key(album.title), set_url=url)

    Attributes:
        album_folder: Folder holding both the audio and the ledger.
    """

    def __init__(self, album_folder: Path) -> None:
        """Load the ledger for ``album_folder``; damage yields an empty store."""
        self.album_folder = Path(album_folder)
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = self._read_from_disk()

    @property
    def path(self) -> Path:
        """Absolute path of the JSON ledger."""
        return (
            self.album_folder.parent
            / UPLOAD_HISTORY_DIR
            / (self.album_folder.name + ".json")
        )

    # --- loading ----------------------------------------------------------

    def _read_from_disk(self) -> Dict[str, Dict[str, Any]]:
        """Parse the ledger, tolerating every kind of damage."""
        target = self.path
        if not target.is_file():
            return {}
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                "upload ledger: ignoring unreadable %s (%s); starting empty. "
                "The file is left in place for inspection.",
                target,
                exc,
            )
            return {}

        if not isinstance(raw, dict):
            logger.warning(
                "upload ledger: %s is a %s, expected an object; starting empty.",
                target,
                type(raw).__name__,
            )
            return {}

        payload = raw.get("entries", raw)
        if not isinstance(payload, dict):
            logger.warning(
                "upload ledger: %s has a malformed 'entries' section; starting empty.",
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
            logger.debug("upload ledger: loaded %d entries from %s", len(entries), target)
        return entries

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

    # --- reads ------------------------------------------------------------

    def has(self, key: str) -> bool:
        """Report whether ``key`` has already been uploaded."""
        with self._lock:
            return key in self._entries

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the entry for ``key``, or ``None``.

        A copy, so callers cannot mutate store state without the lock.
        """
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def __contains__(self, key: object) -> bool:
        """Support ``key in ledger``."""
        return isinstance(key, str) and self.has(key)

    def __len__(self) -> int:
        """Return the number of recorded entries."""
        with self._lock:
            return len(self._entries)

    # --- writes -----------------------------------------------------------

    def record(self, key: str, set_url: Optional[str] = None, note: Optional[str] = None) -> None:
        """Record a successful upload and write the ledger through to disk.

        Args:
            key: :func:`set_key` or :func:`track_key`.
            set_url: Permalink SoundCloud returned, when there is one. A dry run
                has no URL; callers should simply not record one.
            note: Free-form remark kept for the human reading the file.

        Raises:
            Nothing. A failing write is logged, exactly like :meth:`flush`.
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

    def flush(self) -> None:
        """Write the ledger to disk atomically; never raises.

        The temp file is created in the ledger directory, not the album folder,
        so :func:`os.replace` stays a same-directory rename and is therefore
        atomic. A reader sees the old ledger or the new one, never a truncated
        one.
        """
        with self._lock:
            payload = {
                "version": UPLOAD_VERSION,
                "entries": {key: dict(value) for key, value in self._entries.items()},
            }
            target = self.path
            tmp_name = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(self.path.parent),
                    prefix=".",
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
                logger.debug(
                    "upload ledger: wrote %d entries to %s", len(self._entries), target
                )
            except OSError as exc:
                logger.warning(
                    "upload ledger: could not write %s (%s); continuing", target, exc
                )
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        logger.debug(
                            "upload ledger: could not remove temp file %s", tmp_name
                        )

    # --- context manager --------------------------------------------------

    def __enter__(self) -> "UploadLedger":
        """Enter a ``with`` block; returns ``self``."""
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Flush on the way out; returns ``False`` so exceptions keep going."""
        self.flush()
        return False

    def __repr__(self) -> str:
        return "UploadLedger(%r, entries=%d)" % (str(self.album_folder), len(self))
