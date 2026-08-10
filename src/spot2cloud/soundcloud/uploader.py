"""Upload a finished album folder to your own SoundCloud account.

This publishes the user's own releases to the user's own profile -- recordings
the user made or otherwise controls. That is the entire intended use, it is
stated once here, and nothing in this module tries to work out what the audio
is or who owns it; that judgement is the user's.

Every entry point here defaults ``privacy`` to ``"private"`` on purpose, so a
caller that forgets the argument publishes to nobody rather than to everybody.
The ``--privacy`` CLI flag overrides it explicitly.

The download half of this project produces ``Songs/<Album>/`` holding numbered
audio files and a ``cover.jpg``, and nothing else. This module turns that folder
back into a release: it reads the tags, transcodes anything SoundCloud might
reject, and drives the upload page so every track goes up in one pass with the
right title, artist, artwork and privacy.

Browser plumbing lives in ``browser_engine``; it is imported lazily so this
module can be imported (and its scanning half tested) without Playwright.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set
from urllib.parse import urlparse

if TYPE_CHECKING:  # only for annotations; the runtime import stays inside browser_engine
    from playwright.sync_api import Locator, Page

__all__ = [
    "AUDIO_EXTS",
    "FRAME_SETTLE_MS",
    "PREATTACH_KEYS",
    "RESOLVE_TIMEOUT_MS",
    "SELECTORS",
    "SOUNDCLOUD",
    "SOUNDCLOUD_ACCEPTED",
    "SUBMIT_TIMEOUT_MS",
    "UPLOAD_SUBDIR",
    "UPLOAD_URL",
    "AlbumUpload",
    "SelectorError",
    "TrackUpload",
    "UploadError",
    "check_selectors",
    "dismiss_consent",
    "existing_titles",
    "prepare_for_upload",
    "profile_slug",
    "resolve",
    "scan_album",
    "set_privacy",
    "upload_album",
    "upload_frame",
    "wait_for_upload_frame",
]

logger = logging.getLogger(__name__)

SOUNDCLOUD = "https://soundcloud.com"
UPLOAD_URL = "%s/upload" % SOUNDCLOUD

AUDIO_EXTS = frozenset(
    [
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".amr",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wave",
        ".wma",
    ]
)

# SoundCloud's documented list. ``.opus`` is missing on purpose: it is Opus in
# an Ogg container, which the uploader rejects often enough that transcoding is
# cheaper than a failed 40-minute upload.
SOUNDCLOUD_ACCEPTED = frozenset(
    [
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".amr",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".wav",
        ".wave",
        ".wma",
    ]
)

UPLOAD_SUBDIR = ".upload"
TRANSCODE_SUFFIX = ".flac"

# The audio transfer starts the moment the files are attached and runs while the
# metadata form is being filled in, so pressing Upload only commits the form.
SUBMIT_TIMEOUT_MS = 30_000

# The iframe is fetched after the page shell, so it is never there on the first
# look. Generous: everything downstream depends on finding it.
FRAME_SETTLE_MS = 15_000

# Long enough to outlast a React re-render, short enough that walking a fallback
# chain of dead selectors does not stall the run.
RESOLVE_TIMEOUT_MS = 4000

# The metadata form is built only after the browser has read every attached
# file, so the wait has to scale with the album. A flat 20s was sized against a
# single track and times out on anything bigger: eleven MP3s is ~54 MB, and the
# form does not exist until SoundCloud has chewed through all of it.
FORM_TIMEOUT_MS = 20_000
FORM_TIMEOUT_PER_FILE_MS = 15_000

# One deliberate beat, and the only one: the title and artist fields are
# prefilled asynchronously, and a value written before the prefill arrives is
# simply overwritten by it.
FORM_SETTLE_MS = 1500

_FILENAME_TRACK_RE = re.compile(r"^\s*(\d{1,3})\s*[-._)]\s*")
_TRACK_NUMBER_RE = re.compile(r"^\s*(\d+)")


class UploadError(RuntimeError):
    """Raised when an album could not be uploaded."""


class SelectorError(UploadError):
    """Raised when no selector in a fallback chain matched."""

    def __init__(self, key: str, tried: Sequence[str]):
        self.key = key
        self.tried = list(tried)
        super().__init__(
            "no selector matched for %r; tried: %s\n"
            "SoundCloud most likely changed its markup -- update "
            "uploader.SELECTORS[%r]." % (key, ", ".join(self.tried) or "(none)", key)
        )


@dataclass
class TrackUpload:
    """One track queued for upload.

    ``upload_path`` is the file actually handed to the browser and is None
    until :func:`prepare_for_upload` has run; it differs from ``path`` only for
    formats that had to be transcoded.
    """

    path: Path
    title: str
    track_number: int
    artist: Optional[str] = None
    upload_path: Optional[Path] = None


@dataclass
class AlbumUpload:
    """An album folder, resolved into what the upload page needs."""

    folder: Path
    title: str
    artist: Optional[str] = None
    cover: Optional[Path] = None
    tracks: List[TrackUpload] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.tracks)


# Read off the live upload page on 2026-08-09, signed in.
#
# Frame scoping is what makes any of this resolve: the upload UI is an iframe at
# /n/upload and the outer page contains none of these elements, not even a file
# input. A selector audit run against the outer page reports a clean sweep of
# misses and always will.
#
# Only PREATTACH_KEYS exist on a bare upload page. Everything else -- every
# metadata field, the privacy select, the Upload button -- is created once files
# have been attached, so a report of misses on an untouched page is correct, not
# a regression.
#
# Each value is a fallback chain tried in order: name, role and visible text
# first, because they survive a restyle, and generated MUI class names last.
SELECTORS = {
    "file_input": [
        # Unqualified on purpose. The element carries `multiple` as an empty
        # string and no accept attribute, so anything narrower is a guess.
        'input[type="file"]',
        ".chooseFiles input[type=file]",
    ],
    "choose_files": [
        'button:has-text("Choose files")',
        "text=/choose files/i",
    ],
    # Prefilled from the filename stem, i.e. it arrives holding "01 - Title".
    "title_input": [
        'input[name="title"]',
        'input[placeholder*="title" i]',
    ],
    # Prefilled with the account name, not with whoever made the release.
    "artist_input": [
        'input[name="artist"]',
        'input[placeholder*="artist" i]',
    ],
    "artwork_input": [
        'input[type="file"][accept*="image"]',
        ".imageChooser input[type=file]",
    ],
    "artwork_button": [
        'button:has-text("Add new artwork")',
        'button:has-text("artwork")',
    ],
    # A MUI select, not a radio group, and it defaults to Public.
    "privacy_select": [
        'div[role="combobox"][aria-haspopup="listbox"]',
        ".MuiSelect-select",
    ],
    # The option list renders in a portal on the *outer* page, so these are
    # looked for there first. li[data-value=...] did not match on the live page
    # and is only kept as a fallback for whatever ships next.
    "privacy_option_private": [
        'li[role="option"]:has-text("Private")',
        'li[data-value="private"]',
    ],
    "privacy_option_public": [
        'li[role="option"]:has-text("Public")',
        'li[data-value="public"]',
    ],
    # It reads "Upload", not "Save". Exact matches only, deliberately: the
    # frame also carries a "Get unlimited uploads" button, and Playwright's
    # has-text is a case-insensitive substring, so the obvious loose fallback
    # resolves to the upsell. Failing to find the button loses a submit;
    # clicking that one loses the attached files.
    "submit_button": [
        'button:text-is("Upload")',
        "role=button[name=/^upload$/i]",
    ],
    "success_marker": [
        'text="Saved to SoundCloud."',
        "text=/congratulations!? your tracks are now on soundcloud/i",
    ],
    "profile_link": [
        "a.header__userNavButton[href]",
        ".header__userNav a[href]",
    ],
    "profile_track_title": [
        "a.soundTitle__title",
        ".soundTitle__usernameTitleContainer a",
    ],
    "result_link": [
        'a[href*="/sets/"]',
        "role=link[name=/go to (your )?(track|set|album|playlist)/i]",
    ],
    # Checked when a submit goes quiet: an account out of upload minutes says so
    # here, and repeating SoundCloud's own words beats reporting a timeout.
    "quota_notice": [
        "text=/upload limit/i",
        "text=/you have reached/i",
        "text=/minutes? (of upload )?remaining/i",
        "text=/not enough upload time/i",
    ],
    "error_message": [
        "role=alert",
        ".uploadError",
        ".alertMessage",
    ],
}

#: Keys that can match before any file has been attached; see SELECTORS.
PREATTACH_KEYS = frozenset({"file_input", "choose_files"})

# The privacy options are portalled out of the iframe, and the profile page has
# no iframe at all, so these are looked up on the page rather than the frame.
_PAGE_SCOPED = frozenset(
    {
        "privacy_option_private",
        "privacy_option_public",
        "profile_link",
        "profile_track_title",
    }
)


def _engine():
    """browser_engine, imported late so scanning works without Playwright."""
    from spot2cloud.soundcloud import browser_engine

    return browser_engine


def _tags(path: Path) -> Dict[str, str]:
    """First value of every tag on ``path``, lowercased keys, or {} if untagged.

    ``easy=True`` is what makes ID3 and Vorbis comments answer to the same
    names, so callers never have to know whether they hold an MP3 or an Opus.
    """
    try:
        import mutagen

        audio = mutagen.File(str(path), easy=True)
    except Exception:
        logger.debug("could not read tags from %s", path, exc_info=True)
        return {}

    if audio is None or not audio.tags:
        return {}

    out = {}
    for key in ("title", "artist", "albumartist", "album", "tracknumber"):
        try:
            value = audio.tags.get(key)
        except Exception:  # noqa: BLE001 - EasyID3 raises on keys it cannot map
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def _track_number(text: Optional[str]) -> Optional[int]:
    """Parse a track number, tolerating the ``3/10`` form tags often carry."""
    if not text:
        return None
    match = _TRACK_NUMBER_RE.match(text)
    return int(match.group(1)) if match else None


def _from_filename(stem: str):
    """Split a leading ``NN - `` prefix off a filename stem."""
    match = _FILENAME_TRACK_RE.match(stem)
    if not match:
        return None, stem.strip()
    return int(match.group(1)), stem[match.end() :].strip() or stem.strip()


def scan_album(folder) -> AlbumUpload:
    """Read an album folder into an :class:`AlbumUpload`, ordered by track number.

    Top level only: a ``mp3-192 (transcoded)/`` sibling copy of the same album
    would otherwise double every track.
    """
    folder = Path(folder)
    files = sorted(
        (
            entry
            for entry in folder.iterdir()
            if entry.is_file()
            and entry.suffix.lower() in AUDIO_EXTS
            and not entry.name.startswith(".")
        ),
        key=lambda entry: entry.name.lower(),
    )
    if not files:
        raise ValueError(
            "no audio files in %s (looked for %s, top level only)"
            % (folder, ", ".join(sorted(AUDIO_EXTS)))
        )

    album_title = None
    album_artist = None
    scanned = []
    for path in files:
        tags = _tags(path)
        file_number, file_title = _from_filename(path.stem)
        number = _track_number(tags.get("tracknumber"))
        if number is None:
            number = file_number
        scanned.append(
            (
                number,
                TrackUpload(
                    path=path,
                    title=tags.get("title") or file_title,
                    track_number=number or 0,
                    artist=tags.get("artist") or tags.get("albumartist"),
                ),
            )
        )
        album_title = album_title or tags.get("album")
        album_artist = album_artist or tags.get("albumartist") or tags.get("artist")

    # Numbered tracks lead in numeric order; anything untagged and unnumbered
    # falls to the end in the alphabetical order it was collected in.
    scanned.sort(key=lambda item: (item[0] is None, item[0] or 0))
    tracks = []
    for position, (number, track) in enumerate(scanned, start=1):
        if number is None:
            track.track_number = position
        tracks.append(track)

    cover = folder / _cover_name()
    return AlbumUpload(
        folder=folder,
        title=album_title or folder.name,
        artist=album_artist,
        cover=cover if cover.exists() else None,
        tracks=tracks,
    )


def _cover_name() -> str:
    from spot2cloud.download.album_art import COVER_NAME

    return COVER_NAME


def prepare_for_upload(album: AlbumUpload, workdir=None) -> AlbumUpload:
    """Fill in ``upload_path`` on every track, transcoding what SoundCloud may reject.

    Rejected formats become FLAC: SoundCloud re-encodes everything server-side,
    so feeding it lossless gives the best result, and FLAC is unambiguously on
    its accepted list. Already-transcoded files are reused, so a resumed run
    does not spend half an hour re-encoding the same album.
    """
    from spot2cloud.download.downloader import check_ffmpeg, sanitize_filename

    # Up front: discovering a missing ffmpeg on track 9 of 10 wastes the run,
    # and check_ffmpeg's error carries the install instructions.
    ffmpeg = check_ffmpeg()

    workdir = Path(workdir) if workdir is not None else album.folder / UPLOAD_SUBDIR

    for track in album.tracks:
        if track.path.suffix.lower() in SOUNDCLOUD_ACCEPTED:
            track.upload_path = track.path
            continue

        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / (sanitize_filename(track.path.stem) + TRANSCODE_SUFFIX)
        if target.exists() and target.stat().st_size > 0:
            logger.debug("reusing %s", target)
            track.upload_path = target
            continue

        _transcode(ffmpeg, track.path, target)
        track.upload_path = target

    return album


def _transcode(ffmpeg: str, source: Path, target: Path) -> None:
    """Encode ``source`` to FLAC at ``target``, carrying its tags across."""
    # Encode to a temporary name and rename on success, so an interrupted run
    # cannot leave a truncated file that the next run would happily upload.
    partial = target.with_name(target.name + ".part")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-map_metadata",
        "0",
        # Ogg keeps its comments as stream metadata rather than global, so
        # without this second mapping every .opus track loses its tags.
        "-map_metadata:g",
        "0:s:0",
        "-c:a",
        "flac",
        # Named explicitly because the ".part" suffix leaves ffmpeg with no
        # extension to infer the container from.
        "-f",
        "flac",
        str(partial),
    ]
    logger.info("transcoding %s -> %s", source.name, target.name)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise UploadError(
            "ffmpeg could not transcode %s (exit %d)\n%s"
            % (source, result.returncode, (result.stderr or "").strip())
        )
    partial.replace(target)


# The upload UI is not on /upload itself -- it lives in an iframe at
# /n/upload?crossfade=true&v2_layout=true&embedded=crossfade. Every locator must
# be scoped to that frame; the outer page holds no file input at all.
UPLOAD_FRAME_HINT = "/n/upload"
UPLOAD_FRAME_SELECTOR = 'iframe[src*="/n/upload"]'


def upload_frame(page: Page):
    """The frame holding the upload UI, or the page itself if it is not iframed.

    Falling back to the page keeps this working if SoundCloud ever inlines the
    uploader again, rather than hard-failing on a layout change.
    """
    for frame in getattr(page, "frames", []) or []:
        if UPLOAD_FRAME_HINT in (frame.url or ""):
            return frame
    logger.debug("upload iframe not found; falling back to the main frame")
    return page


def wait_for_upload_frame(page: Page, timeout_ms: int = FRAME_SETTLE_MS):
    """:func:`upload_frame`, having first waited for the iframe to attach."""
    try:
        page.wait_for_selector(UPLOAD_FRAME_SELECTOR, state="attached", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - upload_frame's own fallback covers this
        logger.debug("no %s iframe within %dms", UPLOAD_FRAME_HINT, timeout_ms)
    return upload_frame(page)


def dismiss_consent(page: Page) -> bool:
    """Close the OneTrust cookie gate, which otherwise intercepts every click.

    A fresh browser profile always gets this banner, and while it is up the
    upload UI does not render at all.
    """
    for selector in (
        "#onetrust-accept-btn-handler",
        "#onetrust-reject-all-handler",
        ".onetrust-close-btn-handler",
    ):
        try:
            loc = page.locator(selector)
            if loc.count():
                loc.first.click(timeout=4000)
                page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001 - the banner is often simply absent
            continue
    return False


def dismiss_welcome(page: Page) -> bool:
    """Close the "What are you looking to do on SoundCloud?" interstitial.

    SoundCloud redirects /upload here until the account answers it. The close
    button is used rather than one of the four cards on purpose: picking a card
    records a preference against the user\'s account, and nothing here should
    be making that choice on their behalf. The X only dismisses.
    """
    for selector in (
        'button[aria-label*="close" i]',
        'button[title*="close" i]',
        '[data-testid*="close" i]',
        '[class*="close" i][role="button"]',
        'button:has(svg[aria-label*="close" i])',
    ):
        try:
            loc = page.locator(selector)
            if loc.count():
                loc.first.click(timeout=RESOLVE_TIMEOUT_MS)
                page.wait_for_timeout(1500)
                logger.info("dismissed the welcome interstitial via %s", selector)
                return True
        except Exception:  # noqa: BLE001 - wrong guess, try the next shape
            continue
    return False


def _chain(scope, key: str, timeout_ms: int = RESOLVE_TIMEOUT_MS) -> Locator:
    """First selector in ``SELECTORS[key]`` that matches ``scope``, as a locator.

    Returns the whole match, not just the first element: with several tracks
    attached there is one title and one artist field per track.
    """
    tried = []
    for selector in SELECTORS[key]:
        tried.append(selector)
        try:
            locator = scope.locator(selector)
            # "attached" rather than "visible": every file input here is hidden
            # by design and set_input_files works on them anyway.
            locator.first.wait_for(state="attached", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - any miss just means try the next one
            continue
        logger.debug("%s -> %s", key, selector)
        return locator
    raise SelectorError(key, tried)


def resolve(page: Page, key: str, scope=None) -> Locator:
    """One element for ``key``, scoped to the upload frame unless told otherwise."""
    if scope is None:
        scope = page if key in _PAGE_SCOPED else upload_frame(page)
    return _chain(scope, key).first


def check_selectors(page: Page) -> Dict[str, bool]:
    """Report which selector chains match right now, uploading nothing.

    Only :data:`PREATTACH_KEYS` can match on a bare upload page, so the rest
    reading False is the expected result rather than a breakage; it is a
    *change* in this report that is worth reacting to.
    """
    engine = _engine()
    engine.set_asset_blocking(page, False)
    engine.goto(page, UPLOAD_URL)
    dismiss_consent(page)
    _assert_on_upload_page(page, engine)
    frame = wait_for_upload_frame(page)

    results = {}
    for key in SELECTORS:
        scope = page if key in _PAGE_SCOPED else frame
        try:
            _chain(scope, key)
            results[key] = True
        except SelectorError:
            results[key] = False
    return results


# The select keeps its chosen label in a hidden native input. Reading that back
# is the only proof the click landed on the option we meant.
PRIVACY_READBACK = (
    'input.MuiSelect-nativeInput[value="%s"]',
    'input[value="%s"]',
)


def _privacy_reads(frame, label: str) -> bool:
    for selector in PRIVACY_READBACK:
        try:
            if frame.locator(selector % label).count():
                return True
        except Exception:  # noqa: BLE001 - a missing readback is just a miss
            continue
    return False


def set_privacy(page: Page, frame, privacy: str) -> bool:
    """Switch the privacy select, returning whether it actually reads that now.

    This reports the readback rather than the fact that a click happened,
    because the one failure that cannot be undone is publishing something the
    user asked to keep private.
    """
    label = privacy.capitalize()
    if _privacy_reads(frame, label):
        return True

    try:
        _chain(frame, "privacy_select").first.click(timeout=RESOLVE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - reported through the return value
        logger.warning("privacy select would not open")
        return False

    key = "privacy_option_%s" % privacy
    for scope in (page, frame):
        try:
            _chain(scope, key).first.click(timeout=RESOLVE_TIMEOUT_MS)
            break
        except Exception:  # noqa: BLE001 - portal first, frame second
            continue

    return _privacy_reads(frame, label)


# Reading the signed-in user out of the page beats configuring it: the session
# already knows who it is, and a hardcoded slug quietly checks the wrong
# profile the first time someone runs this on another account.
PROFILE_SLUG_JS = (
    "() => { const h = window.__sc_hydration || [];"
    " const me = h.find(x => x.hydratable === 'meUser');"
    " return me && me.data ? (me.data.permalink || null) : null; }"
)


def profile_slug(page: Page) -> Optional[str]:
    """The signed-in user's permalink, or None if the page will not say."""
    try:
        slug = page.evaluate(PROFILE_SLUG_JS)
        if slug:
            return str(slug).strip("/")
    except Exception:
        logger.debug("hydration lookup failed", exc_info=True)

    try:
        href = resolve(page, "profile_link").get_attribute("href") or ""
    except Exception:  # noqa: BLE001
        return None
    path = urlparse(href).path if "://" in href else href
    return path.strip("/").split("/")[0] or None


