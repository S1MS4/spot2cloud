"""Tests for uploader, run entirely offline.

The audio fixtures are real one-second files written by ffmpeg and tagged with
mutagen, because the point of scan_album is reading tags that other tools
wrote. The browser half runs against a fake Page: no Playwright, no network and
nothing is ever uploaded.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spot2cloud.soundcloud import uploader

#: Resolved from PATH. These fixtures are real audio because the point of
#: scan_album is reading tags some other tool wrote; without ffmpeg they skip.
FFMPEG = shutil.which("ffmpeg")


def _nowhere(*names) -> list:
    """Expected upload paths, rendered the way the platform renders them.

    ``uploader`` passes ``str(Path(...))`` to the browser, which is
    backslash-separated on Windows, so a hardcoded "/nowhere/One.flac" only
    matches on POSIX.
    """
    return [str(Path("/nowhere") / name) for name in names]


def _write_audio(path: Path, **tags) -> Path:
    """One second of silence at ``path``, tagged with ``tags``."""
    if FFMPEG is None:
        raise unittest.SkipTest("ffmpeg is required to build the audio fixtures")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-t",
            "1",
            str(path),
        ],
        check=True,
    )
    if tags:
        import mutagen

        audio = mutagen.File(str(path), easy=True)
        if audio.tags is None:
            audio.add_tags()
        for key, value in tags.items():
            audio.tags[key] = [str(value)]
        audio.save()
    return path


class FakeTimeout(Exception):
    """Stands in for playwright's TimeoutError, which the chain walk swallows."""


# Clicking one of these is what flips SoundCloud's hidden privacy input, so the
# fake has to model that link rather than just recording the click.
_PRIVACY_OPTIONS = dict(
    [
        (selector, label)
        for label in ("Private", "Public")
        for selector in uploader.SELECTORS["privacy_option_%s" % label.lower()]
    ]
)


def _counts(present):
    """Normalise a set of selectors, or a {selector: count} map, to a count map."""
    if isinstance(present, dict):
        return dict(present)
    return dict((selector, 1) for selector in present)


class FakeLocator:
    """One selector's worth of matches, which may be more than one element."""

    def __init__(self, scope, selector, index=0):
        self.scope = scope
        self.selector = selector
        self.index = index

    @property
    def first(self):
        return self

    def nth(self, index):
        return FakeLocator(self.scope, self.selector, index)

    def count(self):
        return self.scope.count_of(self.selector)

    def wait_for(self, state=None, timeout=None):
        self.scope.note("wait", self.selector)
        if not self.count():
            raise FakeTimeout(self.selector)

    def click(self, **kwargs):
        self.scope.note("click", self.selector)
        self.scope.record.clicks.append(self.selector)
        self.scope.record.note_privacy_click(self.selector)

    def fill(self, value, **kwargs):
        self.scope.note("fill", self.selector)
        self.scope.record.fills.append((self.selector, value))

    def set_input_files(self, files, **kwargs):
        self.scope.note("files", self.selector)
        self.scope.record.file_calls.append((self.selector, files))

    def inner_text(self):
        text = self.scope.texts.get(self.selector, "")
        return text[self.index] if isinstance(text, list) else text

    def all_inner_texts(self):
        text = self.scope.texts.get(self.selector, [])
        return list(text) if isinstance(text, list) else [text]

    def get_attribute(self, name):
        return self.scope.attributes.get(self.selector, {}).get(name)


class FakeScope:
    """The part of a Playwright Page or Frame that uploader actually touches."""

    def __init__(self, present=(), texts=None, attributes=None, record=None, url=""):
        self.present = _counts(present)
        self.texts = texts or {}
        self.attributes = attributes or {}
        self.url = url
        # A frame records into its page, so a test makes one set of assertions
        # no matter which scope the uploader reached for.
        self.record = record if record is not None else self
        self.clicks = []
        self.fills = []
        self.file_calls = []
        self.waits = []
        self.events = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, milliseconds):
        self.record.waits.append(milliseconds)
        self.note("timeout", milliseconds)

    def note(self, kind, what):
        self.record.events.append((kind, what))

    def note_privacy_click(self, selector):
        label = _PRIVACY_OPTIONS.get(selector)
        if label and self.privacy_sticks:
            self.privacy = label

    def count_of(self, selector):
        # The privacy readback is state, not markup: it answers with whatever
        # the select currently holds.
        if "MuiSelect-nativeInput" in selector or selector.startswith("input[value="):
            return 1 if '"%s"' % self.record.privacy in selector else 0
        return self.present.get(selector, 0)


