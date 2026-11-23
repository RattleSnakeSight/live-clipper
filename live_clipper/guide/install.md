# Install

Live Clipper runs on your own computer: Windows, macOS or Linux. There is no server and no account with the
makers. You install it once, then open it in your web browser.

## What you need

- Python 3.11 or newer.
- Git. The install command downloads Live Clipper from its Git repository.
- FFmpeg and ffprobe (they come together). Live Clipper records and cuts video with them. The captions need
  an FFmpeg build with the `ass` and `drawtext` filters (libass and freetype). The Windows and Linux packages
  below have them; on macOS use `ffmpeg-full`.
- A web browser. The app is a page served by your own computer.
- Disk space: about 2 GB free to start, more for recordings and clips. The setup asks how much the app may use.

Streamlink, which records streams, installs together with Live Clipper. The speech recognition model for
captions downloads the first time a clip is captioned.

## Install with an AI coding agent

Claude Code, Codex, Qwen Code or a similar agent that can run terminal commands can install Live Clipper for
you. Copy this prompt into your agent:

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

The agent follows the Live Clipper skill it installs. You still answer the setup questions, sign in to
platforms and press any button that posts.

Any agent that runs commands can install the app. Only Claude Code and Codex can pair the key helper (step 8);
see [Let your agent set up keys](agent-setup.md).

## Install it yourself

First install the prerequisites:

| System | Commands |
|---|---|
| Windows | `winget install Git.Git`, `winget install Python.Python.3.12`, `winget install Gyan.FFmpeg`, then `python -m pip install --user pipx` |
| macOS | `brew install git python@3.12 pipx ffmpeg-full`, then add `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"` to `~/.zprofile` |
| Debian, Ubuntu | `sudo apt install git python3 python3-venv pipx ffmpeg` |

Run `python -m pipx ensurepath` (or `pipx ensurepath`) and open a new terminal so the new programs are found.
Then install the app with pipx, which keeps it apart from your other Python programs:

```text
pipx install git+https://github.com/RattleSnakeSight/live-clipper
```

Without pipx, `python -m pip install --user git+https://github.com/RattleSnakeSight/live-clipper` also works
where your Python allows user installs (usually on Windows). Homebrew Python and recent Linux systems refuse
them; use pipx there.

Check the machine with `live-clipper doctor`. Each line says `[ok]` or `[FAIL]`: Python, ffmpeg, ffprobe, the
caption filters, the data folder, free disk space and port 8765. A busy port is not a problem: the app then
uses another free port.

If `live-clipper` is not found, run `python -m pipx ensurepath` and open a new terminal, or use
`python -m live_clipper` in its place.

### Or use an install script

A clone of the repository has two scripts in `scripts/`. Each checks Python, FFmpeg and pipx, asks before it
installs anything, installs Live Clipper, runs `doctor` and then starts the app. Read a script before you run
it.

- macOS and Linux: `sh scripts/install.sh` (add `--no-start` to skip the start).
- Windows: `powershell -ExecutionPolicy Bypass -File scripts\install.ps1` (add `-NoStart` to skip the start).

Running a script again later reinstalls the newest version. The scripts do not install Git.

## Install the agent skill (optional)

The skill teaches a coding agent how to install, start, set up and fix Live Clipper. With the app installed:

```text
live-clipper skill install --agent claude
```

Use `--agent codex`, `--agent qwen` or `--agent all` for other agents. `--project` puts the Claude skill in
the current folder only. `live-clipper skill path` prints where the packaged file is. Restart the agent
afterwards.

## Start it

```text
live-clipper start
```

What happens:

1. Live Clipper checks for updates (see [Updating](updating.md)) and starts in the background.
2. The terminal prints `Live Clipper: http://127.0.0.1:8765/?t=...` and your browser opens it. If port 8765 is
   busy, the app picks another free port.
3. The `?t=` part is a sign-in token. The page keeps it, so you can bookmark the page afterwards.
4. The first time, the page shows "Set up Live Clipper" (see [First run](first-run.md)).

The app listens on your own computer only (127.0.0.1); other devices on your network cannot open it. If the
browser ever says "unauthorized", run `live-clipper start` again for a fresh link. On a computer without a
display, use `live-clipper start --no-browser` and open the printed link yourself.

## Commands

| Command | What it does |
|---|---|
| `live-clipper start` | Start in the background and open the browser. `--port`, `--no-browser`, `--foreground`. |
| `live-clipper stop` | Stop the app. |
| `live-clipper status` | Print whether it runs, its port, version and update state, as JSON. |
| `live-clipper logs` | Print recent log lines. `-f` follows them. |
| `live-clipper doctor` | Check that this computer can run Live Clipper. |
| `live-clipper update` | Install the newest version and keep your data. `--check` only checks. |
| `live-clipper service install` | Start Live Clipper when you log in. `uninstall` and `status` too. |
| `live-clipper skill install` | Install the agent skill. |
| `live-clipper agent pair` | Pair the key helper (Claude Code and Codex only). |
| `live-clipper uninstall` | Stop the app and remove start at login. `--purge-data` also deletes your data. |
| `live-clipper version` | Print the version. |

Every command accepts `--home <folder>` before the command name to use another data folder (the same as the
`LIVE_CLIPPER_HOME` environment variable).
