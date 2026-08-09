# CLAUDE.md

Instructions for any agent working in this repo. Read this before touching
`README.md` or any module under the repo root.

## What this project is

A two-stage tool. Stage one turns a Spotify album/playlist, a YouTube
playlist/video or a bare playlist name into `Songs/<Album>/`: tagged MP3s plus
a `cover.jpg`. Stage two, only with `--upload`, drives a real browser against
soundcloud.com and publishes that folder as one set, so the user can put their
own releases on their own profile.

Both stages are resumable. A ledger in `<output-dir>/.download_history/` skips
what is already on disk; one in `<output-dir>/.upload_history/` stops a re-run
from putting a second copy of a set on the profile. Both are directories of
one-JSON-file-per-album, siblings of the album folders, not files inside them.
A download is cheap to repeat, an upload is not, and that asymmetry is why the two
ledgers exist separately, and why only the upload one writes through on every
record.

One installable package under `src/`, grouped by concern. Each subpackage owns
one stage of the work and nothing else:

| Package | Owns |
| --- | --- |
| `cli/` | `args` (every flag), `console` (stdout/stderr, exit codes, logging), `progress` (the two bars), `upload` (login and publishing), `app` (dispatch and the download loop) |
| `download/` | `downloader` (yt-dlp, ffmpeg, tagging), `pipeline` (concurrency), `resolver` (URL expansion, error classification), `album_art` |
| `sources/` | `playlist_parser` (input to tracks), `spotify_scrape` (public-page reader) |
| `soundcloud/` | `browser_engine` (the session), `uploader` (the upload page) |
| `store/` | `json_store` (the atomic ledger base), `history`, `upload_ledger` |
| `services/` | `server` (`--serve`), `watcher` (`--watch`) |

`models.py` sits at the package root because everything imports it.

Do not add a framework, a config system or a plugin layer. New code goes in the
subpackage that already owns its concern; a new subpackage needs a reason you
can state in one line.

## Entry points

`sc-scrape` (the console script from `pyproject.toml`) and `python -m sc_scrape`
both land in `sc_scrape.cli.app:main`. There is no `main.py` and no runnable
file at the repo root; anything that tells the user to run one is stale.

Install for development with `pip install -e .`. Tests run as
`.venv/bin/python -m unittest discover -s tests -t . -p "test_*.py"`. The
`-t .` matters, or discovery cannot import the package.

## Commits

**Never credit Claude, and never add it as a collaborator.** No
`Co-Authored-By: Claude`, no "Generated with Claude Code" footer, no AI mention
in commit messages, PR bodies, release notes or the contributor list. Commits
are authored by the repo owner alone. This applies even when a tool or template
adds the trailer by default: strip it before committing.

Commit as `S1MS4 <205215025+S1MS4@users.noreply.github.com>` and nothing else.
That is the only identity that belongs in this history: GitHub shows the profile
picture only when the commit email is linked to the account, and a personal
address is not, so never substitute one. The identity is already written into
`.git/config`; never override it with `-c user.email=...`.

No real name, personal address or personal library path goes into this repo, in
any file, including this one.

Never `git add -A` or `git add .`. `Songs/`, `storage_state.json`, `.claude/`
and both ledger directories are gitignored for good reasons and a stray `-f`
would publish a live session. Stage the specific files you changed.

## README voice rules

`README.md` is the only doc, and it is the product. It is a GitHub landing
page: badge block, a screenshot, then the shortest path to a working install.
Written for someone who has never opened a terminal but is not being talked
down to.

- **Keep it around 500 words.** It is a front page, not a manual. When
  something needs more room, cut it and point at `sc-scrape --help`, which
  cannot go stale.
