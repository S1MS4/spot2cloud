"""Offline unit tests for the playlist -> MP3 pipeline.

Everything here is hermetic: no network, no real downloads, no real Spotify or
YouTube calls, and no sleeps longer than a few tens of milliseconds. yt-dlp and
spotipy entry points are patched out at the module boundary the production code
actually calls (``resolver.YoutubeDL``, ``playlist_parser._flat_search``,
``playlist_parser._spotify_client``, ``downloader.download_one``), so the tests
exercise the real logic rather than a re-implementation of it.

Only the standard library is used -- ``unittest`` plus ``unittest.mock`` -- so
the test suite adds no dependency to the project.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sc_scrape.store.history as history_mod
from sc_scrape import models
from sc_scrape.download import downloader, pipeline, resolver
from sc_scrape.models import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    DownloadResult,
    PlaylistJob,
    Track,
)
from sc_scrape.sources import playlist_parser

# Several tests drive the modules down their failure paths, which log warnings
# by design; the assertions, not the log lines, are what prove the behaviour.
logging.disable(logging.CRITICAL)

# A syntactically valid 22-character base62 Spotify id, reused throughout.
PLAYLIST_ID = "EXAMPLEPLAYLISTID00000"
ALBUM_ID = "EXAMPLETRACKID00000001"


class TestSanitizeFilename(unittest.TestCase):
    """Filenames must survive Windows, macOS and Linux without mangling."""

    def test_emoji_and_pictographs_are_stripped(self):
        out = downloader.sanitize_filename("Sunset 🎵 Drive 🚗 Mix ⭐")
        self.assertNotIn("🎵", out, "musical-note emoji survived sanitisation: %r" % out)
        self.assertNotIn("🚗", out, "car pictograph survived sanitisation: %r" % out)
        self.assertNotIn("⭐", out, "star pictograph (So) survived: %r" % out)
        self.assertEqual(
            "Sunset Drive Mix",
            out,
            "expected the words to remain with whitespace collapsed, got %r" % out,
        )

    def test_trademark_sign_is_nfkc_folded_to_letters(self):
        # NFKC runs first, so U+2122 becomes the letters "TM" and never reaches
        # the So/Sk filter. Documented here so the behaviour is not mistaken
        # for a leak in the pictograph stripper.
        self.assertEqual("Song TM", downloader.sanitize_filename("Song ™"))

    def test_currency_and_math_symbols_are_retained(self):
        out = downloader.sanitize_filename("Money $ + Power = Fun")
        for char in ("$", "+", "="):
            self.assertIn(
                char,
                out,
                "%r must be kept (Sc/Sm are readable and legal), got %r" % (char, out),
            )
        self.assertEqual("Money $ + Power = Fun", out)

    def test_filesystem_illegal_characters_are_removed(self):
        out = downloader.sanitize_filename('AC<>DC:Back"In/Black\\Now|Or?Never*')
        for char in '<>:"/\\|?*':
            self.assertNotIn(
                char,
                out,
                "illegal character %r survived: %r" % (char, out),
            )
        self.assertEqual("ACDCBackInBlackNowOrNever", out)

    def test_control_characters_are_removed(self):
        # Control bytes -- including tab and newline -- are deleted outright by
        # the illegal-character pass, which runs before whitespace collapsing.
        out = downloader.sanitize_filename("Tab\there\x00and\x1fthere")
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x1f", out)
        self.assertNotIn("\t", out)
        self.assertEqual("Tabhereandthere", out)

    def test_whitespace_is_collapsed_and_trimmed(self):
        out = downloader.sanitize_filename("   many     spaces   and   more   ")
        self.assertEqual("many spaces and more", out)

    def test_newlines_do_not_survive_as_whitespace(self):
        out = downloader.sanitize_filename("first\nsecond")
        self.assertNotIn("\n", out)
        self.assertEqual("firstsecond", out)

    def test_empty_input_becomes_untitled(self):
        for value in ("", "   ", None, "..."):
            self.assertEqual(
                "untitled",
                downloader.sanitize_filename(value),
                "empty-ish input %r must fall back to 'untitled'" % (value,),
            )

    def test_emoji_only_input_becomes_untitled(self):
        self.assertEqual(
            "untitled",
            downloader.sanitize_filename("🎵🎶🎧"),
            "a title made only of pictographs leaves nothing behind",
        )

    def test_truncation_lands_on_a_utf8_byte_boundary(self):
        # Each CJK codepoint is 3 UTF-8 bytes, so a naive byte slice at 10
        # bytes would cut one in half and produce undecodable output.
        name = "日本語" * 40
        out = downloader.sanitize_filename(name, max_bytes=10)
        encoded = out.encode("utf-8")
        self.assertLessEqual(
            len(encoded),
            10,
            "result is %d bytes, over the %d-byte budget: %r" % (len(encoded), 10, out),
        )
        self.assertEqual(
            out,
            encoded.decode("utf-8"),
            "result does not round-trip through UTF-8; the cut split a codepoint",
        )
        self.assertEqual("日本語", out, "expected 3 whole codepoints (9 bytes), got %r" % out)

    def test_truncation_of_mixed_width_text_stays_decodable(self):
        name = "Café Münchner Freiheit — Ohne Dich Schlaf Ich Heut Nacht Nicht Ein"
        for budget in range(4, 40):
            out = downloader.sanitize_filename(name, max_bytes=budget)
            encoded = out.encode("utf-8")
            self.assertLessEqual(
                len(encoded),
                budget,
                "budget %d exceeded: %r is %d bytes" % (budget, out, len(encoded)),
            )
            self.assertEqual(
                out,
                encoded.decode("utf-8"),
                "budget %d produced invalid UTF-8: %r" % (budget, out),
            )

    def test_default_budget_is_respected(self):
        out = downloader.sanitize_filename("ä" * 500)
        self.assertLessEqual(
            len(out.encode("utf-8")),
            models.MAX_FILENAME_BYTES,
            "default truncation must honour MAX_FILENAME_BYTES",
        )

    def test_windows_reserved_stems_are_escaped(self):
        for reserved in ("CON", "PRN", "NUL", "COM1", "LPT9"):
            for candidate in (reserved, reserved.lower(), reserved + ".song"):
                out = downloader.sanitize_filename(candidate)
                self.assertTrue(
                    out.startswith("_"),
                    "%r is a Windows device name and must be prefixed, got %r" % (candidate, out),
                )

    def test_non_reserved_lookalikes_are_left_alone(self):
        for safe in ("CONCERT", "COM10", "LPT", "NULL"):
            out = downloader.sanitize_filename(safe)
            self.assertFalse(
                out.startswith("_"),
                "%r is not a reserved device name and must not be escaped, got %r" % (safe, out),
            )


class TestEnsureOutputDir(unittest.TestCase):
    """The output directory is created eagerly and idempotently."""

    def test_creates_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Songs" / "My Playlist" / "deep"
            self.assertFalse(target.exists())
            result = downloader.ensure_output_dir(target)
            self.assertTrue(result.is_dir(), "nested output dir was not created")
            self.assertEqual(result, target.resolve())

    def test_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Songs"
            first = downloader.ensure_output_dir(target)
            (first / "keep.txt").write_text("hello", encoding="utf-8")
            second = downloader.ensure_output_dir(target)
            self.assertEqual(first, second)
            self.assertEqual(
                "hello",
                (second / "keep.txt").read_text(encoding="utf-8"),
                "a second call must not wipe the directory",
            )

    def test_raises_when_path_is_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "not-a-dir"
            target.write_text("i am a file", encoding="utf-8")
            with self.assertRaises(NotADirectoryError):
                downloader.ensure_output_dir(target)


def _flat_playlist(title, entries):
    """Build a yt-dlp flat-playlist info dict."""
    return {"_type": "playlist", "title": title, "entries": list(entries)}


class _FakeYDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` usable as a context manager."""

    def __init__(self, responses):
        # responses: url -> info dict, or a callable raising/returning.
        self._responses = responses
        self.calls = []

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        self.calls.append(url)
        value = self._responses[url]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value


