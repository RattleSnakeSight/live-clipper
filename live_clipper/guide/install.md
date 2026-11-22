# Install

Live Clipper runs on your own computer: Windows, macOS or Linux. Nothing runs on a server.

## What you need

- Python 3.11 or newer.
- ffmpeg and ffprobe (they come together). Live Clipper uses them to record and cut video.
- About 2 GB of free disk space to start. Clips and recordings take more, and the wizard asks how much the
  app may use.
- A web browser. The app is a page on your own computer.

Streamlink, the tool that records streams, installs together with Live Clipper.

## Route 1: ask your AI agent

If you use Claude Code, Codex or Qwen Code, you can hand the work to it. Copy this message and send it to your
agent:

```text
Please install the Live Clipper skill from https://github.com/RattleSnakeSight/live-clipper
and set Live Clipper up for me. Ask before you install anything. Do not enter passwords
or connect my accounts. I will do those steps myself in the browser.
```

The agent installs the skill, checks your machine, installs the app and starts it. You still do the sign-in
steps yourself.

## Route 2: install it yourself

1. Install Python and ffmpeg if you do not have them:
   - Windows: `winget install Python.Python.3.12` and `winget install Gyan.FFmpeg`
   - macOS: `brew install python@3.12 ffmpeg-full`, then `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"`
     (ffmpeg-full has the libass and freetype support the captions need)
   - Linux: `sudo apt install python3 python3-venv python3-pip ffmpeg` (or your system's equivalent)
2. Install the app with pipx, which keeps it apart from your other Python programs:

   ```text
   pipx install git+https://github.com/RattleSnakeSight/live-clipper
   ```

   If you do not have pipx, this works too:

   ```text
   python -m pip install --user git+https://github.com/RattleSnakeSight/live-clipper
   ```

3. Check the machine: `live-clipper doctor`. It prints `ok` or `FAIL` for Python, ffmpeg, ffprobe, the data
   folder, free disk space and the port.

### Or use an install script

The repository has two scripts in its `scripts` folder: `install.sh` for macOS and Linux, and `install.ps1`
for Windows PowerShell. Each one checks Python, ffmpeg and pipx, asks before it installs anything, and then
installs Live Clipper. Run it again later and it updates the app. Open the script in a text editor and read it
before you run it.

## Install the agent skill (optional)

The skill teaches your coding agent how to install, start, set up and fix Live Clipper. With the app
installed, run one of these:

```text
live-clipper skill install --agent claude
live-clipper skill install --agent codex
live-clipper skill install --agent qwen
live-clipper skill install --agent all
```

Add `--project` to put the Claude skill in the current project folder only. Restart your agent afterwards.

## Start it

There is one command to start the app:

```text
live-clipper start
```

What happens next:

1. Live Clipper starts in the background.
2. Your browser opens at `http://127.0.0.1:8765`. If that port is busy, the app picks another free one and
   prints the address.
3. The address carries a one-time sign-in token (a `?t=` link). The page remembers it, so you can bookmark the
   page afterwards.
4. The first run shows the setup wizard (see First run).

The app listens on your own computer only. Nobody else on your network can open it. If the browser ever shows
"unauthorized", run `live-clipper start` again. It opens a fresh link.

Other commands: `live-clipper stop`, `live-clipper status`, `live-clipper logs` (add `-f` to follow),
`live-clipper update` and `live-clipper uninstall`.
