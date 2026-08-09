"""Everything this CLI writes to the outside world.

stdout carries the final summary and nothing else, so piping the tool into
another program stays useful; stderr carries logs, the progress bar, per-track
lines and errors. Exit statuses live here too, since they are the third channel
the process talks through.

Kept free of every project import except the standard library, so any module
can print without dragging a dependency in behind it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict

__all__ = [
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_USAGE",
    "EXIT_INTERRUPTED",
    "DESC_WIDTH",
    "SYMBOLS",
    "ASCII_ONLY",
    "encodable",
    "print_out",
    "print_err",
    "truncate",
    "configure_logging",
]

#: Exit statuses, named so the returns elsewhere read as intent, not magic.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

#: How much of a playlist name fits in the progress bar's description.
DESC_WIDTH = 30

#: progress line is not worth a ``UnicodeEncodeError``.
_UNICODE_SYMBOLS = {
    "ok": "✓",
    "failed": "✗",
    "skipped": "–",
    "dash": "—",
    "ellipsis": "…",
}
_ASCII_SYMBOLS = {
    "ok": "OK",
    "failed": "FAIL",
    "skipped": "--",
    "dash": "-",
    "ellipsis": "...",
}


def _choose_symbols() -> Dict[str, str]:
    """Pick the progress glyph set stderr can actually encode.

    Returns:
        :data:`_UNICODE_SYMBOLS` when ``sys.stderr``'s encoding round-trips every
        glyph, otherwise :data:`_ASCII_SYMBOLS`.
    """
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        "".join(_UNICODE_SYMBOLS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return _ASCII_SYMBOLS
    return _UNICODE_SYMBOLS


#: Decided once, at import, so every progress line agrees with every other one.
SYMBOLS = _choose_symbols()

#: ``True`` when the glyphs above had to be downgraded; also drives tqdm's own
#: bar characters, which are box-drawing glyphs by default.
ASCII_ONLY = SYMBOLS is _ASCII_SYMBOLS


def encodable(text: str, stream: Any = None) -> str:
    """Return ``text`` with any character ``stream`` cannot encode replaced.

    Playlist and track titles are arbitrary user data: a Japanese playlist name
    on a cp1252 console would otherwise take the whole run down mid-progress --
    including in the final summary, long after the downloads succeeded.

    Args:
        text: Line about to be written.
        stream: Destination stream; defaults to ``sys.stderr``.

    Returns:
        The same string, or a lossy but printable version of it.
    """
    stream = sys.stderr if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return text.encode(encoding, "replace").decode(encoding, "replace")
    return text


def print_out(text: str) -> None:
    """Write one summary line to stdout, never crashing on the console encoding.

    Args:
        text: Line to print.
    """
    print(encodable(text, sys.stdout))


def print_err(text: str) -> None:
    """Write one diagnostic line to stderr, never crashing on the console encoding.

    Args:
        text: Line to print.
    """
    print(encodable(text, sys.stderr), file=sys.stderr)


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking the cut with an ellipsis.

    Args:
        text: String to shorten.
        limit: Maximum length of the result, including the marker.

    Returns:
        ``text`` unchanged when it already fits, else a truncated version.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def configure_logging(verbose: bool) -> None:
    """Route all log records to stderr so stdout carries only the summary.

    Args:
        verbose: When ``True``, log at DEBUG with logger names and levels shown;
            otherwise log bare messages at INFO.
    """
    # Log records carry playlist and track names straight from the network, and
    # logging has no per-record encoding guard: on a cp1252 console an unlucky
    # title would print "--- Logging error ---" and a traceback. Degrading the
    # stream's error handler once fixes every record at the source.
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass

    handler = logging.StreamHandler(stream=sys.stderr)
    if verbose:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        level = logging.DEBUG
    else:
        handler.setFormatter(logging.Formatter("%(message)s"))
        level = logging.INFO

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
