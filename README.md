# sc-scrape

Archive a music library you already own, and publish recordings you made.

Stage one takes a Spotify album or playlist, a YouTube playlist or video, or a
bare playlist name, resolves the tracklist, finds each song on YouTube (with a
duration and live/remix guard), downloads it, converts it with ffmpeg, writes
ID3 tags, embeds the cover art and saves a folder-level `cover.jpg`. Stage two,
which only runs if you ask for it, drives a real browser against soundcloud.com
and publishes that folder as a single set.

Both stages are resumable. A ledger under `<output-dir>/.download_history/`
skips what is already on disk, and one under `<output-dir>/.upload_history/`
stops a re-run from putting a second copy of the same album on your profile.

## Why this exists

Spotify is where most listening happens, and it lets you take nothing with you:
no file, no export, no copy of the library you spent years building. SoundCloud
is the one large platform that still lets an artist publish directly and lets a
listener keep what they are given — and plenty of artists never post there. They
put a record on Spotify and YouTube and stop.

So this does two things. It turns a tracklist you follow on Spotify into files
on your own disk, with the metadata read from Spotify's public pages and every
byte of audio fetched from YouTube. And it takes a release you hold the rights
to and publishes it to SoundCloud with one command, instead of forty minutes of
dragging files into a web form.

## Intended use and responsibility

Two uses: keeping a copy of music you already have the right to keep, and
publishing recordings you made or control.

- **You are responsible for what you download and upload.** Whether you hold
  the rights, or have permission, is yours to judge and yours to answer for.
  Check the terms of the services you pull from and push to, and the copyright
  rules where you live.
- **Nothing here circumvents DRM or an access control.** It reads pages and
  streams that Spotify and YouTube already serve to anyone with a browser. It
  breaks no encryption and defeats no paywall, and it holds no credential of
  yours except the SoundCloud session you create yourself with `--login`.
- **Spotify hands out no audio** and this tool does not try to take any. It
  supplies metadata — title, artist, album name, track number, cover art. Every
  byte of audio comes from YouTube.
- **Ads served by YouTube's *player* never reach your files**, because yt-dlp
  fetches the audio stream directly and never runs the player. Sponsor segments
  **baked into the audio itself** ("this video is brought to you by…") are part
  of the recording and will come through, because there is nothing to strip them
  from.

Support for DRM-protected sources is out of scope and will not be added. See
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Requirements

- **Python 3.9 or newer.** Developed and tested on 3.9.6.
- **ffmpeg** — a **system dependency**, not a pip package. It converts the
  downloaded audio to MP3, writes the tags and the cover art, and transcodes
  for upload. `pip install -r requirements.txt` will not install it.
- **A Playwright browser** — only for the SoundCloud half. `pip` installs the
  Playwright library but not the browser binary; `playwright install chromium`
  does that. Without it every `--upload` run dies with
  "Executable doesn't exist".

### Installing ffmpeg

| OS | Command |
| --- | --- |
| macOS (Homebrew) | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| Windows (winget) | `winget install Gyan.FFmpeg` |

On **Windows**, close and reopen your terminal after installing — a running
shell does not pick up the new `PATH`. If you installed ffmpeg by unzipping a
build by hand instead of using winget, add its `bin` folder (the one containing
`ffmpeg.exe`) to your `PATH` yourself.

Verify before going further:

```bash
ffmpeg -version
```

A version banner means you are done. "command not found" or "is not recognized"
means ffmpeg is not on your `PATH` yet. The same install gives you `ffprobe`,
which the audio-quality section below uses.

## Install

```bash
python3 -m venv .venv
```

Activate it:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (cmd.exe)
.venv\Scripts\activate.bat
```

Then install the tool itself:

```bash
pip install -e .
playwright install chromium
```

That gives you the `sc-scrape` command. `-e` installs in place, so editing the
source changes what runs; drop it if you just want it installed.

`playwright install chromium` is a one-off post-install step. Skip it if you
only ever download; the download half never imports Playwright.

Check it worked:

```bash
sc-scrape --help
```

If you would rather not install anything, `python -m sc_scrape` works from the
project root and takes exactly the same arguments as `sc-scrape`. Every example
below uses `sc-scrape`.

## Sources it accepts

Anything below works positionally or behind `-p`/`-n`/`-u`/`-f`, in any mix:

| Source | Example |
| --- | --- |
| Spotify album | `https://open.spotify.com/album/EXAMPLEALBUMID00000000` |
| Spotify playlist | `https://open.spotify.com/playlist/EXAMPLEPLAYLISTID00000` |
| Spotify URI | `spotify:album:EXAMPLEALBUMID00000000` |
| YouTube playlist | `https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx` |
| YouTube video | `https://www.youtube.com/watch?v=EXAMPLEVID0` |
| A bare playlist name | `"example playlist name"` — searched on YouTube |
| A text file of any of those | `-f playlists.txt`, one entry per line |