def existing_titles(page: Page) -> Set[str]:
    """Track titles already on the signed-in profile.

    Any failure comes back as an empty set: a dedupe check that cannot run
    should cost the caller the optimisation, never the upload it was there to
    speed up.
    """
    try:
        engine = _engine()
        if "soundcloud.com" not in (getattr(page, "url", "") or ""):
            engine.goto(page, SOUNDCLOUD)
        slug = profile_slug(page)
        if not slug:
            logger.debug("no profile slug; uploading without the dedupe check")
            return set()
        engine.goto(page, "%s/%s/tracks" % (SOUNDCLOUD, slug))
        texts = _chain(page, "profile_track_title").all_inner_texts()
    except Exception:
        logger.debug("could not read the profile's tracks", exc_info=True)
        return set()
    return {text.strip() for text in texts if text and text.strip()}


# The multi-file form is a DIFFERENT shape from the single-track one, verified
# live on 2026-08-09. In album mode `title` is the SET name (not a track), each
# track has its own `trackList.N.title`, and privacy is a radio pair rather than
# the MUI combobox used for a single track. Writing a track title into `title`
# leaves the set unnamed, which is how an album once published as its first
# track's name.
ALBUM_SELECTORS = {
    "album_title": 'input[name="title"]',
    "album_artist": 'input[name="artist"]',
    "album_type": 'input[name="albumType"]',
    "track_title": 'input[name="trackList.%d.title"]',
    "privacy_radio": 'input[name="privacy"][value="%s"]',
}


