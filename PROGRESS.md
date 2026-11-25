# Progress

Current state, release notes and the build history of Live Clipper.

## Current state (0.4.2)

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
- Instagram: Meta sets up newly created apps with "Unified Login". The Instagram connection was written for
  the earlier Instagram Login and Facebook Login setups and has not been tried with a newly created Meta app.
- Metricool, an outside scheduling service used by the maintainers' own tested setup for Instagram and
  TikTok, is not part of the app. The app names it and offers nothing to connect for it.
- The app does not keep the computer awake; nothing records while it sleeps.

## Releases

- **0.4.2** (2026-11-25). A fresher look you can make your own: Settings > Appearance adds an accent colour
  (wheel, swatches or a colour code; warnings and errors keep their own colours) and an Automatic theme that
  follows the computer. Destination setup, the format editor and other long screens open one part at a time.
  Channel problems are explained in plain words, with the technical message under Details. Settings now apply
  when saved, or say clearly when they need a restart; a failed start-at-login registration is reported instead of
  shown as saved; the setup re-run has Cancel; first-run Review offers a local trial (Manual posting and the test
  destination). The clip page shows the video and the posting buttons only once the clip is rendered, and picks up
  a render that finishes while it is open. A fresh install keeps its default media folder. Instagram and TikTok
  stay with the Metricool sender when its status file goes missing or cannot be read: only choosing "Directly,
  or not at all" hands them back, and a broken status file no longer stops YouTube posting. Only one copy of the
  app runs per data folder, and `live-clipper stop` never stops another program by mistake.
- **0.4.1** (2026-11-24). Reject and Clean up now delete the files they are meant to: Reject cancels what was
  only waiting and deletes that clip's video files (kept while an upload may still need them, or when shared),
  and Clean up now deletes fully posted and rejected clips at once, finds clips the app never tracked, and shows
  by category which clips and how many bytes go and what is kept, before and when you confirm. A clip that a
  separate sender program may still post, or has claimed and not confirmed, is never deleted, and posting to the
  test destination alone never makes a clip removable. A delete that fails part way reports only the bytes that
  are really gone. Instagram and TikTok through Metricool is the preferred, skippable route on Destinations: it
  shows Connected only when a separately installed sender reports a verified brand and both accounts (the app
  does not include that sender), counts only posts the sender read back as published with a public link, and keeps
  the direct routes off while a sender is configured, also when it is offline, so a network never has two senders.
  The setup question, the cards, the README and both guides say so. No change to the claims table, the receipts or
  any other 0.4.0 format; the sender's heartbeat is a new file in the state folder.
- **0.4.0** (2026-11-24). Sources and Destinations are rebuilt around "Watch a stream" and
  "Post my clips": one way to connect at a time, each field with the provider's name for it and where to
  find it, the installation's own callback and webhook addresses with Copy, and four numbered steps per
  destination. A saved key and a verified account are separate states. A sign-in that lands on a different
  account than the one posting was switched on for switches posting off for that destination and holds
  unconfirmed uploads for your confirmation. Google's sign-in always asks which account to use.
  Posting now keeps two hours between different moments of the same streamer across all real destinations,
  posts a moment once per account, and takes the newest waiting clip first; the rules existed in the code but
  the upload path never applied them. Streamlink is started through the Python interpreter instead of its
  launcher, which Windows application control can refuse; a reader that cannot start is shown as an error and
  retried. A YouTube channel that reached its own upload limit (answered with HTTP 400) now waits and
  retries instead of failing for good. A database from a hand-patched 0.3.0 installation keeps working: its
  claims table is honoured, each native upload stores the file's SHA-256 in it (read before the claim, never
  under the database lock), the same file or clip is not sent twice to one network, and an outside publisher's
  rows still hold the streamer. Capture: live Kick video was received and decoded through Streamlink
  on one Windows computer; long-run and many-streamer capture are not qualified. A refused channel lookup after a token refresh now shows its
  reason instead of an error page. Platforms and Posting in the guide follow the same steps (checked
  2026-11-23).
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
