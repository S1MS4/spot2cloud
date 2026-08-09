"""Offline unit tests for chapter splitting.

Hermetic: ``downloader.YoutubeDL`` is replaced by a fake that renders the same
output templates the real splitter renders and writes real (one-second, silent)
MP3s where a file has to be tagged, so ``tag_mp3`` runs for real and the ID3
assertions mean something. Nothing here touches the network.

The ID3 assertions are the point of the feature: yt-dlp stamps the *parent*
video's tags onto every piece it cuts, so a suite that only checked filenames
would pass on a build that is useless in a music player.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spot2cloud import models
from spot2cloud.download import downloader, pipeline
from spot2cloud.models import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    DownloadResult,
    PlaylistJob,
    Track,
)
from spot2cloud.sources import playlist_parser

# Failure paths are exercised on purpose and log by design; the assertions,
# not the log lines, are what prove the behaviour.
logging.disable(logging.CRITICAL)

VIDEO_URL = "https://www.youtube.com/watch?v=EXAMPLEVID1"

# Deliberately awkward: a slash and a colon must not escape sanitize_filename.
CHAPTERS = [
    {"start_time": 0.0, "end_time": 198.0, "title": "Track One"},
    {"start_time": 198.0, "end_time": 402.0, "title": "Rock/Pop: intro"},
    {"start_time": 402.0, "end_time": 600.0, "title": "Track Three"},
]

#: One second of silence, built once and copied per piece.
_SILENT_MP3 = None


def setUpModule():
    """Build a real MP3 once; tagging a fabricated byte string proves nothing."""
    global _SILENT_MP3
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("ffmpeg is required to build the fixture MP3")
    directory = tempfile.mkdtemp(prefix="chapters-fixture-")
    unittest.addModuleCleanup(shutil.rmtree, directory, ignore_errors=True)
    _SILENT_MP3 = Path(directory) / "silence.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "quiet",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-t",
            "1",
            "-q:a",
            "9",
            str(_SILENT_MP3),
        ],
        check=True,
    )


def _render(template: str, **fields) -> Path:
    """Render one of yt-dlp's output templates the way yt-dlp renders it."""
    return Path(template % fields)


