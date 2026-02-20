# Backlog — Future Tasks

Items not tied to a specific sprint. Pick up when relevant epic starts or as standalone improvements.

---

## Web Source Validation — Platform Allowlist

**Context:** `source_type=web` accepts any URL, but not all platforms can be processed. VideoProcessor uses yt-dlp for video URLs and WebProcessor uses trafilatura for HTML. Some platforms block scraping, require auth, or serve JS-only content.

**Task:** Define an allowlist of supported platforms per source_type:

- **video URLs** — YouTube (confirmed working via yt-dlp), Vimeo, possibly others. Need to test and confirm which platforms yt-dlp can handle reliably.
- **web URLs** — trafilatura works well for static HTML. Platforms with heavy JS rendering (SPAs), paywalls, or anti-scraping (e.g. Medium with limits, LinkedIn) may fail silently or return garbage.

**Deliverables:**

1. Test major platforms (YouTube, Vimeo, Dailymotion for video; Wikipedia, GitHub, dev blogs for web) and document results.
2. Create a `SUPPORTED_PLATFORMS` config (domain -> source_type mapping) with known-good entries.
3. Optionally warn (not block) when URL domain is not in the allowlist: "This platform has not been verified, processing may fail."
4. Add platform-specific notes to API docs (e.g. "YouTube: public videos only, age-restricted may fail").

**Priority:** Medium — current behavior works for known-good URLs, but users will hit confusing errors with unsupported platforms.
