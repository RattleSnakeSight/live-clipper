---
name: live-clipper
description: Install, launch, set up and troubleshoot Live Clipper, a free local app that watches livestreams and cuts vertical clips. Use when the user wants to install Live Clipper, start it, run its first-time setup, check why it is not working, update it or uninstall it.
---

# Live Clipper

Live Clipper is a local Python app. It runs on the user's computer, serves its interface at `http://127.0.0.1:<port>`
and sends nothing to anyone except the platforms the user connects. To check for updates it reads its newest
version number from the repository on GitHub at every start and open; that request carries nothing about the
user.

Repository (the one place this address is written in this file):

`REPO_URL = https://github.com/RattleSnakeSight/live-clipper`

Getting this skill into your agent (the user usually did this already):

- With the app installed: `live-clipper skill install --agent claude` (or `codex`, `qwen`, `all`;
  add `--project` for a Claude skill in the current project).
- From a clone of the repository: `python scripts/install-skill.py --agent claude`.

Ask before you install software, change settings, or start anything that runs at login. Never post or
connect accounts on the user's behalf, and never handle key values yourself. With the user's consent you can
pair the key helper (section 4); it fills in keys the user asks for without passing them through you.

## 1. Check the machine

1. Git: run `git --version`. The install downloads from the Git repository. If missing, offer
   `winget install Git.Git` (Windows), `brew install git` (macOS) or `sudo apt install git` (Linux).
