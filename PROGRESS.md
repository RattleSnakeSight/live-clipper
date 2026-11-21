# Progress

Sanitized build log for Live Clipper. Updated by the maintainers' integration session.

## Plan

1. Behaviour reference: monitoring, selection, cutting/editing and posting traced from the original design,
   with synthetic characterization fixtures (kept outside this repo).
2. Foundation: CLI, supervisor, local server, database, vault, logging, CI.
3. Parallel branches, one git worktree each: `select`, `edit`, `publish`, `monitor`, `ui`, `ops`, `docs`.
4. Integration: one store/config/events implementation, the end-to-end pipeline, real UI against the real API,
   robustness fixes from review.
5. Release: privacy gate on every push, public repository, branch protection, CI on Linux/macOS/Windows,
   clean install from the published repository.

## Status

| Area | State |
|---|---|
| Foundation (CLI, supervisor, server, DB, vault, logging) | merged |
| Selection, formats and rules | merged; parity with the original on synthetic inputs |
| Cutting, editing, presets, captions | merged; parity on cut windows and decoded output properties |
| Posting (YouTube, TikTok, Instagram, test destination) | merged; tested against fake platform responses |
| Monitoring and recording (Twitch, Kick, YouTube) | merged via integration; official live-status APIs or labelled stream probe |
| Web UI, wizard, tour, in-app guide | merged; real-app browser tests |
| Setup, storage, notifications, skill, install scripts | merged |
| Security review findings | fixed |
| Integration review findings | fixed |

## Decisions

- Python 3.11+, FastAPI on 127.0.0.1 only, SQLite, Streamlink + FFmpeg, local faster-whisper.
- No hosted backend, no telemetry, no paid service required. Credentials stay in a local encrypted vault.
- Module names mirror the original design so the same synthetic inputs run through both (parity tests).
- Known quirks of the original are kept by default and listed in `docs/DEVIATIONS.md`, with every deviation.
- Live status uses official platform APIs when the user connects them, otherwise the original stream probe,
  labelled as such. Monitoring permission is not recording or redistribution permission.
- Automatic posting is the default once an eligible destination is connected, enabled and the user has
  confirmed they have the streamer's permission. Manual mode holds finished clips for review.
- Settings precedence: built-in format < your format < rules in order < per-clip override.
- `main` is protected: pull requests only, the `ci-ok` check must pass, no force pushes or deletions.
- License: MIT.

## Verification

Commands (repo root):

```
python -m pip install -e ".[dev,ui]"
python -m playwright install chromium
ruff check .
python -m pytest -q
```

FFmpeg with libass and freetype must be on PATH (macOS: `brew install ffmpeg-full`).
Real speech recognition: `LIVE_CLIPPER_ASR_TEST=1 python -m pytest tests/test_asr.py`.

- Local (Windows, Python 3.13): ruff clean; 648 passed, 4 skipped (opt-in ASR and translation-model tests).
- Opt-in ASR run with a downloaded model: 2 passed (Windows).
- CI: Linux, macOS, Windows x Python 3.11 and 3.13, plus a gitleaks history scan; required check `ci-ok`.

## Open issues

- No real platform account has been used: live status, chat, capture and posting are verified against
  recorded/fake responses and local test streams only.
- Second-language captions need converted translation models the app does not download yet.
- Kick chat needs webhooks with a public HTTPS address; without one the app reports chat as unavailable.
- Kick's v1 livestreams endpoint is marked deprecated by Kick.

## Resuming

Read this file, `docs/ARCHITECTURE.md`, `docs/CONTRACTS.md` and `docs/DEVIATIONS.md`. Work happens on `lc/*`
branches merged to `main` through pull requests once CI passes.
