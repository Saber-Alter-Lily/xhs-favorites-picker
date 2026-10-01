# Changelog

## 0.6.2 - 2026-09-30

- Promotes the downloaded-image library to an explicit **本地阅读** entry instead of the ambiguous “已下载” label.
- Scans the existing `downloads/` library at startup and shows the detected article count in the top navigation.
- Adds a discovery-page banner that opens the local reader directly when an existing v0.4/v0.5/v0.6 library is found.
- The reader now shows the exact `downloads` path it is scanning; an empty library explains how to recover an older installation without moving or re-downloading files.
- No library migration is introduced. Existing v0.4 metadata and image layouts remain read in place.

## 0.6.1 - 2026-09-30

- Fixed Windows PowerShell 5.1 startup/login verification aborting on harmless redbook stderr status output (`Using saved cookie file`).
- Session verification now captures native stdout/stderr and trusts the actual Node exit code instead of PowerShell's `NativeCommandError` wrapper.
- Applied the same compatibility fix to automatic login recovery, first-time setup verification, and manual `login.bat` verification.
- No user library, favorites cache, subscription state, Chrome profile, or cookie migration is required from v0.6.0; v0.4/v0.5 library compatibility remains unchanged.

## 0.6.0 - 2026-09-30

### Added
- Quick-save flow for Xiaohongshu share text/links with domain allowlisting.
- Sanitized local favorites cache; tokenized note URLs are not persisted.
- Manual favorites synchronization.
- Lazy/paginated author loading.
- Risk-control circuit breaker and global request pacing.
- Per-process local API session token, Host/Origin checks and security headers.
- `upgrade_from_v04_v05.bat` for in-place program upgrades while preserving local state.
- Explicit v0.4 metadata/image-name compatibility tests.

### Changed
- Author browsing no longer defaults to full-history `--all` loading.
- Subscription scans default to 8 pages, slower paging and slower inter-author cadence.
- Automatic subscription checks are rate-limited across application restarts.
- Existing post folders are reused by note ID even when titles change.
- Existing metadata is merged and original save timestamps are preserved when possible.
- Chrome remote debugging is loopback-bound and broad `remote-allow-origins=*` was removed.

### Security
- Cookie file ACL hardening is attempted on Windows after export.
- Common token/cookie values are redacted from surfaced redbook errors.
- Local mutating APIs require authenticated JSON requests.
- Non-local Host headers are rejected to reduce DNS-rebinding exposure.
