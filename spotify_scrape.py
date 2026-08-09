"""Read Spotify album/playlist metadata off the public web page, no API keys.

Spotify gates Web API access behind Premium, so the client-credentials flow in
:mod:`playlist_parser` is no longer available to everyone. Everything this
project needs -- track ids, titles, the 640x640 cover, the collection name --
is server-rendered into the public ``open.spotify.com`` HTML, and reading that
needs nothing but :mod:`urllib`.

The catch, and the reason this module is built the way it is: playlists are
sometimes served truncated to their first 30 rows. A scraper that quietly
returned those 30 would be worse than no scraper at all, because the user would
get a folder that looks complete. So every scrape cross-checks its own row
count against the track count the page states about itself, re-fetches a
truncated page, and raises :class:`ScrapeMismatchError` rather than returning a
short list. Albums always render in full.

Verified against the live site 2026-08-09.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from models import SOURCE_SPOTIFY, PlaylistJob, Track
from playlist_parser import KIND_ALBUM, build_search_query, parse_spotify_ref

__all__ = [
    "ScrapeError",
    "ScrapeMismatchError",
    "ScrapeLayoutError",
    "FETCH_ATTEMPTS",
    "fetch_page",
    "parse_tracks",
    "parse_cover",
    "parse_name",
    "parse_stated_count",
    "scrape_spotify",
]

logger = logging.getLogger(__name__)

#: Spotify serves a stub with no track rows to clients it does not recognise as
#: a browser. Nothing more elaborate than this is needed.
USER_AGENT = "Mozilla/5.0"

FETCH_TIMEOUT = 30

#: Spotify serves two renderings of the same playlist more or less at random:
#: one with every row, one truncated to 30. A truncated page is not something
#: to report, it is something to ask for again.
FETCH_ATTEMPTS = 3

#: Each rendered row carries the track id in its ``aria-labelledby`` and the
#: exact title in its ``aria-label``. Playlist rows suffix the id with ``-<n>``
#: (their position), album rows do not; the suffix is kept out of the returned
#: id but used for de-duplication, so a playlist holding the same track twice
#: keeps both rows while a row rendered twice collapses to one.
_TRACK_ROW_RE = re.compile(
    r'aria-labelledby="listrow-title-track-spotify:track:'
    r'([A-Za-z0-9]{22})(-\d+)?"\s+aria-label="([^"]+)"'
)

_META_RE_TEMPLATE = r'<meta[^>]*(?:property|name)="%s"[^>]*content="([^"]*)"'

#: "Example Album - Album by Artist Name | Spotify". ``.*`` is greedy so a collection
#: actually named "... - Album by ..." keeps its name: only the last separator
#: is treated as the suffix.
_OG_TITLE_RE = re.compile(
    r"^(?P<name>.*) - (?P<kind>Album|Single|EP|Compilation|[Pp]laylist) by "
    r"(?P<owner>.+)$"
)

#: Playlists state their length outright in <meta name="music:song_count">.
#: Albums do not, but their og:description reads "Artist Name - album - 2026 - 10
#: songs" and the rendered header says "10 songs, 31 min 55 sec". Only the
#: songs/items unit is matched, so neither the "34M saves" figure nor a number
#: inside a playlist's own description can be mistaken for a track count.
_STATED_COUNT_RE = re.compile(r"([\d,]+)\s+(?:songs?|items?)\b")

_TIME_AND_DURATION_RE = re.compile(r'data-testid="time-and-duration".{0,400}', re.S)

_SUFFIX = " | Spotify"


# --- errors ---------------------------------------------------------------


class ScrapeError(RuntimeError):
    """Any failure to read a Spotify page. Never raised for a partial result."""


class ScrapeMismatchError(ScrapeError):
    """The rows found disagree with the track count the page states."""


class ScrapeLayoutError(ScrapeError):
    """A selector matched nothing, so Spotify's markup has probably changed."""


# --- fetching -------------------------------------------------------------


def fetch_page(url: str) -> str:
    """GET a Spotify page as text, translating transport errors to ScrapeError."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:  # a subclass of URLError: catch first
        if exc.code == 404:
            raise ScrapeError(
                "Spotify page %s: album or playlist not found, or it is private"
                % url
            ) from exc
        raise ScrapeError(
            "Spotify page %s: HTTP %s %s" % (url, exc.code, exc.reason)
        ) from exc
    except urllib.error.URLError as exc:
        raise ScrapeError(
            "Spotify page %s: could not be reached (%s)" % (url, exc.reason)
        ) from exc
    return raw.decode(charset, "replace")


# --- parsing --------------------------------------------------------------


def _meta(page: str, prop: str) -> Optional[str]:
    match = re.search(_META_RE_TEMPLATE % re.escape(prop), page)
    return html.unescape(match.group(1)) if match else None


#: Splits the page at each track row so per-row artist links can be read.
_ROW_SPLIT = "aria-labelledby=\"listrow-title-track-spotify:track:"
_ROW_ARTIST_RE = re.compile(r'href="/artist/[^"]+"[^>]*>([^<]{1,80})<')


def parse_track_artists(page: str) -> Dict[str, str]:
    """Map track id -> primary artist, read from each row's own artist links.

    Compilations and features credit tracks to different artists than the album
    does, and using the album artist for all of them sends the wrong name into
    the YouTube search -- which is how a track once matched an unrelated song.
    """
    artists = {}
    for chunk in page.split(_ROW_SPLIT)[1:]:
        track_id = chunk[:22]
        found = _ROW_ARTIST_RE.findall(chunk[:4000])
        if track_id and found:
            artists[track_id] = html.unescape(found[0]).strip()
    return artists


def parse_tracks(page: str) -> List[Tuple[str, str]]:
    """Extract ``(track_id, title)`` in page order, dropping repeated rows."""
    seen = set()
    tracks = []
    for track_id, position, title in _TRACK_ROW_RE.findall(page):
        key = track_id + position
        if key in seen:
            continue
        seen.add(key)
        tracks.append((track_id, html.unescape(title)))
    return tracks


def parse_cover(page: str) -> Optional[str]:
    """The og:image, which is the 640x640 square art rather than a thumbnail."""
    return _meta(page, "og:image")


def parse_name(page: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(name, artist)``; artist is None unless the page is an album.

    A playlist's og:title either carries no "by" suffix at all or names the
    *owner*, who is not the artist of anything in it.
    """
    title = _meta(page, "og:title")
    if not title:
        return None, None
    if title.endswith(_SUFFIX):
        title = title[: -len(_SUFFIX)]
    match = _OG_TITLE_RE.match(title)
    if not match:
        return title.strip() or None, None
    is_album = match.group("kind").lower() != "playlist"
    return match.group("name").strip() or None, (
        match.group("owner").strip() if is_album else None
    )