class FakePage(FakeScope):
    """A page, plus the two things only a page has: frames and hydration."""

    def __init__(
        self, present=(), frames=(), hydration=None, privacy="Public", privacy_sticks=True, **kwargs
    ):
        super().__init__(present=present, **kwargs)
        self.url = self.url or "https://soundcloud.com/upload"
        self.frames = list(frames)
        self.hydration = hydration
        self.privacy = privacy
        self.privacy_sticks = privacy_sticks
        self.selector_waits = []

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.selector_waits.append(selector)
        self.note("wait", selector)
        if not any(uploader.UPLOAD_FRAME_HINT in f.url for f in self.frames):
            raise FakeTimeout(selector)

    def evaluate(self, script):
        if self.hydration is None:
            raise FakeTimeout("no hydration on this page")
        return self.hydration


class FakeFrame(FakeScope):
    """The /n/upload iframe."""

    def __init__(self, page, present=(), **kwargs):
        super().__init__(present=present, record=page, **kwargs)
        self.url = self.url or ("https://soundcloud.com/n/upload?crossfade=true&embedded=crossfade")
        page.frames.append(self)


def _first(key):
    return uploader.SELECTORS[key][0]


def _attached_frame(page, tracks=1, without=(), extra=None):
    """The frame as SoundCloud leaves it once files have been attached."""
    present = {
        _first("file_input"): 1,
        _first("choose_files"): 1,
        _first("title_input"): tracks,
        _first("artist_input"): tracks,
        _first("artwork_input"): tracks,
        _first("privacy_select"): 1,
        _first("submit_button"): 1,
        _first("success_marker"): 1,
    }
    for key in without:
        present.pop(_first(key), None)
    present.update(extra or {})
    return FakeFrame(page, present=present)


def _album(*names):
    tracks = [
        uploader.TrackUpload(
            path=Path("/nowhere/%s.opus" % name),
            title=name,
            track_number=index,
            upload_path=Path("/nowhere/%s.flac" % name),
        )
        for index, name in enumerate(names, start=1)
    ]
    return uploader.AlbumUpload(
        folder=Path("/nowhere"), title="Test Album", artist="Artist Name", tracks=tracks
    )


class ScanAlbumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Example Album"
        self.folder.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_orders_by_track_number_not_filename(self):
        # Alphabetical order is the reverse of track order, so a scanner that
        # just sorted by name would come out backwards.
        _write_audio(
            self.folder / "zulu.mp3",
            title="Zulu",
            tracknumber="1",
            album="Example Album",
            albumartist="Artist Name",
        )
        _write_audio(
            self.folder / "alpha.mp3",
            title="Alpha",
            tracknumber="3",
            album="Example Album",
            albumartist="Artist Name",
        )
        _write_audio(
            self.folder / "mike.mp3",
            title="Mike",
            tracknumber="2",
            album="Example Album",
            albumartist="Artist Name",
        )

        album = uploader.scan_album(self.folder)

        self.assertEqual([t.track_number for t in album.tracks], [1, 2, 3])
        self.assertEqual([t.title for t in album.tracks], ["Zulu", "Mike", "Alpha"])
        self.assertEqual(album.title, "Example Album")
        self.assertEqual(album.artist, "Artist Name")

    def test_transcoded_subfolder_is_ignored(self):
        # A previous SoundCloud download leaves an "mp3-192 (transcoded)/" copy
        # of the album beside it; scanning it doubles every track.
        _write_audio(self.folder / "01 - One.opus", title="One", tracknumber="1")
        _write_audio(
            self.folder / "mp3-192 (transcoded)" / "01 - One.mp3", title="One", tracknumber="1"
        )

        album = uploader.scan_album(self.folder)

        self.assertEqual(len(album.tracks), 1)
        self.assertEqual(album.tracks[0].path.suffix, ".opus")

    def test_track_number_falls_back_to_the_filename_prefix(self):
        _write_audio(self.folder / "02 - Second.opus", title="Second")
        _write_audio(self.folder / "01 - First.opus", title="First")

        album = uploader.scan_album(self.folder)

        self.assertEqual([t.track_number for t in album.tracks], [1, 2])
        self.assertEqual([t.title for t in album.tracks], ["First", "Second"])

    def test_title_falls_back_to_filename_without_prefix(self):
        _write_audio(self.folder / "03 - Bare Title.wav")

        album = uploader.scan_album(self.folder)

        self.assertEqual(album.tracks[0].title, "Bare Title")
        self.assertEqual(album.tracks[0].track_number, 3)
        self.assertEqual(album.title, "Example Album")

    def test_cover_art_beside_the_tracks_is_picked_up(self):
        _write_audio(self.folder / "01 - One.opus")
        cover = self.folder / "cover.jpg"
        cover.write_bytes(b"not really a jpeg")

        self.assertEqual(uploader.scan_album(self.folder).cover, cover)

    def test_missing_cover_is_none(self):
        _write_audio(self.folder / "01 - One.opus")

        self.assertIsNone(uploader.scan_album(self.folder).cover)

    def test_empty_folder_raises(self):
        with self.assertRaises(ValueError) as caught:
            uploader.scan_album(self.folder)
        self.assertIn(str(self.folder), str(caught.exception))


class PrepareForUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Album"
        self.folder.mkdir()
        self.addCleanup(self.tmp.cleanup)

        _write_audio(self.folder / "01 - Opus Track.opus", title="Opus Track", tracknumber="1")
        _write_audio(self.folder / "02 - Mp3 Track.mp3", title="Mp3 Track", tracknumber="2")
        self.album = uploader.scan_album(self.folder)

    def test_transcodes_opus_and_leaves_mp3_alone(self):
        prepared = uploader.prepare_for_upload(self.album)
        opus, mp3 = prepared.tracks

        self.assertEqual(opus.upload_path.suffix, ".flac")
        self.assertEqual(opus.upload_path.parent, self.folder / uploader.UPLOAD_SUBDIR)
        self.assertTrue(opus.upload_path.exists())
        self.assertGreater(opus.upload_path.stat().st_size, 0)

        self.assertEqual(mp3.upload_path, mp3.path)
        self.assertTrue(all(t.upload_path is not None for t in prepared.tracks))

    def test_is_idempotent_and_does_not_retranscode(self):
        first = uploader.prepare_for_upload(self.album)
        produced = first.tracks[0].upload_path

        with mock.patch.object(uploader.subprocess, "run") as run:
            second = uploader.prepare_for_upload(uploader.scan_album(self.folder))

        run.assert_not_called()
        self.assertEqual(second.tracks[0].upload_path, produced)

    def test_transcoded_output_carries_tags_across(self):
        prepared = uploader.prepare_for_upload(self.album)

        import mutagen

        tags = mutagen.File(str(prepared.tracks[0].upload_path), easy=True)
        self.assertEqual(tags["title"], ["Opus Track"])


