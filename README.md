# sc-scrape

**Spotify lets you take nothing with you.** No file, no export, no copy of the
library you spent years building. This gets your music onto your own disk, and
your own recordings onto SoundCloud in one command. Early days, open source,
fork it if you want to add to the project.

[![CI](https://github.com/S1MS4/sc-scrape/actions/workflows/ci.yml/badge.svg)](https://github.com/S1MS4/sc-scrape/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](#quick-start)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![yt-dlp](https://img.shields.io/badge/Audio-yt--dlp-FF0000?logo=youtube&logoColor=white)](#how-it-works)
[![Playwright](https://img.shields.io/badge/Upload-Playwright-2EAD33?logo=playwright&logoColor=white)](#uploading-to-soundcloud)
[![Ruff](https://img.shields.io/badge/Lint-Ruff-D7FF64?logo=ruff&logoColor=black)](pyproject.toml)
[![Tests](https://img.shields.io/badge/Tests-279%20offline-brightgreen)](#building-from-source)
[![Release](https://img.shields.io/github/v/release/S1MS4/sc-scrape?include_prereleases&label=Release)](https://github.com/S1MS4/sc-scrape/releases/latest)
[![Code size](https://img.shields.io/github/languages/code-size/S1MS4/sc-scrape)](https://github.com/S1MS4/sc-scrape)
[![Last commit](https://img.shields.io/github/last-commit/S1MS4/sc-scrape)](https://github.com/S1MS4/sc-scrape/commits/main)
[![Issues](https://img.shields.io/github/issues/S1MS4/sc-scrape)](https://github.com/S1MS4/sc-scrape/issues)
[![Stars](https://img.shields.io/github/stars/S1MS4/sc-scrape?style=social)](https://github.com/S1MS4/sc-scrape/stargazers)

<p align="center">
  [photo1]
</p>

Paste a link, get a folder of tagged audio with cover art. Re-run it and it
skips what it already has.

## Quick start

1. Install **ffmpeg**: `brew install ffmpeg`, `sudo apt install ffmpeg`, or
   `winget install Gyan.FFmpeg`. Reopen your terminal afterwards.
2. Install the tool:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
3. Run it:
   ```bash
   sc-scrape "https://open.spotify.com/playlist/<id>"
   ```

Takes Spotify links and URIs, YouTube links, bare playlist names, or a text
file of any of those. **No Spotify account or API key needed.**

**Requirements:** Python 3.9+, ffmpeg. For uploading, also
`playwright install chromium`.

<p align="center">
  [photo2]
</p>

## How it works

```
Spotify / YouTube  ──►  tracklist  ──►  yt-dlp + ffmpeg  ──►  Songs/Album/
                                                                   │
                                                              --upload
                                                                   ▼
                                                            SoundCloud set
```

Spotify supplies metadata only: titles, artists, cover art. Every byte of audio
comes from YouTube, matched against that metadata by title, artist and duration.
Nothing here circumvents DRM or an access control, and support for
DRM-protected sources is out of scope. That is what separates a lawful tool
from a circumvention device under DMCA 1201.

Common flags: `-o` output dir, `-q` bitrate, `-w` workers, `--format`
(mp3/opus/m4a/flac/wav), `--split-chapters`, `--watch`. Run `sc-scrape --help`
for the rest.

YouTube's ceiling for music is about 130 kbps opus, so `-q 320` adds no
fidelity. Use `--format opus` to copy that stream instead of re-encoding it.

## Uploading to SoundCloud

For recordings you own or control. `sc-scrape --login` opens SoundCloud's real
sign-in page; **no password ever passes through this tool.**

```bash
sc-scrape -d "Songs/My Album" --upload --privacy private
```

**Still unproven.** SoundCloud's bot detection has blocked Playwright's bundled
Chromium during login on my machine, so the page selectors are unvalidated
against the live site. `--dry-run` reports which ones resolve. The reliable
route is to be a real browser: start Chrome with `--remote-debugging-port=9222`
and pass `--attach`. Note `--privacy` defaults to `public`.

<p align="center">
  [photo3]
</p>

## Building from source

Requires Python 3.9+.

```bash
pip install -e ".[dev]"
python -m unittest discover -s tests -t . -p "test_*.py"
ruff check .
```

279 tests, about 1.3 seconds, fully offline: no network, no downloads, no
browser.

## Disclaimer

**I am not responsible for what you do with this script, or for your account
being suspended, limited or deleted.** Automating a browser against
soundcloud.com may breach SoundCloud's terms, and downloading from YouTube may
breach YouTube's. That is between you and them.

If you do not hold the rights to something, do not download it and do not
upload it.

## License

Apache 2.0, see [LICENSE](LICENSE).
