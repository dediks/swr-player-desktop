---
name: windows-release
description: >-
  Runbook for preparing, validating, and publishing SWARARIA AUDIO LAB Windows
  releases (SemVer version bump, Pre-release RC -> QA -> Stable tag flow, local
  Windows build from WSL2, and WSL2/Windows port-conflict avoidance). Use this
  skill when the user asks to prepare a release, bump the app version, cut a
  tag/pre-release, troubleshoot GitHub Actions Windows release builds, or build
  the Windows installer locally.
---

# Windows Desktop Release & Local Build Runbook

Detailed user-facing release documentation lives in [docs/RELEASE.md](../../../docs/RELEASE.md).

## 1. Pre-Release & Stable Release Procedure

1. **Ensure work is merged into `main`:**
   - Never cut public release tags from unmerged feature branches; verify `git status -s` is clean and `main` is synced with `origin/main` (`git fetch origin && git checkout main && git pull --ff-only origin main`).
   - Remind the user never to click GitHub Actions *"Re-run failed jobs"* after pushing a fix (re-runs execute the old commit SHA); always start a fresh run or push a new tag.
2. **Bump version in all 3 files identically (no `v` prefix):**
   - `pyproject.toml` (`version = "X.Y.Z"`)
   - `swararia/server/state.py` (`APP_VERSION = "X.Y.Z"`)
   - `packaging/installer.iss` (`#define MyAppVersion "X.Y.Z"`)
3. **Validate version sync, tag format, and both test suites:**
   ```bash
   .venv/bin/python packaging/build_desktop.py --check-version --release-tag vX.Y.Z-rc.1
   .venv/bin/python tests/python/run_tests.py
   for f in tests/js/*.test.mjs; do node "$f"; done
   ```
4. **Cut Pre-release RC first (fast GitHub Releases CDN, safe from stable auto-updater):**
   - Only commit/tag/push when explicitly requested by the user:
     `git tag vX.Y.Z-rc.1 && git push origin vX.Y.Z-rc.1`
   - `.github/workflows/release-windows.yml` runs the Ubuntu `verify` gate first, then builds on Windows with `--verify-bundle --installer`, smoke-tests `SwarariaAudioLab.exe -m yt_dlp --version`, verifies SHA-256, and publishes with `--prerelease` (ignored by `/releases/latest` in-app updater).
5. **Run Windows QA smoke checklist** (see `docs/RELEASE.md`), then cut the stable tag `vX.Y.Z` on `main` once verified.

## 2. Local Windows Build & WSL2 Dev Guardrails

- **PyInstaller cannot cross-compile Windows `.exe` from WSL2 Linux Python.** Sync the repo to a native Windows drive (excluding `.venv`, `build`, `dist`, `library`) and run `packaging/build_desktop.py --verify-bundle [--installer]` with Windows Python. Skip `--fetch-ffmpeg` when `.vendor/bin/` already has `ffmpeg.exe`, `ffprobe.exe`, and shared DLLs.
- **Avoid silent port `8765` shadowing in WSL2:** Close the installed `SwarariaAudioLab.exe` before starting `.venv/bin/python -m swararia` in WSL2; otherwise Windows `127.0.0.1:8765` routes to the installed `.exe` instead of the WSL2 dev server.
- **Optional isolated dev data:** Use `SWARARIA_LIBRARY_DIR=/tmp/swr-dev-music SWARARIA_DATA_DIR=/tmp/swr-dev-data` when testing without touching the user's real `Music\SwarariaAudioLab` library or `settings.json`.
