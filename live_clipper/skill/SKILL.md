---
name: live-clipper
description: Install, launch, set up and troubleshoot Live Clipper, a free local app that watches livestreams and cuts vertical clips. Use when the user wants to install Live Clipper, start it, run its first-time setup, check why it is not working, update it or uninstall it.
---

# Live Clipper

Live Clipper is a local Python app. It runs on the user's computer, serves its interface at `http://127.0.0.1:<port>`
and sends nothing to anyone except the platforms the user connects.

Repository (the one place this address is written in this file):

`REPO_URL = https://github.com/RattleSnakeSight/live-clipper`

Getting this skill into your agent (the user usually did this already):

- With the app installed: `live-clipper skill install --agent claude` (or `codex`, `qwen`, `all`;
  add `--project` for a Claude skill in the current project).
- From a clone of the repository: `python scripts/install-skill.py --agent claude`.

Ask before you install software, change settings, or start anything that runs at login. Never post, connect
accounts or enter credentials on the user's behalf. The user does those steps in the browser.

## 1. Check the machine

1. Python 3.11 or newer: run `python --version` (try `python3` and `py -3` too). If it is missing or older,
   tell the user and offer to install it:
   - Windows: `winget install Python.Python.3.12`
   - macOS: `brew install python@3.12`
   - Linux: `sudo apt install python3 python3-venv python3-pip` (or the distribution's equivalent)
2. ffmpeg and ffprobe: run `ffmpeg -version`. If missing, offer:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
   Open a new terminal afterwards so the PATH change applies.

## 2. Install

Preferred, with pipx:

```
pipx install git+<REPO_URL>
```

If pipx is missing, install it first (`python -m pip install --user pipx`, then `python -m pipx ensurepath`),
or use the fallback:

```
python -m pip install --user git+<REPO_URL>
```

After the install, `live-clipper skill install --agent claude` installs this skill for the agent in use.
The other route is a clone of `<REPO_URL>` and `python scripts/install-skill.py --agent claude`.

The repository also has install scripts: `scripts/install.sh` (macOS and Linux) and `scripts/install.ps1`
(Windows). They ask before installing Python, ffmpeg or pipx, then install the app and start it in the same
run, so tell the user that first. Pass `--no-start` or `-NoStart` to install without starting. Do not pass
`--yes` or `-Yes` unless the user agreed to every step.

Verify with `live-clipper version` and `live-clipper doctor`. Every line of `doctor` should say `[ok]`.
If `live-clipper` is not found after a user-level install, the Python scripts folder is not on PATH:
run `python -m pipx ensurepath` or call `python -m live_clipper` instead.

## 3. Launch and set up

1. Run `live-clipper start`. It starts in the background and opens the browser.
   Use `live-clipper start --no-browser` on a machine without a display, and read the address from
   `live-clipper status`.
2. The browser shows a first-run wizard. Tell the user it asks about: how they will use the app, which
   platforms to watch, storage folder and disk cap, how long to keep posted clips, speech recognition
   settings (defaults are picked from the hardware), where to post (or nowhere), automatic or manual posting,
   notifications, and start at login.
3. Suggest the defaults unless the user has a reason to change one. Let the user answer in the browser.
   Do not choose posting destinations or turn on automatic posting for them.
4. Connecting platform accounts happens in the app. Point the user to the in-app guide.

## 4. Troubleshoot

Run these in order and read the output before changing anything:

- `live-clipper doctor` checks Python, ffmpeg, free disk space, data folder and port.
- `live-clipper status` prints whether the app is running.
- `live-clipper logs` prints recent log lines (`-f` to follow). Secrets are masked, but do not paste logs
  anywhere without the user's say-so.
- The Status page in the app shows service states, restart count, disk space and the log tail.

Common causes: ffmpeg not on PATH (reinstall, open a new terminal), disk cap reached (post or discard
clips, or raise the cap in the app), port already in use (the app picks a free one; read it from
`live-clipper status`), another copy already running (`live-clipper start` opens the running one).

## 5. Update and uninstall

- Update: `live-clipper update`. Data is kept. Re-running the install script also works.
- Stop: `live-clipper stop`.
- Start at login: `live-clipper service install` or `live-clipper service uninstall`.
- Uninstall: `live-clipper uninstall` (add `--purge-data` only if the user wants every clip and setting
  deleted; it asks for confirmation). Then run `pipx uninstall live-clipper` or
  `python -m pip uninstall live-clipper`.