class TestExpandPlaylist(unittest.TestCase):
    """``expand_playlist`` returns the name, the tracks and the failures."""

    def test_playlist_yields_title_and_tracks_in_order(self):
        fake = _FakeYDL(
            {
                "PL1": _flat_playlist(
                    "Road Trip",
                    [
                        {"url": "https://youtu.be/aaa", "title": "One"},
                        {"url": "https://youtu.be/bbb", "title": "Two"},
                        {"id": "ccc"},
                    ],
                )
            }
        )
        with mock.patch.object(resolver, "YoutubeDL", fake):
            title, tracks, failures = resolver.expand_playlist("PL1")
        self.assertEqual("Road Trip", title)
        self.assertEqual([], failures)
        self.assertEqual(3, len(tracks))
        self.assertEqual(["One", "Two"], [t.title for t in tracks[:2]])
        self.assertEqual(
            "https://www.youtube.com/watch?v=ccc",
            tracks[2].query,
            "an entry carrying only an id must fall back to a watch?v= URL",
        )
        self.assertEqual(
            tracks[2].query,
            tracks[2].title,
            "a titleless entry uses its URL as the title",
        )

    def test_non_playlist_uses_the_real_video_title(self):
        # Current, deliberate behaviour: the video's own title wins so progress
        # output and the final filename read as a song, not a URL.
        fake = _FakeYDL(
            {
                "https://youtu.be/solo": {
                    "_type": "video",
                    "title": "  Example Track  ",
                }
            }
        )
        with mock.patch.object(resolver, "YoutubeDL", fake):
            title, tracks, failures = resolver.expand_playlist("https://youtu.be/solo")
        self.assertIsNone(title, "a single video has no playlist name")
        self.assertEqual([], failures)
        self.assertEqual(1, len(tracks))
        self.assertEqual(
            "Example Track",
            tracks[0].title,
            "the real video title must be used (stripped), not the URL",
        )
        self.assertEqual("https://youtu.be/solo", tracks[0].query)

    def test_non_playlist_without_a_title_falls_back_to_the_url(self):
        fake = _FakeYDL({"https://youtu.be/solo": {"_type": "video", "title": "   "}})
        with mock.patch.object(resolver, "YoutubeDL", fake):
            title, tracks, failures = resolver.expand_playlist("https://youtu.be/solo")
        self.assertIsNone(title)
        self.assertEqual("https://youtu.be/solo", tracks[0].title)

    def test_extraction_failure_returns_a_failed_result(self):
        fake = _FakeYDL({"BAD": RuntimeError("This video is private")})
        with mock.patch.object(resolver, "YoutubeDL", fake):
            title, tracks, failures = resolver.expand_playlist("BAD")
        self.assertIsNone(title)
        self.assertEqual([], tracks)
        self.assertEqual(1, len(failures))
        self.assertEqual(STATUS_FAILED, failures[0].status)
        self.assertEqual("Private video", failures[0].reason)


class TestClassifyError(unittest.TestCase):
    """yt-dlp's exception text becomes a short, actionable sentence."""

    def assertClassifies(self, message, expected):
        actual = resolver.classify_error(RuntimeError(message))
        self.assertEqual(
            expected,
            actual,
            "message %r classified as %r, expected %r" % (message, actual, expected),
        )

    def test_empty_search_becomes_no_search_results(self):
        self.assertClassifies(
            "ERROR: [youtube:search] No video results", "No search results for this track"
        )

    def test_age_gate_asks_for_a_signed_in_account(self):
        self.assertClassifies(
            "Sign in to confirm your age. This video may be inappropriate for some users.",
            "Age-restricted: YouTube requires a signed-in account",
        )

    def test_bot_check_becomes_sign_in_required(self):
        self.assertClassifies(
            "Sign in to confirm you're not a bot. Use --cookies-from-browser.",
            "Sign-in required: YouTube asked to verify you are not a bot",
        )

    def test_members_only_content_is_named_as_such(self):
        self.assertClassifies(
            "Join this channel to get access to members-only content",
            "Members-only video",
        )

    def test_bare_sign_in_message_uses_the_generic_branch(self):
        self.assertClassifies("ERROR: unable to download: please sign in", "Sign-in required")

    def test_copyright_claim_becomes_a_copyright_block(self):
        self.assertClassifies(
            "This video is no longer available due to a copyright claim by Foo Records",
            "Blocked on copyright grounds",
        )

    def test_uploader_removal_becomes_video_removed(self):
        self.assertClassifies(
            "This video has been removed by the uploader",
            "Video removed or channel terminated",
        )

    def test_bare_video_unavailable_becomes_removed(self):
        self.assertClassifies("Video unavailable", "Video unavailable or removed")

    def test_postprocessing_failure_names_ffmpeg(self):
        self.assertClassifies(
            "ERROR: Postprocessing: ffmpeg exited with code 1",
            "Audio conversion failed (ffmpeg/post-processing error)",
        )

    def test_urlopen_timeout_becomes_a_network_error(self):
        self.assertClassifies(
            "ERROR: unable to download video data: <urlopen error timed out>",
            "Network error (check your connection, then retry)",
        )

    def test_geo_restricted_exception_type_wins(self):
        from yt_dlp.utils import GeoRestrictedError

        reason = resolver.classify_error(GeoRestrictedError("not available"))
        self.assertEqual("Region-blocked: not available in your country", reason)

    def test_geo_blocked_text_beats_generic_video_unavailable(self):
        # YouTube's geo message *starts* with "Video unavailable", so ordering
        # in classify_error is load-bearing: a naive implementation reports the
        # video as removed and the user never learns it is a region block.
        message = (
            "Video unavailable. The uploader has not made this video available in your country"
        )
        reason = resolver.classify_error(RuntimeError(message))
        self.assertEqual(
            "Region-blocked: not available in your country",
            reason,
            "geo-blocking must win over the generic 'Video unavailable' branch, got %r" % reason,
        )
        self.assertNotEqual("Video unavailable or removed", reason)

    def test_private_video_beats_sign_in_required(self):
        # YouTube's private-video text ends with "Sign in ...", which a naive
        # ordering would swallow into the generic sign-in branch.
        message = "Private video. Sign in if you've been granted access to this video"
        reason = resolver.classify_error(RuntimeError(message))
        self.assertEqual(
            "Private video",
            reason,
            "private videos must classify as private, not sign-in-required, got %r" % reason,
        )
        self.assertNotEqual("Sign-in required", reason)

    def test_permission_error_names_the_output_directory(self):
        reason = resolver.classify_error(PermissionError(13, "Permission denied"))
        self.assertEqual("Permission denied writing to the output directory", reason)

    def test_generic_oserror_reports_the_strerror(self):
        reason = resolver.classify_error(OSError(28, "No space left on device"))
        self.assertEqual("Filesystem error: No space left on device", reason)

    def test_unknown_message_is_collapsed_and_truncated(self):
        reason = resolver.classify_error(RuntimeError("weird\n\n   thing   happened"))
        self.assertEqual("weird thing happened", reason)
        long_reason = resolver.classify_error(RuntimeError("z" * 500))
        self.assertLessEqual(len(long_reason), 200)
        self.assertTrue(long_reason.endswith("..."))


