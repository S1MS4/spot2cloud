# sc-scrape

Archive a music library you own, and publish recordings you made.

Stage one turns a Spotify album or playlist, a YouTube playlist or video, or a
bare playlist name into a folder of tagged audio with cover art. Stage two, only
if you ask for it, drives a real browser against soundcloud.com and publishes
that folder as one set. Both stages resume, so a re-run skips what is already
done and never puts a second copy of an album on your profile.

## Install

Needs Python 3.9+ and ffmpeg on your PATH.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium   # only needed for uploading
```

This gives you the `sc-scrape` command. `python -m sc_scrape` also works.

## Usage

```bash
sc-scrape "https://open.spotify.com/playlist/<id>"
sc-scrape -n "playlist name" -o ~/Music/rips
sc-scrape -f playlists.txt -q 320 -w 8
sc-scrape --split-chapters -u "https://www.youtube.com/watch?v=<id>"

sc-scrape --login
sc-scrape -d "Songs/My Album" --upload --privacy private
```

Accepts Spotify links and URIs, YouTube links, bare playlist names, or a text
file of any of those. Spotify needs no credentials: the tracklist is read from
the public page.

Common flags: `-o` output dir, `-q` bitrate (64 to 320, default 192), `-w`
workers (1 to 8), `--format` (mp3, opus, m4a, flac, wav), `--overwrite`,
`--split-chapters`, `--upload`, `-d` folder to upload, `--privacy`, `--attach`,
`--dry-run`, `--watch`, `--serve`, `-v`. Run `sc-scrape --help` for all of them.

Exit codes: 0 fine, 1 something failed, 2 usage error, 130 interrupted.

## Intended use

For music you have the right to keep, and recordings you made or control. You
are responsible for what you download and upload.

Nothing here circumvents DRM or an access control. It reads pages and streams
that Spotify and YouTube already serve to any browser. Support for
DRM-protected sources is out of scope and will not be added, which is what
separates a lawful tool from a circumvention device under DMCA 1201.

Spotify supplies metadata only. Every byte of audio comes from YouTube.

## Known limits

YouTube's ceiling for music is about 130 kbps opus, so `-q 320` adds no
fidelity. Use `--format opus` to copy that stream instead of re-encoding it.

SoundCloud sits behind bot detection, and it has blocked Playwright's bundled
Chromium during login on this machine. The DOM selectors in
`soundcloud/uploader.py` are therefore unvalidated against the real page.
`--dry-run` reports which ones resolve. The reliable route is to start your own
Chrome with `--remote-debugging-port=9222` and pass `--attach`.

`--privacy` defaults to `public` on the command line, while the library
functions default to `private`. Pass `--privacy private` while you are still
checking a release.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t . -p "test_*.py"
```

279 tests, about 1.3 seconds, offline: no network, no downloads, no browser.

## Disclaimer

**I am not responsible for what you do with this script, or for your account
being suspended, limited or deleted.**

Automating a browser against soundcloud.com may breach SoundCloud's terms, and
downloading from YouTube may breach YouTube's. That is between you and them. The
software is provided as is, without warranty of any kind; see
[LICENSE](LICENSE), sections 7 and 8.

If you do not hold the rights to something, do not download it and do not
upload it.
