"""Loopback HTTP endpoint that lets the Spicetify extension queue a playlist.

The browser-side companion is a Spicetify extension running inside Spotify's
Electron shell. It cannot spawn processes, so it POSTs the current playlist's
URI here and this process does the downloading.

Security: loopback only, on purpose
-----------------------------------
There is **no authentication**. Anything that can reach this port can make the
machine download arbitrary content into the output folder, so the socket is
bound to ``127.0.0.1`` and nothing else. Binding ``0.0.0.0`` would hand every
device on the coffee-shop Wi-Fi an unauthenticated "download this" trigger and
a way to fill the disk. :func:`build_server` therefore *rejects* a non-loopback
host outright rather than trusting the caller to pass the right thing -- a
typo in a config file must not silently expose the endpoint.

``Access-Control-Allow-Origin: *`` is set because the Spicetify page is a
different origin. That is safe here only because of the bind: CORS controls
which *pages* may read the response, never which *hosts* may reach the port.

Concurrency: one job at a time
------------------------------
Requests are accepted immediately (**202 Accepted**) and the work is handed to
a single long-lived consumer thread reading a :class:`queue.Queue`. Two rapid
clicks queue up instead of starting two ``ThreadPoolExecutor`` pools that would
race each other over the same playlist folder and the same
``download_history.json``. The HTTP response never waits on a download, which
can take minutes; the queue depth is reported back so the UI can say
"3rd in line".

Testing
-------
:func:`build_server` returns a ready server without entering a blocking accept
loop, and :class:`PlaylistRequestHandler` is exported, so tests can bind port
``0`` and drive the real socket without :func:`serve_forever`.
"""

from __future__ import annotations

import errno
import json
import logging
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from models import (
    BITRATE_DEFAULT,
    DEFAULT_OUTPUT_DIR,
    MAX_WORKERS_DEFAULT,
    SERVER_HOST,
    SERVER_PORT,
    STATUS_FAILED,
    STATUS_OK,
)

__all__ = [
    "API_VERSION",
    "MAX_BODY_BYTES",
    "MAX_URI_LENGTH",
    "LOOPBACK_HOSTS",
    "PATH_ROOT",
    "PATH_HEALTH",
    "PATH_PLAYLIST",
    "JobWorker",
    "PlaylistRequestHandler",
    "PlaylistServer",
    "build_server",
    "serve_forever",
]

logger = logging.getLogger(__name__)

#: Reported by ``/health`` so the extension can refuse to talk to a build it
#: does not understand.
API_VERSION = 1

#: A queue request is a few dozen bytes of JSON. The cap exists so a bad (or
#: hostile) client cannot make the process buffer an unbounded body.
MAX_BODY_BYTES = 8 * 1024

#: Longest accepted playlist reference. Real Spotify URIs are ~40 characters.
MAX_URI_LENGTH = 2048

#: Hosts this server is willing to bind. See the module docstring.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

PATH_ROOT = "/"
PATH_HEALTH = "/health"
PATH_PLAYLIST = "/playlist"

#: Seconds a single connection may sit idle before its handler thread gives
#: up, so a half-open connection cannot pin a thread forever.
CONNECTION_TIMEOUT = 30.0

_USAGE = (
    "sc-scrape local server\n"
    "\n"
    "  POST /playlist   {\"uri\": \"spotify:playlist:<id>\"}  -> 202 queued\n"
    "  GET  /health                                       -> 200 status\n"
    "\n"
    "Bound to loopback only; there is no authentication.\n"
)


class _Stop(object):
    """Sentinel pushed onto the queue to end the consumer thread."""