class TestDetectInputKind(unittest.TestCase):
    """Classification of whatever the user pasted."""

    def test_spotify_playlist_uri_and_url(self):
        for text in (
            "spotify:playlist:%s" % PLAYLIST_ID,
            "https://open.spotify.com/playlist/%s" % PLAYLIST_ID,
            "http://open.spotify.com/playlist/%s" % PLAYLIST_ID,
            "open.spotify.com/playlist/%s" % PLAYLIST_ID,
        ):
            self.assertEqual(
                playlist_parser.KIND_SPOTIFY,
                playlist_parser.detect_input_kind(text),
                "%r should classify as spotify" % text,
            )

    def test_spotify_intl_locale_prefix(self):
        text = "https://open.spotify.com/intl-de/playlist/%s" % PLAYLIST_ID
        self.assertEqual(playlist_parser.KIND_SPOTIFY, playlist_parser.detect_input_kind(text))
        self.assertEqual(
            (playlist_parser.KIND_PLAYLIST, PLAYLIST_ID),
            playlist_parser.parse_spotify_ref(text),
        )

    def test_spotify_si_query_parameter_is_ignored(self):
        text = "https://open.spotify.com/playlist/%s?si=0b9d3f1c2e4a4f9d" % PLAYLIST_ID
        self.assertEqual(playlist_parser.KIND_SPOTIFY, playlist_parser.detect_input_kind(text))
        self.assertEqual(
            (playlist_parser.KIND_PLAYLIST, PLAYLIST_ID),
            playlist_parser.parse_spotify_ref(text),
            "the ?si= share token must not leak into the parsed id",
        )

    def test_spotify_album_uri_classifies_as_spotify_and_parses_as_album(self):
        text = "spotify:album:%s" % ALBUM_ID
        self.assertEqual(
            playlist_parser.KIND_SPOTIFY,
            playlist_parser.detect_input_kind(text),
            "album URIs are Spotify inputs, not bare names",
        )
        self.assertEqual(
            (playlist_parser.KIND_ALBUM, ALBUM_ID),
            playlist_parser.parse_spotify_ref(text),
        )

    def test_spotify_album_url_classifies_as_spotify_and_parses_as_album(self):
        text = "https://open.spotify.com/album/%s?si=abcdef" % ALBUM_ID
        self.assertEqual(playlist_parser.KIND_SPOTIFY, playlist_parser.detect_input_kind(text))
        self.assertEqual(
            (playlist_parser.KIND_ALBUM, ALBUM_ID),
            playlist_parser.parse_spotify_ref(text),
        )

    def test_every_youtube_url_form_classifies_as_youtube(self):
        for text in (
            "https://www.youtube.com/watch?v=EXAMPLEVID0",
            "https://youtu.be/EXAMPLEVID0",
            "https://www.youtube.com/playlist?list=PLabcdefghij",
            "https://music.youtube.com/playlist?list=OLAK5uy_abcdefghij",
            "https://www.youtube-nocookie.com/watch?v=x",
        ):
            self.assertEqual(
                playlist_parser.KIND_YOUTUBE,
                playlist_parser.detect_input_kind(text),
                "%r should classify as youtube" % text,
            )

    def test_bare_name_and_empty_input(self):
        for text in ("lofi beats to study to", "", "   ", "Discover Weekly"):
            self.assertEqual(
                playlist_parser.KIND_NAME,
                playlist_parser.detect_input_kind(text),
                "%r should classify as a bare name" % text,
            )

    def test_bare_22_char_id_parses_as_a_playlist(self):
        self.assertEqual(
            (playlist_parser.KIND_PLAYLIST, PLAYLIST_ID),
            playlist_parser.parse_spotify_ref(PLAYLIST_ID),
        )

    def test_garbage_raises_value_error(self):
        for text in (
            "definitely not a spotify link",
            "https://open.spotify.com/track/%s" % PLAYLIST_ID,
            "spotify:playlist:tooshort",
            "",
        ):
            with self.assertRaises(ValueError, msg="%r must be rejected" % text):
                playlist_parser.parse_spotify_ref(text)

    def test_parse_spotify_playlist_id_rejects_albums(self):
        with self.assertRaises(ValueError):
            playlist_parser.parse_spotify_playlist_id("spotify:album:%s" % ALBUM_ID)


