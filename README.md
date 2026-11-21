# Live Clipper

Free, self-hosted live-stream clipper. Runs on your own computer; nothing leaves it unless you post a clip.
Work in progress.

## Install and run

Needs Python 3.11+ and [ffmpeg](https://ffmpeg.org/) on your PATH.

```
pipx install git+https://github.com/<owner>/live-clipper.git   # or: python -m pip install ...
live-clipper doctor     # check this machine
live-clipper start      # starts in the background and opens your browser
```

Other commands: `stop`, `status`, `logs [--follow]`, `update`, `service install|uninstall|status`
(start at login), `uninstall [--purge-data]`, `version`. Your data stays in your user data folder
across updates; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Guide, agents and scripts

- User guide (also shown in the app): [live_clipper/guide](live_clipper/guide/install.md).
- Let a coding agent do it: install the skill with `python scripts/install-skill.py --agent all`, or read
  [AGENTS.md](AGENTS.md).
- One-step installers: `scripts/install.sh` (macOS, Linux) and `scripts/install.ps1` (Windows).

## Develop

```
python -m venv .venv
.venv/bin/pip install -e .[dev]      # Windows: .venv\Scripts\pip
ruff check .
pytest -q
```
