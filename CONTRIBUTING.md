# Contributing

Patches are welcome. Keep them small and keep the tests green.

## Scope

**Support for DRM-protected sources is out of scope and will not be merged.**

This tool reads pages and streams that a service already serves publicly. It
breaks no encryption, defeats no access control, and logs into no account it
was not handed a session for. That line is what separates a lawful tool from a
circumvention device under DMCA §1201 and its equivalents elsewhere, and it is
not negotiable. A pull request that decrypts a protected stream, works around a
paywall or an entitlement check, or bundles keys for either, will be closed
without review.

Everything else is on the table.

## Before you open a pull request

Run the suite:

```bash
.venv/bin/python -m unittest discover -p "test_*.py"
```

It must stay green, and it must stay offline: no network, no real downloads, no
API calls, no browser. Add a fake or a fixture rather than a test dependency.
The suite uses the standard library's `unittest` and nothing else.

Use example data in tests and docs. No real Spotify or YouTube ids, no named
commercial recordings, no personal library paths.

## House rules

- Flat layout, one module per concern. No packages, no `src/`, no config
  system, no plugin layer.
- `models.py` stays dependency-free — the tests import it without yt-dlp,
  spotipy or Playwright installed.
- Playwright stays lazily imported, so `uploader.py` and its scanning half work
  with no browser installed.
- Ledgers log their I/O and JSON errors and carry on. They never raise: losing
  a ledger costs a redundant download, raising throws away work that succeeded.
- Errors become results, not exceptions. One dead link must never abort a
  batch.
- Comments explain why, not what.