class TestPickBestMatch(unittest.TestCase):
    """The wrong-match guard: duration first, then live/cover/loop titles."""

    def test_hour_long_loop_is_rejected_in_favour_of_the_right_duration(self):
        track = Track(
            query="ytsearch5:Artist - Song audio",
            title="Song",
            artist="Artist",
            duration_ms=210_000,  # 3:30
        )
        candidates = [
            {"title": "Song - 1 hour loop", "duration": 3600, "id": "loop"},
            {"title": "Artist - Song", "duration": 212, "id": "real"},  # 3:32
        ]
        chosen = playlist_parser.pick_best_match(candidates, track)
        self.assertIsNotNone(chosen, "a 3:32 candidate for a 3:30 track must be accepted")
        self.assertIs(
            candidates[1],
            chosen,
            "expected the 3:32 upload, got %r" % (chosen.get("title"),),
        )

    def test_closest_duration_wins_among_survivors(self):
        track = Track(query="ytsearch5:x", title="Song", duration_ms=210_000)
        candidates = [
            {"title": "Artist - Song", "duration": 212, "id": "further"},
            {"title": "Artist - Song (Official Audio)", "duration": 209, "id": "closer"},
        ]
        chosen = playlist_parser.pick_best_match(candidates, track)
        self.assertIs(
            candidates[1],
            chosen,
            "with both candidates acceptable, the closest duration must win",
        )

    def test_live_candidate_is_kept_when_the_track_itself_is_live(self):
        track = Track(
            query="ytsearch5:Band - Song (Live) audio",
            title="Song (Live)",
            artist="Band",
            duration_ms=240_000,
        )
        candidates = [{"title": "Band - Song (Live) at Wembley", "duration": 241, "id": "l"}]
        chosen = playlist_parser.pick_best_match(candidates, track)
        self.assertIsNotNone(
            chosen,
            "a track genuinely titled 'Song (Live)' must not have live candidates "
            "filtered out - otherwise it is unfindable",
        )
        self.assertIs(candidates[0], chosen)

    def test_live_candidate_is_rejected_for_a_studio_track(self):
        track = Track(
            query="ytsearch5:Band - Song audio",
            title="Song",
            artist="Band",
            duration_ms=240_000,
        )
        candidates = [{"title": "Band - Song (Live at Wembley)", "duration": 241, "id": "l"}]
        self.assertIsNone(
            playlist_parser.pick_best_match(candidates, track),
            "a live take is the wrong version of a studio track",
        )

    def test_nothing_acceptable_returns_none(self):
        track = Track(
            query="ytsearch5:Artist - Song audio",
            title="Song",
            artist="Artist",
            duration_ms=210_000,
        )
        candidates = [
            {"title": "Song (Nightcore)", "duration": 180, "id": "n"},
            {"title": "Song karaoke version", "duration": 212, "id": "k"},
            {"title": "Song", "duration": 3600, "id": "h"},
        ]
        self.assertIsNone(
            playlist_parser.pick_best_match(candidates, track),
            "every candidate is either the wrong length or the wrong version",
        )

    def test_empty_candidate_list_returns_none(self):
        self.assertIsNone(playlist_parser.pick_best_match([], Track(query="q", title="t")))

    def test_title_containment_outranks_search_position(self):
        track = Track(query="ytsearch5:x", title="Example Track", duration_ms=None)
        candidates = [
            {"title": "Some Other Song", "id": "a"},
            {"title": "Artist Name - Example Track", "id": "b"},
        ]
        chosen = playlist_parser.pick_best_match(candidates, track)
        self.assertIs(
            candidates[1],
            chosen,
            "a candidate whose title contains the track title beats hit #1",
        )


class TestMatchTrack(unittest.TestCase):
    """``match_track`` turns a ytsearch query into a concrete video URL."""

    def test_plain_url_track_passes_through_unchanged(self):
        track = Track(query="https://youtu.be/aaa", title="Solo")
        with mock.patch.object(playlist_parser, "_flat_search") as search:
            resolved, reason = playlist_parser.match_track(track)
        search.assert_not_called()
        self.assertIsNone(reason)
        self.assertIs(track, resolved, "a plain URL must be returned untouched")

    def test_ytsearch_track_resolves_to_the_chosen_video_url(self):
        track = Track(
            query="ytsearch5:Artist - Song audio",
            title="Song",
            artist="Artist",
            duration_ms=210_000,
            source_id="spotifyid",
        )
        candidates = [
            {"title": "Song 10 hours", "duration": 36000, "id": "loop"},
            {"title": "Artist - Song", "duration": 211, "url": "https://youtu.be/right"},
        ]
        with mock.patch.object(playlist_parser, "_flat_search", return_value=candidates) as search:
            resolved, reason = playlist_parser.match_track(track)
        search.assert_called_once_with(track.query, models.SEARCH_CANDIDATES)
        self.assertIsNone(reason)
        self.assertEqual("https://youtu.be/right", resolved.query)
        self.assertEqual("Song", resolved.title, "metadata must be carried over")
        self.assertEqual("spotifyid", resolved.source_id)
        self.assertEqual(
            "ytsearch5:Artist - Song audio",
            track.query,
            "match_track must not mutate the original track",
        )

    def test_candidate_with_only_an_id_becomes_a_watch_url(self):
        track = Track(query="ytsearch5:Song audio", title="Song")
        with mock.patch.object(
            playlist_parser, "_flat_search", return_value=[{"title": "Song", "id": "vid123"}]
        ):
            resolved, reason = playlist_parser.match_track(track)
        self.assertIsNone(reason)
        self.assertEqual("https://www.youtube.com/watch?v=vid123", resolved.query)

    def test_no_candidates_reports_no_search_results(self):
        track = Track(query="ytsearch5:Nothing audio", title="Nothing")
        with mock.patch.object(playlist_parser, "_flat_search", return_value=[]):
            resolved, reason = playlist_parser.match_track(track)
        self.assertIsNone(resolved)
        self.assertEqual("no search results", reason)

    def test_all_rejected_reports_a_reason(self):
        track = Track(query="ytsearch5:Song audio", title="Song", duration_ms=210_000)
        candidates = [{"title": "Song (karaoke)", "duration": 210, "id": "k"}]
        with mock.patch.object(playlist_parser, "_flat_search", return_value=candidates):
            resolved, reason = playlist_parser.match_track(track)
        self.assertIsNone(resolved)
        self.assertIsNotNone(reason, "a rejection must always come with an explanation")
        self.assertIn("no acceptable match", reason)

    def test_empty_query_is_reported(self):
        resolved, reason = playlist_parser.match_track(Track(query="   ", title="x"))
        self.assertIsNone(resolved)
        self.assertEqual("empty search query", reason)


def _spotify_track(index, album_images=None, album_name="Some Album"):
    """Build one ``playlist_items`` entry."""
    payload = {
        "type": "track",
        "id": "trackid%04d" % index,
        "name": "Track %d" % index,
        "artists": [{"name": "Artist %d" % index}],
        "duration_ms": 200000 + index,
    }
    if album_name is not None or album_images is not None:
        payload["album"] = {"name": album_name, "images": album_images or []}
    return {"track": payload}


COVERS = [
    {"url": "https://i.scdn.co/image/small", "width": 64, "height": 64},
    {"url": "https://i.scdn.co/image/huge", "width": 640, "height": 640},
    {"url": "https://i.scdn.co/image/medium", "width": 300, "height": 300},
]


