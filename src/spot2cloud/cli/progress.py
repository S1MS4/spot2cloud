"""Progress bars for the two long-running stages.

Both reporters degrade to plain stderr lines when tqdm is missing or stderr is
not a TTY, so output stays readable in a pipe or a CI log.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from spot2cloud.cli.console import ASCII_ONLY, DESC_WIDTH, SYMBOLS, encodable, truncate
from spot2cloud.models import STATUS_OK, STATUS_SKIPPED, DownloadResult, PlaylistJob

__all__ = ["ProgressReporter", "UploadReporter"]

logger = logging.getLogger("spot2cloud")


class ProgressReporter:
    """``on_progress`` callback that drives one tqdm bar for one playlist.

    ``pipeline.run_job`` calls this from the **main** thread once per track,
    including skips, so no locking is needed. Per-track lines are emitted through
    ``tqdm.write`` (pinned to stderr) so they scroll above the bar rather than
    shredding it.

    The bar is suppressed in three cases: ``--verbose`` (yt-dlp's own output
    interleaves with it and makes it useless), stderr not being a TTY (piped or
    CI output should stay free of escape codes), and tqdm being unavailable.
    """

    def __init__(self, job: PlaylistJob, verbose: bool = False) -> None:
        """Open a bar for ``job``; ``verbose`` means plain log lines, no bar."""
        self._total = len(job.tracks)
        # DownloadResult only carries a title; the "Artist - Title" label lives on
        # the Track. Results arrive out of order, so map them back by query.
        self._labels = {track.query: track.display() for track in job.tracks if track.query}
        self._bar = None
        if verbose or self._total == 0:
            return
        try:
            from tqdm import tqdm
        except ImportError:  # pragma: no cover - tqdm is a declared dependency
            logger.debug("tqdm unavailable; falling back to plain progress lines")
            return
        self._bar = tqdm(
            total=self._total,
            desc=encodable(truncate(job.name, DESC_WIDTH)),
            unit="track",
            file=sys.stderr,
            disable=not sys.stderr.isatty(),
            ascii=ASCII_ONLY,
            dynamic_ncols=True,
            leave=True,
        )

    def _write(self, line: str) -> None:
        """Emit one progress line without corrupting the bar."""
        line = encodable(line)
        if self._bar is not None:
            # tqdm.write defaults to stdout, which is reserved for the summary.
            self._bar.write(line, file=sys.stderr)
        else:
            logger.info("%s", line)

    def _label(self, result: DownloadResult) -> str:
        """Best human-readable name for a result: ``Artist - Title`` when the
        originating track is known, else the resolved title, else the raw query.
        """
        return self._labels.get(result.url) or result.title or result.url

    def __call__(self, result: DownloadResult, done: int, total: int) -> None:
        """Report one finished track. Signature required by ``ProgressCallback``."""
        index = "[%d/%d]" % (done, total)
        if result.status == STATUS_OK:
            line = "%s %s %s" % (index, SYMBOLS["ok"], self._label(result))
        elif result.status == STATUS_SKIPPED:
            line = "%s %s %s (%s)" % (
                index,
                SYMBOLS["skipped"],
                self._label(result),
                result.reason or "already downloaded",
            )
        else:
            line = "%s %s %s %s %s" % (
                index,
                SYMBOLS["failed"],
                result.url,
                SYMBOLS["dash"],
                result.reason or "unknown error",
            )
        self._write(line)
        if self._bar is not None:
            self._bar.update(1)

    def close(self) -> None:
        """Close the bar, if one was opened. Safe to call more than once."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None


class UploadReporter:
    """One tqdm bar over the album folders being published.

    Same discipline as :class:`ProgressReporter`: every line goes through
    ``tqdm.write`` pinned to stderr so it scrolls above the bar instead of
    shredding it, and the bar is suppressed under ``--verbose``, off a TTY, or
    when tqdm is missing. Uploads are sequential -- one browser page, one set at
    a time -- so no locking is needed here either.
    """

    def __init__(self, total: int, verbose: bool = False) -> None:
        """Open a bar for ``total`` folders; ``verbose`` means no bar."""
        self._total = total
        self._bar = None
        if verbose or total == 0:
            return
        try:
            from tqdm import tqdm
        except ImportError:  # pragma: no cover - tqdm is a declared dependency
            logger.debug("tqdm unavailable; falling back to plain progress lines")
            return
        self._bar = tqdm(
            total=total,
            desc="uploading",
            unit="set",
            file=sys.stderr,
            disable=not sys.stderr.isatty(),
            ascii=ASCII_ONLY,
            dynamic_ncols=True,
            leave=True,
        )

    def _write(self, line: str) -> None:
        """Emit one progress line without corrupting the bar."""
        line = encodable(line)
        if self._bar is not None:
            # tqdm.write defaults to stdout, which is reserved for the summary.
            self._bar.write(line, file=sys.stderr)
        else:
            logger.info("%s", line)

    def _finish(self, index: int, text: str) -> None:
        """Write one finished-set line and advance the bar by one."""
        self._write("[%d/%d] %s" % (index, self._total, text))
        if self._bar is not None:
            self._bar.update(1)

    def note(self, line: str) -> None:
        """Write one line that is not tied to a set, e.g. a selector report."""
        self._write(line)

    def begin(self, title: str) -> None:
        """Name the set now going up, so a long upload is not silent."""
        if self._bar is not None:
            self._bar.set_description(encodable(truncate(title, DESC_WIDTH)))

    def ok(self, index: int, title: str, detail: Optional[str] = None) -> None:
        """Report a set that finished, with its URL or a note such as "dry run"."""
        suffix = " %s %s" % (SYMBOLS["dash"], detail) if detail else ""
        self._finish(
            index,
            "Uploading %s%s done%s" % (truncate(title, DESC_WIDTH), SYMBOLS["ellipsis"], suffix),
        )

    def skipped(self, index: int, title: str, url: Optional[str] = None) -> None:
        self._finish(
            index,
            "%s %s (already uploaded: %s)"
            % (SYMBOLS["skipped"], truncate(title, DESC_WIDTH), url or "no URL recorded"),
        )

    def failed(self, index: int, title: str, reason: str) -> None:
        self._finish(
            index,
            "%s %s %s %s"
            % (SYMBOLS["failed"], truncate(title, DESC_WIDTH), SYMBOLS["dash"], reason),
        )

    def close(self) -> None:
        """Close the bar, if one was opened. Safe to call more than once."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None