Locale prefixes and `?si=` share tokens on Spotify links are stripped, so a URL
copied straight out of the "Share" menu is fine.

### Spotify needs no credentials

Spotify's track ids, titles, collection name and 640×640 cover are
server-rendered into the public `open.spotify.com` HTML, so `spotify_scrape.py`
reads them with nothing but `urllib`. **No account, no app registration, no API
key, no Premium.**

The one thing that can go wrong with scraping is silent truncation: Spotify
sometimes serves a playlist rendered down to its first 30 rows. So every scrape
checks itself. It counts the track rows it found and compares that against the
count the page states about itself (`music:song_count`, else the `og:description`
"… – 10 songs" line, else the rendered "10 songs, 31 min 55 sec" header). If the
two disagree it re-fetches, up to three times, and if they still disagree it
raises rather than handing you a folder that looks complete and is not. If the
page states no count at all, that is also a refusal — an unverified tracklist is
exactly what the module exists not to return. Albums always render in full.

`SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` are read as an **optional
fallback** only: if they are set and the scrape fails, the old Web API path is
tried. Most people cannot use it — Spotify has put Web API access behind
Premium — which is why the scraper is the primary path and the credentials are
not required for anything.

**Spotify supplies metadata only** — title, artist, album name, track number
and cover art. It hands out no audio and this tool does not try to take any.
**Every byte of audio comes from YouTube**, matched against that metadata.

One consequence worth knowing: the public page states no per-track durations,
so tracks that came from the scraper carry no `duration_ms`, and the ±15%
duration guard in `pick_best_match` cannot run for them. Matching falls back to
the title comparison plus the live/cover/remix/karaoke/nightcore/sped-up title
filter. Tracks from a YouTube playlist, and from the Web API fallback, do carry
durations and do get the duration check.

## Usage

```bash
# a Spotify playlist link, straight from the "Share" menu
sc-scrape "https://open.spotify.com/playlist/EXAMPLEPLAYLISTID00000"

# a playlist by name, plus a Spotify album URI, into a custom folder
sc-scrape -n "example playlist name" -p spotify:album:EXAMPLEALBUMID00000000 -o ~/Music/rips

# a whole YouTube playlist at 320 kbps with 8 workers
sc-scrape -p "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx" -q 320 -w 8

# a batch file (one entry per line, # comments allowed), re-fetching everything
sc-scrape -f playlists.txt --overwrite -v

# a "Full Album Stream" video, cut into one tagged MP3 per chapter
sc-scrape --split-chapters -u "https://www.youtube.com/watch?v=EXAMPLEVID1"

# keep watching playlists.txt, or serve the Spicetify extension endpoint
sc-scrape --watch
sc-scrape --serve

# save a SoundCloud session once (a browser opens; you log in there)
sc-scrape --login

# download an album and publish it as a public SoundCloud set
sc-scrape --upload -p "https://open.spotify.com/album/EXAMPLEALBUMID00000000"

# upload a folder that is already on disk, keeping it private (-d implies --upload)
sc-scrape -d "Songs/Example Album" --privacy private

# check every selector against the live page without uploading anything
sc-scrape -d "Songs/Example Album" --dry-run --headed
```

### CLI flags

**Inputs**

| Flag | Default | Description |
| --- | --- | --- |
| *(positional)* `INPUT` | — | Playlist links, Spotify URIs, video URLs or playlist names, in any mix. Combinable with the four below. |
| `-p`, `--playlist URL` | — | A Spotify or YouTube playlist/album URL or URI. Repeatable. |
| `-n`, `--name NAME` | — | A playlist name to search for on YouTube. Repeatable. |
| `-u`, `--url URL` | — | A single video URL. Repeatable. |
| `-f`, `--file PATH` | — | Text file with one entry per line; blank lines and `#` comments ignored. |