class _FakeSpotify:
    """Records every call so offsets and limits can be asserted."""

    def __init__(self, pages, meta=None, album_meta=None, album_pages=None):
        self.pages = pages
        self.meta = meta or {"name": "My Mix"}
        self.album_meta = album_meta or {}
        self.album_pages = album_pages or []
        self.playlist_calls = []
        self.items_calls = []
        self.album_calls = []
        self.album_tracks_calls = []

    def playlist(self, playlist_id, fields=None):
        self.playlist_calls.append((playlist_id, fields))
        return self.meta

    def playlist_items(self, playlist_id, limit=None, offset=None, additional_types=None):
        self.items_calls.append(
            {
                "playlist_id": playlist_id,
                "limit": limit,
                "offset": offset,
                "additional_types": additional_types,
            }
        )
        index = len(self.items_calls) - 1
        return self.pages[index] if index < len(self.pages) else {"items": [], "total": 0}

    def album(self, album_id):
        self.album_calls.append(album_id)
        return self.album_meta

    def album_tracks(self, album_id, limit=None, offset=None):
        self.album_tracks_calls.append({"album_id": album_id, "limit": limit, "offset": offset})
        index = len(self.album_tracks_calls) - 1
        return (
            self.album_pages[index] if index < len(self.album_pages) else {"items": [], "total": 0}
        )


class TestSpotifyPlaylistParsing(unittest.TestCase):
    """Paging, item filtering and cover-art selection, with the API mocked."""

    def setUp(self):
        # The API parsing path now runs only as a fallback -- resolve_input
        # scrapes first, because the Web API is Premium-gated. Force the
        # fallback, and keep the scraper away from the network.
        from sc_scrape.sources import spotify_scrape

        scrape = mock.patch.object(
            spotify_scrape, "fetch_page", side_effect=spotify_scrape.ScrapeError("no scrape")
        )
        creds = mock.patch.dict(
            os.environ,
            {"SPOTIPY_CLIENT_ID": "id", "SPOTIPY_CLIENT_SECRET": "secret"},
        )
        scrape.start()
        creds.start()
        self.addCleanup(scrape.stop)
        self.addCleanup(creds.stop)

    def _two_page_client(self):
        page_one_items = [_spotify_track(i, album_images=COVERS) for i in range(100)]
        # Two unusable items ride along in page two: a removed track (null) and
        # a podcast episode. Neither may become a Track.
        page_two_items = [_spotify_track(100 + i, album_images=COVERS) for i in range(20)]
        page_two_items.insert(5, {"track": None})
        page_two_items.insert(9, {"track": {"type": "episode", "name": "Some Podcast"}})
        return _FakeSpotify(
            pages=[
                {"items": page_one_items, "total": 122},
                {"items": page_two_items, "total": 122},
            ]
        )

    def test_two_page_playlist_yields_120_tracks_with_correct_offsets(self):
        client = self._two_page_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input(
                "https://open.spotify.com/playlist/%s?si=xyz" % PLAYLIST_ID
            )

        self.assertEqual(models.SOURCE_SPOTIFY, job.source)
        self.assertEqual("My Mix", job.name, "the folder is named after the playlist")
        self.assertEqual(
            2, len(client.items_calls), "120 items over a 100-item page size is two calls"
        )
        self.assertEqual(
            [0, 100],
            [call["offset"] for call in client.items_calls],
            "offsets must advance by the number of items actually returned",
        )
        self.assertEqual(
            [100, 100],
            [call["limit"] for call in client.items_calls],
            "Spotify caps playlist_items at 100 per request",
        )
        self.assertEqual(
            120,
            len(job.tracks),
            "expected 120 playable tracks (122 items minus a null and an episode), got %d"
            % len(job.tracks),
        )
        self.assertEqual([PLAYLIST_ID], [call[0] for call in client.playlist_calls])

    def test_null_track_and_episode_items_are_skipped(self):
        client = self._two_page_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input("spotify:playlist:%s" % PLAYLIST_ID)
        titles = [t.title for t in job.tracks]
        self.assertNotIn("Some Podcast", titles, "podcast episodes are not songs")
        self.assertTrue(all(t.title for t in job.tracks), "no nameless tracks may survive")
        self.assertEqual(
            sorted(titles, key=lambda s: int(s.split()[1])),
            titles,
            "playlist order must be preserved",
        )

    def test_largest_cover_image_is_chosen_not_the_first(self):
        client = self._two_page_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input("spotify:playlist:%s" % PLAYLIST_ID)
        self.assertEqual(
            "https://i.scdn.co/image/huge",
            job.tracks[0].cover_url,
            "the 640x640 cover must win over the 64x64 one listed first",
        )

    def test_tracks_carry_a_ytsearch_query_and_metadata(self):
        client = self._two_page_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input("spotify:playlist:%s" % PLAYLIST_ID)
        first = job.tracks[0]
        self.assertTrue(
            first.query.startswith("ytsearch%d:" % models.SEARCH_CANDIDATES),
            "Spotify has no audio, so every track must arrive as a YouTube search: %r"
            % first.query,
        )
        self.assertEqual("Track 0", first.title)
        self.assertEqual("Artist 0", first.artist)
        self.assertEqual("Some Album", first.album)
        self.assertEqual(200000, first.duration_ms)
        self.assertEqual("spotify:trackid0000", first.history_key())


class TestSpotifyAlbumParsing(unittest.TestCase):
    """Albums use a different endpoint, a 50-item page and container metadata."""

    def setUp(self):
        # The API parsing path now runs only as a fallback -- resolve_input
        # scrapes first, because the Web API is Premium-gated. Force the
        # fallback, and keep the scraper away from the network.
        from sc_scrape.sources import spotify_scrape

        scrape = mock.patch.object(
            spotify_scrape, "fetch_page", side_effect=spotify_scrape.ScrapeError("no scrape")
        )
        creds = mock.patch.dict(
            os.environ,
            {"SPOTIPY_CLIENT_ID": "id", "SPOTIPY_CLIENT_SECRET": "secret"},
        )
        scrape.start()
        creds.start()
        self.addCleanup(scrape.stop)
        self.addCleanup(creds.stop)

    def _album_client(self):
        # Simplified album tracks: no nested "album" block at all.
        items = []
        for index in range(3):
            items.append(
                {
                    "type": "track",
                    "id": "albumtrack%d" % index,
                    "name": "Album Track %d" % index,
                    "artists": [{"name": "The Band"}],
                    "duration_ms": 180000 + index,
                }
            )
        return _FakeSpotify(
            pages=[],
            album_meta={
                "name": "The Real Album",
                "images": COVERS,
                "artists": [{"name": "The Band"}],
            },
            album_pages=[{"items": items, "total": 3}],
        )

    def test_album_url_resolves_via_album_endpoints(self):
        client = self._album_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input("https://open.spotify.com/album/%s" % ALBUM_ID)
        self.assertEqual([ALBUM_ID], client.album_calls, "the album container must be fetched")
        self.assertEqual([], client.items_calls, "playlist_items must not be used for an album")
        self.assertEqual("The Real Album", job.name)
        self.assertEqual(models.SOURCE_SPOTIFY, job.source)
        self.assertEqual(3, len(job.tracks))

    def test_album_tracks_is_paged_at_50_not_100(self):
        client = self._album_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            playlist_parser.resolve_input("spotify:album:%s" % ALBUM_ID)
        self.assertEqual(1, len(client.album_tracks_calls))
        self.assertEqual(
            50,
            client.album_tracks_calls[0]["limit"],
            "album_tracks caps at 50 per request, unlike playlist_items",
        )
        self.assertEqual(0, client.album_tracks_calls[0]["offset"])

    def test_album_name_and_cover_are_stamped_onto_every_track(self):
        client = self._album_client()
        with mock.patch.object(playlist_parser, "_spotify_client", return_value=client):
            job = playlist_parser.resolve_input("spotify:album:%s" % ALBUM_ID)
        for track in job.tracks:
            self.assertEqual(
                "The Real Album",
                track.album,
                "simplified album tracks carry no album block, so the container "
                "name must be applied: %r" % track,
            )
            self.assertEqual(
                "https://i.scdn.co/image/huge",
                track.cover_url,
                "the album's largest cover must be embedded in every track",
            )
            self.assertEqual("The Band", track.artist)