class FrameScopingTests(unittest.TestCase):
    def test_finds_the_n_upload_frame_among_the_others(self):
        page = FakePage()
        FakeScope(url="https://soundcloud.com/upload", record=page)
        page.frames.append(FakeScope(url="https://www.google.com/recaptcha/api2/aframe"))
        frame = _attached_frame(page)

        self.assertIs(uploader.upload_frame(page), frame)

    def test_falls_back_to_the_page_when_nothing_is_iframed(self):
        page = FakePage()

        self.assertIs(uploader.upload_frame(page), page)

    def test_wait_for_upload_frame_waits_on_the_iframe_element(self):
        page = FakePage()
        frame = _attached_frame(page)

        self.assertIs(uploader.wait_for_upload_frame(page), frame)
        self.assertIn(uploader.UPLOAD_FRAME_SELECTOR, page.selector_waits)

    def test_wait_for_upload_frame_gives_up_and_returns_the_page(self):
        page = FakePage()

        self.assertIs(uploader.wait_for_upload_frame(page), page)
        self.assertIn(uploader.UPLOAD_FRAME_SELECTOR, page.selector_waits)

    def test_selectors_resolve_in_the_frame_and_not_on_the_page(self):
        # The live outer page holds no file input at all; only frame scoping
        # makes this chain match.
        page = FakePage()
        frame = _attached_frame(page)

        self.assertIs(uploader.resolve(page, "file_input").scope, frame)

    def test_a_match_on_the_outer_page_alone_is_not_enough(self):
        page = FakePage(present={_first("file_input"): 1})
        _attached_frame(page, without=("file_input",))

        with self.assertRaises(uploader.SelectorError):
            uploader.resolve(page, "file_input")

    def test_privacy_options_are_looked_for_on_the_page_not_the_frame(self):
        page = FakePage(present={_first("privacy_option_private"): 1})
        _attached_frame(page)

        self.assertIs(uploader.resolve(page, "privacy_option_private").scope, page)


class ConsentTests(unittest.TestCase):
    def test_accepts_the_onetrust_banner(self):
        page = FakePage(present={"#onetrust-accept-btn-handler": 1})

        self.assertTrue(uploader.dismiss_consent(page))
        self.assertEqual(page.clicks, ["#onetrust-accept-btn-handler"])

    def test_an_absent_banner_is_not_a_failure(self):
        page = FakePage()

        self.assertFalse(uploader.dismiss_consent(page))
        self.assertEqual(page.clicks, [])

    def test_upload_album_dismisses_consent_before_resolving_anything(self):
        # While the banner is up the upload UI does not render, so every
        # selector would miss if this ran the other way round.
        page = FakePage(
            present={
                "#onetrust-accept-btn-handler": 1,
                _first("privacy_option_private"): 1,
            }
        )
        _attached_frame(page)
        with mock.patch.object(uploader, "_engine", return_value=mock.Mock()):
            uploader.upload_album(page, _album("One"), skip_existing=False)

        kinds = [what for _, what in page.events]
        self.assertLess(
            kinds.index("#onetrust-accept-btn-handler"),
            kinds.index(uploader.UPLOAD_FRAME_SELECTOR),
        )


class ResolveTests(unittest.TestCase):
    def test_walks_the_fallback_chain(self):
        chain = uploader.SELECTORS["submit_button"]
        page = FakePage()
        FakeFrame(page, present={chain[-1]: 1})

        self.assertEqual(uploader.resolve(page, "submit_button").selector, chain[-1])

    def test_prefers_the_first_match(self):
        chain = uploader.SELECTORS["submit_button"]
        page = FakePage()
        FakeFrame(page, present={chain[0]: 1, chain[-1]: 1})

        self.assertEqual(uploader.resolve(page, "submit_button").selector, chain[0])

    def test_raises_selector_error_naming_everything_tried(self):
        page = FakePage()
        FakeFrame(page)

        with self.assertRaises(uploader.SelectorError) as caught:
            uploader.resolve(page, "submit_button")

        error = caught.exception
        self.assertEqual(error.key, "submit_button")
        self.assertEqual(error.tried, uploader.SELECTORS["submit_button"])
        for selector in uploader.SELECTORS["submit_button"]:
            self.assertIn(selector, str(error))


class CheckSelectorsTests(unittest.TestCase):
    def test_reports_per_key_and_uploads_nothing(self):
        page = FakePage()
        _attached_frame(page, without=("submit_button",))
        engine = mock.Mock()

        with mock.patch.object(uploader, "_engine", return_value=engine):
            report = uploader.check_selectors(page)

        self.assertEqual(sorted(report), sorted(uploader.SELECTORS))
        self.assertTrue(report["file_input"])
        self.assertTrue(report["title_input"])
        self.assertFalse(report["submit_button"])
        engine.goto.assert_called_once_with(page, uploader.UPLOAD_URL)
        engine.set_asset_blocking.assert_called_once_with(page, False)
        self.assertEqual(page.file_calls, [])
        self.assertEqual(page.clicks, [])

    def test_a_bare_page_still_matches_the_preattach_keys(self):
        # The metadata form does not exist until files are attached, so this is
        # the honest report for an untouched upload page.
        page = FakePage()
        FakeFrame(page, present={_first("file_input"): 1, _first("choose_files"): 1})

        with mock.patch.object(uploader, "_engine", return_value=mock.Mock()):
            report = uploader.check_selectors(page)

        self.assertTrue(all(report[key] for key in uploader.PREATTACH_KEYS))
        self.assertFalse(report["title_input"])