**Download**

| Flag | Default | Description |
| --- | --- | --- |
| `-o`, `--output-dir DIR` | `./Songs` | Root directory for the per-album folders. |
| `-q`, `--quality KBPS` | `192` | MP3 bitrate handed to ffmpeg, 64–320. A trailing `k` (`192k`) is accepted. |
| `-w`, `--workers N` | `4` | Parallel downloads per playlist, clamped to 1–8. |
| `--overwrite` | off | Ignore the resume ledger and re-download everything. |
| `--format {mp3,opus,m4a,flac,wav}` | `mp3` | Audio format to save. `opus` and `m4a` are copied straight from YouTube rather than re-encoded, so they keep the most fidelity and ignore `-q`. |
| `--split-chapters` | off | Split a chaptered full-album stream into one MP3 per chapter. Safe to leave on: a video with no chapters takes the normal path. |
| `-v`, `--verbose` | off | Verbose output including yt-dlp's own diagnostics. Disables the progress bar. |

**SoundCloud upload**

| Flag | Default | Description |
| --- | --- | --- |
| `--login` | off | Open SoundCloud in a browser, wait for you to sign in, save the session to `--state-file` and exit. Every other flag is ignored. |
| `--upload` | off | Publish each downloaded album to SoundCloud once it has finished. |
| `-d`, `--dir DIR` | — | Upload an album folder that is already on disk. Implies `--upload`, downloads nothing, and cannot be combined with `-p`/`-n`/`-u`/`-f` or positional inputs. |
| `--privacy {private,public}` | `public` | Visibility of the uploaded set. |
| `--artist NAME` | the album's own tag | Artist to credit on every uploaded track. SoundCloud prefills this field with your account name. |
| `--attach` | off | Drive the Chrome you already have running with `--remote-debugging-port` instead of launching one, using that browser's own profile and session. |
| `--attach-endpoint URL` | `http://127.0.0.1:9222` | CDP endpoint for `--attach`. |
| `--headed` | off | Run the browser visibly. Try this when a headless run gets challenged. |
| `--dry-run` | off | With `--upload`: navigate and check every selector, upload nothing. See the caveat below. |
| `--state-file PATH` | `storage_state.json` | The saved SoundCloud session written by `--login`. |

**Long-running modes**

| Flag | Default | Description |
| --- | --- | --- |
| `--watch` | off | Poll `playlists.txt` every 5 s and download new entries as they appear. |
| `--serve` | off | Run the loopback endpoint the Spicetify extension talks to (`127.0.0.1:5001`). |

`--watch` and `--serve` cannot be used together.

**Exit codes**

| Code | Meaning |
| --- | --- |
| `0` | Everything downloaded, or was already present, and any requested upload succeeded. |
| `1` | At least one track or upload failed, or a setup step failed. |
| `2` | Usage error (no inputs, bad `--quality`, incompatible flags). |
| `130` | Interrupted with Ctrl-C. |

Everything diagnostic — logs, the progress bar, per-track lines, errors — goes
to **stderr**. Only the final summary goes to **stdout**, so piping the tool
into another program stays useful.

### `playlists.txt` / `urls.txt` format

One entry per line. Lines whose first non-space character is `#` are comments,
blank lines are ignored, and everything else is passed through verbatim.

```text
# my regulars
https://open.spotify.com/playlist/EXAMPLEPLAYLISTID00000
https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx

# a bare name works too
example playlist name
```

## Output layout

```text
Songs/
├── skipped.txt                        # only if something failed, at the ROOT
├── .download_history/
│   └── Example Album.json             # resume ledger, one per album folder
├── .upload_history/
│   └── Example Album.json             # written only after an upload
└── Example Album/
    ├── (Bracketed) Track Three.mp3    # normal download: named by title
    ├── Track Two.mp3
    └── cover.jpg                      # 640x640 from Spotify, or the video thumbnail
```

With `--split-chapters` the pieces are numbered, because the chapter order is
the album order:

```text
Songs/
├── .download_history/
│   └── Some Full Album Stream.json
└── Some Full Album Stream/
    ├── 01 - Intro.mp3
    ├── 02 - Second Track.mp3
    ├── 03 - Third Track.mp3
    └── cover.jpg
```

Notes on the details:

- The folder is `<--output-dir>/<sanitised album or playlist name>/`. Filenames
  are sanitised to be safe on Windows, macOS and Linux and capped at 180 bytes.
- Downloads are **`.mp3`** unless you ask for something else with `--format`.
  `opus` and `m4a` are what YouTube already serves, so they are copied rather
  than re-encoded — best fidelity, and `-q` is ignored for them. The track
  number lives in the `TRCK` ID3 tag, not in the filename, except for chapter
  splits.
- `skipped.txt` is written once per run in the **root** output directory, not
  per album folder, as `URL<TAB>reason` lines.
- The two ledgers live in hidden directories **beside** the album folders, not
  inside them, one JSON file per album. That keeps the album folder holding
  nothing but the release, so it can be handed to the uploader — or to a music
  player — as-is.
- `.upload/` appears inside an album folder only when a transcode was needed —
  that is, when a file's extension is not on SoundCloud's accepted list. The
  transcode target is FLAC, since SoundCloud re-encodes server-side anyway.
  MP3 is accepted as-is, so a folder this tool produced never grows an
  `.upload/`. A folder of `.opus` files you assembled yourself will.

## `--split-chapters`

An album is often published as one 32-minute "Full Album Stream" video with
chapter markers rather than as separate tracks — including, frequently, your
own. Downloading that video normally leaves you with one enormous MP3.

`--split-chapters` probes the video's chapter markers first (metadata only), and
if there are any, downloads once and cuts the result into one MP3 per chapter
via yt-dlp's `FFmpegSplitChapters`. Pieces land as `NN - Title.mp3` in chapter
order. Everything happens in a temporary directory inside the album folder — the
un-split parent is a duplicate of the pieces, so it is deleted wholesale.

The part that needs saying: **yt-dlp stamps the parent video's metadata onto
every piece it cuts.** Left alone, a ten-track album shows up in a music player
as ten copies of "Some Artist – Full Album Stream". This pipeline re-tags each
piece afterwards with its own chapter title and its own track number, keeping
the album name and cover art from the parent.

A video with no chapters falls back to a plain single-file download, so the flag
is safe to leave on for a mixed batch. Re-runs are cheap: the piece names are
derived from a metadata-only probe, so an already-complete split is recognised
without re-fetching half an hour of audio.

## Audio quality, honestly

YouTube's ceiling for music is about **130 kbps opus** (or 130 kbps AAC). That
is the whole budget. Here is a real listing:

```bash
.venv/bin/yt-dlp --extractor-args "youtube:player_client=android_vr" \
  -F "https://www.youtube.com/watch?v=EXAMPLEVID0"
```

```text
139 m4a   audio only   |  1.24MiB   49k | mp4a.40.5   49k 22k [en] low
249 webm  audio only   |  1.17MiB   46k | opus        46k 48k [en] low
140 m4a   audio only   |  3.29MiB  130k | mp4a.40.2  130k 44k [en] medium
251 webm  audio only   |  3.27MiB  129k | opus       129k 48k [en] medium
```

There is no "high". `bestaudio` picks the 130 kbps row and that is the source.

So: **`-q 320` does not add fidelity.** The source is already a lossy ~130 kbps
stream; re-encoding it to a 320 kbps MP3 gives you a file 2.5× the size
containing the same information, minus a second generation of lossy loss. `-q
192` is the default because it is roughly transparent to a 130 kbps source at a
sane size. Use `-q 320` only if some downstream tool demands it.

Keeping the original opus untouched is the highest-fidelity option available,
because it skips the re-encode entirely — but **this pipeline does not offer
it**: `build_ydl_opts` always runs `FFmpegExtractAudio` to MP3. If you want
that, fetch with yt-dlp directly (`yt-dlp -x --audio-format opus`) into a folder
laid out the same way; the upload stage reads `.opus` fine and will transcode it
to FLAC in `.upload/` on its way to SoundCloud.

To check what you actually ended up with:

```bash
ffprobe -v error -show_entries format=bit_rate,duration \
  -show_entries stream=codec_name -of default=noprint_wrappers=1 \
  "Songs/Example Album/01 - Track One.opus"
```

```text
codec_name=opus
codec_name=mjpeg          # the embedded cover art
duration=198.020000
bit_rate=132193
```

That same track re-encoded to a 192 kbps MP3 reports `bit_rate=194727`: half
again the size, not one bit more music.

## Uploading to SoundCloud

