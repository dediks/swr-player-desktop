# Project Context — SWARARIA AUDIO LAB

Local-first Python stdlib HTTP server + vanilla ES-module frontend + pywebview desktop app wrapping FFmpeg and `yt-dlp`.
See `docs/architecture.md` for architecture details, `docs/RELEASE.md` for the release workflow, and `docs/handoff/` for active feature handoff notes.

## Starting a New Session & Workflow
- **Session continuity:** When resuming or checking recent work, inspect `git status -s`, `git diff`, and `git log -n 5` first; check `docs/handoff/` before changing dual/native remote or Android UI flows.
- **Git & edit scope:** Never run `git commit` or `git push` unless explicitly asked. When performing Git workflow steps (branching, fetch, merge, tag, push), briefly explain the step sequence and *why* each step is needed. Keep edits tightly scoped to the request and propose a brief plan before multi-file refactors.
- **Language:** Match the user's chat language (English or Indonesian). Keep Python/JS code, comments, docstrings, and tests in English; keep user-facing UI copy, docs (`README.md`, `docs/DOCUMENTATION.md`, `docs/RELEASE.md`), and installer/build logs in Indonesian.
- **Runtime data safety:** Never read, modify, or delete user media, cookies, or browser profiles under `library/` (or `.vendor/` unless packaging work requires it).

## Repository Layout
- `swararia/config.py` — repo and static path configuration (`REPO_ROOT`, `STATIC_DIR`)
- `swararia/core/` — audio analysis/conversion, `yt-dlp` engine, safe filesystem browser, task queue
- `swararia/remote/` — native remote pairing, sessions, and TLS listener
- `swararia/server/` — HTTP server, routes (`routes/`), LAN/WSL network bridge, browser-remote hub
- `swararia/desktop/` — pywebview launcher
- `swararia/web/static/` — bundler-free frontend (`pages/<page>/`, `modules/`, `css/`, `assets/`)
- `packaging/` — PyInstaller spec (`SwarariaAudioLab.spec`), `build_desktop.py`, Inno Setup (`installer.iss`)
- `tests/python/` & `tests/js/` — Python and Node unit/guard tests

## Guardrails & Conventions
- **Strict one-way layer imports** (enforced by `tests/python/test_architecture.py`):
  `config` <- `core`, `remote` <- `server` <- `desktop`.
  - `core` must not import `server`, `desktop`, or `remote`.
  - `remote` must not import `server` or `desktop`.
  - `server` must not import `desktop`.
- **Package hygiene:** Keep `__init__.py` in every Python directory under `swararia/` (except `web/static/`). Keep versions synced across `pyproject.toml` (`version`), `swararia/server/state.py` (`APP_VERSION`), and `packaging/installer.iss` (`MyAppVersion`) — enforced by `test_packaging.py` and `packaging/build_desktop.py --check-version`.
- **Windows packaging safety:** Keep all `print(...)` string literals in `packaging/build_desktop.py` safe for Windows `cp1252`/`cp437` console encodings (enforced by `test_build_desktop_console_prints_are_cp1252_safe`).
- **Library live-sync:** Any task or endpoint that adds, renames, converts, splits, or deletes audio files must invalidate `AudioMetadataCache` (cross-platform `Path`-safe) and bump `hub.remote_hub.bump_library_revision()` (or invoke `TaskQueueManager.on_library_changed`) so FOH Player and remotes auto-refresh.
- **Testability:** Reference collaborators through their module (`state.COOKIES_FILE`, `hub.remote_hub`) instead of copying symbols at import time so `patch.object(...)` works.
- **Adding HTTP routes:** Add the handler in `swararia/server/routes/`, wire it in `get.py` or `post.py`, and add the route literal to `tests/python/route_table.txt` (`test_route_table.py`).
- **Frontend:** No bundler; keep page `<script>` URLs and `swararia/web/static/modules/spa-router.js` URLs identical (`test_static_integrity.py` checks `/static/...` references and relative imports).

## Verification
Always run **both** test suites after code edits, and clearly call out anything that still needs Windows/WSL or real phone/PC verification when developing on Linux:
- Python tests (no `pytest` needed): `.venv/bin/python tests/python/run_tests.py`
- JavaScript tests: `for f in tests/js/*.test.mjs; do node "$f"; done`
- CI equivalents (when `pytest` / `ruff` are installed): `python -m pytest` and `ruff check swararia tests`