class SetPrivacyTests(unittest.TestCase):
    def test_no_op_when_it_already_reads_the_requested_value(self):
        page = FakePage(privacy="Private")
        frame = _attached_frame(page)

        self.assertTrue(uploader.set_privacy(page, frame, "private"))
        self.assertEqual(page.clicks, [])

    def test_opens_the_select_and_clicks_the_portalled_option(self):
        page = FakePage(present={_first("privacy_option_private"): 1})
        frame = _attached_frame(page)

        self.assertTrue(uploader.set_privacy(page, frame, "private"))
        self.assertEqual(
            page.clicks,
            [_first("privacy_select"), _first("privacy_option_private")],
        )

    def test_falls_back_to_the_frame_when_the_option_is_not_portalled(self):
        page = FakePage()
        frame = _attached_frame(page, extra={_first("privacy_option_private"): 1})

        self.assertTrue(uploader.set_privacy(page, frame, "private"))

    def test_returns_false_when_the_click_does_not_take(self):
        # The click lands and the select still reads Public. Trusting the click
        # instead of the readback is what would publish someone's private set.
        page = FakePage(present={_first("privacy_option_private"): 1}, privacy_sticks=False)
        frame = _attached_frame(page)

        self.assertFalse(uploader.set_privacy(page, frame, "private"))

    def test_returns_false_when_the_select_will_not_open(self):
        page = FakePage()
        frame = _attached_frame(page, without=("privacy_select",))

        # Patched rather than captured: other test modules in this suite call
        # logging.disable at import time.
        with mock.patch.object(uploader.logger, "warning") as warned:
            self.assertFalse(uploader.set_privacy(page, frame, "private"))
        self.assertTrue(warned.called)

    def test_public_is_switchable_too(self):
        page = FakePage(present={_first("privacy_option_public"): 1}, privacy="Private")
        frame = _attached_frame(page)

        self.assertTrue(uploader.set_privacy(page, frame, "public"))
        self.assertEqual(page.privacy, "Public")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = mock.Mock()
        patcher = mock.patch.object(uploader, "_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_slug_comes_from_the_session_not_from_configuration(self):
        page = FakePage(hydration="git-rm")

        self.assertEqual(uploader.profile_slug(page), "git-rm")

    def test_slug_falls_back_to_the_header_link(self):
        page = FakePage(
            present={_first("profile_link"): 1},
            attributes={_first("profile_link"): {"href": "/git-rm"}},
        )

        self.assertEqual(uploader.profile_slug(page), "git-rm")

    def test_slug_handles_an_absolute_header_link(self):
        page = FakePage(
            present={_first("profile_link"): 1},
            attributes={_first("profile_link"): {"href": "https://soundcloud.com/git-rm"}},
        )

        self.assertEqual(uploader.profile_slug(page), "git-rm")

    def test_existing_titles_reads_the_profiles_tracks_page(self):
        page = FakePage(
            hydration="git-rm",
            present={_first("profile_track_title"): 2},
            texts={_first("profile_track_title"): ["Track Eleven,", " Track Ten "]},
        )

        self.assertEqual(uploader.existing_titles(page), {"Track Eleven,", "Track Ten"})
        self.engine.goto.assert_called_with(page, "https://soundcloud.com/git-rm/tracks")

    def test_existing_titles_is_empty_when_the_slug_is_unknown(self):
        self.assertEqual(uploader.existing_titles(FakePage()), set())

    def test_existing_titles_is_empty_when_the_listing_does_not_match(self):
        page = FakePage(hydration="git-rm")

        self.assertEqual(uploader.existing_titles(page), set())

    def test_existing_titles_swallows_a_navigation_failure(self):
        self.engine.goto.side_effect = RuntimeError("nav died")
        page = FakePage(hydration="git-rm", url="https://example.com")

        self.assertEqual(uploader.existing_titles(page), set())


class UploadAlbumTests(unittest.TestCase):
    def setUp(self):
        self.engine = mock.Mock()
        patcher = mock.patch.object(uploader, "_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, tracks=1, without=(), extra=None, **kwargs):
        """A page and its upload frame, in the state that follows an attach."""
        page = FakePage(present={_first("privacy_option_private"): 1}, **kwargs)
        frame = _attached_frame(page, tracks=tracks, without=without, extra=extra)
        return page, frame

    def test_dry_run_attaches_nothing_and_clicks_nothing(self):
        page, _ = self._session()

        result = uploader.upload_album(page, _album("One"), dry_run=True)

        self.assertIsNone(result)
        self.assertEqual(page.file_calls, [])
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.fills, [])
        self.engine.goto.assert_called_with(page, uploader.UPLOAD_URL)

    def test_dry_run_does_not_need_prepare_for_upload_to_have_run(self):
        page, _ = self._session()
        album = _album("One")
        album.tracks[0].upload_path = None

        self.assertIsNone(uploader.upload_album(page, album, dry_run=True))

    def test_every_track_goes_up_in_one_call_in_order(self):
        page, _ = self._session(tracks=3)

        uploader.upload_album(page, _album("One", "Two", "Three"))

        self.assertEqual(
            page.file_calls[0],
            (
                _first("file_input"),
                _nowhere("One.flac", "Two.flac", "Three.flac"),
            ),
        )
        self.assertEqual(len([c for c in page.file_calls if c[0] == _first("file_input")]), 1)

    def test_title_comes_from_the_track_not_from_the_prefill(self):
        # SoundCloud prefills this from the filename stem, so leaving it alone
        # ships every track as "01 - Title".
        page, _ = self._session(tracks=2)

        uploader.upload_album(page, _album("Track Eleven,", "Track Ten"))

        self.assertEqual(
            [value for selector, value in page.fills if selector == _first("title_input")],
            ["Track Eleven,", "Track Ten"],
        )

    def test_artist_argument_overrides_the_albums_own_tag(self):
        page, _ = self._session()

        uploader.upload_album(page, _album("One"), artist="Spurr")

        self.assertIn((_first("artist_input"), "Spurr"), page.fills)
        self.assertNotIn((_first("artist_input"), "Artist Name"), page.fills)

    def test_artist_defaults_to_the_album(self):
        page, _ = self._session()

        uploader.upload_album(page, _album("One"))

        self.assertIn((_first("artist_input"), "Artist Name"), page.fills)

    def test_cover_is_attached_when_the_folder_has_one(self):
        page, _ = self._session()
        album = _album("One")
        album.cover = Path("/nowhere/cover.jpg")

        uploader.upload_album(page, album)

        self.assertIn((_first("artwork_input"), _nowhere("cover.jpg")[0]), page.file_calls)

    def test_a_missing_artwork_control_does_not_fail_the_upload(self):
        page, _ = self._session(without=("artwork_input", "artwork_button"))
        album = _album("One")
        album.cover = Path("/nowhere/cover.jpg")

        with mock.patch.object(uploader.logger, "warning") as warned:
            uploader.upload_album(page, album)
        self.assertTrue(warned.called)

        self.assertIn(_first("submit_button"), page.clicks)

    def test_a_failed_privacy_change_raises_instead_of_uploading(self):
        page, _ = self._session(privacy_sticks=False)

        with self.assertRaises(uploader.UploadError) as caught:
            uploader.upload_album(page, _album("One"), privacy="private")

        self.assertIn("Private", str(caught.exception))
        self.assertNotIn(_first("submit_button"), page.clicks)

    def test_a_failed_public_change_is_not_fatal(self):
        # Failing closed only matters in one direction: staying private is
        # never the outcome that cannot be taken back.
        page, _ = self._session(privacy_sticks=False)

        uploader.upload_album(page, _album("One"), privacy="public")

        self.assertIn(_first("submit_button"), page.clicks)

    def test_privacy_is_set_before_the_upload_button_is_pressed(self):
        page, _ = self._session()

        uploader.upload_album(page, _album("One"), privacy="private")

        self.assertLess(
            page.clicks.index(_first("privacy_option_private")),
            page.clicks.index(_first("submit_button")),
        )

    def test_tracks_already_on_the_profile_are_skipped(self):
        page, _ = self._session(
            tracks=2,
            hydration="git-rm",
            texts={_first("profile_track_title"): ["Two"]},
        )
        page.present[_first("profile_track_title")] = 1

        uploader.upload_album(page, _album("One", "Two", "Three"))

        self.assertEqual(page.file_calls[0][1], _nowhere("One.flac", "Three.flac"))

    def test_an_album_that_is_entirely_uploaded_already_does_nothing(self):
        page, _ = self._session(hydration="git-rm", texts={_first("profile_track_title"): ["One"]})
        page.present[_first("profile_track_title")] = 1

        self.assertIsNone(uploader.upload_album(page, _album("One")))
        self.assertEqual(page.file_calls, [])
        self.assertEqual(page.clicks, [])

    def test_skip_existing_false_never_reads_the_profile(self):
        page, _ = self._session(hydration="git-rm", texts={_first("profile_track_title"): ["One"]})
        page.present[_first("profile_track_title")] = 1

        uploader.upload_album(page, _album("One"), skip_existing=False)

        self.assertEqual(page.file_calls[0][1], _nowhere("One.flac"))

    def test_a_broken_dedupe_check_does_not_block_the_upload(self):
        # Losing the optimisation is fine; losing the upload is not.
        page, _ = self._session(hydration="git-rm")

        def explode(_page, url):
            if url.endswith("/tracks"):
                raise RuntimeError("profile page would not load")

        self.engine.goto.side_effect = explode

        uploader.upload_album(page, _album("One"))

        self.assertEqual(page.file_calls[0][1], _nowhere("One.flac"))
        self.assertIn(_first("submit_button"), page.clicks)

    def test_returns_the_link_the_success_panel_offers(self):
        page, _ = self._session(extra={_first("result_link"): 1})
        page.frames[0].attributes[_first("result_link")] = {"href": "/git-rm/sets/mixtape"}

        url = uploader.upload_album(page, _album("One"))

        self.assertEqual(url, "https://soundcloud.com/git-rm/sets/mixtape")

    def test_falls_back_to_the_profiles_tracks_page(self):
        page, _ = self._session(hydration="git-rm")

        self.assertEqual(
            uploader.upload_album(page, _album("One")),
            "https://soundcloud.com/git-rm/tracks",
        )

    def test_no_confirmation_is_an_error_carrying_what_the_page_said(self):
        page, _ = self._session(
            without=("success_marker",),
            extra={uploader.SELECTORS["quota_notice"][0]: 1},
        )
        page.frames[0].texts[uploader.SELECTORS["quota_notice"][0]] = (
            "You have reached your upload limit."
        )

        with self.assertRaises(uploader.UploadError) as caught:
            uploader.upload_album(page, _album("One"))

        self.assertIn("upload limit", str(caught.exception))

    def test_rejects_unknown_privacy(self):
        page, _ = self._session()

        with self.assertRaises(ValueError):
            uploader.upload_album(page, _album("One"), privacy="ok")

        self.assertEqual(page.file_calls, [])

    def test_unprepared_track_is_rejected(self):
        page, _ = self._session()
        album = _album("One")
        album.tracks[0].upload_path = None

        with self.assertRaises(uploader.UploadError) as caught:
            uploader.upload_album(page, album)

        self.assertIn("prepare_for_upload", str(caught.exception))
        self.assertEqual(page.file_calls, [])

    def test_an_empty_album_is_rejected(self):
        page, _ = self._session()

        with self.assertRaises(uploader.UploadError):
            uploader.upload_album(page, _album())

    def test_asset_blocking_is_turned_off(self):
        page, _ = self._session()

        uploader.upload_album(page, _album("One"))

        self.engine.set_asset_blocking.assert_any_call(page, False)


if __name__ == "__main__":
    unittest.main()