def is_album_form(frame) -> bool:
    """Whether the set editor is showing, rather than the single-track form."""
    try:
        return frame.locator('input[name="trackList.0.title"]').count() > 0
    except Exception:  # noqa: BLE001
        return False


def fill_album_form(page, frame, album, artist=None, privacy="private") -> None:
    """Fill the set editor: album name, per-track titles, artist and privacy.

    A privacy setting that will not take raises ``UploadError``. Never let that
    fall through -- the form defaults to Public.
    """
    frame.locator(ALBUM_SELECTORS["album_title"]).first.fill(album.title)

    credited = artist or album.artist
    if credited:
        frame.locator(ALBUM_SELECTORS["album_artist"]).first.fill(credited)

    for index, track in enumerate(album.tracks):
        field = frame.locator(ALBUM_SELECTORS["track_title"] % index)
        if field.count():
            field.first.fill(track.title)
        else:
            logger.warning("no trackList.%d.title field for %r", index, track.title)

    wanted = "Private" if privacy == "private" else "Public"
    radio = frame.locator(ALBUM_SELECTORS["privacy_radio"] % wanted)
    if radio.count():
        radio.first.check(force=True)
    if privacy == "private" and not _privacy_is(frame, "Private"):
        raise UploadError(
            "could not set the album to Private; refusing to upload rather than publish it publicly"
        )