class _FakeYDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` that fakes ``--split-chapters``.

    Writes a parent file and one piece per chapter through the caller's own
    output templates, and records the destination on the chapter dict, exactly
    as ``FFmpegSplitChaptersPP`` does.
    """

    def __init__(self, opts, calls, chapters, error=None, write_pieces=True):
        self.opts = opts
        self._calls = calls
        self._chapters = chapters
        self._error = error
        self._write_pieces = write_pieces

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        self._calls.append({"opts": self.opts, "url": url, "download": download})
        if self._error is not None:
            raise self._error
        # Copied per call: the production code reads filepath off these dicts.
        chapters = [dict(chapter) for chapter in self._chapters]
        info = {
            "id": "vid1",
            "title": "Artist Name - Example Album (Full Album Stream)",
            "chapters": chapters,
        }
        if not download:
            return info

        parent = _render(self.opts["outtmpl"]["default"], id="vid1", ext="mp3")
        parent.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(_SILENT_MP3), str(parent))
        if self._write_pieces:
            for number, chapter in enumerate(chapters, start=1):
                piece = _render(self.opts["outtmpl"]["chapter"], section_number=number, ext="mp3")
                piece.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(_SILENT_MP3), str(piece))
                chapter["filepath"] = str(piece)
        return info


def _patch_ydl(calls, chapters=CHAPTERS, error=None, write_pieces=True):
    """Patch ``downloader.YoutubeDL`` with the fake, recording every call."""

    def factory(opts):
        return _FakeYDL(opts, calls, chapters, error, write_pieces)

    return mock.patch.object(downloader, "YoutubeDL", factory)


def _tags(path: Path):
    """The ID3 frames of a finished piece."""
    from mutagen.id3 import ID3

    return ID3(str(path))


def _opts():
    """A realistic opts dict, as ``pipeline`` hands one to the downloader."""
    return downloader.build_ydl_opts(Path("."), quality="192")


class TestProbeChapters(unittest.TestCase):
    """Metadata-only, best-effort, and always using the SABR workaround."""

    def test_returns_the_chapter_dicts(self):
        calls = []
        with _patch_ydl(calls):
            chapters = downloader.probe_chapters(VIDEO_URL)

        self.assertEqual(
            ["Track One", "Rock/Pop: intro", "Track Three"],
            [c["title"] for c in chapters],
        )
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0]["download"], "probing must not download anything")
        self.assertTrue(calls[0]["opts"]["skip_download"])

    def test_chapterless_video_returns_empty(self):
        with _patch_ydl([], chapters=[]):
            self.assertEqual([], downloader.probe_chapters(VIDEO_URL))

    def test_missing_chapters_key_returns_empty(self):
        class _NoChapters(_FakeYDL):
            def extract_info(self, url, download=False):
                return {"id": "vid1", "title": "plain video"}

        with mock.patch.object(downloader, "YoutubeDL", lambda opts: _NoChapters(opts, [], [])):
            self.assertEqual([], downloader.probe_chapters(VIDEO_URL))

    def test_extraction_failure_never_raises(self):
        from yt_dlp.utils import DownloadError

        for error in (DownloadError("boom"), OSError("disk"), ValueError("weird")):
            with self.subTest(error=type(error).__name__):
                with _patch_ydl([], error=error):
                    self.assertEqual([], downloader.probe_chapters(VIDEO_URL))

    def test_uses_the_youtube_player_client_workaround(self):
        calls = []
        with _patch_ydl(calls):
            downloader.probe_chapters(VIDEO_URL)

        self.assertEqual(
            list(models.YOUTUBE_PLAYER_CLIENTS),
            calls[0]["opts"]["extractor_args"]["youtube"]["player_client"],
        )

    def test_search_shaped_result_is_unwrapped(self):
        class _Playlist(_FakeYDL):
            def extract_info(self, url, download=False):
                return {"_type": "playlist", "entries": [{"chapters": CHAPTERS}]}

        with mock.patch.object(downloader, "YoutubeDL", lambda opts: _Playlist(opts, [], [])):
            self.assertEqual(3, len(downloader.probe_chapters("ytsearch5:whatever")))


class TestDownloadChapters(unittest.TestCase):
    """One MP3 per chapter, each carrying its own tags, and no parent left over."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.track = Track(
            query=VIDEO_URL,
            title="Artist Name - Example Album (Full Album Stream)",
            artist="BA.",
            album="Example Album",
            cover_url="https://example.invalid/cover.jpg",
            source_id="spotifyalbumid",
        )
        cover = mock.patch.object(
            downloader, "_fetch_cover", return_value=(b"\xff\xd8fake-jpeg", "image/jpeg")
        )
        self.fetch_cover = cover.start()
        self.addCleanup(cover.stop)

    def _run(self, calls=None, overwrite=False, **patch_kwargs):
        calls = [] if calls is None else calls
        with _patch_ydl(calls, **patch_kwargs):
            return downloader.download_chapters(
                self.track, self.folder, _opts(), overwrite=overwrite
            )

    def test_one_result_per_chapter_with_numbered_sanitised_names(self):
        results = self._run()

        self.assertEqual([STATUS_OK] * 3, [r.status for r in results])
        self.assertEqual(
            ["01 - Track One.mp3", "02 - RockPop intro.mp3", "03 - Track Three.mp3"],
            [r.path.name for r in results],
        )
        for result in results:
            self.assertTrue(result.path.exists())
            self.assertEqual(VIDEO_URL, result.url)

    def test_every_piece_gets_its_own_title_and_track_number(self):
        results = self._run()
        tags = [_tags(r.path) for r in results]

        titles = [str(t["TIT2"]) for t in tags]
        numbers = [str(t["TRCK"]) for t in tags]
        self.assertEqual(["Track One", "Rock/Pop: intro", "Track Three"], titles)
        self.assertEqual(["1", "2", "3"], numbers)
        # The bug this feature exists to avoid: yt-dlp stamps the parent video's
        # tags onto every piece, so identical TIT2/TRCK across the album.
        self.assertEqual(3, len(set(titles)), "each piece needs its own TIT2")
        self.assertEqual(3, len(set(numbers)), "each piece needs its own TRCK")
        self.assertNotIn(self.track.title, titles)

    def test_album_artist_and_cover_are_inherited_from_the_track(self):
        results = self._run()

        for result in results:
            tags = _tags(result.path)
            self.assertEqual("Example Album", str(tags["TALB"]))
            self.assertEqual("BA.", str(tags["TPE1"]))
            self.assertEqual(b"\xff\xd8fake-jpeg", tags.getall("APIC")[0].data)

    def test_untitled_chapters_are_named_by_position(self):
        with _patch_ydl([], chapters=[{"start_time": 0.0, "end_time": 5.0}]):
            results = downloader.download_chapters(self.track, self.folder, _opts())

        self.assertEqual("01 - Chapter 1.mp3", results[0].path.name)

    def test_the_unsplit_parent_is_deleted(self):
        results = self._run()

        leftovers = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual([r.path.name for r in results], leftovers)
        self.assertEqual([], list(self.folder.glob("vid1.*")))
        self.assertEqual([], list(self.folder.glob(".chapters-*")))

    def test_existing_pieces_are_skipped_unless_overwrite(self):
        first = self._run()
        (self.folder / "02 - RockPop intro.mp3").write_bytes(b"hand-written")

        results = self._run()

        self.assertEqual([STATUS_SKIPPED] * 3, [r.status for r in results])
        self.assertEqual(b"hand-written", (self.folder / "02 - RockPop intro.mp3").read_bytes())
        self.assertEqual([r.path for r in first], [r.path for r in results])

    def test_a_partial_folder_downloads_only_the_missing_pieces(self):
        self._run()
        (self.folder / "02 - RockPop intro.mp3").unlink()

        results = self._run()

        self.assertEqual([STATUS_SKIPPED, STATUS_OK, STATUS_SKIPPED], [r.status for r in results])
        self.assertTrue((self.folder / "02 - RockPop intro.mp3").exists())

    def test_a_complete_folder_is_not_downloaded_again(self):
        self._run()
        calls = []

        results = self._run(calls=calls)

        self.assertEqual([STATUS_SKIPPED] * 3, [r.status for r in results])
        self.assertEqual(
            [],
            [c for c in calls if c["download"]],
            "an up-to-date album must not re-fetch half an hour of audio",
        )

    def test_overwrite_replaces_existing_pieces(self):
        self._run()
        (self.folder / "02 - RockPop intro.mp3").write_bytes(b"hand-written")

        results = self._run(overwrite=True)

        self.assertEqual([STATUS_OK] * 3, [r.status for r in results])
        self.assertNotEqual(b"hand-written", (self.folder / "02 - RockPop intro.mp3").read_bytes())

    def test_a_download_failure_becomes_one_failed_result(self):
        from yt_dlp.utils import DownloadError

        results = self._run(error=DownloadError("Video unavailable"))

        self.assertEqual(1, len(results))
        self.assertEqual(STATUS_FAILED, results[0].status)
        self.assertTrue(results[0].reason)
        self.assertEqual([], list(self.folder.glob(".chapters-*")))

    def test_a_missing_piece_becomes_a_failed_result_not_an_exception(self):
        results = self._run(write_pieces=False)

        self.assertEqual([STATUS_FAILED] * 3, [r.status for r in results])
        self.assertIn("splitter", results[0].reason)

    def test_the_callers_opts_dict_is_never_mutated(self):
        opts = _opts()
        before = list(opts["postprocessors"])

        with _patch_ydl([]):
            downloader.download_chapters(self.track, self.folder, opts)

        self.assertEqual(before, opts["postprocessors"])
        self.assertEqual(
            str(Path(".") / "%(id)s.%(ext)s"),
            opts["outtmpl"],
            "the split templates must not leak back into the shared opts",
        )

    def test_the_splitter_runs_after_the_audio_is_extracted(self):
        calls = []
        self._run(calls=calls)

        download = [c for c in calls if c["download"]][0]
        keys = [pp["key"] for pp in download["opts"]["postprocessors"]]
        self.assertEqual("FFmpegSplitChapters", keys[-1])
        self.assertLess(keys.index("FFmpegExtractAudio"), keys.index("FFmpegSplitChapters"))


def _job(*titles):
    tracks = [
        Track(query="https://youtu.be/vid%d" % index, title=title)
        for index, title in enumerate(titles)
    ]
    return PlaylistJob(name="Test Playlist", tracks=tracks)


class TestRunJobSplitChapters(unittest.TestCase):
    """A chaptered track fans out; everything else keeps its old guarantees."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            playlist_parser, "match_track", side_effect=lambda track: (track, None)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.chaptered = "https://youtu.be/vid1"
        self.folder = self.root / "Test Playlist"

    def _patches(self, chapters_side_effect=None):
        """Patch the downloader entry points ``run_job`` reaches for."""

        def fake_probe(url, opts=None):
            return CHAPTERS if url == self.chaptered else []

        def fake_chapters(track, output_dir, opts, overwrite=False):
            return [
                DownloadResult.ok(
                    track.query,
                    chapter["title"],
                    Path(output_dir)
                    / ("%02d - %s.mp3" % (number, downloader.sanitize_filename(chapter["title"]))),
                )
                for number, chapter in enumerate(CHAPTERS, start=1)
            ]

        def fake_one(track, output_dir, opts, overwrite=False):
            return DownloadResult.ok(
                track.query, track.title, Path(output_dir) / ("%s.mp3" % track.title)
            )

        return (
            mock.patch.object(downloader, "probe_chapters", side_effect=fake_probe),
            mock.patch.object(
                downloader,
                "download_chapters",
                side_effect=chapters_side_effect or fake_chapters,
            ),
            mock.patch.object(downloader, "download_one", side_effect=fake_one),
            mock.patch.object(downloader, "build_ydl_opts", return_value={}),
        )

    def _run(self, job, chapters_side_effect=None, **kwargs):
        probe, chapters, one, opts = self._patches(chapters_side_effect)
        with probe as self.probe, chapters as self.chapters, one as self.one, opts:
            return pipeline.run_job(job, self.root, workers=2, **kwargs)

    def test_one_track_fans_out_into_one_result_per_chapter(self):
        results = self._run(_job("First", "Album", "Last"), split_chapters=True)

        self.assertEqual(
            ["First", "Track One", "Rock/Pop: intro", "Track Three", "Last"],
            [r.title for r in results],
            "chapters must land between their neighbours, in track order",
        )
        self.assertEqual([STATUS_OK] * 5, [r.status for r in results])
        self.assertEqual(1, self.chapters.call_count)
        self.assertEqual(2, self.one.call_count)

    def test_progress_fires_once_per_result(self):
        seen = []
        self._run(
            _job("First", "Album", "Last"),
            split_chapters=True,
            on_progress=lambda result, done, total: seen.append((result.title, done, total)),
        )

        self.assertEqual(
            [1, 2, 3, 4, 5],
            [done for _, done, _ in seen],
            "a ten-chapter video has to report ten times, not once",
        )
        # ``total`` stays the track count: it is the size of the job, and the
        # chapter count is not knowable before the videos have been probed.
        self.assertEqual([3] * 5, [total for *_, total in seen])
        # Results are emitted as they land, so only the chapters of one track
        # have a guaranteed order: they arrive together and in chapter order.
        titles = [title for title, _, _ in seen]
        self.assertEqual(
            {"First", "Last", "Track One", "Rock/Pop: intro", "Track Three"}, set(titles)
        )
        first = titles.index("Track One")
        self.assertEqual(["Track One", "Rock/Pop: intro", "Track Three"], titles[first : first + 3])

    def test_the_flag_defaults_off(self):
        results = self._run(_job("First", "Album"))

        self.assertEqual(["First", "Album"], [r.title for r in results])
        self.assertEqual(0, self.probe.call_count)
        self.assertEqual(0, self.chapters.call_count)

    def test_a_chapterless_video_falls_back_to_a_plain_download(self):
        results = self._run(_job("Plain", "Album"), split_chapters=True)

        self.assertEqual(4, len(results))
        self.assertEqual(
            ["https://youtu.be/vid0"],
            [call.args[0].query for call in self.one.call_args_list],
            "only the chapterless track goes through download_one",
        )

    def test_history_is_recorded_per_chapter(self):
        self._run(_job("Plain", "Album"), split_chapters=True)

        import spot2cloud.store.history as _h

        ledger = json.loads(_h.HistoryStore(self.folder).path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                "chapter:01 - Track One.mp3",
                "chapter:02 - RockPop intro.mp3",
                "chapter:03 - Track Three.mp3",
                "query:https://youtu.be/vid0",
            ],
            sorted(ledger["entries"]),
            "a resume has to recognise the pieces, not the parent track",
        )
        self.assertNotIn("query:%s" % self.chaptered, ledger["entries"])

    def test_a_chapter_download_that_fails_does_not_sink_the_job(self):
        def boom(track, output_dir, opts, overwrite=False):
            raise RuntimeError("splitter exploded")

        results = self._run(_job("First", "Album"), chapters_side_effect=boom, split_chapters=True)

        self.assertEqual([STATUS_OK, STATUS_FAILED], [r.status for r in results])
        self.assertIn("splitter exploded", results[1].reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