2. Python 3.11 or newer: run `python --version` (try `python3` and `py -3` too). If it is missing or older,
   tell the user and offer to install it:
   - Windows: `winget install Python.Python.3.12`
   - macOS: `brew install python@3.12`
   - Linux: `sudo apt install python3 python3-venv python3-pip` (or the distribution's equivalent)
3. ffmpeg and ffprobe: run `ffmpeg -version`. If missing, offer:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg-full` (the captions need its libass and freetype), then put it on PATH:
     `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"` (add that line to `~/.zprofile` to keep it)
   - Linux: `sudo apt install ffmpeg`
   Open a new terminal afterwards so the PATH change applies. `live-clipper doctor` later confirms that the
   caption filters (`ass`, `drawtext`) are present.

## 2. Install

Preferred, with pipx:

```
pipx install git+<REPO_URL>
```

If it is already installed, `pipx reinstall live-clipper` refreshes it from the same source (between releases
the version number does not change, so `pipx upgrade` can keep the old copy).

If pipx is missing, install it first (Windows: `python -m pip install --user pipx`; macOS: `brew install pipx`;
Debian or Ubuntu: `sudo apt install pipx`; then `python -m pipx ensurepath` or `pipx ensurepath`), or use the
fallback where the system Python allows user installs (Homebrew and recent Linux refuse them):

```
python -m pip install --user git+<REPO_URL>
```

After the install, `live-clipper skill install --agent claude` installs this skill for the agent in use.
The other route is a clone of `<REPO_URL>` and `python scripts/install-skill.py --agent claude`.

The repository also has install scripts: `scripts/install.sh` (macOS and Linux) and `scripts/install.ps1`
(Windows). They ask before installing Python, ffmpeg or pipx, then install the app and start it in the same
run, so tell the user that first. Pass `--no-start` or `-NoStart` to install without starting. Do not pass
`--yes` or `-Yes` unless the user agreed to every step.

Verify with `live-clipper version` and `live-clipper doctor`. Every line of `doctor` should say `[ok]`;
`[FAIL] Port 8765: in use` only means the app will pick another free port.
If `live-clipper` is not found after a user-level install, the Python scripts folder is not on PATH:
run `python -m pipx ensurepath` or call `python -m live_clipper` instead.

## 3. Launch and set up

1. Run `live-clipper start`. It starts in the background, prints
   `Live Clipper: http://127.0.0.1:<port>/?t=...` and opens the browser.
   Use `live-clipper start --no-browser` on a machine without a display and give the user the printed link;
   running it again while the app is up prints the link again. `live-clipper status` shows the port only.
2. The browser shows "Set up Live Clipper". It cannot be skipped, but every question has a default. Tell the
   user it asks about: how they will use the app, which platforms to watch, storage folder and disk cap, how
   long to keep posted clips, speech recognition settings (defaults are picked from the hardware), where to
   post (or nowhere), automatic or manual posting, notifications, and start at login.
3. Suggest the defaults unless the user has a reason to change one. Let the user answer in the browser.
   Do not choose posting destinations or turn on automatic posting for them. Automatic posting is the app's
   default; for a first test suggest Manual posting (it appears once any destination is picked) and, on
   Destinations, "Use the test destination", which posts nowhere.
4. Help the user add one channel they own or have permission to clip, and switch monitoring on. Success
   looks like this: the channel goes live, a clip appears on the Clips page under "Waiting for your
   approval", and "Post to" the test destination marks it published on this computer only. A clip needs the
   channel to be live and a qualifying moment, so it can take a while.
5. Connecting platform accounts happens in the app. Tell the user now that you can set up their platform
   keys for them (next section), and that typing them in on Sources and Destinations always works too.

## 4. Set up platform keys (when the user wants it)

You can fill in the user's platform keys without ever seeing them. The user starts each request in the app.
Pairing is available to Claude Code and Codex only; any other agent tells the user to type the keys in on
Sources and Destinations.

1. Ask the user whether you may set up keys for them. If yes, pair yourself with this Live Clipper (add the
   same `--home <folder>` you used for `start`, if any):
   - Claude Code: `live-clipper agent pair --agent claude`
   - Codex: `live-clipper agent pair --agent codex`
2. Start the helper in the background and keep it running while the user sets up:
   `live-clipper agent serve`. If the app was closed, or this is a new session, run it again; it reattaches.
   `live-clipper agent serve --once` answers one request and exits.
3. Tell the user: open Sources or Destinations (or the last setup page), open "Let your agent set this up",
   pick where the keys are (environment variables, a file they choose, or a credential manager entry) and
   press "Ask my agent". The helper reads only that platform's named keys from that place and saves them.
4. Read the helper's output: it prints field names and the result, never values. Never read, print or pass
   the key values yourself, never put them on a command line, and never ask the user to paste them to you.
5. If keys are missing or not accepted, say so and point to the in-app guide page "Let your agent set up
   keys" for where to get them. Developer app registration, sign-in consent and platform approval are the
   user's to do in the browser. Typing the keys in by hand always works.
6. The user withdraws access in Settings > Agent setup > Revoke, or you run `live-clipper agent unpair`.

## 5. Troubleshoot

Run these in order and read the output before changing anything:

- `live-clipper doctor` checks Python, ffmpeg, free disk space, data folder and port.
- `live-clipper status` prints whether the app is running.
- `live-clipper logs` prints recent log lines (`-f` to follow). Secrets are masked, but do not paste logs
  anywhere without the user's say-so.
- The Status page in the app shows service states, restart count, disk space and the log tail.

Common causes: ffmpeg not on PATH (reinstall, open a new terminal), disk cap reached (post or discard
clips, or raise the cap in the app), port already in use (the app picks a free one; read it from
`live-clipper status`), another copy already running (`live-clipper start` opens the running one).

## 6. Update and uninstall

- Update: `live-clipper update`. Data is kept. Re-running the install script also works.
- Update pending: when `live-clipper start` prints "Update pending" or the browser shows it, a newer version
  is out and Live Clipper does not watch, clip or post until it is updated. Ask the user, then run
  `live-clipper update` (it stops the app, installs the new version and starts it again). For a pipx install,
  `pipx upgrade live-clipper` then `live-clipper start` also works. Check with `live-clipper update --check`.
- "Can't check for updates": the check got no answer (offline, GitHub unreachable, an unusable proxy
  setting). Live Clipper retries by itself and does not run until a check works. Do not try to bypass it;
  check the connection.
- Versions up to 0.2.0 have no update check: update them once with `live-clipper update`.
- Stop: `live-clipper stop`.
- Start at login: `live-clipper service install` or `live-clipper service uninstall`.
- Uninstall: `live-clipper uninstall` (add `--purge-data` only if the user wants every clip and setting
  deleted; it asks for confirmation). Then run `pipx uninstall live-clipper` or
  `python -m pip uninstall live-clipper`.
