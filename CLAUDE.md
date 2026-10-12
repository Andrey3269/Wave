# CLAUDE.md — Wave

Read `docs/engineering/` (when present) before making a substantial change. Treat the current repository as the source of truth and verify all paths before acting.

## Product
Wave/«ВОЛНА» is an existing Windows desktop music client. In the reviewed archive it uses PyWebView + FastAPI/Uvicorn, vanilla HTML/CSS/JavaScript frontend, SQLite user data, YouTube Music metadata via `ytmusicapi`, SoundCloud metadata via a module using yt-dlp internals, and `yt-dlp` + `httpx` for stream resolution/proxy. Supported Track sources in the uploaded snapshot are `yt` and `sc`. Verify every detail against the working tree before relying on it.

## Engineering rules
- Do not rewrite from scratch; refactor incrementally.
- Before editing: check `git status`, inspect callers/tests, list affected files and regression risks.
- Preserve the live `Track` contract, source IDs, API shapes and playback state unless a migration is explicitly designed.
- Protect `%LOCALAPPDATA%\Wave`/`wave.db` and user downloads. Use isolated temporary data for tests.
- Keep provider failures isolated. Add timeout/retry/backoff/concurrency bounds and cache expiration deliberately.
- Test audio Range semantics, queue/player continuity, SQLite persistence and the existing Playwright e2e after relevant changes.
- No mock data, dead buttons, empty handlers or false claims of working functionality in production.
- Never add Spotify/Yandex scraping or bypass service restrictions. Use those products as design/UX references; add integrations only after official access/terms and a real implementation plan are verified.
- Do not commit secrets/cookies/tokens. Do not include `__pycache__`, user data, downloaded media, virtual environments or build outputs.
- No destructive Git operations, database replacement, release, force-push or merge without approval.

## Verification report
After a substantial task report: files changed; behavior changed; commands executed with PASS/FAIL/BLOCKED/NOT RUN; remaining risks; next recommended step. Do not claim Windows build/e2e/live provider test passed unless actually run.
