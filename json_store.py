"""Atomic, damage-tolerant JSON ledger shared by the download and upload stores.

Both ledgers answer the same question - has this already happened? - and both
have to survive a crash mid-write and a corrupted file on the next run. That
machinery lives here once. Subclasses supply their own entry schema and their
own write methods, which is all that genuinely differs between them.

Failure policy: nothing here raises for an I/O or JSON problem. Losing a ledger
costs a redundant download or a duplicate set; raising would throw away work
that already succeeded. A damaged file is logged and treated as empty, and left
on disk so the user can inspect it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["JsonStore"]


class JsonStore:
    """A ``{key: entry}`` mapping in one JSON file, loaded eagerly.

    The file is a sibling of the folder it describes, not a child of it: an
    album folder is a music library entry and should hold music and artwork
    only.

    Concurrency: all state sits behind one :class:`threading.Lock`. :meth:`flush`
    holds it for the whole write, so concurrent flushes serialise instead of
    racing on the temp file. A subclass that flushes from a write method must
    release the lock first - a plain ``Lock`` is not reentrant.
    """

    #: Ledger directory name, resolved against the described folder's parent.
    directory = ""
    #: Schema version stamped into the file.
    version = 1
    #: Prefix on this store's log lines.
    label = "store"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._unflushed = 0
        self._entries = self._read_from_disk()

    @property
    def _log(self) -> logging.Logger:
        # The subclass's own module logger, so callers can filter on "history"
        # or "upload_ledger" rather than on this shared base.
        return logging.getLogger(type(self).__module__)

    @property
    def path(self) -> Path:
        return self.folder.parent / self.directory / (self.folder.name + ".json")

    @staticmethod
    def _coerce_entry(value: Any) -> Optional[Dict[str, Any]]:
        """Normalise one stored value, or return ``None`` to drop it."""
        raise NotImplementedError

    def _read_from_disk(self) -> Dict[str, Dict[str, Any]]:
        """Parse the ledger, tolerating every kind of damage."""
        target = self.path
        if not target.is_file():
            return {}
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            self._log.warning(
                "%s: ignoring unreadable ledger %s (%s); starting empty. "
                "The file is left in place for inspection.",
                self.label,
                target,
                exc,
            )
            return {}

        if not isinstance(raw, dict):
            self._log.warning(
                "%s: ledger %s is a %s, expected an object; starting empty.",
                self.label,
                target,
                type(raw).__name__,
            )
            return {}

        # ``entries`` was added with the version stamp; a payload without it is
        # the original bare mapping.
        payload = raw.get("entries", raw)
        if not isinstance(payload, dict):
            self._log.warning(
                "%s: ledger %s has a malformed 'entries' section; starting empty.",
                self.label,
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
            self._log.debug("%s: loaded %d entries from %s", self.label, len(entries), target)
        return entries

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """A copy of the entry for ``key``, so callers cannot mutate state
        without the lock."""
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.has(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def flush(self) -> None:
        """Write the ledger to disk atomically.

        The temp file is created in the ledger directory, not the folder being
        described, so :func:`os.replace` stays a same-directory rename and is
        therefore atomic even if the two ever land on different filesystems. A
        reader sees the old ledger or the new one, never a truncated one.
        """
        with self._lock:
            payload = {
                "version": self.version,
                "entries": {key: dict(value) for key, value in self._entries.items()},
            }
            target = self.path
            tmp_name = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(target.parent),
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
                self._unflushed = 0
                self._log.debug("%s: wrote %d entries to %s", self.label, len(self._entries), target)
            except OSError as exc:
                self._log.warning(
                    "%s: could not write %s (%s); continuing", self.label, target, exc
                )
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        self._log.debug("%s: could not remove temp file %s", self.label, tmp_name)

    def __enter__(self) -> "JsonStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Flush on the way out, then let any in-flight exception propagate."""
        self.flush()
        return False

    def __repr__(self) -> str:
        return "%s(%r, entries=%d)" % (type(self).__name__, str(self.folder), len(self))