class TestSpotifyCredentials(unittest.TestCase):
    """Missing credentials must fail loudly with instructions, not silently."""

    def test_missing_env_vars_raise_spotify_credentials_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(playlist_parser.SpotifyCredentialsError) as ctx:
                playlist_parser._spotify_client()
        message = str(ctx.exception)
        self.assertIn("SPOTIPY_CLIENT_ID", message)
        self.assertIn("SPOTIPY_CLIENT_SECRET", message)
        self.assertIn("developer.spotify.com", message)

    def test_blank_env_vars_are_treated_as_missing(self):
        env = {"SPOTIPY_CLIENT_ID": "   ", "SPOTIPY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(playlist_parser.SpotifyCredentialsError):
                playlist_parser._spotify_client()

    def test_resolving_a_spotify_input_without_credentials_reports_the_scrape_failure(self):
        # The scraper runs first and needs no credentials, so a credential-less
        # failure surfaces as ScrapeError. SpotifyCredentialsError here would
        # send the user off to build a developer app they cannot use: Web API
        # access is behind Premium.
        from sc_scrape.sources import spotify_scrape

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                spotify_scrape, "fetch_page", side_effect=spotify_scrape.ScrapeError("boom")
            ):
                with self.assertRaises(spotify_scrape.ScrapeError):
                    playlist_parser.resolve_input("spotify:playlist:%s" % PLAYLIST_ID)


