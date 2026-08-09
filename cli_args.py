"""The command-line surface: every flag, and the validation argparse cannot do.

Pure declaration plus two validators. Nothing here touches the network, the
filesystem or a browser, so ``--help`` costs nothing but an argparse import.
"""

from __future__ import annotations

import argparse
from typing import Any

from models import (
    AUDIO_FORMATS,
    BITRATE_DEFAULT,
    DEFAULT_OUTPUT_DIR,
    MAX_WORKERS_DEFAULT,
)

__all__ = [
    "QUALITY_MIN",
    "QUALITY_MAX",
    "STATE_FILE_DEFAULT",
    "ATTACH_ENDPOINT_DEFAULT",
    "build_parser",
    "parse_quality",
]

#: Accepted ``--quality`` range in kbps. Below 64 the MP3 is unpleasant; 320 is
#: the top of the LAME/ffmpeg CBR scale, so anything higher is a typo.
QUALITY_MIN = 64
QUALITY_MAX = 320

#: Playwright storage-state file: the saved SoundCloud session, written by
#: ``--login`` and read by every ``--upload`` run.
STATE_FILE_DEFAULT = "storage_state.json"

#: Where ``--attach`` looks for an already-running Chrome. Matches the port in
#: browser_engine's own instructions for starting one.
ATTACH_ENDPOINT_DEFAULT = "http://127.0.0.1:9222"