class JobWorker(object):
    """Serialises playlist downloads onto one background thread.

    A single consumer is a deliberate choice, not a simplification:
    :func:`pipeline.run_job` is already internally concurrent, and two of them
    running at once would fan out twice the connections and interleave writes to
    the same folder's resume ledger. Queuing keeps the second click honest.
    """

    def __init__(
        self,
        output_dir=DEFAULT_OUTPUT_DIR,
        quality: str = BITRATE_DEFAULT,
        workers: int = MAX_WORKERS_DEFAULT,
        overwrite: bool = False,
        verbose: bool = False,
    ) -> None:
        self.output_dir = output_dir
        self.quality = quality
        self.workers = workers
        self.overwrite = overwrite
        self.verbose = verbose
        self._queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._active = 0
        self._done = 0
        self._failed = 0
        self._stopping = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the consumer thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._consume, name="playlist-worker", daemon=True
        )
        self._thread.start()
        logger.debug("server: worker thread started")

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the consumer to finish, waiting ``timeout`` seconds for it.

        Queued-but-unstarted jobs are dropped: they were never promised, and blocking
        Ctrl-C behind an hour of downloads is worse than losing a queue the user can
        re-add in one click. A job already in flight is left to finish or be killed
        with the process, since the thread is a daemon.
        """
        with self._lock:
            self._stopping = True
        dropped = self.drain()
        if dropped:
            logger.warning("server: dropped %d queued job(s) on shutdown", dropped)
        self._queue.put(_Stop)
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("server: worker still busy after %.1fs; leaving it", timeout)
            return False
        self._thread = None
        return True

    def drain(self) -> int:
        """Discard every queued job. Returns how many were thrown away."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            if item is not _Stop:
                dropped += 1
        return dropped

    # -- producer side ----------------------------------------------------

    def submit(self, uri: str) -> int:
        """Queue one playlist reference, returning its position in line.

        That count includes the job in flight and the one just added.
        """
        self._queue.put(uri)
        depth = self.pending()
        logger.info("server: queued %r (depth %d)", uri, depth)
        return depth

    def pending(self) -> int:
        """Jobs not yet finished: those waiting plus the one running.

        The stop sentinel is not a job, so it is discounted once shutdown has
        begun; otherwise ``/health`` would report a phantom backlog of one.
        """
        with self._lock:
            active = self._active
            stopping = self._stopping
        depth = self._queue.qsize() + active
        return max(0, depth - 1) if stopping else depth

    def stats(self) -> Dict[str, int]:
        with self._lock:
            stats = {
                "running": self._active,
                "completed": self._done,
                "failed": self._failed,
            }
        stats["queued"] = self.pending()
        return stats

    # -- consumer side ----------------------------------------------------

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _Stop:
                    logger.debug("server: worker stopping")
                    return
                with self._lock:
                    self._active += 1
                try:
                    self._run(item)
                finally:
                    with self._lock:
                        self._active -= 1
            finally:
                self._queue.task_done()

    def _run(self, uri: str) -> None:
        """Resolve and download one entry, swallowing every failure.

        Nothing here may raise: an exception escaping would kill the only
        consumer thread and silently turn the server into a black hole that
        accepts requests and never downloads anything.
        """
        # Imported lazily: ``downloader`` hard-imports yt-dlp, so a module-level
        # import would make this server unimportable (and untestable) without
        # it, and would slow every start.
        try:
            from downloader import ensure_output_dir
            from pipeline import run_job
            from playlist_parser import resolve_input
        except Exception as exc:  # noqa: BLE001 - missing dependency, not a crash
            logger.error("server: cannot run jobs (%s: %s)", type(exc).__name__, exc)
            with self._lock:
                self._failed += 1
            return

        logger.info("server: starting %r", uri)
        try:
            output_dir = ensure_output_dir(self.output_dir)
            job = resolve_input(uri)
            results = list(
                run_job(
                    job,
                    output_dir,
                    quality=self.quality,
                    workers=self.workers,
                    overwrite=self.overwrite,
                    verbose=self.verbose,
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001 - one bad request != dead worker
            logger.error(
                "server: job %r failed (%s: %s)", uri, type(exc).__name__, exc,
                exc_info=self.verbose,
            )
            with self._lock:
                self._failed += 1
            return

        downloaded = sum(1 for item in results if getattr(item, "status", None) == STATUS_OK)
        failed = sum(1 for item in results if getattr(item, "status", None) == STATUS_FAILED)
        logger.info(
            "server: finished %r -- '%s': %d downloaded, %d failed, %d total",
            uri, getattr(job, "name", "?"), downloaded, failed, len(results),
        )
        with self._lock:
            self._done += 1
            if failed:
                self._failed += failed


class PlaylistRequestHandler(BaseHTTPRequestHandler):
    """Tiny JSON API in front of a :class:`JobWorker`.

    Routes:
        ``GET /``          plain-text usage blurb.
        ``GET /health``    ``{"status": "ok", "queued": N, "version": 1}``.
        ``POST /playlist`` ``{"uri": ...}`` (``url`` also accepted) -> 202.
        ``OPTIONS *``      CORS preflight.

    Everything else is a 404 with a JSON body, so a client that only knows how
    to parse JSON never chokes on the default HTML error page.
    """

    server_version = "sc-scrape/%d" % API_VERSION
    #: HTTP/1.0 keeps this connection-per-request, which sidesteps keep-alive
    #: bookkeeping entirely for a client that makes one call per click.
    protocol_version = "HTTP/1.0"
    timeout = CONNECTION_TIMEOUT

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route the base class's stderr access log into the module logger.

        The default implementation writes straight to ``sys.stderr``, which
        interleaves badly with the CLI's own progress output and cannot be
        silenced. DEBUG, because one line per request is noise at INFO.
        """
        logger.debug("server: %s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        """Send handler-level errors to the logger too, at DEBUG.

        These are mostly clients hanging up mid-request, which is not an
        operator-visible problem.
        """
        logger.debug("server: %s - %s", self.address_string(), format % args)

    @property
    def worker(self) -> Optional[JobWorker]:
        return getattr(self.server, "worker", None)

    def _cors_headers(self) -> None:
        """Emit the CORS headers the Spicetify (Electron) origin needs."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        """Write a complete response, tolerating a client that hung up."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("server: client disconnected before the response was sent")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_error_json(self, status: int, message: str) -> None:
        logger.debug("server: %s %s -> %s (%s)", self.command, self.path, status, message)
        self._send_json(status, {"status": "error", "error": message})

    def _route(self) -> str:
        return urlparse(self.path or "/").path or "/"

    # -- verbs ------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        """Answer the CORS preflight for any path."""
        try:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors_headers()
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("server: client disconnected during preflight")

    def do_GET(self) -> None:  # noqa: N802
        """Serve ``/`` and ``/health``; 404 anything else."""
        route = self._route()
        if route == PATH_HEALTH:
            worker = self.worker
            payload: Dict[str, Any] = {
                "status": "ok",
                "queued": worker.pending() if worker is not None else 0,
                "version": API_VERSION,
            }
            if worker is not None:
                payload.update(worker.stats())
                payload["status"] = "ok"
            self._send_json(HTTPStatus.OK, payload)
            return
        if route == PATH_ROOT:
            self._send_bytes(HTTPStatus.OK, "text/plain; charset=utf-8",
                             _USAGE.encode("utf-8"))
            return
        self._send_error_json(
            HTTPStatus.NOT_FOUND,
            "no such endpoint: GET %s (try GET /health or POST /playlist)" % route,
        )

    def do_HEAD(self) -> None:  # noqa: N802
        """Same routing as GET, without a body (handled by ``_send_bytes``)."""
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        """Queue a playlist on ``/playlist``; 404 anything else."""
        route = self._route()
        if route != PATH_PLAYLIST:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "no such endpoint: POST %s (the only POST route is %s)"
                % (route, PATH_PLAYLIST),
            )
            return

        payload, error = self._read_json_body()
        if error is not None:
            status, message = error
            self._send_error_json(status, message)
            return

        uri, message = _extract_uri(payload)
        if uri is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST, message or "invalid request")
            return

        worker = self.worker
        if worker is None:  # pragma: no cover - only a misbuilt server hits this
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE, "no download worker is attached"
            )
            return

        depth = worker.submit(uri)
        self._send_json(
            HTTPStatus.ACCEPTED,
            {"status": "queued", "uri": uri, "queued": depth, "version": API_VERSION},
        )

    # -- body parsing -----------------------------------------------------

    def _read_json_body(
        self,
    ) -> Tuple[Optional[Any], Optional[Tuple[int, str]]]:
        """Read and decode the request body.

        Returns ``(payload, None)`` on success, or ``(None, (status, message))``
        describing exactly what was wrong -- a client-side bug should read as a
        sentence, not as a stack trace in the server log.
        """
        content_type = (self.headers.get("Content-Type") or "").lower()
        if content_type and "json" not in content_type:
            return None, (
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json, got %r"
                % (self.headers.get("Content-Type"),),
            )

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None, (HTTPStatus.LENGTH_REQUIRED, "Content-Length header is required")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return None, (
                HTTPStatus.BAD_REQUEST,
                "Content-Length %r is not a number" % (raw_length,),
            )
        if length < 0:
            return None, (HTTPStatus.BAD_REQUEST, "Content-Length must not be negative")
        if length == 0:
            return None, (HTTPStatus.BAD_REQUEST, 'request body is empty; expected {"uri": "..."}')
        if length > MAX_BODY_BYTES:
            # Refused without reading, so an oversized body is never buffered.
            return None, (
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request body of %d bytes exceeds the %d byte limit"
                % (length, MAX_BODY_BYTES),
            )

        try:
            raw = self.rfile.read(length)
        except (ConnectionError, OSError) as exc:
            return None, (HTTPStatus.BAD_REQUEST, "could not read the request body: %s" % exc)
        if len(raw) != length:
            return None, (
                HTTPStatus.BAD_REQUEST,
                "request body was truncated (%d of %d bytes)" % (len(raw), length),
            )

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, (HTTPStatus.BAD_REQUEST, "request body is not valid UTF-8: %s" % exc)

        try:
            return json.loads(text), None
        except ValueError as exc:
            return None, (
                HTTPStatus.BAD_REQUEST,
                'request body is not valid JSON (%s); expected {"uri": "spotify:playlist:..."}'
                % exc,
            )


