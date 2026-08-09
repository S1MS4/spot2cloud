"""Tests for spotify_scrape, run entirely offline.

The fixtures are trimmed copies of markup taken from the live album and
playlist pages on 2026-08-09 -- attribute order and spacing included, because
that is exactly what the regexes depend on.
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

import models
import spotify_scrape

ALBUM_ID = "EXAMPLEALBUMID00000000"
COVER = "https://i.scdn.co/image/0000000000000000000000000000000000000000"

#: (id, title) of the real Example Album, in album order.
ALBUM_TRACKS = [
    ("EXAMPLETRACKID00000001", "Track One"),
    ("EXAMPLETRACKID00000002", "Track Two"),
    ("EXAMPLETRACKID00000003", "(Bracketed) Track Three"),
    ("EXAMPLETRACKID00000004", "Track Four"),
    ("EXAMPLETRACKID00000005", "Track Five"),
    ("EXAMPLETRACKID00000006", "Track Six"),
    ("EXAMPLETRACKID00000007", "Track Seven"),
    ("EXAMPLETRACKID00000008", "Track Eight"),
    ("EXAMPLETRACKID00000009", "Track Nine"),
    ("EXAMPLETRACKID00000010", "Track Ten"),
]


def _row(track_id, title, position=None):
    """One track row, verbatim in shape; playlists suffix the id with -<n>."""
    return (
        '<div class="e-10451-list-row" data-encore-id="listRow" role="group" '
        'aria-labelledby="listrow-title-track-spotify:track:%s%s" '
        'aria-label="%s" data-testid="track-row"></div>'
        % (track_id, "" if position is None else "-%d" % position, title)
    )


def _page(rows, og_title, og_description=None, og_image=COVER, header=None,
          song_count=None):
    parts = ["<html><head>", '<meta property="og:title" content="%s"/>' % og_title]
    if song_count is not None:
        parts.append('<meta name="music:song_count" content="%s"/>' % song_count)
    if og_description is not None:
        parts.append('<meta property="og:description" content="%s"/>' % og_description)
    if og_image is not None:
        parts.append('<meta property="og:image" content="%s"/>' % og_image)
    parts.append("</head><body>")
    if header is not None:
        parts.append(
            '<div data-testid="time-and-duration"><span>2026</span>'
            "<span>%s</span></div>" % header
        )
    parts.extend(rows)
    parts.append("</body></html>")
    return "".join(parts)


def album_page(tracks=None, og_description="Artist Name · album · 2026 · 10 songs"):
    return _page(
        [_row(track_id, title) for track_id, title in (tracks or ALBUM_TRACKS)],
        og_title="Example Album - Album by Artist Name | Spotify",
        og_description=og_description,
        header="10 songs<!-- -->, <span>31 min 55 sec</span>",
    )


def playlist_page(tracks, stated=None, og_description="Songs From Lover, Live"):
    """A playlist as Spotify renders it: rows numbered, length in a meta tag."""
    return _page(
        [
            _row(track_id, title, position)
            for position, (track_id, title) in enumerate(tracks)
        ],
        og_title="Live From Paris",
        og_description=og_description,
        og_image="https://i.scdn.co/image/ab67706c0000da84",
        song_count=len(tracks) if stated is None else stated,
    )


class TestParseTracks(unittest.TestCase):
    """Row extraction: order, ids, duplicates and entities."""

    def test_album_rows_parse_in_order_with_correct_ids(self):
        rows = spotify_scrape.parse_tracks(album_page())
        self.assertEqual(
            ALBUM_TRACKS, rows, "all 10 tracks must come back in album order"
        )

    def test_repeated_rows_collapse_but_keep_first_position(self):
        # Spotify renders some rows more than once; the same track must not be
        # downloaded twice, and the surviving copy must hold its place.
        page = album_page(tracks=ALBUM_TRACKS[:3] + ALBUM_TRACKS[:3])
        self.assertEqual(ALBUM_TRACKS[:3], spotify_scrape.parse_tracks(page))

    def test_a_playlist_may_hold_the_same_track_twice(self):
        # Playlist rows carry their position in the id, which is what tells a
        # genuine repeat apart from a re-rendered row.
        duplicated = [ALBUM_TRACKS[0], ALBUM_TRACKS[1], ALBUM_TRACKS[0]]
        rows = spotify_scrape.parse_tracks(playlist_page(duplicated))
        self.assertEqual(duplicated, rows, "position makes these three distinct rows")

    def test_titles_are_unescaped(self):
        page = album_page(
            tracks=[
                ("EXAMPLETRACKID00000001", "Me &amp; You"),
                ("EXAMPLETRACKID00000002", "Don&#x27;t Stop"),
            ]
        )
        self.assertEqual(
            ["Me & You", "Don't Stop"],
            [title for _, title in spotify_scrape.parse_tracks(page)],
            "an escaped title would be tagged and searched for verbatim",
        )

    def test_unrelated_markup_yields_nothing(self):
        self.assertEqual([], spotify_scrape.parse_tracks("<html><body>hi</body></html>"))


class TestParseMetadata(unittest.TestCase):
    """Where the name, the cover and the stated track count are read from."""

    def test_album_name_and_artist_come_from_og_title(self):
        self.assertEqual(("Example Album", "Artist Name"), spotify_scrape.parse_name(album_page()))

    def test_playlist_owner_is_not_treated_as_an_artist(self):
        page = _page([], og_title="Chill Hits - playlist by Spotify | Spotify")
        self.assertEqual(
            ("Chill Hits", None),
            spotify_scrape.parse_name(page),
            "the person who made a playlist did not perform what is on it",
        )

    def test_a_name_containing_the_suffix_is_not_over_stripped(self):
        page = _page(
            [], og_title="Songs - Album by Nobody - Album by Artist Name | Spotify"
        )
        self.assertEqual(
            ("Songs - Album by Nobody", "Artist Name"),
            spotify_scrape.parse_name(page),
            "only the last ' - Album by ' is Spotify's, the rest is the name",
        )

    def test_a_name_with_no_suffix_survives_whole(self):
        self.assertEqual(
            ("Live From Paris", None),
            spotify_scrape.parse_name(playlist_page(ALBUM_TRACKS[:1])),
        )

    def test_missing_og_title_is_reported_as_missing(self):
        self.assertEqual((None, None), spotify_scrape.parse_name("<html></html>"))

    def test_cover_is_the_og_image(self):
        self.assertEqual(COVER, spotify_scrape.parse_cover(album_page()))
        self.assertIsNone(spotify_scrape.parse_cover("<html></html>"))

    def test_count_comes_from_og_description(self):
        self.assertEqual(10, spotify_scrape.parse_stated_count(album_page()))

    def test_playlist_count_comes_from_the_song_count_meta_tag(self):
        page = playlist_page(ALBUM_TRACKS[:8])
        self.assertEqual(8, spotify_scrape.parse_stated_count(page))

    def test_a_number_in_the_playlists_own_description_is_not_the_count(self):
        page = playlist_page(ALBUM_TRACKS[:8], og_description="The hottest 50 songs")
        self.assertEqual(
            8,
            spotify_scrape.parse_stated_count(page),
            "music:song_count is the only count a playlist states about itself",
        )

    def test_playlist_items_count_ignores_the_saves_figure(self):
        # Description-only rendering, served when the playlist has none of its
        # own: "37 items" is the count, "77.6K saves" is not.
        page = _page(
            [],
            og_title="rep Tour and Friends",
            og_description="Playlist · Taylor Swift · 37 items · 77.6K saves",
        )
        self.assertEqual(37, spotify_scrape.parse_stated_count(page))

    def test_count_falls_back_to_the_rendered_header(self):
        page = album_page(og_description=None)
        self.assertEqual(
            10,
            spotify_scrape.parse_stated_count(page),
            'the visible "10 songs, 31 min 55 sec" line is the backup source',
        )

    def test_thousands_separator_is_tolerated(self):
        page = _page([], og_title="Big", og_description="Playlist · x · 1,234 items")
        self.assertEqual(1234, spotify_scrape.parse_stated_count(page))

    def test_no_stated_count_returns_none(self):
        self.assertIsNone(spotify_scrape.parse_stated_count("<html></html>"))


class TestFetchPage(unittest.TestCase):
    """Transport failures must arrive as ScrapeError, not urllib exceptions."""

    def _urlopen_raising(self, exc):
        return mock.patch.object(spotify_scrape.urllib.request, "urlopen", side_effect=exc)

    def test_404_says_the_page_is_missing_or_private(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self._urlopen_raising(error):
            with self.assertRaises(spotify_scrape.ScrapeError) as ctx:
                spotify_scrape.fetch_page("https://open.spotify.com/album/x")
        self.assertIn("album or playlist not found, or it is private", str(ctx.exception))

    def test_other_http_errors_carry_the_status(self):
        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with self._urlopen_raising(error):
            with self.assertRaises(spotify_scrape.ScrapeError) as ctx:
                spotify_scrape.fetch_page("https://open.spotify.com/album/x")
        self.assertIn("429", str(ctx.exception))

    def test_network_errors_become_scrape_errors(self):
        with self._urlopen_raising(urllib.error.URLError("no route to host")):
            with self.assertRaises(spotify_scrape.ScrapeError) as ctx:
                spotify_scrape.fetch_page("https://open.spotify.com/album/x")
        self.assertIn("could not be reached", str(ctx.exception))


class TestScrapeSpotify(unittest.TestCase):
    """The whole job, with the network replaced by a fixture."""

    def _scrape(self, page, ref="https://open.spotify.com/album/%s" % ALBUM_ID):
        with mock.patch.object(spotify_scrape, "fetch_page", return_value=page) as fetch:
            job = spotify_scrape.scrape_spotify(ref)
        return job, fetch

    def test_album_becomes_a_complete_job(self):
        job, fetch = self._scrape(album_page())
        self.assertEqual("Example Album", job.name)
        self.assertEqual(models.SOURCE_SPOTIFY, job.source)
        self.assertEqual(10, len(job))
        self.assertEqual(
            [track_id for track_id, _ in ALBUM_TRACKS],
            [track.source_id for track in job.tracks],
        )
        self.assertEqual(list(range(1, 11)), [t.track_number for t in job.tracks])
        self.assertEqual(
            ["https://open.spotify.com/album/%s" % ALBUM_ID],
            [call.args[0] for call in fetch.call_args_list],
            "the canonical page is fetched, not whatever the user pasted",
        )

    def test_share_tokens_and_locale_prefixes_are_dropped(self):
        _, fetch = self._scrape(
            album_page(),
            ref="https://open.spotify.com/intl-de/album/%s?si=abc123" % ALBUM_ID,
        )
        self.assertEqual(
            "https://open.spotify.com/album/%s" % ALBUM_ID, fetch.call_args.args[0]
        )

    def test_uris_are_accepted_too(self):
        _, fetch = self._scrape(album_page(), ref="spotify:album:%s" % ALBUM_ID)
        self.assertEqual(
            "https://open.spotify.com/album/%s" % ALBUM_ID, fetch.call_args.args[0]
        )

    def test_each_track_carries_the_metadata_the_tagger_needs(self):
        job, _ = self._scrape(album_page())
        first = job.tracks[0]
        self.assertEqual("Track One", first.title)
        self.assertEqual("Artist Name", first.artist)
        self.assertEqual("Example Album", first.album)
        self.assertEqual(COVER, first.cover_url)
        self.assertIsNone(
            first.duration_ms, "the page states no per-track times, so none is guessed"
        )
        self.assertEqual("spotify:EXAMPLETRACKID00000001", first.history_key())
        self.assertTrue(
            first.query.startswith("ytsearch%d:" % models.SEARCH_CANDIDATES),
            "Spotify has no audio, so the track must arrive as a YouTube search: %r"
            % first.query,
        )
        self.assertIn("Artist Name - Track One", first.query)

    def test_playlist_tracks_get_no_album_and_no_artist(self):
        job, _ = self._scrape(
            playlist_page(ALBUM_TRACKS[:8]),
            ref="https://open.spotify.com/playlist/%s" % ALBUM_ID,
        )
        self.assertEqual("Live From Paris", job.name)
        self.assertEqual(8, len(job))
        for track in job.tracks:
            self.assertIsNone(track.album, "a playlist is not an album")
            self.assertIsNone(track.artist, "the page names the owner, not the artist")

    def test_a_short_tracklist_raises_rather_than_returning_a_partial_job(self):
        # The real failure mode: a playlist served truncated to its first rows.
        truncated = playlist_page(ALBUM_TRACKS, stated=30)
        with mock.patch.object(
            spotify_scrape, "fetch_page", return_value=truncated
        ) as fetch:
            with self.assertRaises(spotify_scrape.ScrapeMismatchError) as ctx:
                spotify_scrape.scrape_spotify(
                    "https://open.spotify.com/playlist/%s" % ALBUM_ID
                )
        message = str(ctx.exception)
        self.assertIn("found 10 tracks", message)
        self.assertIn("states 30", message)
        self.assertEqual(
            spotify_scrape.FETCH_ATTEMPTS,
            fetch.call_count,
            "a truncated page is worth asking for again before giving up",
        )

    def test_a_truncated_page_is_refetched_not_returned(self):
        truncated = playlist_page(ALBUM_TRACKS[:3], stated=10)
        whole = playlist_page(ALBUM_TRACKS, stated=10)
        with mock.patch.object(
            spotify_scrape, "fetch_page", side_effect=[truncated, whole]
        ) as fetch:
            job = spotify_scrape.scrape_spotify(
                "https://open.spotify.com/playlist/%s" % ALBUM_ID
            )
        self.assertEqual(10, len(job), "the second, whole rendering must win")
        self.assertEqual(2, fetch.call_count)

    def test_no_rows_at_all_is_reported_as_a_layout_change(self):
        page = _page(
            [], og_title="Empty - Album by Artist Name | Spotify", og_description="Artist Name · album · 0 songs"
        )
        with mock.patch.object(spotify_scrape, "fetch_page", return_value=page):
            with self.assertRaises(spotify_scrape.ScrapeLayoutError) as ctx:
                spotify_scrape.scrape_spotify("spotify:album:%s" % ALBUM_ID)
        message = str(ctx.exception)
        self.assertIn("listrow-title-track-spotify:track:", message)
        self.assertIn("layout has probably changed", message)

    def test_rows_that_cannot_be_verified_are_refused(self):
        page = _page(
            [_row(track_id, title) for track_id, title in ALBUM_TRACKS],
            og_title="Example Album - Album by Artist Name | Spotify",
        )
        with mock.patch.object(spotify_scrape, "fetch_page", return_value=page):
            with self.assertRaises(spotify_scrape.ScrapeLayoutError) as ctx:
                spotify_scrape.scrape_spotify("spotify:album:%s" % ALBUM_ID)
        self.assertIn("cannot be verified", str(ctx.exception))

    def test_a_missing_name_is_a_layout_change(self):
        page = album_page().replace('property="og:title"', 'property="og:gone"')
        with mock.patch.object(spotify_scrape, "fetch_page", return_value=page):
            with self.assertRaises(spotify_scrape.ScrapeLayoutError) as ctx:
                spotify_scrape.scrape_spotify("spotify:album:%s" % ALBUM_ID)
        self.assertIn("og:title", str(ctx.exception))

    def test_all_scrape_errors_share_a_base(self):
        for error in (spotify_scrape.ScrapeMismatchError, spotify_scrape.ScrapeLayoutError):
            self.assertTrue(issubclass(error, spotify_scrape.ScrapeError))
        self.assertTrue(issubclass(spotify_scrape.ScrapeError, RuntimeError))

    def test_a_track_link_is_rejected_before_any_fetch(self):
        with mock.patch.object(spotify_scrape, "fetch_page") as fetch:
            with self.assertRaises(ValueError):
                spotify_scrape.scrape_spotify("https://open.spotify.com/track/%s" % ALBUM_ID)
        fetch.assert_not_called()


class TestPlaylistParserWiring(unittest.TestCase):
    """The scraper leads and the Web API is only a fallback.

    Spotify put the Web API behind Premium, so most users have no working
    credentials and ``resolve_input`` must reach the scraper without them.
    """

    def setUp(self):
        import playlist_parser

        self.playlist_parser = playlist_parser
        # The API path logs its own missing-credentials error on the way to the
        # scraper. That is the expected route here, not a problem to report.
        patcher = mock.patch.object(playlist_parser.logger, "error")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scraper_is_used_when_credentials_are_missing(self):
        import os

        job = models.PlaylistJob(name="Example Album", tracks=[], source=models.SOURCE_SPOTIFY)
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(spotify_scrape, "scrape_spotify", return_value=job) as scrape:
                resolved = self.playlist_parser.resolve_input(
                    "https://open.spotify.com/album/%s" % ALBUM_ID
                )
        self.assertEqual("Example Album", resolved.name)
        self.assertEqual(
            "https://open.spotify.com/album/%s" % ALBUM_ID,
            resolved.origin,
            "resolve_input always stamps the original input",
        )
        scrape.assert_called_once()

    def test_a_scrape_failure_keeps_the_scrapers_reason(self):
        import os

        failure = spotify_scrape.ScrapeMismatchError("found 30 tracks but the page states 80")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(spotify_scrape, "scrape_spotify", side_effect=failure):
                with self.assertRaises(RuntimeError) as ctx:
                    self.playlist_parser.resolve_input("spotify:playlist:%s" % ALBUM_ID)
        message = str(ctx.exception)
        self.assertIn("found 30 tracks but the page states 80", message)
        self.assertNotIn(
            "developer.spotify.com",
            message,
            "sending the user off for API keys is bad advice under the Premium gate",
        )


if __name__ == "__main__":
    unittest.main()