EPILOG = """\
examples:
  # a Spotify playlist link, straight from the "Share" menu
  python3 main.py "https://open.spotify.com/playlist/EXAMPLEPLAYLISTID00000"

  # a playlist by name, plus a Spotify album URI, into a custom folder
  python3 main.py -n "example playlist name" -p spotify:album:EXAMPLEALBUMID00000000 -o ~/Music/rips

  # a whole YouTube playlist at 320 kbps with 8 workers
  python3 main.py -p "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx" -q 320 -w 8

  # a batch file (one entry per line, # comments allowed), re-fetching everything
  python3 main.py -f playlists.txt --overwrite -v

  # a "Full Album Stream" video, cut into one tagged MP3 per chapter
  python3 main.py --split-chapters -u "https://www.youtube.com/watch?v=EXAMPLEVID1"

  # keep watching playlists.txt, or serve the Spicetify extension endpoint
  python3 main.py --watch
  python3 main.py --serve

  # save a SoundCloud session once (a browser opens; you log in there)
  python3 main.py --login

  # download your own release and publish it publicly
  python3 main.py --upload -p "https://open.spotify.com/album/EXAMPLEALBUMID00000000"

  # upload a folder that is already on disk, keeping it private (-d implies --upload)
  python3 main.py -d "Songs/Example Album" --privacy private --artist "Artist Name"

  # drive the Chrome you are already logged in to, over CDP
  python3 main.py -d "Songs/Example Album" --upload --attach

  # check the live page without attaching a file or pressing Upload
  python3 main.py -d "Songs/Example Album" --upload --attach --dry-run
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    ``--quality`` is accepted as free text here and validated by
    :func:`parse_quality`, so the error message can explain the allowed range
    instead of argparse listing hundreds of choices.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Download Spotify and YouTube playlists as tagged audio files (MP3 by "
            "default, see --format) with ID3 tags and embedded cover art.\n"
            "\n"
            "Inputs may be given positionally or through -p/-n/-u/-f, in any mix: a "
            "Spotify playlist or album (URL or spotify: URI), a YouTube playlist or "
            "video URL, or a plain playlist name to search for. Spotify tracks are "
            "matched against YouTube by title, artist and duration.\n"
            "\n"
            "Each playlist lands in its own folder under --output-dir, with a resume "
            "ledger alongside it, so re-running skips what is already on disk. One bad track "
            "never aborts the batch: failures are reported and written to skipped.txt.\n"
            "\n"
            "Requires ffmpeg on PATH. Spotify inputs need no credentials: the "
            "tracklist, collection name and cover art are read from Spotify's "
            "public pages. SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET are used "
            "only as a fallback, if that scrape fails."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        metavar="INPUT",
        help=(
            "playlist links, Spotify URIs, video URLs or playlist names, in any mix "
            "(can be combined with -p/-n/-u/-f)"
        ),
    )
    parser.add_argument(
        "-p",
        "--playlist",
        action="append",
        dest="playlist",
        metavar="URL",
        help="a Spotify or YouTube playlist/album URL or URI; repeat for several",
    )
    parser.add_argument(
        "-n",
        "--name",
        action="append",
        dest="name",
        metavar="NAME",
        help="a playlist name to search for; repeat for several",
    )
    parser.add_argument(
        "-u",
        "--url",
        action="append",
        dest="url",
        metavar="URL",
        help="a single video URL; repeat for several",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="file",
        metavar="PATH",
        help="text file with one entry per line (blank lines and # comments ignored)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        default="./%s" % DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="root directory for the playlist folders (default: ./%s)" % DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "-q",
        "--quality",
        dest="quality",
        default=BITRATE_DEFAULT,
        metavar="KBPS",
        help="MP3 bitrate in kbps, %d-%d (default: %s)" % (QUALITY_MIN, QUALITY_MAX, BITRATE_DEFAULT),
    )
    parser.add_argument(
        "-w",
        "--workers",
        dest="workers",
        type=int,
        default=MAX_WORKERS_DEFAULT,
        metavar="N",
        help="parallel downloads per playlist (default: %d)" % MAX_WORKERS_DEFAULT,
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-download tracks that are already on disk (default: skip them)",
    )
    parser.add_argument(
        "--format",
        dest="audio_format",
        choices=AUDIO_FORMATS,
        default="mp3",
        help=(
            "audio format to save (default: %(default)s). opus and m4a are what "
            "YouTube already serves, so they are copied rather than re-encoded "
            "-- best fidelity, and -q is ignored for them"
        ),
    )
    parser.add_argument(
        "--split-chapters",
        action="store_true",
        help="split a full-album stream into one MP3 per chapter",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="watch playlists.txt and download new entries as they appear",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run the localhost endpoint used by the Spicetify extension",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "open SoundCloud in a browser, wait for you to log in, save the session "
            "to --state-file and exit (all other flags are ignored)"
        ),
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help=(
            "publish each downloaded album to your own SoundCloud account once it "
            "has finished; this is for uploading your own releases"
        ),
    )
    parser.add_argument(
        "-d",
        "--dir",
        dest="dir",
        metavar="DIR",
        help=(
            "upload an album folder that is already on disk; implies --upload and "
            "downloads nothing (cannot be combined with -p/-n/-u/-f or positional inputs)"
        ),
    )
    parser.add_argument(
        "--privacy",
        dest="privacy",
        choices=("private", "public"),
        default="public",
        help="visibility of the uploaded tracks (default: public)",
    )
    parser.add_argument(
        "--artist",
        dest="artist",
        metavar="NAME",
        help=(
            "artist to credit on every uploaded track (default: the album's own "
            "tag); SoundCloud prefills this field with your account name"
        ),
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help=(
            "drive the Chrome already running with --remote-debugging-port instead "
            "of launching one; uses that browser's own profile and session"
        ),
    )
    parser.add_argument(
        "--attach-endpoint",
        dest="attach_endpoint",
        default=ATTACH_ENDPOINT_DEFAULT,
        metavar="URL",
        help="CDP endpoint for --attach (default: %s)" % ATTACH_ENDPOINT_DEFAULT,
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="run the browser visibly; try this when a headless run gets challenged",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "with --upload: open the live upload page and report what would go "
            "up, attaching no files and pressing nothing"
        ),
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        default=STATE_FILE_DEFAULT,
        metavar="PATH",
        help="saved SoundCloud session written by --login (default: %s)" % STATE_FILE_DEFAULT,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose output, including yt-dlp's own diagnostics (disables the progress bar)",
    )
    return parser


def parse_quality(value: Any) -> str:
    """Validate ``--quality`` and return it as a normalised bitrate string.

    Raises ``ValueError`` if it is not a whole number within
    :data:`QUALITY_MIN`..:data:`QUALITY_MAX`.
    """
    text = str(value).strip().lower()
    if text.endswith("k"):
        text = text[:-1].strip()
    try:
        kbps = int(text)
    except (TypeError, ValueError):
        raise ValueError(
            "--quality must be a whole number of kbps between %d and %d "
            "(for example 128, 192 or 320), not %r" % (QUALITY_MIN, QUALITY_MAX, value)
        )
    if not QUALITY_MIN <= kbps <= QUALITY_MAX:
        raise ValueError(
            "--quality must be between %d and %d kbps, got %d"
            % (QUALITY_MIN, QUALITY_MAX, kbps)
        )
    return str(kbps)

