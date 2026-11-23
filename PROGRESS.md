# Progress

Current state, release notes and the build history of Live Clipper.

## Current state (0.3.0)

Live Clipper is a local app that watches Twitch, Kick and YouTube streams (and creator feeds), cuts vertical
clips with captions, and posts them to YouTube Shorts, TikTok and Instagram Reels, automatically or after your
review. Everything below is in `main`:

| Area | State |
|---|---|
| Monitoring and recording (Twitch, Kick, YouTube, creator feeds) | Official live-status APIs or a labelled stream probe; Streamlink and FFmpeg capture |
| Clip selection, formats and rules | Built-in GTA VI format; your own formats, rules and per-clip changes |
| Cutting, editing, presets, captions | Local speech recognition; original-language captions; re-render after edits |
| Posting (YouTube Shorts, TikTok, Instagram Reels, test destination) | Automatic or manual; retries without duplicate posts |
| Browser app | Setup, guided tour, in-app guide, dark and light themes, phone and desktop layouts |
| Agent support | Agent skill for Claude Code, Codex and Qwen Code; key helper for Claude Code and Codex |
| Updates | Required update check with "Update pending" and "Can't check for updates" screens |

Checks on every change: ruff and the full test suite on Linux, macOS and Windows with Python 3.11 and 3.13,
and a gitleaks scan of the whole history. The required check on `main` is `ci-ok`.

Known limits:

- No real platform account has been used: live status, chat, capture and posting are verified against
  recorded or fake responses and local test streams only.
- Second-language captions need converted translation models that the app does not download.
- Kick chat needs a public https address for webhooks; without one, Kick chat is unavailable.
- Kick marks the livestreams endpoint the app uses as deprecated.
- The app does not keep the computer awake; nothing records while it sleeps.

## Releases

- **0.3.0** (2026-11-22). Live Clipper checks for a newer version at every start and open and pauses its work
  while an update is pending. It shows "Can't check for updates" and retries when no answer comes. Versions up
  to 0.2.0 do not check and must be updated once by hand.
- **0.2.0** (2026-11-22). Dark theme by default with a light option in Settings; the installing agent can
  fill in platform keys through a paired helper; reworked phone and desktop layouts; local API reference.
  The version had stayed at 0.1.0 until then, so earlier installs saw newer commits as up to date.
- **0.1.0** (2026-11-20 onwards). First public build: monitoring, selection, editing, posting, the browser
  app, the in-app guide and the install scripts.

Documentation-only changes between releases keep the version number; see
[Updating](live_clipper/guide/updating.md) for refreshing the in-app guide.

## Design decisions

- Python 3.11+, FastAPI on 127.0.0.1 only, SQLite, Streamlink and FFmpeg, local faster-whisper.
- No hosted backend, no telemetry, no paid service required. Credentials stay in a local encrypted vault.
- Module names mirror the original design, so the same synthetic inputs run through both (parity tests).
- Known quirks of the original are kept by default and listed with every deviation in
  [docs/DEVIATIONS.md](docs/DEVIATIONS.md).
- Live status uses official platform APIs when the user connects them, otherwise the stream probe, labelled
  as such. Permission to monitor is not permission to record or redistribute.
- Automatic posting is the default once an eligible destination is connected, enabled and acknowledged.
  Manual mode holds finished clips for review.
- Settings precedence: built-in format < your format < rules in order < per-clip change.
- The release manifest is the version line in `live_clipper/__init__.py` on `main`.
- `main` is protected: pull requests only, `ci-ok` must pass, no force pushes or deletions.
- License: MIT.

## History: how the first build was made

Kept for reference; it describes the work up to 0.1.0.

1. Behaviour reference: monitoring, selection, cutting, editing and posting were traced from the original
   design, with synthetic characterization fixtures (kept outside this repository).
2. Foundation: CLI, supervisor, local server, database, vault, logging, CI.
3. Subsystems built in parallel, one branch each: select, edit, publish, monitor, ui, ops, docs. Their file
   ownership is listed in [docs/CONTRACTS.md](docs/CONTRACTS.md).
4. Integration: one store, config and event implementation, the end-to-end pipeline, the real UI against the
   real API, and robustness and security review fixes.
5. Release: privacy gate on every push, public repository, branch protection, CI on three systems, and a clean
   install from the published repository.
