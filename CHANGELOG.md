# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-09

First packaged release. Everything before this lived as loose modules in a
directory that was never under version control.

### Added

- `sc-scrape` console script, plus `python -m sc_scrape` for running from a
  clone without installing.
- Installable `sc_scrape` package with concern-grouped subpackages: `cli`,
  `download`, `sources`, `soundcloud`, `store`, `services`.
- Apache-2.0 licence, contribution scope rules, and a disclaimer covering
  account suspension and absence of warranty.
- Ruff lint and format configuration, and CI running the suite on Python
  3.9-3.13 plus macOS and Windows.
- `--format {mp3,opus,m4a,flac,wav}`, `--artist`, `--attach` and
  `--attach-endpoint` are now documented in the README; they existed but were
  not written down.

### Fixed

- `probe_existing_download` referenced an undefined name on the
  non-authoritative path. The surrounding `except Exception` swallowed the
  `NameError` into `None`, so the skip-existing check silently never fired for
  plain YouTube and search-resolved tracks: every one of them was re-downloaded
  on every run. It now reads the extension back off the configured `--format`.
- Both resume ledgers wrote their temp file into the album folder while the
  target lived in a sibling directory, making `os.replace` a cross-directory
  rename rather than the same-directory one the atomicity guarantee assumes.
  Flushing a ledger also created an album folder for a download that never
  happened.
- `pick_best_match` carried two contradictory comment blocks explaining the
  same branch; the stale one claimed a guard tested a condition it does not.

### Changed

- Resume ledgers moved to `<output-dir>/.download_history/` and
  `<output-dir>/.upload_history/`, one JSON file per album, so an album folder
  holds only music and artwork.
- `HistoryStore` and `UploadLedger` now share `json_store.JsonStore` instead of
  duplicating the loader, the atomic write and the mapping protocol.
- Log records are package-qualified, so `logging.getLogger("sc_scrape")`
  captures everything.

### Removed

- Unreferenced code: `downloader.download_many`, `downloader.read_urls_file`,
  `downloader.is_probable_url`, `pipeline.run_jobs`, `resolver.playlist_title`,
  `resolver.expand_urls`, `browser_engine.human_pause`,
  `HistoryStore.prune_missing`, and five dead constants.
- `main._call_filtered`, a reflection layer that dropped keyword arguments a
  callee would not accept. It dropped nothing at any of its five call sites.

[1.0.0]: https://github.com/S1MS4/sc-scrape/releases/tag/v1.0.0