def parse_stated_count(page: str) -> Optional[int]:
    """The track count the page states about itself, or None if it states none."""
    song_count = _meta(page, "music:song_count")
    if song_count and song_count.strip().isdigit():
        return int(song_count.strip())
    # og:description carries the count only while the collection has no
    # description of its own to put there, so it cannot be the primary source.
    for text in (_meta(page, "og:description"), _rendered_header(page)):
        if not text:
            continue
        match = _STATED_COUNT_RE.search(text)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _rendered_header(page: str) -> Optional[str]:
    """The visible "10 songs, 31 min 55 sec" line, used if og:description is gone."""
    match = _TIME_AND_DURATION_RE.search(page)
    if not match:
        return None
    # Spaces, not "": the year and the count sit in adjacent spans, and
    # stripping the tags away would splice them into one number.
    return html.unescape(re.sub(r"<[^>]*>", " ", match.group(0)))


# --- entry point ----------------------------------------------------------


def scrape_spotify(url_or_uri: str) -> PlaylistJob:
    """Build a :class:`~models.PlaylistJob` from a public Spotify page.

    Args:
        url_or_uri: Any form :func:`playlist_parser.parse_spotify_ref` accepts.
            Locale prefixes and ``?si=`` share tokens are discarded, so the
            canonical page is always the one fetched.

    Returns:
        A job whose ``source`` is :data:`~models.SOURCE_SPOTIFY`, complete or
        not at all. Tracks carry no ``duration_ms``: the page states only the
        collection total, never per-track times, so there is nothing to put
        there and :func:`playlist_parser.pick_best_match` falls back to
        matching on title alone.

    Raises:
        ValueError: If the reference is not a Spotify album or playlist.
        ScrapeLayoutError: If a selector matched nothing.
        ScrapeMismatchError: If every attempt returned fewer rows than the page
            claims to hold.
        ScrapeError: If the page could not be fetched.
    """
    kind, ref_id = parse_spotify_ref(url_or_uri)
    url = "https://open.spotify.com/%s/%s" % (kind, ref_id)

    for attempt in range(1, FETCH_ATTEMPTS + 1):
        page = fetch_page(url)
        rows = parse_tracks(page)
        if not rows:
            raise ScrapeLayoutError(
                "Spotify page %s: no track rows matched "
                '\'aria-labelledby="listrow-title-track-spotify:track:<id>"\' -- '
                "the page layout has probably changed" % url
            )

        stated = parse_stated_count(page)
        if stated is None:
            # Without the page's own count there is nothing to check the rows
            # against, and an unverified tracklist is exactly what this module
            # refuses to return.
            raise ScrapeLayoutError(
                "Spotify page %s: no track count matched in the music:song_count "
                'or og:description meta tags or the data-testid="time-and-duration" '
                "header, so the %d rows found cannot be verified -- the page layout "
                "has probably changed" % (url, len(rows))
            )
        if stated == len(rows):
            break
        logger.debug(
            "attempt %d of %d: %s rendered %d of %d rows",
            attempt, FETCH_ATTEMPTS, url, len(rows), stated,
        )
    else:
        raise ScrapeMismatchError(
            "Spotify page %s: found %d tracks but the page states %d, on every "
            "one of %d attempts. Spotify truncates long playlists to 30 rows; "
            "refusing to return a partial tracklist."
            % (url, len(rows), stated, FETCH_ATTEMPTS)
        )

    name, artist = parse_name(page)
    if not name:
        raise ScrapeLayoutError(
            "Spotify page %s: no name matched in the og:title meta tag -- the "
            "page layout has probably changed" % url
        )
    cover_url = parse_cover(page)
    is_album = kind == KIND_ALBUM

    logger.debug("scraped %d tracks from %s", len(rows), url)
    # Per-row artists: a compilation credits each track differently, and the
    # album artist would send the wrong name into the YouTube search.
    per_track = parse_track_artists(page)

    return PlaylistJob(
        name=name,
        tracks=[
            Track(
                query=build_search_query(title, per_track.get(track_id) or artist),
                title=title,
                artist=per_track.get(track_id) or artist,
                album=name if is_album else None,
                track_number=position,
                cover_url=cover_url,
                source_id=track_id,
            )
            for position, (track_id, title) in enumerate(rows, start=1)
        ],
        source=SOURCE_SPOTIFY,
        origin=url_or_uri,
    )
