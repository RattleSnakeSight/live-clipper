# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Qwen Code and others) working with Live Clipper.

## If the user wants to install or run the app

Follow `skills/live-clipper/SKILL.md` (the same file ships inside the package as
`live_clipper/skill/SKILL.md`; edit one, copy to the other, a test checks they match). In short:

1. Check Git, Python 3.11+ and FFmpeg with ffprobe. Ask before installing anything.
2. `pipx install git+https://github.com/RattleSnakeSight/live-clipper` (fallback
   `python -m pip install --user git+https://github.com/RattleSnakeSight/live-clipper`), or run
   `scripts/install.sh` / `scripts/install.ps1`.
3. `live-clipper start`, then let the user answer "Set up Live Clipper" in the browser. Automatic posting is
   the app's default; for a first test suggest Manual posting and the test destination.
4. Problems: `live-clipper doctor`, `live-clipper status`, `live-clipper logs`.

Install the skill for your agent in one of two ways:

- After `pipx install`: `live-clipper skill install --agent claude` (or `codex`, `qwen`, `all`;
  `--project`, `--dest PATH`). `live-clipper skill path` prints the packaged file.
- From a clone (`git clone https://github.com/RattleSnakeSight/live-clipper`):
  `python scripts/install-skill.py --agent claude`. This uses no network.

Do not connect accounts, enable automatic posting or post anything for the user, and never read, print or
pass key values yourself. Platform keys: with the user's consent, Claude Code and Codex can pair the key helper
(`live-clipper agent pair --agent claude|codex`, then `live-clipper agent serve`); other agents leave key entry
to the user. The user asks for keys in
the app; the helper reads only that platform's named keys from the place the user chose and saves them. See
SKILL.md section 4 and the in-app guide page "Let your agent set up keys".

## Repository layout

- `live_clipper/` is the app (flat modules). `registry.py` lists the services that plug into the core.
- `live_clipper/agent_setup.py` (requests, pairing, saving) and `live_clipper/agent_helper.py` (the helper
  behind `live-clipper agent serve`) are the agent-assisted key setup.
- `live_clipper/guide/` is the in-app user guide (Markdown, shipped as package data, listed in
  `GUIDE_PAGES` in `setup_service.py`). The app renders it with `web/js/md.js`: link sibling pages as
  `[Title](page.md)` without anchors, no `<url>` autolinks, HTML or images, and no code blocks inside lists.
  `tests/test_docs.py` checks this and that the README and Install page tester prompts match.
- `live_clipper/web/` is the browser UI.
- `live_clipper/sources/guide.py` and `live_clipper/publish/requirements.py` hold what the Sources and
  Destinations pages tell a customer about each way to connect: portal steps, one entry per field, error
  recovery. Portal labels come from the providers' own pages and carry the date they were read (`AS_OF`);
  mark what could not be confirmed with `verified=False`. The guide pages Platforms and Posting repeat them.
- `docs/ARCHITECTURE.md` is the plug-in contract; `docs/CONTRACTS.md` is the contract between subsystems;
  `docs/DEVIATIONS.md` records where behavior differs from the design it was ported from.
- `live_clipper/skill/` is the agent skill as shipped; `skills/live-clipper/` is its copy for clones; `scripts/` holds the install scripts.
- `tests/` holds the tests. No test may use the network.
- `live_clipper/update_gate.py` is the required update check. Every installed copy reads the line
  `__version__ = "X.Y.Z"` of `live_clipper/__init__.py` on `main`: that line is the release manifest. A release
  is a commit on main that raises it; keep the file and the line format (a test checks it). Tests point the
  check at a fake upstream (`tests/fake_upstream.py`, `tests/update_shim/`); the app has no switch for it.

## Build, lint, test

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ui]"   # Windows: .venv\Scripts\python
.venv/bin/python -m playwright install chromium
ruff check .
python -m pytest -q
```

Both checks must pass before you commit. FFmpeg with the `ass` and `drawtext` filters is needed by some tests;
browser tests need the `ui` extra and Chromium. Missing parts make those tests skip, not fail. Details and a map
of where to change what: CONTRIBUTING.md.

## Rules for contributors

- No personal data anywhere: no real names, usernames, e-mail addresses, local paths, hostnames, channel
  names, social handles or IDs in code, tests, fixtures, docs or commit messages.
- Examples and test data are synthetic: channels `streamer_alpha`, `streamer_beta`; handles `@example_clips`;
  domains `example.com`.
- Never commit secrets or a real `.env`. `.env.example` holds placeholders only.
- Tests never contact real platforms and never post anything. Use `httpx.MockTransport` and fakes.
- Never log tokens. Secrets go through the vault (`ctx.secrets`) and the logging redaction.
- Code must work on Windows, macOS and Linux with Python 3.11+.
