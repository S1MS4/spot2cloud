# CLAUDE.md

Instructions for any agent working in this repo. Read this before touching
`README.md` or any module under the repo root.

## What this project is

A two-stage tool. Stage one turns a Spotify album/playlist, a YouTube
playlist/video or a bare playlist name into `Songs/<Album>/` — tagged MP3s plus
a `cover.jpg`. Stage two, only with `--upload`, drives a real browser against
soundcloud.com and publishes that folder as one set, so the user can put their
own releases on their own profile.

Both stages are resumable. A ledger in `<output-dir>/.download_history/` skips
what is already on disk; one in `<output-dir>/.upload_history/` stops a re-run
from putting a second copy of a set on the profile. Both are directories of
one-JSON-file-per-album, siblings of the album folders, not files inside them.
A download is cheap to repeat, an upload is not — that asymmetry is why the two
ledgers exist separately, and why only the upload one writes through on every
record.

Flat layout on purpose: one module per concern, no packages, no `src/`. Keep it
that way. Do not add a framework, a config system, a plugin layer or a package
directory unless the user asks for one.

The `cli_*.py` files and `json_store.py` are flat siblings, not an accidental
package. `main.py` owns argument dispatch and the download loop only:

| Module | Owns |
| --- | --- |
| `cli_console.py` | stdout/stderr, glyph tables, exit codes, logging setup |
| `cli_args.py` | every flag, plus the validation argparse cannot do |
| `cli_progress.py` | the two tqdm reporters |
| `cli_upload.py` | login, and publishing album folders |
| `json_store.py` | the atomic JSON ledger both resume stores subclass |

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

Never `git add -A` or `git add .` — `Songs/`, `storage_state.json`, `.claude/`
and both ledger directories are gitignored for good reasons and a stray `-f`
would publish a live session. Stage the specific files you changed.

## README voice rules

`README.md` is the only doc, and it is the product. It is written for someone
who has never opened a terminal but is not being talked down to. Match the
existing tone exactly:

- Plain declarative sentences that say what the thing does. Second person for
  instructions ("close and reopen your terminal"), never "we".
- Every claim is checkable. Numbers come from running something, not from
  memory. Copy-pasteable commands, real example URLs.
- State limitations in the open. The Cloudflare problem, the 128 kbps ceiling
  and the duplicate-set risk are documented, not softened. Do not remove or
  hedge a limitation section to make the project look better.
- No corporate filler: "leverage", "seamless", "robust", "comprehensive",
  "delve", "it's worth noting", "in today's landscape".
- No three-item rhetorical lists, no "Not only", no section that ends by
  summarising itself.
- Do not add badges, a feature grid, a roadmap or an emoji header.

## Code rules

- **`models.py` stays dependency-free.** Everything else may import it, and the
  tests import it without yt-dlp, spotipy or Playwright installed.
- **Never share yt-dlp state across threads.** Each worker builds its own opts
  dict via `downloader.build_ydl_opts`. A single dict reused by the pool is a
  data race.
- **Ledgers never raise.** `json_store.JsonStore`, which `history.py` and
  `upload_ledger.py` both subclass, logs I/O and JSON errors and carries on.
  Losing a ledger costs a redundant download or a duplicate set; raising throws
  away work that already succeeded. Writes are atomic — temp file **in the
  ledger directory**, `fsync`, `os.replace`, so the rename stays
  same-directory — and every mutation is under the store's lock.
- **`storage_state.json` is the login.** Anyone holding it is signed in as the
  user. Keep it gitignored, keep the 0600 chmod in `browser_engine.login`, and
  never print, copy or upload its contents.
- **`spotify_scrape.py` refuses rather than guesses.** Spotify sometimes serves
  a playlist truncated to 30 rows, so the scrape cross-checks its row count
  against the count the page states, re-fetches, and raises
  `ScrapeMismatchError`. Never relax that check into a warning.
- **Playwright is imported lazily.** `uploader.py` must stay importable, and
  its scanning half testable, with no browser installed. `browser_engine.py`
  does import it at module scope, so `main.py` imports `cli_upload` inside the
  branches that need it — never at module level. Check with
  `python -c "import sys, main; print('playwright' in sys.modules)"`.
- **Nothing may reach urllib3 before `main._silence_third_party_warnings()`.**
  It runs at import time, before every other project import, and the `# noqa:
  E402` on those imports is what marks it deliberate.
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

## Tests

```bash
.venv/bin/python -m unittest discover -p "test_*.py"
```

Standard-library `unittest` only, and offline: no network, no real downloads,
no API calls, no browser. Keep it that way — add a fake or a fixture rather
than a test dependency.

The README quotes a test count. It is 283 and the suite is green as of the
last run. Re-run it before publishing any number, and do not claim a pass you
have not seen.

## Housekeeping

`Songs/`, `.upload/`, `.venv/`, `storage_state.json` and both history files are
gitignored. ffmpeg and the Playwright browser binary are system dependencies
that `requirements.txt` cannot install; if you touch install docs, the
`ffmpeg -version` check and `playwright install chromium` stay in.