def _privacy_is(frame, wanted: str) -> bool:
    """Whether the privacy radio for ``wanted`` is actually selected."""
    try:
        return frame.locator(ALBUM_SELECTORS["privacy_radio"] % wanted).first.is_checked()
    except Exception:  # noqa: BLE001
        return False


def upload_album(
    page: Page,
    album: AlbumUpload,
    privacy: str = "private",
    dry_run: bool = False,
    artist: Optional[str] = None,
    skip_existing: bool = True,
) -> Optional[str]:
    """Upload every track of ``album`` and return where they landed, if it can be read.

    Requires :func:`prepare_for_upload` to have run. ``artist`` overrides
    ``album.artist`` on the artist field. ``dry_run`` walks the page and reports
    the plan without attaching a file or pressing Upload.
    """
    if privacy not in ("private", "public"):
        raise ValueError("privacy must be 'private' or 'public', got %r" % (privacy,))
    if not album.tracks:
        raise UploadError("album %r has no tracks to upload" % album.title)

    engine = _engine()
    # Artwork previews and the progress UI are images and CSS, so the blocking
    # that makes scraping fast has to come off for this page.
    engine.set_asset_blocking(page, False)

    # Before /upload rather than after: this navigates, and navigating once the
    # upload page is up would throw away whatever is already attached.
    known = existing_titles(page) if skip_existing else set()

    engine.goto(page, UPLOAD_URL)
    dismiss_consent(page)
    _assert_on_upload_page(page, engine)
    frame = wait_for_upload_frame(page)

    pending = []
    for track in album.tracks:
        if track.title in known:
            logger.info("skipping %r: already on the profile", track.title)
            continue
        pending.append(track)
    if not pending:
        logger.info("every track of %r is already on the profile", album.title)
        return None

    credit = artist or album.artist
    files = _upload_paths(pending, dry_run)

    if dry_run:
        # Stops here on purpose: every remaining step needs the metadata form,
        # and the only way to make that form exist is to attach the files --
        # which is itself the upload.
        _report_plan(frame, pending, files, privacy, credit)
        return None

    _chain(frame, "file_input").first.set_input_files(files)
    _wait_for_form(frame, len(files))

    if is_album_form(frame):
        # Multiple files: the set editor is showing, which names the album in
        # its own field and each track in trackList.N.title. The single-track
        # path would leave the set unnamed and every prefix intact.
        album_for_form = replace(album, tracks=list(pending))
        fill_album_form(page, frame, album_for_form, artist=credit, privacy=privacy)
        if album.cover is not None:
            _attach_artwork(frame, album.cover)
        _chain(frame, "submit_button").first.click()
        _wait_for_success(frame)
        return _result_url(page, frame)

    _fill_metadata(frame, pending, credit)
    if album.cover is not None:
        _attach_artwork(frame, album.cover)

    if not set_privacy(page, frame, privacy) and privacy == "private":
        # Never fall through to a publish. Nothing has been submitted at this
        # point, so bailing out costs the user a retry and not a takedown.
        raise UploadError(
            "could not switch %r to Private; it is still Public, so nothing was "
            "submitted. Set the privacy by hand in the open browser, or update "
            "uploader.SELECTORS['privacy_select']." % album.title
        )

    _chain(frame, "submit_button").first.click()
    _wait_for_success(frame)
    return _result_url(page, frame)


