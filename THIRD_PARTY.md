# Third-party references

This utility intentionally reuses existing open-source work instead of reimplementing Xiaohongshu login/signing behavior.

## @lucasygu/redbook

- Repository: `lucasygu/redbook`
- Runtime roles: favorites enumeration, note detail retrieval, platform search, author-profile post enumeration, subscription update checks, logged-in session handling
- Runtime version pinned by `scripts/setup_windows.ps1`: `0.8.2`
- License: MIT

## Andy-SoulShell/xhs-downloader

- Repository: `Andy-SoulShell/xhs-downloader`
- Role in this utility: reference implementation only; it is **not** a runtime dependency
- `stable_image_url()` follows the upstream media parser's approach of removing an ephemeral route prefix and image-style suffix from a URL already returned to the client, then using the stable resource key on the image CDN.
- License: MIT

The local web UI uses only Python's standard library; no additional frontend/backend framework is bundled.

No code in this package is intended to decrypt DRM or obtain content that the logged-in account cannot normally view.
