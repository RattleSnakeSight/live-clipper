# Install

Live Clipper runs on your own computer (Windows, macOS or Linux). It needs Python 3.11 or newer and ffmpeg.

## The quick way

Run the install script for your system. It checks Python and ffmpeg, asks before installing anything, and
then installs Live Clipper with pipx.

- macOS and Linux: download `scripts/install.sh` from the repository and run `sh install.sh`.
- Windows (PowerShell): download `scripts/install.ps1` and run it.

<!-- TODO(integrator): put the real raw GitHub one-liners here once the repository address is final -->

## The manual way

1. Install Python 3.11 or newer and ffmpeg (`winget install Gyan.FFmpeg`, `brew install ffmpeg`, or
   `sudo apt install ffmpeg`).
2. Install the app: `pipx install git+<repository URL>`. Without pipx, use
   `python -m pip install --user git+<repository URL>`.
3. Check the machine: `live-clipper doctor`.
4. Start it: `live-clipper start`. Your browser opens on the setup wizard.

## Let an AI agent do it

If you use Claude Code, Codex or Qwen Code, install the Live Clipper skill and ask the agent to
install and set up Live Clipper. The skill is in `skills/live-clipper` in the repository, and
`scripts/install-skill.py` copies it to the right folder.