- Plain declarative sentences. Second person for instructions ("reopen your
  terminal"), never "we". **No em dashes**; use a comma, a colon or a full stop.
- Every claim is checkable. Numbers come from running something, not from
  memory. Check flag defaults against `build_parser` before quoting them.
- State limitations in the open. The SoundCloud bot-detection block, the
  unvalidated selectors, the ~130 kbps ceiling and the `--privacy` default
  mismatch are documented, not softened. Do not hedge one to make the project
  look better, and do not cut one to save words: cut a feature section instead.
- No corporate filler: "leverage", "seamless", "robust", "comprehensive",
  "delve", "it's worth noting", "in today's landscape".
- No three-item rhetorical lists, no "Not only", no section that ends by
  summarising itself.
- Badges are wanted, at the top, in one block. Every badge must point at
  something real in this repo. No feature grid, no roadmap, no emoji header.
- `[photoN]` markers are placeholders the owner replaces with screenshots.
  Leave them alone unless you are adding the image.

## Code rules

- **`models.py` stays dependency-free.** Everything else may import it, and the
  tests import it without yt-dlp, spotipy or Playwright installed.
- **Never share yt-dlp state across threads.** Each worker builds its own opts
  dict via `downloader.build_ydl_opts`. A single dict reused by the pool is a
  data race.
- **Ledgers never raise.** `json_store.JsonStore`, which `history.py` and
  `upload_ledger.py` both subclass, logs I/O and JSON errors and carries on.
  Losing a ledger costs a redundant download or a duplicate set; raising throws
  away work that already succeeded. Writes are atomic (temp file **in the
  ledger directory**, `fsync`, `os.replace`, so the rename stays
  same-directory) and every mutation is under the store's lock.
- **`storage_state.json` is the login.** Anyone holding it is signed in as the
  user. Keep it gitignored, keep the 0600 chmod in `browser_engine.login`, and
  never print, copy or upload its contents.
- **`spotify_scrape.py` refuses rather than guesses.** Spotify sometimes serves
  a playlist truncated to 30 rows, so the scrape cross-checks its row count
  against the count the page states, re-fetches, and raises
  `ScrapeMismatchError`. Never relax that check into a warning.
- **Playwright is imported lazily.** `uploader.py` must stay importable, and
  its scanning half testable, with no browser installed. `browser_engine.py`
  does import it at module scope, so `cli/app.py` imports `cli.upload` inside
  the branches that need it, never at module level. Check with
  `python -c "import sys, sc_scrape.cli.app; print('playwright' in sys.modules)"`.
- **The urllib3 warning filter lives in `sc_scrape/__init__.py`.** Python runs
  it before any submodule, whichever way the package is entered, so the
  ordering is structural rather than a hand-placed call. Do not move it.
- **Errors become results, not exceptions.** `resolver.classify_error` turns a
  yt-dlp failure into a short human sentence on a failed `DownloadResult`. One
  dead link never aborts a batch.
- **Comments explain why, never what.** Cover the comment and read the code: if
  the code alone says the same thing, the comment goes. No banner rules, no
  `Args:`/`Returns:` blocks restating an annotated signature, no docstring on a
  dunder that says what the dunder is. Prefer a better name or an extracted
  function over a comment.
- **Example data only, everywhere.** No real Spotify or YouTube ids, no named
  commercial recordings, no personal library paths in code, tests or docs. Use
  `EXAMPLEALBUMID00000000`-style ids (Spotify ids must stay 22 base62
  characters or `spotify_scrape._TRACK_ROW_RE` stops matching), `Example
  Album`, `Artist Name`, `Track One`.

## Lint

Ruff is configured in `pyproject.toml` and CI enforces both `ruff check` and
`ruff format --check`. Run them before committing:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format .
```

What is ignored is written down with its reason. `PLC0415` is off because the
lazy imports are load-bearing; the `UP` rules that assume 3.10+ are off because
this targets 3.9. If you need a new suppression, put the reason next to it.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t . -p "test_*.py"
```

Standard-library `unittest` only, and offline: no network, no real downloads,
no API calls, no browser. Keep it that way: add a fake or a fixture rather
than a test dependency. CI runs exactly this command on 3.9 through 3.13, plus
one macOS and one Windows job.

The README quotes a test count. It is 279 and the suite is green as of the last
run. Re-run it before publishing any number, and do not claim a pass you have
not seen.

## Housekeeping

`Songs/`, `.upload/`, `.venv/`, `.claude/`, `storage_state.json`, both ledger
directories and the build artefacts (`dist/`, `build/`, `*.egg-info/`,
`.ruff_cache/`) are gitignored.

ffmpeg and the Playwright browser binary are system dependencies that neither
pip nor `pyproject.toml` can install; if you touch install docs, the
`ffmpeg -version` check and `playwright install chromium` stay in.

Version lives in `src/sc_scrape/__init__.py` and hatch reads it from there, so
`pyproject.toml` has no second copy to drift.