def _upload_paths(tracks: Sequence[TrackUpload], dry_run: bool) -> List[str]:
    """The files to hand the browser, in track order."""
    files = []
    for track in tracks:
        # A dry run never opens these, so it does not need the transcode to
        # have happened yet.
        source = track.upload_path or (track.path if dry_run else None)
        if source is None:
            raise UploadError(
                "track %r has no upload_path; call prepare_for_upload first" % track.title
            )
        files.append(str(source))
    return files


def _report_plan(frame, tracks, files, privacy, credit) -> None:
    """Log what a real run would have done, for ``dry_run``."""
    try:
        _chain(frame, "file_input")
        ready = "ready"
    except SelectorError:
        ready = "NOT ready: no file input in the upload frame"
    logger.info(
        "dry run: upload page %s; would attach %d file(s) as %s, artist %r",
        ready,
        len(files),
        privacy,
        credit,
    )
    for track, path in zip(tracks, files):
        logger.info("dry run:   %s -> %r", Path(path).name, track.title)


#: SoundCloud answers /upload with a redirect, not an error, when the account
#: cannot upload yet. Each of these needs something from the user that cannot be
#: done from here, and without the check below the run just burns its whole form
#: budget hunting for fields on a page that has none.
_INTERSTITIALS = (
    (
        "/welcome",
        "the account has not finished onboarding: open soundcloud.com, answer "
        '"What are you looking to do on SoundCloud?", then run this again',
    ),
    ("/signin", "the saved session is not signed in any more; run --login again"),
)


