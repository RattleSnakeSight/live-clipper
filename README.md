# Live Clipper

Live Clipper watches the livestreams you choose, finds the moments worth keeping, and cuts them into vertical
clips with captions. You review the clips, or let it post them to your own YouTube Shorts, TikTok or
Instagram Reels accounts. It runs on your own computer and is free.

It is made for people who clip streams on the side or run a small clip channel: you pick the streamers, you
decide what gets posted, and your accounts stay with you. It ships with a format tuned for Grand Theft Auto VI
streams, which you can copy and change for any other game or topic.

- **Free and open source** (MIT). No paid tier, no hosted service, no account with us.
- **Local.** The app is a page served by your computer on 127.0.0.1. Clips, settings and logins stay on your
  machine; logins are kept in an encrypted file.
- **No telemetry.** It talks to the platforms you connect, and reads its newest version number from GitHub to
  check for updates. Nothing about you is sent. See [Privacy](live_clipper/guide/privacy.md).

## What you need

- Windows, macOS or Linux, on a computer that stays on and awake while your streamers are live. The app does
  not record while the computer sleeps.
- Python 3.11 or newer, Git, and FFmpeg with ffprobe (with the `ass` and `drawtext` filters for captions; on
  macOS use Homebrew's `ffmpeg-full`).
- Disk space for recordings and clips. Setup asks for a folder and a size cap.
- For captions, speech recognition runs on your computer. A recent processor works; an NVIDIA graphics card is
  faster. The model downloads the first time a clip is captioned.

Live Clipper costs nothing. Watching and recording work without any keys, for streams that Streamlink can
play or feeds a creator gives you. Precise live status, chat on Kick
and YouTube, and posting need free developer applications that you register yourself on each platform; their
approval and limits are up to each platform.

## What it works with

| Platform | Watch (live status, chat, recording) | Post |
|---|---|---|
| Twitch | Official API or a stream probe; chat read-only, anonymous or signed in | No |
| Kick | Official API or a stream probe; chat only through a public https address (webhooks) | No |
| YouTube | Official Data API (daily quota) or the live page; chat uses quota | Shorts, directly through Google with your own Google Cloud project |
| TikTok | No | Drafts or private posts, directly through your own TikTok app; public posts need TikTok's audit |
| Instagram | No | Reels, directly through your own Meta app and a professional account |

Metricool, an outside scheduling service, is not part of Live Clipper. The maintainers' own tested setup
published Instagram and TikTok clips through it with an extra helper program this repository does not
include; a stock install posts to those two directly, with the limits above, and asks for no Metricool
account. Watching streams, the test destination and YouTube never need it. See
[Posting](live_clipper/guide/posting.md).

A creator can also give you a feed address (rtmp, rtmps, srt, rtsp or http) to record. Details, limits and the
keys each platform needs: [Platforms](live_clipper/guide/platforms.md) and
[Posting](live_clipper/guide/posting.md).

Being allowed to watch a channel is not permission to record it or repost it. Ask each streamer before you
clip and post their stream, and follow each platform's terms.

## Get started

### With a coding agent

Claude Code, Codex, Qwen Code or a similar agent that runs terminal commands can install Live Clipper and walk
you through a first test. Paste this prompt:

```text
Install Live Clipper for me from https://github.com/RattleSnakeSight/live-clipper and walk me through a safe first test.

1. Check for Git, Python 3.11 or newer, pipx, FFmpeg and ffprobe. Tell me what is missing and ask before you install anything.
2. Install it: pipx install git+https://github.com/RattleSnakeSight/live-clipper
   If it is already installed, run: pipx reinstall live-clipper
3. Install your skill for it: live-clipper skill install --agent claude (Codex: --agent codex, Qwen Code: --agent qwen).
   If the skill only loads in a new session, tell me and continue after I reopen you.
4. Run live-clipper version and live-clipper doctor. Every doctor line should say [ok]; "Port 8765: in use" is fine.
5. Run live-clipper start. It prints "Live Clipper: http://127.0.0.1:<port>/?t=..." and my browser opens "Set up Live Clipper".
6. I answer the setup myself. For this first test, suggest Manual posting and the test destination, so nothing is posted without my click. I sign in to every platform myself.
7. Help me add one channel that I own or have permission to clip, and switch monitoring on. Success: a clip appears under Clips > "Waiting for your approval", and "Post to" the test destination shows it as published on this computer only.
8. Offer to set up my platform keys. Only if I agree, and only if you are Claude Code or Codex: run live-clipper agent pair --agent claude (or --agent codex), keep live-clipper agent serve running, and I press "Ask my agent" in the app. Never read, print or ask me for key values. If I say no, I type the keys in myself.
```

Any agent that runs commands can install the app. Only Claude Code and Codex can pair the key helper, which
fills in platform keys you ask for without the keys passing through the chat. Creating developer apps,
signing in and approving access always stay with you. See
[Let your agent set up keys](live_clipper/guide/agent-setup.md).

### By hand

```text
pipx install git+https://github.com/RattleSnakeSight/live-clipper
live-clipper doctor
live-clipper start
```

[Install](live_clipper/guide/install.md) lists the prerequisite commands per system, a pip fallback, and the
install scripts in `scripts/`.

### First run

`live-clipper start` opens your browser at `http://127.0.0.1:8765` (or another free port) on "Set up Live
Clipper". Every question has a default for your computer. Then a short tour shows the app; you can skip it and
replay it from Settings. The app opens in a dark theme; Settings > Appearance switches to light.

Posting is **automatic** by default, but nothing is posted until you connect an account, confirm you have the
right to post and switch it on. For your first test, choose **Manual** posting and the **test destination**,
which posts nowhere: finished clips then wait on the Clips page for you to review. See
[First run](live_clipper/guide/first-run.md).

## Guide

The same pages open inside the app under Guide.

- [Install](live_clipper/guide/install.md): prerequisites, install routes, commands.
- [First run](live_clipper/guide/first-run.md): the setup questions, connecting accounts, a safe first test.
- [Platforms](live_clipper/guide/platforms.md): watching Twitch, Kick and YouTube; where each key is found.
- [Monitoring streams](live_clipper/guide/monitoring.md): channels, states, watch modes, keeping the computer on.
- [Formats and presets](live_clipper/guide/formats-and-presets.md): what becomes a clip and how it looks; your own format.
- [Editing clips](live_clipper/guide/editing.md): trim, captions, framing, the review queue.
- [Posting](live_clipper/guide/posting.md): connecting YouTube Shorts, TikTok and Instagram Reels step by step, Metricool, delivery states, retries.
- [Let your agent set up keys](live_clipper/guide/agent-setup.md): the key helper and its limits.
- [Troubleshooting](live_clipper/guide/troubleshooting.md): checks, common problems, cleanup order.
- [Updating](live_clipper/guide/updating.md): updates, Update pending, offline behaviour.
- [Uninstalling](live_clipper/guide/uninstalling.md): what is removed and what stays.
- [Privacy](live_clipper/guide/privacy.md): what is stored, what is sent, and when.

## Updates

```text
live-clipper update
```

Your data stays where it is. Live Clipper runs only when it is the newest version: at every start and every
open it checks GitHub, and when a newer version is out it shows **Update pending** and pauses watching,
clipping and posting until you update. Without an answer (for example offline at start) it shows **Can't
check for updates** and retries instead of running. Versions up to 0.2.0 do not have this check, so update
them once by hand with `live-clipper update`. See [Updating](live_clipper/guide/updating.md).

## Uninstall

```text
live-clipper uninstall            # keeps your clips and settings
live-clipper uninstall --purge-data
pipx uninstall live-clipper
```

`--purge-data` deletes the data folder, after asking. Access you granted on each platform stays until you
remove it there. See [Uninstalling](live_clipper/guide/uninstalling.md).

## Status

Version 0.4.0. Automated tests run on Linux, macOS and Windows with Python 3.11 and 3.13 on every change:

- Monitoring: channel states, retries, reconnects and crash recovery, with local test streams and recorded
  or fake platform responses.
- Clip selection, cutting, captions and editing: output files checked with ffprobe; custom formats, rules
  and per-clip changes change the rendered file.
- End to end on real FFmpeg: a local test stream with a chat spike becomes one rendered clip, posted to the
  test destination (automatic mode) or held for review (manual mode). Killing the worker mid-way does not
  duplicate clips or posts.
- Posting: upload, retry, rate-limit, expired and revoked sign-in paths against fake platform responses.
- The browser app: setup, tour, help text on every control, the update screens, both themes.

What has run against real platforms, and what has not:

- **A hand-repaired 0.3.0 installation, 2026-11-23.** On the maintainers' own Windows computer, a copy of
  0.3.0 with local repairs captured a live Kick stream and published the same English clip once each to
  YouTube Shorts (directly), Instagram Reels and TikTok (both through Metricool, with a helper program that
  is not in this repository). That is evidence for the workflow, not for this repository's code as it
  stands: the repairs were local.
- **This repository's code.** Not yet run against real Twitch, Kick, YouTube, TikTok or Instagram accounts
  from a clean install. The posting rules, the sign-in steps and capture start are verified against fake
  platform responses and local test streams only. Each route's status will be updated here as it is
  verified from a clean install of a released commit.

Not verified yet:

- Captions with a real downloaded speech model are tested on request, not on every change.
- Captions in a second language need converted translation models that the app does not download.
- Kick's livestreams endpoint that the app uses is marked deprecated by Kick.

Release notes and history: [PROGRESS.md](PROGRESS.md). Design and code layout:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

Fixes, features and documentation are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) explains the setup on each
system, the checks to run and where to change what. Report security problems privately as described in
[SECURITY.md](SECURITY.md). Conduct rules: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Coding agents read
[AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE). Third-party components: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
