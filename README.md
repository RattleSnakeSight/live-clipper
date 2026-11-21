# Live Clipper

Live Clipper watches the streamers you pick, around the clock. It finds the moments worth keeping, cuts them
into vertical clips and posts them to your own channels. It runs on your computer and costs nothing.

It is for people who clip streams as a side project or a small channel: you choose the streamers, you decide
what gets posted, and you do not hand your accounts to a service.

- Free and open source (MIT).
- Self-hosted. There is no hosted backend and no account with us.
- No telemetry. The app talks only to the platforms you connect. See the [privacy page](live_clipper/guide/privacy.md).
- Logins are kept in an encrypted file on your machine. The web interface listens on 127.0.0.1 only.

## Get started

### 1. Ask your coding agent to install it

Claude Code, Codex, Qwen Code and similar agents can do the install and walk you through it. Paste this:

```
Install the Live Clipper skill and then use it to install and start Live Clipper.
Repository: https://github.com/RattleSnakeSight/live-clipper
Run: pipx install git+https://github.com/RattleSnakeSight/live-clipper
then: live-clipper skill install --agent claude   (use codex or qwen if that is your agent)
Ask me before installing anything. Do not connect accounts or post anything for me.
```

The agent can also clone the repository and run `python scripts/install-skill.py --agent claude`. Either way
it then follows [the skill](live_clipper/skill/SKILL.md). Agents working on the code read [AGENTS.md](AGENTS.md).

### 2. Run one command

```
live-clipper start
```

### 3. Finish in the browser

Your browser opens on 127.0.0.1. Finish the setup wizard, connect your accounts, paste a streamer link,
choose how to monitor it, and switch it on.

## Install without an agent

With pipx:

```
pipx install git+https://github.com/RattleSnakeSight/live-clipper
live-clipper doctor
live-clipper start
```

Or use the install scripts from a clone: `sh scripts/install.sh` (macOS, Linux) or `scripts/install.ps1`
(Windows PowerShell). They check Python, FFmpeg and pipx, and ask before installing anything.

## Requirements

- Python 3.11 or newer.
- FFmpeg and ffprobe on your PATH.
- A computer that can stay on. Speech recognition runs locally; the setup wizard picks defaults from your
  hardware. A recent CPU works, a GPU makes it faster.
- Disk space for recordings and clips. The wizard asks for a storage folder and a size cap.

## Platforms

| Platform | Live status | Chat | Capture | Posting |
|---|---|---|---|---|
| Twitch | official API (Helix) or stream probe | IRC read-only, or EventSub with your token | Streamlink | not a posting target |
| Kick | official API, or stream probe | webhooks only (needs a public HTTPS address) | Streamlink | not a posting target |
| YouTube | official Data API, or stream probe | live chat API (uses quota) | Streamlink (YouTube may refuse some networks) | Shorts upload (your own Google app) |
| TikTok | not applicable | not applicable | not applicable | Content Posting API (your own TikTok app) |
| Instagram | not applicable | not applicable | not applicable | Reels via Graph API (your own Meta app) |

All of the above is written and tested against recorded and fake responses and local test streams. None of
it has been run against a real account on the real services yet. See [Status](#status).

Being allowed to watch a channel is not permission to record it or repost it. Ask each streamer before you
clip and post their stream. You are responsible for following their rules and each platform's terms.

Details are in the [platforms guide](live_clipper/guide/platforms.md) and the [posting guide](live_clipper/guide/posting.md).

## Guide

The same pages open inside the app.

- [Install](live_clipper/guide/install.md)
- [First run](live_clipper/guide/first-run.md)
- [Monitoring](live_clipper/guide/monitoring.md)
- [Editing](live_clipper/guide/editing.md)
- [Formats and presets](live_clipper/guide/formats-and-presets.md)
- [Platforms](live_clipper/guide/platforms.md)
- [Posting](live_clipper/guide/posting.md)
- [Privacy](live_clipper/guide/privacy.md)
- [Troubleshooting](live_clipper/guide/troubleshooting.md)
- [Updating](live_clipper/guide/updating.md)
- [Uninstalling](live_clipper/guide/uninstalling.md)

## Update

```
live-clipper update
```

Your data stays where it is. See the [updating guide](live_clipper/guide/updating.md).

## Uninstall

```
live-clipper uninstall            # keeps your clips and settings
live-clipper uninstall --purge-data
pipx uninstall live-clipper
```

`--purge-data` deletes every clip and setting, and asks first. See the [uninstall guide](live_clipper/guide/uninstalling.md).

## Troubleshooting

- `live-clipper doctor` checks Python, FFmpeg, disk space, the data folder and the port.
- `live-clipper status` says whether the app is running and on which address.
- `live-clipper logs` prints recent log lines (`-f` to follow). Tokens are masked, but read a log before you share it.
- `live-clipper` not found after install: run `python -m pipx ensurepath` and open a new terminal.

More in the [troubleshooting guide](live_clipper/guide/troubleshooting.md).

## Status

Verified with automated tests on Linux, macOS and Windows (Python 3.11 and 3.13), on every change:

- Monitoring: channel states, retries, reconnects and crash recovery, using local test streams and fake
  platform responses. Live-status clients for Twitch, Kick and YouTube against recorded API responses.
- Clip selection: same decisions as the design this was ported from, on identical synthetic inputs.
- Cutting and editing: cut windows and output files (size, codec, frame rate, audio) checked with ffprobe.
- End to end on real FFmpeg: a local test stream with a chat spike becomes one rendered clip that is posted to
  the built-in test destination (automatic mode) or held for review (manual mode). Custom formats, rules and
  per-clip overrides change the rendered file. Killing the worker mid-way does not duplicate clips or posts.
- Posting: upload, retry, rate-limit, expired and revoked sign-in paths against fake platform responses.
- The browser app: setup wizard, guided tour (finish, skip, restart, replay), help text on every control.

Not verified yet:

- Any real platform account: live status, chat, capture and posting have not been run against real
  Twitch, Kick, YouTube, TikTok or Instagram accounts. That needs your own developer apps and sign-in.
- Speech-to-text captions with a real downloaded model are tested on request (`LIVE_CLIPPER_ASR_TEST=1`),
  not on every change; the default test run uses a fixed transcript. The opt-in run passed on Windows.
- Captions in a second language need converted translation models that the app does not download yet.
  Original-language captions work; a second-language render reports that the model is missing.

## Development

```
python -m venv .venv
.venv/bin/pip install -e .[dev]      # Windows: .venv\Scripts\pip
ruff check .
pytest -q
```

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems as described in
[SECURITY.md](SECURITY.md). Conduct rules are in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