def _assert_on_upload_page(page, engine=None) -> None:
    """Get past whatever SoundCloud put in front of /upload, or say why we cannot.

    The welcome wall is dismissable, so it is worth one attempt at the close
    button and a second navigation before giving up. Everything else needs the
    user, so it fails immediately rather than burning the form budget on a page
    that has no form.
    """
    if "/welcome" in (getattr(page, "url", "") or "") and dismiss_welcome(page):
        if engine is not None:
            engine.goto(page, UPLOAD_URL)
            dismiss_consent(page)

    url = getattr(page, "url", "") or ""
    for marker, remedy in _INTERSTITIALS:
        if marker in url:
            raise UploadError("SoundCloud redirected to %s: %s" % (url, remedy))
    if "/upload" not in url:
        raise UploadError("expected the upload page, landed on %s" % url)


def _wait_for_form(frame, attached: int = 1) -> None:
    """Block until the metadata form exists and has finished prefilling itself.

    ``attached`` is how many files were just handed over. The budget grows with
    it because nothing appears until the browser has read them all, and a whole
    album takes minutes rather than seconds.
    """
    budget = FORM_TIMEOUT_MS + FORM_TIMEOUT_PER_FILE_MS * max(0, attached - 1)
    logger.info(
        "waiting up to %ds for the upload form (%d file(s) attached)",
        budget // 1000,
        attached,
    )
    _chain(frame, "title_input", timeout_ms=budget)
    frame.wait_for_timeout(FORM_SETTLE_MS)


