"""Turn whatever the user typed into a :class:`~models.PlaylistJob`.

Three input shapes are accepted, distinguished by :func:`detect_input_kind`:

``spotify``
    ``spotify:playlist:<id>`` URIs and ``open.spotify.com/playlist/<id>`` URLs
    (including locale-prefixed ones such as ``/intl-de/playlist/<id>``).
    Resolved through the official Web API with spotipy. Spotify does not expose
    audio, so every resulting :class:`~models.Track` carries a ``ytsearchN:``
    query that the downloader later matches against YouTube using
    :func:`pick_best_match`.
``youtube``
    Any http(s) URL on ``youtube.com`` / ``youtu.be``. Delegated wholesale to
    :func:`resolver.expand_playlist`.
``name``
    Anything else: a bare playlist name, looked up on YouTube.

Spotify now gates Web API access behind Premium, so credentials are the
exception rather than the rule: when they are configured the API still wins
(it sees whole playlists and real durations), but when they are missing the
public page is read instead by :mod:`spotify_scrape`, which validates its own
row count and fails loudly rather than returning a partial playlist.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from models import (
    youtube_extractor_args,
    DURATION_TOLERANCE,
    SEARCH_CANDIDATES,
    SOURCE_SEARCH,
    SOURCE_SPOTIFY,
    SOURCE_YOUTUBE,
    PlaylistJob,
    Track,
)

__all__ = [
    "SpotifyCredentialsError",
    "BAD_MATCH_RE",
    "KIND_SPOTIFY",
    "KIND_YOUTUBE",
    "KIND_NAME",
    "detect_input_kind",
    "parse_spotify_playlist_id",
    "parse_spotify_ref",
    "KIND_PLAYLIST",
    "KIND_ALBUM",
    "build_search_query",
    "pick_best_match",
    "match_track",
    "resolve_input",
]

logger = logging.getLogger(__name__)


KIND_SPOTIFY = "spotify"
KIND_YOUTUBE = "youtube"
KIND_NAME = "name"

#: Spotify ids are 22 characters of base62.
_SPOTIFY_ID = r"[A-Za-z0-9]{22}"

#: ``spotify:playlist:EXAMPLEPLAYLISTID00000`` (optionally ``spotify:user:x:playlist:id``).
_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(?:user:[^:]+:)?(playlist|album):(" + _SPOTIFY_ID + r")$",
    re.IGNORECASE,
)

#: ``https://open.spotify.com/intl-de/playlist/EXAMPLEPLAYLISTID00000?si=...``
#: The optional ``intl-de``/``intl-pt-br`` segment is a locale prefix Spotify
#: started adding in 2023; embed links use ``/embed/playlist/<id>``.
_SPOTIFY_URL_RE = re.compile(
    r"^(?:https?://)?(?:open|play)\.spotify\.com"
    r"(?:/intl-[A-Za-z-]+)?"
    r"(?:/embed)?"
    r"(?:/user/[^/]+)?"
    r"/(playlist|album)/(" + _SPOTIFY_ID + r")\b",
    re.IGNORECASE,
)

#: Spotify collection kinds this module can expand.
KIND_PLAYLIST = "playlist"
KIND_ALBUM = "album"

_YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:[A-Za-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com|youtu\.be)(?:/|$)",
    re.IGNORECASE,
)

_LIST_PARAM_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


#: Titles that almost never correspond to the studio recording the user asked
#: for. Applied to *candidates* only, and skipped when the track's own title
#: matches the same pattern -- a song genuinely called "Song (Live)" or
#: "Remix" must still be findable. Kept as a module constant so it is testable
#: and tunable without touching the matching logic.
BAD_MATCH_RE = re.compile(
    r"("
    r"\blive\b|\bao\s+vivo\b|\bin\s+concert\b|\bconcert\b|\bunplugged\b"
    r"|\bcovers?\b|\bcovered\s+by\b|\btribute\b"
    r"|\bremix(?:es|ed)?\b|\bbootleg\b|\bmashup\b|\bmash-?up\b|\bflip\b|\bedit\b|\bvip\s+mix\b"
    r"|\breaction\b|\breacts?\s+to\b|\breview\b"
    r"|\bkaraoke\b|\bsing\s*-?\s*along\b|\bbacking\s+track\b"
    r"|\binstrumental\b|\bacapella\b|\ba\s+cappella\b"
    r"|\bsped\s*-?\s*up\b|\bspedup\b|\bspeed\s*-?\s*up\b|\bslowed\b|\breverb\b"
    r"|\bnightcore\b|\bdaycore\b"
    r"|\b8\s*d\s+audio\b|\b8d\b"
    r"|\b(?:1|one|2|two|3|three|10|ten)\s*hours?\b"
    r"|\bloops?\b|\bloope?d\b"
    r"|\bextended\s+mix\b|\bcompilation\b|\bmegamix\b|\bmix\s*\d+\b"
    r"|\blyrics?\s+video\b|\bfull\s+album\b"
    r")",
    re.IGNORECASE,
)

#: Characters that confuse yt-dlp's ``ytsearchN:`` expression or the shell-ish
#: quoting some extractors still apply to it.
_QUERY_STRIP_RE = re.compile(r"[\"'‘’“”\r\n\t\\]+")
_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: Spotify caps ``playlist_items`` at 100 per request.
_PAGE_LIMIT = 100

#: ``album_tracks`` caps at 50, not 100.
_ALBUM_PAGE_LIMIT = 50

#: Hard stop so a misbehaving API/mock can never spin forever.
_MAX_PAGES = 200


class SpotifyCredentialsError(RuntimeError):
    """Raised when the Spotify API credentials are absent from the environment.

    Carries the full setup instructions in its message so the CLI can print
    ``str(exc)`` verbatim and the user needs no further documentation.
    """

    MESSAGE = (
        "Spotify playlists need API credentials, and none were found.\n"
        "\n"
        "Set both SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET in your "
        "environment.\n"
        "Create a free app at https://developer.spotify.com/dashboard "
        "(Create app -> copy the Client ID and Client secret), then run:\n"
        "\n"
        '    export SPOTIPY_CLIENT_ID="your-client-id"\n'
        '    export SPOTIPY_CLIENT_SECRET="your-client-secret"\n'
        "\n"
        "Without them only spotify_scrape's public-page reader is available, "
        "and it can see no more than the first 30 rows of a playlist."
    )

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message or self.MESSAGE)


def detect_input_kind(text: str) -> str:
    """Classify a user-supplied playlist reference as spotify, youtube or name.

    Empty input is classified as a name, so the caller reports "no tracks found"
    rather than crashing.
    """
    cleaned = _clean_input(text)
    if not cleaned:
        return KIND_NAME
    if _SPOTIFY_URI_RE.match(cleaned) or _SPOTIFY_URL_RE.match(cleaned):
        return KIND_SPOTIFY
    if _YOUTUBE_URL_RE.match(cleaned):
        return KIND_YOUTUBE
    return KIND_NAME


def parse_spotify_ref(text: str) -> Tuple[str, str]:
    """Extract the collection kind and 22-character id from a Spotify link.

    Handles both playlists and albums, because they are different endpoints:
    playlists page at 100 items and carry per-item album metadata, whereas albums
    page at 50 and hold the album name and cover art on the container object
    rather than on each track. A bare 22-char id is treated as a playlist.

    Raises ``ValueError`` for anything unrecognisable, with a message showing the
    accepted forms.
    """
    cleaned = _clean_input(text)
    match = _SPOTIFY_URI_RE.match(cleaned) or _SPOTIFY_URL_RE.match(cleaned)
    if match:
        return match.group(1).lower(), match.group(2)
    if re.match(r"^" + _SPOTIFY_ID + r"$", cleaned):
        return KIND_PLAYLIST, cleaned
    raise ValueError(
        "%r is not a Spotify playlist or album reference. Expected one of "
        "'spotify:playlist:<22-char-id>', 'spotify:album:<22-char-id>', "
        "'https://open.spotify.com/playlist/<22-char-id>' or "
        "'https://open.spotify.com/album/<22-char-id>' "
        "(individual track links are not collections)." % (text,)
    )


def parse_spotify_playlist_id(text: str) -> str:
    """Extract the id from a Spotify playlist reference, rejecting albums.

    Kept for callers that specifically want a playlist. Prefer
    :func:`parse_spotify_ref`, which also accepts albums.
    """
    kind, ref_id = parse_spotify_ref(text)
    if kind != KIND_PLAYLIST:
        raise ValueError(
            "%r is a Spotify %s, not a playlist. Use parse_spotify_ref() to "
            "accept both." % (text, kind)
        )
    return ref_id


def _clean_input(text: str) -> str:
    """Trim whitespace and the ``<...>`` wrapping some chat clients add."""
    if not text:
        return ""
    return str(text).strip().strip("<>").strip()


def build_search_query(title: str, artist: Optional[str]) -> str:
    """Build the ``ytsearchN:`` expression used to find a track on YouTube.

    The trailing ``audio`` biases YouTube away from music videos, which are often
    padded with intros and so fail the duration check. With no artist, that half
    of the query is omitted entirely.
    """
    clean_title = _sanitise_query_part(title)
    clean_artist = _sanitise_query_part(artist) if artist else ""
    if clean_artist and clean_title:
        terms = "%s - %s" % (clean_artist, clean_title)
    else:
        terms = clean_title or clean_artist
    return "ytsearch%d:%s" % (SEARCH_CANDIDATES, terms.strip())


def _sanitise_query_part(value: Optional[str]) -> str:
    """Drop quotes/newlines/backslashes and collapse whitespace."""
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", _QUERY_STRIP_RE.sub(" ", str(value))).strip()


def _normalise(value: Optional[str]) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE_RE.sub(" ", _PUNCT_RE.sub(" ", stripped.lower())).strip()


def _candidate_seconds(candidate: Dict[str, Any]) -> Optional[float]:
    """Read a candidate's duration in seconds, tolerating None/''/0/strings."""
    raw = candidate.get("duration")
    if raw is None or raw == "":
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# YouTube auto-generates an "<Artist> - Topic" channel carrying the official
# audio for every track a label distributes, which is exactly what a Spotify
# tracklist refers to. Preferring it is the single biggest matching win: it
# skips fan reuploads, contest compilations and hour-long mixes that a title
# filter alone lets through. Scraped Spotify metadata carries no durations, so
# without this the duration guard cannot run and nothing else catches them.
def is_topic_upload(candidate: Dict[str, Any]) -> bool:
    """Whether a search hit comes from an auto-generated Topic channel."""
    for key in ("channel", "uploader", "channel_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip().lower().endswith("- topic"):
            return True
    return False


def pick_best_match(candidates: List[dict], track: Track) -> Optional[dict]:
    """Choose the YouTube search hit that is really ``track``, or nothing.

    This is the wrong-match guard. YouTube search for a song title returns
    hour-long loops, sped-up edits, karaoke versions and reactions above the real
    thing often enough that taking hit #1 blindly produces a folder full of junk.
    Filters are applied in order, then survivors are ranked.

    Mind the units: a candidate's ``duration`` is in seconds, while
    ``track.duration_ms`` is in milliseconds. ``None`` there disables the duration
    filter. Returning ``None`` makes the caller record "no acceptable match"
    rather than download something wrong.
    """
    if not candidates:
        return None

    want_seconds = None  # type: Optional[float]
    if track.duration_ms:
        want_seconds = float(track.duration_ms) / 1000.0
        if want_seconds <= 0:
            want_seconds = None

    # A track that is itself a live take / remix / instrumental must not be
    # filtered out by the pattern that describes it.
    track_is_special = bool(BAD_MATCH_RE.search(track.title or ""))
    want_norm = _normalise(track.title)

    scored = []  # type: List[Tuple[int, float, int, Dict[str, Any]]]
    for rank, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        cand_title = candidate.get("title") or ""

        cand_seconds = _candidate_seconds(candidate)
        delta = float("inf")
        if want_seconds is not None and cand_seconds is not None:
            delta = abs(cand_seconds - want_seconds)
            if delta > want_seconds * DURATION_TOLERANCE:
                logger.debug(
                    "rejected %r for %r: %.0fs vs %.0fs",
                    cand_title, track.title, cand_seconds, want_seconds,
                )
                continue

        if not track_is_special:
            bad = BAD_MATCH_RE.search(cand_title)
            if bad:
                logger.debug(
                    "rejected %r for %r: matched %r",
                    cand_title, track.title, bad.group(0),
                )
                continue

        # Topic uploads outrank everything: they are the label's own audio for
        # this exact track, so a Topic hit beats a closer-titled fan reupload.
        topic = 0 if is_topic_upload(candidate) else 1
        contains = 0 if (want_norm and want_norm in _normalise(cand_title)) else 1

        # Title mismatch with nothing to verify it against: ranking would still
        # hand back its least-bad guess, which is how one track once matched an
        # unrelated song. A missing track is honest, a wrong one is not. Being
        # an official Topic upload does NOT rescue it: Topic proves the upload
        # is the label's, not that it is the right song -- a Topic track from an
        # unrelated album once won on that basis alone.
        if contains and delta == float("inf"):
            logger.debug(
                "rejected %r for %r: unverifiable and the title does not match",
                cand_title, track.title,
            )
            continue

        scored.append((topic, contains, delta, rank, candidate))

    if not scored:
        logger.debug("no acceptable match for %r among %d candidates",
                     track.display(), len(candidates))
        return None

    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return scored[0][4]


def _spotify_client():
    """Build an authenticated spotipy client from the environment.

    Client-credentials flow, which is enough for public playlists and needs no
    user login. The credentials are checked *before* the client is constructed, so
    a missing one yields setup instructions rather than a spotipy stack trace.
    """
    client_id = (os.environ.get("SPOTIPY_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SPOTIPY_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        missing = [
            name
            for name, value in (
                ("SPOTIPY_CLIENT_ID", client_id),
                ("SPOTIPY_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        logger.error("missing Spotify credentials: %s", ", ".join(missing))
        raise SpotifyCredentialsError()

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
    except ImportError as exc:  # pragma: no cover - install-time problem
        raise RuntimeError(
            "spotipy is required for Spotify playlists: pip install spotipy"
        ) from exc

    auth = SpotifyClientCredentials(
        client_id=client_id, client_secret=client_secret
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=3)


def _spotify_error(exc: Exception, playlist_id: str) -> RuntimeError:
    """Translate a ``spotipy.SpotifyException`` into a user-facing error."""
    status = getattr(exc, "http_status", None)
    detail = getattr(exc, "msg", None) or str(exc)
    if status == 401:
        hint = (
            "bad or expired credentials -- re-check SPOTIPY_CLIENT_ID and "
            "SPOTIPY_CLIENT_SECRET"
        )
    elif status == 403:
        hint = "access forbidden -- the app may not be allowed to read this playlist"
    elif status == 404:
        hint = "playlist not found, or it is private/deleted"
    elif status == 429:
        hint = "rate limited by Spotify -- wait a moment and retry"
    else:
        hint = "Spotify API request failed"
    return RuntimeError(
        "Spotify playlist %s: HTTP %s, %s (%s)"
        % (playlist_id, status if status is not None else "?", hint, detail)
    )


def _largest_image_url(images: Any) -> Optional[str]:
    """Pick the highest-resolution cover URL from a Spotify ``images`` array.

    Spotify normally returns images largest-first, but empty arrays, missing
    ``width``/``height`` and null entries all occur in the wild, so the array
    is scored explicitly and falls back to document order on a tie.
    """
    if not isinstance(images, (list, tuple)):
        return None
    best = None  # type: Optional[Tuple[int, int, str]]
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not url:
            continue
        width = image.get("width") or 0
        height = image.get("height") or 0
        try:
            area = int(width) * int(height)
        except (TypeError, ValueError):
            area = 0
        # Negative index so that, at equal area, the earlier entry wins.
        key = (area, -index, url)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def _track_from_spotify(item: Any) -> Optional[Track]:
    """Convert one ``playlist_items`` entry into a :class:`~models.Track`.

    Returns ``None`` for entries that carry no playable track: removed or
    region-unavailable songs (``track`` is null), podcast episodes
    (``track.type != "track"``) and nameless payloads.
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("track")
    if not isinstance(raw, dict):
        return None  # removed track, or a null placeholder

    # ``type`` is absent from a few historic payloads; absent means track.
    kind = raw.get("type")
    if kind is not None and kind != "track":
        logger.debug("skipping non-track item of type %r", kind)
        return None

    title = (raw.get("name") or "").strip()
    if not title:
        logger.debug("skipping item with no track name")
        return None

    artist = None
    artists = raw.get("artists")
    if isinstance(artists, (list, tuple)):
        for entry in artists:
            if isinstance(entry, dict) and entry.get("name"):
                artist = entry["name"]
                break

    album_name = None
    cover_url = None
    album = raw.get("album")
    if isinstance(album, dict):
        album_name = album.get("name") or None
        cover_url = _largest_image_url(album.get("images"))

    duration_ms = raw.get("duration_ms")
    try:
        duration_ms = int(duration_ms) if duration_ms else None
    except (TypeError, ValueError):
        duration_ms = None

    return Track(
        query=build_search_query(title, artist),
        title=title,
        artist=artist,
        album=album_name,
        cover_url=cover_url,
        duration_ms=duration_ms,
        source_id=raw.get("id") or None,
    )


def _spotify_album_job(album_id: str, text: str) -> PlaylistJob:
    """Resolve a Spotify album into a :class:`~models.PlaylistJob`.

    Albums are a separate endpoint from playlists, and differ in two ways that
    matter here: ``album_tracks`` returns *simplified* track objects with no
    nested ``album`` block, and it pages at 50 rather than 100. The album name and
    cover art therefore come from the container object and are stamped onto every
    track, so each MP3 still gets a correct ``TALB`` frame and cover.
    """
    client = _spotify_client()

    try:
        import spotipy
        spotify_exception = spotipy.SpotifyException
    except ImportError:  # pragma: no cover
        spotify_exception = ()

    try:
        meta = client.album(album_id)
    except spotify_exception as exc:
        raise _spotify_error(exc, album_id) from exc

    album_name = None
    cover_url = None
    album_artist = None
    if isinstance(meta, dict):
        album_name = (meta.get("name") or "").strip() or None
        cover_url = _largest_image_url(meta.get("images"))
        artists = meta.get("artists")
        if isinstance(artists, (list, tuple)):
            for entry in artists:
                if isinstance(entry, dict) and entry.get("name"):
                    album_artist = entry["name"]
                    break
    name = album_name or "Spotify album %s" % album_id

    tracks = []  # type: List[Track]
    offset = 0
    total = None  # type: Optional[int]
    skipped = 0
    for _ in range(_MAX_PAGES):
        try:
            page = client.album_tracks(
                album_id, limit=_ALBUM_PAGE_LIMIT, offset=offset
            )
        except spotify_exception as exc:
            raise _spotify_error(exc, album_id) from exc

        if not isinstance(page, dict):
            break
        items = page.get("items") or []
        if total is None:
            try:
                total = int(page["total"])
            except (KeyError, TypeError, ValueError):
                total = None

        for raw in items:
            # Simplified album tracks are bare track objects; wrap them so the
            # playlist item parser can be reused verbatim.
            track = _track_from_spotify({"track": raw})
            if track is None:
                skipped += 1
                continue
            # The album block is absent on simplified objects, so supply it.
            tracks.append(
                replace(
                    track,
                    album=track.album or album_name,
                    cover_url=track.cover_url or cover_url,
                    artist=track.artist or album_artist,
                )
            )

        offset += len(items)
        if not items:
            break
        if total is not None:
            if offset >= total:
                break
        elif len(items) < _ALBUM_PAGE_LIMIT:
            break
    else:  # pragma: no cover - only a runaway API reaches this
        logger.warning(
            "stopped paging album %s after %d pages", album_id, _MAX_PAGES
        )

    logger.info(
        "spotify album %r: %d tracks (%d unusable items skipped)",
        name, len(tracks), skipped,
    )
    return PlaylistJob(
        name=name, tracks=tracks, source=SOURCE_SPOTIFY, origin=text
    )


def _spotify_job(text: str) -> PlaylistJob:
    """Resolve a Spotify playlist reference into a :class:`~models.PlaylistJob`.

    Each track carries a ``ytsearchN:`` query plus full metadata. A ``RuntimeError``
    from a failed API call names the playlist id and the HTTP status.
    """
    playlist_id = parse_spotify_playlist_id(text)
    client = _spotify_client()

    try:
        import spotipy
        spotify_exception = spotipy.SpotifyException
    except ImportError:  # pragma: no cover
        spotify_exception = ()

    try:
        meta = client.playlist(playlist_id, fields="name")
    except spotify_exception as exc:
        raise _spotify_error(exc, playlist_id) from exc
    name = None
    if isinstance(meta, dict):
        name = (meta.get("name") or "").strip() or None
    name = name or "Spotify playlist %s" % playlist_id

    tracks = []  # type: List[Track]
    offset = 0
    total = None  # type: Optional[int]
    skipped = 0
    for _ in range(_MAX_PAGES):
        try:
            page = client.playlist_items(
                playlist_id,
                limit=_PAGE_LIMIT,
                offset=offset,
                additional_types=("track",),
            )
        except spotify_exception as exc:
            raise _spotify_error(exc, playlist_id) from exc

        if not isinstance(page, dict):
            break
        items = page.get("items") or []
        if total is None:
            try:
                total = int(page["total"])
            except (KeyError, TypeError, ValueError):
                total = None

        for item in items:
            track = _track_from_spotify(item)
            if track is None:
                skipped += 1
            else:
                tracks.append(track)

        offset += len(items)
        if not items:
            break
        if total is not None:
            if offset >= total:
                break
        elif len(items) < _PAGE_LIMIT:
            break
    else:  # pragma: no cover - only a runaway API reaches this
        logger.warning("stopped paging playlist %s after %d pages",
                       playlist_id, _MAX_PAGES)

    logger.info("spotify playlist %r: %d tracks (%d unusable items skipped)",
                name, len(tracks), skipped)
    return PlaylistJob(
        name=name, tracks=tracks, source=SOURCE_SPOTIFY, origin=text
    )


def _spotify_any_job(text: str) -> PlaylistJob:
    """Resolve a Spotify album or playlist, by scraping first, API second.

    Scraping leads because it needs no credentials and Spotify now gates Web
    API access behind Premium, so for most users the API path cannot run at
    all. The API is still tried when credentials happen to be present: it is
    strictly better data (real durations, per-track artists, whole playlists
    past the first page).
    """
    from spotify_scrape import ScrapeError, scrape_spotify

    kind, ref_id = parse_spotify_ref(text)
    try:
        return scrape_spotify(text)
    except ScrapeError as scrape_exc:
        if not _spotify_credentials_present():
            # Re-raise the scraper's own reason. Pointing the user at the
            # developer dashboard would be bad advice: the keys they'd create
            # cannot reach the Web API without Premium.
            raise
        logger.debug("scrape of %s %s failed, trying the API: %s", kind, ref_id, scrape_exc)
        if kind == KIND_ALBUM:
            return _spotify_album_job(ref_id, text)
        return _spotify_job(text)


def _spotify_credentials_present() -> bool:
    """Whether both Spotify API env vars are set to something non-blank."""
    return all(
        (os.environ.get(name) or "").strip()
        for name in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET")
    )


def _fallback_name_from_url(url: str) -> str:
    """Derive a folder-worthy name from a YouTube URL when no title is known."""
    match = _LIST_PARAM_RE.search(url or "")
    if match:
        return "YouTube playlist %s" % match.group(1)
    tail = (url or "").rstrip("/").split("/")[-1].split("?")[0]
    return "YouTube %s" % tail if tail else "YouTube playlist"


def _youtube_job(text: str) -> PlaylistJob:
    """Expand a YouTube URL via :func:`resolver.expand_playlist`.

    Per-entry failures the resolver reports are logged, not raised: a playlist
    with three private videos should still download the rest.
    """
    from resolver import expand_playlist

    title, tracks, failures = expand_playlist(text)
    for failure in failures or []:
        logger.warning("skipped %s: %s",
                       getattr(failure, "url", "?"),
                       getattr(failure, "reason", failure))
    name = (title or "").strip() or _fallback_name_from_url(text)
    return PlaylistJob(
        name=name, tracks=list(tracks or []), source=SOURCE_YOUTUBE, origin=text
    )


#: YouTube's "playlist" search filter (``sp`` parameter, EgIQAw%3D%3D). Using
#: the results page rather than ``ytsearchN:`` is what makes a *playlist*
#: rather than a video come back.
_PLAYLIST_SEARCH_URL = (
    "https://www.youtube.com/results?search_query=%s&sp=EgIQAw%%3D%%3D"
)


def _flat_search(query: str, limit: int) -> List[Dict[str, Any]]:
    """Run a flat (metadata-only) yt-dlp extraction and return its entries.

    Isolated in its own function so tests can patch it without touching the
    network, and so a missing/broken yt-dlp degrades to "nothing found"
    instead of an exception.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:  # pragma: no cover - install-time problem
        logger.warning("yt-dlp is not installed; cannot search by name")
        return []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "extractor_args": youtube_extractor_args(),
        "skip_download": True,
        "ignoreerrors": True,
        "playlistend": limit,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as exc:  # noqa: BLE001 - a failed search is not fatal
        logger.warning("search %r failed: %s", query, exc)
        return []
    if not isinstance(info, dict):
        return []
    entries = info.get("entries") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_playlist_url(entry: Dict[str, Any]) -> Optional[str]:
    """Return a playlist URL for a flat search entry, or ``None``.

    Handles the three shapes yt-dlp emits: an explicit ``_type: "playlist"``,
    a ``url`` carrying a ``list=`` parameter, and a bare ``PL...``/``OLAK5uy``
    id in ``id``/``playlist_id``.
    """
    if entry.get("_type") in ("playlist", "url_transparent"):
        url = entry.get("url") or entry.get("webpage_url")
        if url:
            return url
    url = entry.get("url") or entry.get("webpage_url") or ""
    if _LIST_PARAM_RE.search(url):
        return url
    ident = entry.get("playlist_id") or entry.get("id") or ""
    if re.match(r"^(?:PL|OLAK5uy|RD|UU|LL|FL)[A-Za-z0-9_-]{8,}$", str(ident)):
        return "https://www.youtube.com/playlist?list=%s" % ident
    return None


def _name_job(text: str) -> PlaylistJob:
    """Find a YouTube playlist matching a free-text name and expand it.

    When no playlist can be found the job comes back with the requested name and
    an **empty** track list, so the CLI reports "no tracks found" cleanly instead
    of showing the user a traceback for a typo.
    """
    name = _clean_input(text)
    try:
        from urllib.parse import quote_plus
        search_url = _PLAYLIST_SEARCH_URL % quote_plus(name)
    except Exception:  # pragma: no cover - quote_plus does not fail in practice
        search_url = None

    queries = []  # type: List[str]
    if search_url:
        queries.append(search_url)
    queries.append("ytsearch1:%s playlist" % _sanitise_query_part(name))

    for query in queries:
        for entry in _flat_search(query, 1):
            playlist_url = _entry_playlist_url(entry)
            if not playlist_url:
                continue
            logger.info("name %r resolved to %s", name, playlist_url)
            try:
                from resolver import expand_playlist
                title, tracks, failures = expand_playlist(playlist_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not expand %s: %s", playlist_url, exc)
                continue
            for failure in failures or []:
                logger.warning("skipped %s: %s",
                               getattr(failure, "url", "?"),
                               getattr(failure, "reason", failure))
            tracks = list(tracks or [])
            if not tracks:
                continue
            return PlaylistJob(
                name=(title or "").strip() or name,
                tracks=tracks,
                source=SOURCE_SEARCH,
                origin=text,
            )

    logger.warning("no playlist found for %r", name)
    return PlaylistJob(name=name or "Search", tracks=[],
                       source=SOURCE_SEARCH, origin=text)


def resolve_input(text: str) -> PlaylistJob:
    """Turn any supported user input into a :class:`~models.PlaylistJob`.

    ``origin`` is always set to ``text``. A name that matches nothing yields a job
    with an empty track list rather than an exception.

    ``SpotifyCredentialsError`` here means neither the API nor the public-page
    scraper could resolve a Spotify input; its message is the real reason, which
    may have nothing to do with credentials.
    """
    kind = detect_input_kind(text)
    logger.debug("input %r classified as %s", text, kind)
    if kind == KIND_SPOTIFY:
        job = _spotify_any_job(text)
    elif kind == KIND_YOUTUBE:
        job = _youtube_job(text)
    else:
        job = _name_job(text)
    job.origin = text
    return job


def match_track(track: Track) -> Tuple[Optional[Track], Optional[str]]:
    """Resolve a search-query track to a concrete YouTube URL.

    Spotify-sourced tracks arrive carrying a ``ytsearchN:`` query rather than a
    URL, because which YouTube upload corresponds to a Spotify track is a guess
    that has to be made against real candidates. This runs that search and applies
    :func:`pick_best_match`, so the duration and live/cover guards actually gate
    what gets downloaded.

    Tracks whose query is already a plain URL are returned untouched, so this is
    safe to call unconditionally for every track in a job. Returns a
    ``(track, reason)`` pair: on failure ``track`` is ``None`` and ``reason`` is
    ready to be recorded.
    """
    query = (track.query or "").strip()
    if not query:
        return None, "empty search query"
    if not query.startswith("ytsearch"):
        return track, None

    candidates = _flat_search(query, SEARCH_CANDIDATES)
    if not candidates:
        return None, "no search results"

    chosen = pick_best_match(candidates, track)
    if chosen is None:
        return None, "no acceptable match (all candidates rejected as wrong versions)"

    url = _entry_to_video_url(chosen)
    if not url:
        return None, "search result had no usable URL"

    logger.info("matched %r -> %s (%s)", track.display(), chosen.get("title"), url)
    return replace(track, query=url), None


def _entry_to_video_url(entry: Dict[str, Any]) -> Optional[str]:
    """Return a playable video URL for a flat search entry.

    Mirrors the fallback chain used by :mod:`resolver` so both modules agree on
    how a flat entry becomes a URL.
    """
    for key in ("url", "webpage_url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    video_id = entry.get("id")
    if isinstance(video_id, str) and video_id:
        return "https://www.youtube.com/watch?v=%s" % video_id
    return None