SoundCloud has no third-party upload API, so uploading means driving the real
web app with Playwright. `soundcloud/browser_engine.py` owns the session,
`soundcloud/uploader.py`
owns the page.

**Scope: this stage is for recordings you own or control.** It publishes to
your own profile, through a session you created yourself, and it makes no
attempt to work out what the audio is or who owns it — that judgement is yours.

Every function in `uploader.py` defaults `privacy` to `private`, so a caller
that forgets the argument publishes to nobody rather than to everybody. **The
CLI is the exception:** `--privacy` defaults to `public`, so `--upload` without
that flag puts the set on your public profile. Pass `--privacy private` while
you are still checking a release.

### Logging in

```bash
sc-scrape --login
```

A browser window opens on SoundCloud's own sign-in page. You type your
credentials there — including any 2FA or captcha — and this program polls until
it sees a real `oauth_token` cookie, then saves the session to
`storage_state.json` and chmods it to `600`. It is in `.gitignore`.

**No password is ever handled by this tool.** There is no `--password` flag and
no prompt, by design: a credential we never hold is one we cannot log, leak or
leave in a shell history. What we do hold is the session file, and that file
*is* the login — anyone holding it is signed in as you. Treat it as a password;
delete it to log out.

Then:

```bash
sc-scrape --upload -p "https://open.spotify.com/album/EXAMPLEALBUMID00000000"
sc-scrape -d "Songs/Example Album" --privacy private
```

The uploader reads the folder top level only (a `mp3-192 (transcoded)/`
subfolder next to the tracks will not double them), orders tracks by their
`TRCK` tag falling back to a leading `NN -` in the filename, hands every file to
the page in **one** `set_input_files` call — which is what makes SoundCloud
treat them as a set rather than N unrelated uploads — ticks "Make a playlist",
sets the type to Album, fills in the title, attaches `cover.jpg`, sets privacy,
waits (up to 30 minutes) and saves. The resulting set URL is recorded in
`<output-dir>/.upload_history/<Album>.json` so a re-run skips it.

### Known limitation: read this before trusting the upload stage

SoundCloud sits behind bot detection, and **it has blocked Playwright's bundled
Chromium during login on this machine.** That is not a hypothetical.

Because of that block, **the DOM selectors in `uploader.py` are UNVALIDATED
against the live site.** They were written and tested against a fake page. The
control flow — scanning, ordering, transcoding, the ledger, the error paths — is
covered by tests and works. But **not one selector is confirmed to match real
SoundCloud markup.** Expect some to need updating.

They all live in a single `SELECTORS` dict at the top of `uploader.py`, for
exactly this reason: each key is a fallback chain tried in order, most durable
first (ARIA role, accessible name, visible text) down to generated CSS classes
last. When SoundCloud ships a redesign, that dict is the only thing to edit.

To check them:

```bash
sc-scrape -d "Songs/Example Album" --dry-run --headed
```

This navigates to the upload page, tries every chain, reports which ones matched
and uploads nothing.

**Caveat on reading that report:** `album_title_input` and `set_artwork_input`
only exist once a set is actually being edited, so they legitimately read as
missing on a bare upload page. The CLI nonetheless treats *any* missing
selector as a failed dry run and stops. So a dry run reporting those two as
missing is expected, not a defect; what matters is which *other* keys are
missing, and whether that set changes over time.

### The more reliable route: drive your own Chrome

The best way to look like a real browser is to be one. Two options, both
sidestepping the bundled Chromium entirely.

**1. Sign in with your normal Chrome, then lift the cookies.** The login never
happens under automation at all:

```bash
# log in to soundcloud.com in your ordinary Chrome first, then:
.venv/bin/python -c "from sc_scrape.soundcloud import browser_engine; browser_engine.import_cookies_from_browser('chrome')"
```

That writes `storage_state.json` (mode 600) and every `--upload` run afterwards
uses it. `'safari'`, `'firefox'`, `'edge'` and `'brave'` also work. On macOS the
terminal needs Full Disk Access (System Settings → Privacy & Security → Full
Disk Access) to read the cookie store.

**2. Attach to a running Chrome over CDP.** Start Chrome with remote debugging
on a *separate* profile directory, so automation cannot disturb your day-to-day
one. The profile persists cookies across runs, so you log in there once:

```bash
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.chrome-automation"
```

Then hand `--attach` to any upload run:

```bash
sc-scrape -d "Songs/Example Album" --upload --attach
```

`--attach-endpoint` points somewhere other than `http://127.0.0.1:9222` if you
started Chrome on a different port. The same thing from Python, if you would
rather drive it yourself:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path

from sc_scrape.soundcloud import browser_engine, uploader

album = uploader.prepare_for_upload(uploader.scan_album(Path("Songs/Example Album")))
with browser_engine.attached_browser() as page:
    print(uploader.upload_album(page, album, privacy="private"))
PY
```

Attaching only detaches at the end; it never closes your browser window.

Separately, `--login` and `--upload` already prefer your **installed** Chrome
over the bundled Chromium when `/Applications/Google Chrome.app` exists, falling
back silently if it does not.

### Or just upload by hand

Manual upload through SoundCloud's own web uploader is a perfectly good
fallback, and the folder layout is already exactly what it wants:

1. Go to <https://soundcloud.com/upload>.
2. Select every audio file in the album folder at once.
3. Tick **Make a playlist**, set the type to **Album**.
4. Type the album name.
5. Drag `cover.jpg` in as the artwork.
6. Set privacy, save.

That is the same sequence the automation performs, which is why the folder
layout is what it is.

**Free accounts cap total upload time at roughly 3 hours** across everything you
have ever uploaded. The uploader watches for SoundCloud's own quota notice while
waiting and reports its wording, rather than sitting on a progress bar that will
never finish.

## Watch folder

```bash
sc-scrape --watch
```

Keep `playlists.txt` open in an editor and paste references into it. The file is
`stat()`-ed every 5 seconds and re-read only when its mtime or size changed, so
an idle watcher costs one syscall per tick.

Entries already handled are recorded in a JSON sidecar named
`.playlists.txt.processed`, keyed on the raw line text — re-pasting the same URL
is a no-op, editing a line makes it new again. Failures are split in two: a
problem with the *entry* (a malformed Spotify reference, a name that matches
nothing) is permanent and never retried, while a problem with the *environment*
(missing credentials, DNS, connection reset, HTTP 429/5xx) is left unrecorded
and retried on the next pass.

## Spicetify integration

`download_playlist.js` adds a **"Download Full Playlist to Songs"** entry to the
right-click menu in the Spotify desktop client, which POSTs the current
playlist's URI to the local server.

1. Start the endpoint and leave it running:

   ```bash
   sc-scrape --serve
   ```

   It binds `127.0.0.1:5001` and serves `/`, `/health` and `POST /playlist`.
   Requests are accepted immediately (202) and handed to a single consumer
   thread, so two rapid clicks queue instead of racing over the same folder.

2. Copy the extension into Spicetify's extensions folder. **Ask Spicetify where
   that is** rather than hardcoding it — the location differs across versions:

   ```bash
   # macOS / Linux
   cp extension/download_playlist.js "$(spicetify path -e root)"/
   ```

   ```bat
   :: Windows
   for /f "delims=" %%p in ('spicetify path -e root') do copy extension\download_playlist.js "%%p"
   ```

3. Register and apply:

   ```bash
   spicetify config extensions download_playlist.js
   spicetify apply
   ```

4. Right-click any playlist or album in Spotify and choose **Download Full
   Playlist to Songs**.

The server is **loopback-only and unauthenticated by design**. Anything that can
reach the port can make this machine download arbitrary content, so `127.0.0.1`
is the point: `build_server` rejects a non-loopback host outright rather than
trusting a config file. Do not port-forward it, do not bind `0.0.0.0`, do not
run it on a machine you share with people you do not trust.
(`Access-Control-Allow-Origin: *` is set because the Spicetify page is a
different origin; CORS governs which *pages* may read the response, never which
*hosts* may reach the port.)

## Troubleshooting

**"ffmpeg was not found on your PATH"** — the tool checks before it starts and
refuses to run without it. Install it per the table above and confirm with
`ffmpeg -version`. On Windows open a *new* terminal afterwards; a manual
(non-winget) install also needs its `bin` folder added to `PATH` by hand.

**"The page needs to be reloaded"** — this is YouTube's SABR rollout. The
default web player clients return formats with no usable URL, and yt-dlp
surfaces that as a reload message. The fix is already in
`models.youtube_extractor_args`, which names the player clients explicitly:

```python
YOUTUBE_PLAYER_CLIENTS = ("android_vr", "web_safari", "tv", "default")
```

`android_vr` is what currently works. Verified 2026-08-07: `default` and `mweb`
hit SABR, `tv_embedded` reports an unsupported device, `ios` offers no matching
audio format. Which clients work changes on YouTube's schedule, so the rest are
fallbacks — if downloads start failing wholesale, this tuple is the first place
to look, and `pip install -U yt-dlp` is the first thing to try.

**Age-restricted or sign-in-required videos** — these land in `skipped.txt` as
"Age-restricted: YouTube requires a signed-in account" or "Sign-in required".
YouTube will not serve them anonymously. yt-dlp can authenticate with cookies
from a browser you are signed into, but **that is not wired to a CLI flag
here**, so for now these tracks are skipped. Other classified reasons you may
see: "Private video", "Members-only video", "Region-blocked", "Blocked on
copyright grounds".

**"no acceptable match" in `skipped.txt`** — every YouTube candidate was
rejected as the wrong version of the song. Five candidates are fetched per
track, not one, because YouTube search puts hour-long loops, sped-up edits,
karaoke versions and reactions above the real thing often enough that trusting
hit #1 fills a folder with junk. Two filters run: a **duration guard** rejecting
anything more than 15% away from the known length (this is what stops a
60-minute loop or a 30-second snippet from standing in for a 3-minute track),
and a **title filter** rejecting live takes, covers, remixes, karaoke,
nightcore, instrumentals and sped-up/slowed edits — which steps aside when the
track you asked for is itself titled that way. The duration guard is inactive
for Spotify-scraped tracks, which carry no per-track duration (see above). If a
track you actually want keeps getting rejected, it usually is not on YouTube
under a recognisable title: find it by hand and pass the URL with `-u`.

**`NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+`** — harmless. The
Python shipped with macOS is linked against LibreSSL and urllib3 warns at import
time. The package suppresses it in normal runs; you will still see it in
raw `unittest` output. Downloads are unaffected. A python.org or Homebrew Python
makes it go away.

**Downloads suddenly failing across the board** — YouTube changes its player
regularly and breaks extractors. Update yt-dlp first, before anything else:

```bash
pip install -U yt-dlp
```

**SoundCloud challenges or blocks the browser** — this is bot detection, and
there is no code here or anywhere that guarantees a run is not challenged. In
order: rerun with `--headed` so the browser paints like a real one; use your own
Chrome (see above) so nothing is being spoofed at all; then **wait**. Blocks
expire. Escalating — retrying in a loop, cycling user agents, hammering the
login page — makes it worse and risks the account, and the block notice itself
cites "rapid taps or clicks". Waiting is the right response.

## How the code is laid out

One installable package, grouped by what each part does:

```text
src/sc_scrape/
├── cli/          arguments, console output, progress bars, dispatch
├── download/     yt-dlp, ffmpeg, tagging, concurrency
├── sources/      turning a link or a name into a list of tracks
├── soundcloud/   the browser session and the upload page
├── store/        the two resume ledgers
├── services/     --watch and --serve
└── models.py     shared types and constants, dependency-free
extension/        the Spicetify client
tests/
```

`models.py` imports nothing but the standard library, so the tests run without
yt-dlp, spotipy or Playwright installed. Playwright is imported lazily and only
by `soundcloud/`, which is why a download-only install never needs a browser.

## Running tests

```bash
.venv/bin/python -m unittest discover -s tests -t . -p "test_*.py"
```

**279 tests**, about 1.3 seconds. The suite is offline: no network, no real
downloads, no API calls, no browser. It uses only the standard library's
`unittest`, so there is nothing extra to install.

## Disclaimer

**I am not responsible for what you do with this script, and I am not
responsible for your account being suspended, limited or deleted.**

Automating a browser against soundcloud.com may itself breach SoundCloud's
terms of use, and downloading from YouTube may breach YouTube's. Whether it
does, and what follows if it does, is between you and them. Read their terms
before you run this. The upload stage in particular drives a live web app on a
real account — yours.

The software is provided as is, without warranty of any kind, express or
implied. No contributor is liable for any claim, damage, loss of data, loss of
an account, or other liability arising from its use. [LICENSE](LICENSE),
sections 7 and 8, says that in the language a court reads.

If you do not hold the rights to something, do not download it and do not
upload it. That is the whole rule.