def _extract_uri(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """Pull and validate the playlist reference out of a decoded body.

    Accepts ``uri`` or ``url`` because the Spicetify API hands out both a
    ``spotify:playlist:...`` URI and an ``open.spotify.com`` URL depending on which
    menu the user clicked. Returns ``(uri, None)`` when valid, else
    ``(None, message)``.
    """
    if not isinstance(payload, dict):
        return None, 'request body must be a JSON object, e.g. {"uri": "spotify:playlist:..."}'

    value = payload.get("uri")
    if value is None:
        value = payload.get("url")
    if value is None:
        return None, 'missing "uri" (or "url") field'
    if not isinstance(value, str):
        return None, '"uri" must be a string, got %s' % type(value).__name__

    uri = value.strip()
    if not uri:
        return None, '"uri" must not be empty'
    if len(uri) > MAX_URI_LENGTH:
        return None, '"uri" is longer than %d characters' % MAX_URI_LENGTH
    # Control characters would end up in log lines and yt-dlp search
    # expressions; neither has any business containing a newline.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in uri):
        return None, '"uri" must not contain control characters'
    return uri, None


class PlaylistServer(ThreadingHTTPServer):
    """A :class:`~http.server.ThreadingHTTPServer` that owns a worker thread."""

    daemon_threads = True
    #: Without this, a restart within the TIME_WAIT window fails to bind.
    allow_reuse_address = True

    def __init__(self, address, handler_class, worker: JobWorker) -> None:
        self.worker = worker
        ThreadingHTTPServer.__init__(self, address, handler_class)

    def server_close(self) -> None:
        """Close the socket and stop the worker, in that order."""
        try:
            ThreadingHTTPServer.server_close(self)
        finally:
            if self.worker is not None:
                self.worker.stop()


def build_server(
    host: str = SERVER_HOST,
    port: int = SERVER_PORT,
    output_dir=DEFAULT_OUTPUT_DIR,
    quality: str = BITRATE_DEFAULT,
    workers: int = MAX_WORKERS_DEFAULT,
    overwrite: bool = False,
    verbose: bool = False,
    start_worker: bool = True,
) -> PlaylistServer:
    """Bind the HTTP server and start its worker, without serving.

    Split out of :func:`serve_forever` so tests (and any future GUI) can drive a
    real socket on port ``0`` without entering a blocking accept loop; read the
    chosen port back from ``server.server_address[1]``. Call ``server_close()``
    when done -- it stops the worker for you.

    A non-loopback ``host`` raises ``ValueError``. That is a refusal, not a
    fallback: the endpoint is unauthenticated, and quietly binding something else
    would turn it into a remote-triggerable downloader.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "refusing to bind %r: this endpoint has no authentication, so it "
            "must stay on loopback (%s). Anything else would let every host on "
            "the network trigger downloads on this machine."
            % (host, ", ".join(sorted(LOOPBACK_HOSTS)))
        )

    worker = JobWorker(
        output_dir=output_dir,
        quality=quality,
        workers=workers,
        overwrite=overwrite,
        verbose=verbose,
    )
    # A bind failure propagates as OSError; ``serve_forever`` turns EADDRINUSE
    # into a sentence. The worker has no thread yet, so nothing leaks.
    server = PlaylistServer((host, int(port)), PlaylistRequestHandler, worker)
    if start_worker:
        worker.start()
    return server


def serve_forever(
    host: str = SERVER_HOST,
    port: int = SERVER_PORT,
    output_dir=DEFAULT_OUTPUT_DIR,
    quality: str = BITRATE_DEFAULT,
    workers: int = MAX_WORKERS_DEFAULT,
    overwrite: bool = False,
    verbose: bool = False,
) -> int:
    """Run the local endpoint until Ctrl-C.

    Returns 0 on a clean shutdown, including Ctrl-C, and 1 if the socket could not
    be bound -- an address already in use is reported as one sentence naming the
    port, not as a traceback.
    """
    try:
        server = build_server(
            host=host,
            port=port,
            output_dir=output_dir,
            quality=quality,
            workers=workers,
            overwrite=overwrite,
            verbose=verbose,
        )
    except ValueError as exc:
        logger.error("server: %s", exc)
        return 1
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            logger.error(
                "server: port %s on %s is already in use -- another copy of this "
                "server is probably running. Stop it, or start this one on a "
                "different port.",
                port, host,
            )
        elif exc.errno == errno.EACCES:
            logger.error(
                "server: not allowed to bind port %s (ports below 1024 need "
                "elevated privileges); pick a port above 1024.", port,
            )
        else:
            logger.error("server: could not bind %s:%s: %s", host, port, exc)
        return 1

    bound_host, bound_port = server.server_address[0], server.server_address[1]
    logger.info(
        "server: listening on http://%s:%s (loopback only, no authentication)",
        bound_host, bound_port,
    )
    logger.info("server: POST /playlist {\"uri\": \"spotify:playlist:<id>\"} to queue")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Stopped.")
    finally:
        # Deliberately *not* ``server.shutdown()``: that call exists to stop the
        # accept loop from a *different* thread, and it blocks until the loop
        # signals that it has exited. Called here -- on the very thread that
        # just left the loop -- it is at best a no-op and, if ``serve_forever``
        # never got as far as its own loop, an unbreakable deadlock.
        # ``server_close`` closes the socket and stops the worker, dropping
        # anything still queued.
        server.server_close()
    return 0