def _fill_metadata(frame, tracks: Sequence[TrackUpload], credit: Optional[str]) -> None:
    """Overwrite the prefills on every attached track.

    Both fields arrive filled in and both are wrong: the title is the filename
    stem, so it still carries the ``01 - `` prefix, and the artist is the
    account name rather than whoever made the release.
    """
    titles = _chain(frame, "title_input")
    count = titles.count()
    if count < len(tracks):
        logger.warning(
            "%d title field(s) for %d attached track(s); only the first %d were "
            "renamed -- check the rest before publishing",
            count,
            len(tracks),
            count,
        )
    for index, track in enumerate(tracks[:count]):
        titles.nth(index).fill(track.title)

    if not credit:
        return
    try:
        artists = _chain(frame, "artist_input")
    except SelectorError:
        logger.warning("no artist field on this form; the account name stands")
        return
    for index in range(min(artists.count(), len(tracks))):
        artists.nth(index).fill(credit)


def _attach_artwork(frame, cover: Path) -> None:
    """Attach ``cover`` to every artwork control on the form.

    A missing artwork control is a warning, not a failure: the audio is the
    point and artwork can be added afterwards on the site.
    """
    try:
        try:
            inputs = _chain(frame, "artwork_input")
        except SelectorError:
            # Some layouts only create the input once the control is clicked.
            _chain(frame, "artwork_button").first.click()
            inputs = _chain(frame, "artwork_input")
        for index in range(inputs.count()):
            inputs.nth(index).set_input_files(str(cover))
    except Exception:
        logger.warning("could not attach %s", cover, exc_info=True)