class TestHistoryStore(unittest.TestCase):
    """The resume ledger: durable, corruption-tolerant and thread-safe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_add_then_flush_survives_a_reload(self):
        mp3 = self.dir / "Song.mp3"
        mp3.write_bytes(b"ID3")
        store = history_mod.HistoryStore(self.dir)
        store.add("query:https://youtu.be/aaa", mp3, "Song")
        store.flush()

        reloaded = history_mod.HistoryStore(self.dir)
        self.assertTrue(
            reloaded.has("query:https://youtu.be/aaa"),
            "a flushed entry must be visible to a fresh store",
        )
        self.assertEqual(1, len(reloaded))
        entry = reloaded.get("query:https://youtu.be/aaa")
        self.assertEqual("Song", entry["title"])
        self.assertEqual(
            "Song.mp3",
            entry["path"],
            "paths inside the playlist folder are stored relative so the folder can move",
        )
        self.assertEqual(
            mp3.resolve(),
            reloaded.resolve_path("query:https://youtu.be/aaa").resolve(),
            "the relative entry must expand back to the real MP3",
        )

    def test_corrupt_json_yields_an_empty_store_and_keeps_the_file(self):
        ledger = history_mod.HistoryStore(self.dir).path
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"entries": {"a": ', encoding="utf-8")

        store = history_mod.HistoryStore(self.dir)  # must not raise
        self.assertEqual(0, len(store), "a broken ledger is treated as empty")
        self.assertFalse(store.has("a"))
        self.assertTrue(
            ledger.is_file(),
            "the corrupt ledger must be left in place so the user can inspect it",
        )
        self.assertEqual('{"entries": {"a": ', ledger.read_text(encoding="utf-8"))

    def test_non_object_json_yields_an_empty_store(self):
        ledger = history_mod.HistoryStore(self.dir).path
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(0, len(history_mod.HistoryStore(self.dir)))

    def test_legacy_flat_mapping_is_upgraded(self):
        ledger = history_mod.HistoryStore(self.dir).path
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"query:x": "Old.mp3"}', encoding="utf-8")
        store = history_mod.HistoryStore(self.dir)
        self.assertTrue(store.has("query:x"))
        self.assertEqual("Old.mp3", store.get("query:x")["path"])

    def test_concurrent_adds_from_eight_threads_lose_nothing(self):
        # 200 adds cross the autoflush threshold repeatedly, so this is also the
        # deadlock guard: add() must decide under the lock and release it before
        # flushing, or the flush re-enters a non-reentrant Lock and hangs here.
        store = history_mod.HistoryStore(self.dir)
        errors = []
        barrier = threading.Barrier(8)

        def worker(thread_index):
            try:
                barrier.wait(timeout=5)
                for key_index in range(25):
                    key = "query:t%d-k%d" % (thread_index, key_index)
                    store.add(key, self.dir / ("%s.mp3" % key.replace(":", "_")), key)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertions
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        store.flush()

        self.assertEqual([], errors, "no worker may raise: %r" % errors)
        self.assertEqual(200, len(store), "8 threads x 25 keys must all land, got %d" % len(store))

        raw = history_mod.HistoryStore(self.dir).path.read_text(encoding="utf-8")
        payload = json.loads(raw)  # must not raise: writes are atomic
        self.assertEqual(history_mod.HISTORY_VERSION, payload["version"])
        self.assertEqual(
            200,
            len(payload["entries"]),
            "the on-disk ledger must contain all 200 entries, got %d" % len(payload["entries"]),
        )
        for thread_index in range(8):
            for key_index in range(25):
                key = "query:t%d-k%d" % (thread_index, key_index)
                self.assertIn(key, payload["entries"], "%s went missing" % key)

    def test_remove_forgets_a_key(self):
        store = history_mod.HistoryStore(self.dir)
        store.add("query:x", self.dir / "x.mp3", "X")
        store.remove("query:x")
        self.assertFalse(store.has("query:x"))
        self.assertIsNone(store.resolve_path("query:x"))

    def test_flush_leaves_no_temp_files_behind(self):
        store = history_mod.HistoryStore(self.dir)
        for index in range(12):  # crosses the autoflush threshold several times
            store.add("query:%d" % index, self.dir / ("%d.mp3" % index), str(index))
        store.flush()

        leftovers = sorted(p.name for p in self.dir.iterdir() if p.name != models.HISTORY_NAME)
        self.assertEqual(
            [],
            leftovers,
            "atomic writes must clean up after themselves; found %r" % leftovers,
        )

    def test_context_manager_flushes_on_exit(self):
        with history_mod.HistoryStore(self.dir) as store:
            store.add("query:ctx", self.dir / "ctx.mp3", "Ctx")
        self.assertTrue((history_mod.HistoryStore(self.dir).path).is_file())
        self.assertTrue(history_mod.HistoryStore(self.dir).has("query:ctx"))


def _job(count, name="Test Playlist"):
    tracks = [Track(query="https://youtu.be/vid%d" % i, title="Track %d" % i) for i in range(count)]
    return PlaylistJob(name=name, tracks=tracks, source=models.SOURCE_YOUTUBE)


class TestRunJob(unittest.TestCase):
    """Concurrency invariants: ordered results, per-track isolation, no shared opts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # match_track is exercised on its own elsewhere; here it is a pass-through
        # so run_job's own behaviour is what is under test.
        patcher = mock.patch.object(
            playlist_parser, "match_track", side_effect=lambda track: (track, None)
        )
        self.match_track = patcher.start()
        self.addCleanup(patcher.stop)

    def _folder(self, name="Test Playlist"):
        return self.root / downloader.sanitize_filename(name)

    def test_results_are_returned_in_original_track_order(self):
        job = _job(4)
        completion_order = []
        lock = threading.Lock()
        # Later tracks finish first, so completion order is the reverse of
        # submission order and a naive as_completed() collection would shuffle.
        delays = {0: 0.03, 1: 0.02, 2: 0.01, 3: 0.0}

        def fake_download(track, output_dir, opts, overwrite=False):
            index = int(track.title.split()[1])
            time.sleep(delays[index])
            with lock:
                completion_order.append(index)
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            results = pipeline.run_job(job, self.root, workers=4)

        self.assertEqual(
            [3, 2, 1, 0],
            completion_order,
            "test setup problem: downloads did not complete out of order (%r)" % completion_order,
        )
        self.assertEqual(
            ["Track 0", "Track 1", "Track 2", "Track 3"],
            [r.title for r in results],
            "results must be re-ordered back into playlist order, got %r"
            % [r.title for r in results],
        )
        self.assertTrue(all(r.status == STATUS_OK for r in results))

    def test_on_progress_is_called_once_per_track(self):
        job = _job(5)
        seen = []

        def fake_download(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        def on_progress(result, done, total):
            seen.append((result.title, done, total))

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            pipeline.run_job(job, self.root, workers=3, on_progress=on_progress)

        self.assertEqual(
            5, len(seen), "expected one progress callback per track, got %d" % len(seen)
        )
        self.assertEqual(
            [1, 2, 3, 4, 5],
            [entry[1] for entry in seen],
            "the 'done' counter must advance by one each time, got %r"
            % [entry[1] for entry in seen],
        )
        self.assertEqual({5}, {entry[2] for entry in seen}, "'total' must stay constant")

    def test_a_raising_worker_fails_only_its_own_track(self):
        job = _job(4)

        def fake_download(track, output_dir, opts, overwrite=False):
            if track.title == "Track 2":
                raise RuntimeError("boom")
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            results = pipeline.run_job(job, self.root, workers=4)

        self.assertEqual(4, len(results))
        statuses = [r.status for r in results]
        self.assertEqual(
            [STATUS_OK, STATUS_OK, STATUS_FAILED, STATUS_OK],
            statuses,
            "only the exploding track may fail, got %r" % statuses,
        )
        self.assertIn(
            "boom",
            results[2].reason,
            "the failure reason must name the underlying error, got %r" % results[2].reason,
        )

    def test_a_worker_returning_junk_becomes_a_failure(self):
        job = _job(1)
        with (
            mock.patch.object(downloader, "download_one", return_value="not a result"),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            results = pipeline.run_job(job, self.root, workers=1)
        self.assertEqual(STATUS_FAILED, results[0].status)

    def test_second_run_skips_everything_and_never_calls_download_one(self):
        job = _job(3)

        def fake_download(track, output_dir, opts, overwrite=False):
            path = Path(output_dir) / ("%s.mp3" % downloader.sanitize_filename(track.title))
            path.write_bytes(b"ID3 fake audio")
            return DownloadResult.ok(track.query, track.title, path)

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            first = pipeline.run_job(job, self.root, workers=2)
        self.assertEqual(
            [STATUS_OK] * 3, [r.status for r in first], "first run must download everything"
        )
        self.assertTrue(
            history_mod.HistoryStore(self._folder()).path.is_file(),
            "the ledger must be flushed to the playlist folder",
        )

        with (
            mock.patch.object(downloader, "download_one") as never,
            mock.patch.object(downloader, "build_ydl_opts") as opts,
        ):
            second = pipeline.run_job(job, self.root, workers=2)

        never.assert_not_called()
        opts.assert_not_called()
        self.assertEqual(
            [STATUS_SKIPPED] * 3,
            [r.status for r in second],
            "a fully-downloaded playlist must be entirely skipped on re-run",
        )
        self.assertEqual(["Track 0", "Track 1", "Track 2"], [r.title for r in second])

    def test_overwrite_ignores_the_history(self):
        job = _job(2)

        def fake_download(track, output_dir, opts, overwrite=False):
            path = Path(output_dir) / ("%s.mp3" % downloader.sanitize_filename(track.title))
            path.write_bytes(b"ID3")
            return DownloadResult.ok(track.query, track.title, path)

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            pipeline.run_job(job, self.root, workers=2)
            with mock.patch.object(downloader, "download_one", side_effect=fake_download) as again:
                results = pipeline.run_job(job, self.root, workers=2, overwrite=True)

        self.assertEqual(2, again.call_count, "--overwrite must re-download every track")
        self.assertEqual([STATUS_OK, STATUS_OK], [r.status for r in results])

    def test_a_deleted_file_is_redownloaded_despite_the_history(self):
        job = _job(2)
        paths = []

        def fake_download(track, output_dir, opts, overwrite=False):
            path = Path(output_dir) / ("%s.mp3" % downloader.sanitize_filename(track.title))
            path.write_bytes(b"ID3")
            paths.append(path)
            return DownloadResult.ok(track.query, track.title, path)

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            pipeline.run_job(job, self.root, workers=2)
            paths[0].unlink()
            with mock.patch.object(downloader, "download_one", side_effect=fake_download) as again:
                results = pipeline.run_job(job, self.root, workers=2)

        self.assertEqual(1, again.call_count, "only the track whose MP3 vanished may be re-fetched")
        self.assertEqual([STATUS_OK, STATUS_SKIPPED], [r.status for r in results])

    def test_build_ydl_opts_is_called_once_per_track(self):
        job = _job(6)

        def fake_download(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts") as build,
        ):
            build.side_effect = lambda *a, **kw: {"marker": object()}
            pipeline.run_job(job, self.root, workers=4)

        self.assertEqual(
            6,
            build.call_count,
            "yt-dlp opts are not thread-safe: each worker must build its own "
            "dict, expected 6 calls, got %d" % build.call_count,
        )

    def test_each_worker_receives_a_distinct_opts_dict(self):
        job = _job(5)
        seen_ids = []
        lock = threading.Lock()

        def fake_download(track, output_dir, opts, overwrite=False):
            with lock:
                seen_ids.append(id(opts))
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", side_effect=lambda *a, **k: {}),
        ):
            pipeline.run_job(job, self.root, workers=5)

        self.assertEqual(
            5,
            len(set(seen_ids)),
            "every task must get its own opts object; saw %d distinct of %d"
            % (len(set(seen_ids)), len(seen_ids)),
        )

    def test_empty_job_returns_no_results(self):
        with mock.patch.object(downloader, "download_one") as never:
            results = pipeline.run_job(_job(0), self.root, workers=4)
        self.assertEqual([], results)
        never.assert_not_called()

    def test_unmatched_track_becomes_a_failure(self):
        job = _job(2)
        self.match_track.side_effect = lambda track: (
            (None, "no acceptable match") if track.title == "Track 1" else (track, None)
        )

        def fake_download(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            results = pipeline.run_job(job, self.root, workers=2)

        self.assertEqual([STATUS_OK, STATUS_FAILED], [r.status for r in results])
        self.assertEqual("no acceptable match", results[1].reason)

    def test_blank_job_name_falls_back_to_a_folder(self):
        job = _job(1, name="   ")

        def fake_download(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            pipeline.run_job(job, self.root, workers=1)

        self.assertTrue(
            (self.root / pipeline.FALLBACK_PLAYLIST_NAME).is_dir(),
            "a nameless playlist must still get a real folder",
        )

    def test_progress_callback_exceptions_are_swallowed(self):
        job = _job(2)

        def fake_download(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(track.query, track.title, Path(output_dir) / "x.mp3")

        def bad_callback(result, done, total):
            raise ValueError("UI blew up")

        with (
            mock.patch.object(downloader, "download_one", side_effect=fake_download),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        ):
            results = pipeline.run_job(job, self.root, workers=2, on_progress=bad_callback)
        self.assertEqual(2, len(results), "a broken UI callback must not lose results")


class TestProbeExistingDownload(unittest.TestCase):
    """The skip-existing probe for tracks that are not authoritative.

    This path used to reference an undefined name, which the surrounding
    ``except Exception`` swallowed into ``None`` -- so every non-authoritative
    track was silently re-downloaded instead of skipped.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _probe(self, opts=None):
        info = {"id": "vid1", "title": "Example Track", "_type": "video"}
        fake = _FakeYDL({"https://youtu.be/vid1": info})
        with mock.patch.object(downloader, "YoutubeDL", fake):
            return downloader.probe_existing_download(
                "https://youtu.be/vid1",
                self.dir,
                opts,
                track=Track(query="https://youtu.be/vid1", title=None),
            )

    def test_existing_file_is_reported_as_skipped(self):
        (self.dir / "Example Track.mp3").write_bytes(b"ID3")
        result = self._probe(downloader.build_ydl_opts(self.dir))
        self.assertIsNotNone(result, "an existing file must not be re-downloaded")
        self.assertEqual(models.STATUS_SKIPPED, result.status)

    def test_missing_file_probes_to_none(self):
        self.assertIsNone(self._probe(downloader.build_ydl_opts(self.dir)))

    def test_the_probe_follows_the_requested_format(self):
        (self.dir / "Example Track.opus").write_bytes(b"OggS")
        opts = downloader.build_ydl_opts(self.dir, audio_format="opus")
        result = self._probe(opts)
        self.assertIsNotNone(result, "--format opus must look for the .opus file")
        self.assertEqual("Example Track.opus", result.path.name)


class TestClampWorkers(unittest.TestCase):
    """Worker counts are clamped rather than trusted."""

    def test_out_of_range_and_junk_worker_counts_are_clamped(self):
        self.assertEqual(1, pipeline.clamp_workers(0))
        self.assertEqual(1, pipeline.clamp_workers(-5))
        self.assertEqual(4, pipeline.clamp_workers(4))
        self.assertEqual(models.MAX_WORKERS_LIMIT, pipeline.clamp_workers(9999))
        self.assertEqual(models.MAX_WORKERS_DEFAULT, pipeline.clamp_workers("nonsense"))


class TestWriteFailedLog(unittest.TestCase):
    """``skipped.txt`` is a convenience report, never a reason to crash."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_writes_tab_separated_url_and_reason(self):
        results = [
            DownloadResult.ok("https://youtu.be/good", "Good", self.dir / "Good.mp3"),
            DownloadResult.failed("https://youtu.be/bad", "Private video"),
            DownloadResult.skipped("https://youtu.be/old", "Old"),
            DownloadResult.failed("ytsearch5:Nope audio", "no search results"),
        ]
        path = downloader.write_failed_log(results, self.dir)

        self.assertIsNotNone(path, "two failures must produce a log")
        self.assertEqual(models.SKIPPED_LOG_NAME, path.name)
        self.assertEqual("skipped.txt", path.name)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            ["https://youtu.be/bad\tPrivate video", "ytsearch5:Nope audio\tno search results"],
            lines,
            "only failures belong in the log, one 'URL<TAB>reason' per line",
        )
        for line in lines:
            self.assertEqual(1, line.count("\t"), "exactly one tab per line: %r" % line)

    def test_multiline_reasons_are_collapsed_to_one_line(self):
        results = [DownloadResult.failed("u", "line one\nline two   \t  three")]
        path = downloader.write_failed_log(results, self.dir)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines), "a multi-line reason must not break the format")
        self.assertEqual("u\tline one line two three", lines[0])

    def test_returns_none_when_nothing_failed(self):
        results = [
            DownloadResult.ok("https://youtu.be/good", "Good", self.dir / "Good.mp3"),
            DownloadResult.skipped("https://youtu.be/old", "Old"),
        ]
        self.assertIsNone(
            downloader.write_failed_log(results, self.dir),
            "no failures means no report",
        )
        self.assertFalse((self.dir / models.SKIPPED_LOG_NAME).exists())

    def test_returns_none_when_the_directory_does_not_exist(self):
        missing = self.dir / "does" / "not" / "exist"
        result = downloader.write_failed_log([DownloadResult.failed("u", "why")], missing)
        self.assertIsNone(result, "an unwritable destination is logged, not raised")

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root ignores directory permissions",
    )
    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_returns_none_when_the_directory_is_read_only(self):
        locked = self.dir / "locked"
        locked.mkdir()
        original = stat.S_IMODE(locked.stat().st_mode)
        os.chmod(str(locked), 0o500)
        try:
            result = downloader.write_failed_log([DownloadResult.failed("u", "why")], locked)
        finally:
            os.chmod(str(locked), original)
        self.assertIsNone(result, "a read-only output dir must not raise")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
