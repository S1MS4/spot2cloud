"""Shared types and constants.

Deliberately dependency-free so every other module can import it without
pulling in yt-dlp or spotipy, and so tests can run without them installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__all__ = [
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "SOURCE_SPOTIFY",
    "SOURCE_YOUTUBE",
    "SOURCE_SEARCH",
    "BITRATE_DEFAULT",
    "DEFAULT_OUTPUT_DIR",
    "MAX_FILENAME_BYTES",
    "SKIPPED_LOG_NAME",
    "HISTORY_DIR",
    "MAX_WORKERS_DEFAULT",
    "MAX_WORKERS_LIMIT",
    "SEARCH_CANDIDATES",
    "DURATION_TOLERANCE",
    "YOUTUBE_PLAYER_CLIENTS",
    "youtube_extractor_args",
    "SERVER_HOST",
    "SERVER_PORT",
    "WATCH_FILE_NAME",
    "Track",
    "PlaylistJob",
    "DownloadResult",
]

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

SOURCE_SPOTIFY = "spotify"
SOURCE_YOUTUBE = "youtube"
SOURCE_SEARCH = "search"

BITRATE_DEFAULT = "192"
DEFAULT_OUTPUT_DIR = "Songs"
SKIPPED_LOG_NAME = "skipped.txt"
# Resume ledgers live beside the album folders, not inside them: an album
# folder is a music library entry and should hold music and artwork only.
HISTORY_DIR = ".download_history"
WATCH_FILE_NAME = "playlists.txt"

# 180 rather than 255: leaves room for the ".mp3" suffix and a " (2)" collision
# marker while staying under the per-filename byte limit on ext4/APFS.
MAX_FILENAME_BYTES = 180

MAX_WORKERS_DEFAULT = 4
MAX_WORKERS_LIMIT = 8

# Search several candidates rather than trusting the top hit, which is often a
# cover, a live take or an hour-long loop. See playlist_parser.pick_best_match.
SEARCH_CANDIDATES = 5
DURATION_TOLERANCE = 0.15

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5001

# YouTube's SABR rollout makes the default web clients return formats with no
# usable URL, surfacing as "The page needs to be reloaded." Naming clients
# explicitly works around it. Verified 2026-08-07: android_vr extracts fine
# while default and mweb hit SABR, tv_embedded reports an unsupported device,
# and ios offers no matching audio format. The rest are fallbacks, since which
# clients work changes on YouTube's schedule.
YOUTUBE_PLAYER_CLIENTS = ("android_vr", "web_safari", "tv", "default")


def youtube_extractor_args():
    """Fresh extractor_args per call; yt-dlp mutates the dict it is given."""
    return {"youtube": {"player_client": list(YOUTUBE_PLAYER_CLIENTS)}}


@dataclass
class Track:
    """One unit of audio to fetch.

    ``query`` is either a URL or a ``ytsearchN:`` expression. Spotify tracks
    start as the latter: which YouTube upload corresponds to a Spotify track is
    a guess, resolved later by playlist_parser.match_track.

    ``source_id`` marks metadata as authoritative (it came from the Spotify
    API), which decides both the history key and whether tag_mp3 overwrites
    what yt-dlp already wrote.
    """

    query: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    track_number: Optional[int] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    source_id: Optional[str] = None

    def display(self) -> str:
        return "%s - %s" % (self.artist, self.title) if self.artist else self.title

    def history_key(self) -> str:
        # Key on the Spotify id so a re-run skips the track even if the YouTube
        # match would now resolve differently.
        if self.source_id:
            return "spotify:%s" % self.source_id
        return "query:%s" % self.query


@dataclass
class PlaylistJob:
    """A named playlist and its tracks; ``name`` becomes the output folder."""

    name: str
    tracks: List[Track] = field(default_factory=list)
    source: str = SOURCE_YOUTUBE
    origin: Optional[str] = None

    def __len__(self) -> int:
        return len(self.tracks)


@dataclass
class DownloadResult:
    """Outcome of one track.

    download_one never raises for a per-track problem; failures come back as
    STATUS_FAILED so a batch can carry on.
    """

    url: str
    title: Optional[str] = None
    path: Optional[Path] = None
    status: str = STATUS_OK
    reason: Optional[str] = None

    @classmethod
    def ok(cls, url: str, title: Optional[str], path: Path) -> "DownloadResult":
        return cls(url=url, title=title, path=path, status=STATUS_OK)

    @classmethod
    def skipped(
        cls,
        url: str,
        title: Optional[str] = None,
        path: Optional[Path] = None,
        reason: str = "already downloaded",
    ) -> "DownloadResult":
        return cls(url=url, title=title, path=path, status=STATUS_SKIPPED, reason=reason)

    @classmethod
    def failed(
        cls, url: str, reason: str, title: Optional[str] = None
    ) -> "DownloadResult":
        return cls(url=url, title=title, path=None, status=STATUS_FAILED, reason=reason)

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK
