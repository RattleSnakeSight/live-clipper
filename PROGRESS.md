# Progress

Sanitized build log for Live Clipper. Updated by the maintainers' integration session.

## Plan

1. Behaviour reference: monitoring, selection, cutting/editing and posting traced from the original design,
   with synthetic characterization fixtures (kept outside this repo).
2. Foundation: CLI, supervisor, local server, database, vault, logging, CI. **Done** (merged).
3. Parallel branches (one git worktree each): `select`, `edit`, `publish`, `monitor`, `ui`, `ops`.
4. Integration on `main`: engine wiring, parity tests, browser tests, docs.
5. Release: privacy gate, public repository, branch protection, CI on Linux/macOS/Windows, clean install test.

## Status

| Area | Branch | State |
|---|---|---|
| Foundation | `foundation` | merged |
| Selection + formats/rules | `select` | in progress |
| Cutting/editing + presets | `edit` | in progress |
| Posting | `publish` | in progress |
| Monitoring/recording | `monitor` | in progress |
| Web UI, wizard, tour, guide | `ui` | in progress |
| Setup, storage, notifications, skill, docs | `ops` | in progress |

## Decisions

- Python 3.11+, FastAPI on 127.0.0.1 only, SQLite, Streamlink + FFmpeg, local faster-whisper.
- No hosted backend, no telemetry, no paid service required. Credentials stay in a local encrypted vault.
- Module names mirror the original design so the same synthetic inputs run through both (parity tests).
- Known quirks of the original are kept by default and documented in `docs/DEVIATIONS.md`.
- Live status uses official platform APIs when the user connects them; otherwise the original stream probe,
  labelled as such. Monitoring permission is not recording or redistribution permission.
- License: MIT.

## Verification

Commands (run from the repo root):

```
python -m pip install -e .[dev]
ruff check .
pytest -q
```

- Foundation: `ruff check .` clean, `pytest -q` 50 passed (Windows, Python 3.13).

## Open issues

- Public namespace for the repository: waiting on a maintainer decision.

## Resuming

Read this file, `docs/ARCHITECTURE.md` and `docs/CONTRACTS.md`. Branch worktrees are siblings of the main
checkout (`git worktree list`). Merge order: select, edit, publish, ops, monitor, ui; then integration.