def _page_message(scope) -> Optional[str]:
    """Text of a quota notice or error banner if one is showing."""
    for key in ("quota_notice", "error_message"):
        try:
            text = (_chain(scope, key).first.inner_text() or "").strip()
        except Exception:  # noqa: BLE001 - nothing showing is the normal case
            continue
        if text:
            return text
    return None


def _wait_for_success(frame) -> None:
    """Block until SoundCloud confirms, or fail saying what it said instead."""
    try:
        _chain(frame, "success_marker", timeout_ms=SUBMIT_TIMEOUT_MS)
    except SelectorError as exc:
        message = _page_message(frame)
        raise UploadError(
            "no upload confirmation within %dms%s; the tracks may still be "
            "processing on soundcloud.com"
            % (SUBMIT_TIMEOUT_MS, " (the page said: %s)" % message if message else "")
        ) from exc


def _result_url(page: Page, frame) -> Optional[str]:
    """Where the upload landed.

    Falls back to the profile's own tracks page, which is where loose tracks
    show up and is more use to the caller than None.
    """
    for scope in (frame, page):
        try:
            href = _chain(scope, "result_link").first.get_attribute("href")
        except Exception:  # noqa: BLE001 - the panel does not always link out
            continue
        if href:
            return href if href.startswith("http") else SOUNDCLOUD + href

    slug = profile_slug(page)
    return "%s/%s/tracks" % (SOUNDCLOUD, slug) if slug else None
