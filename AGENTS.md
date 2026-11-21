# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Qwen Code and others) working with Live Clipper.

## If the user wants to install or run the app

Follow `skills/live-clipper/SKILL.md`. In short:

1. Check Python 3.11+ and ffmpeg. Ask before installing anything.
2. `pipx install git+<REPO_URL>` (fallback `python -m pip install --user git+<REPO_URL>`), or run
   `scripts/install.sh` / `scripts/install.ps1`.
3. `live-clipper start`, then let the user answer the setup wizard in the browser.
4. Problems: `live-clipper doctor`, `live-clipper status`, `live-clipper logs`.

`scripts/install-skill.py --agent claude|codex|qwen|all` copies the skill into the agent's skills folder.

Do not connect accounts, enter credentials, enable automatic posting or post anything for the user.

## Repository layout

- `live_clipper/` is the app (flat modules). `registry.py` lists the services that plug into the core.
- `live_clipper/guide/` is the in-app user guide (Markdown, shipped as package data).
- `live_clipper/web/` is the browser UI.
- `docs/ARCHITECTURE.md` is the plug-in contract; `docs/CONTRACTS.md` is the contract between subsystems;
  `docs/DEVIATIONS.md` records where behavior differs from the design it was ported from.
- `skills/live-clipper/` is the agent skill; `scripts/` holds the install scripts.
- `tests/` holds the tests. No test may use the network.

## Build, lint, test

```
python -m venv .venv
.venv/bin/pip install -e .[dev]      # Windows: .venv\Scripts\pip
ruff check .
pytest -q
```

Both commands must pass before you commit. ffmpeg on PATH is needed by some tests.

## Rules for contributors

- No personal data anywhere: no real names, usernames, e-mail addresses, local paths, hostnames, channel
  names, social handles or IDs in code, tests, fixtures, docs or commit messages.
- Examples and test data are synthetic: channels `streamer_alpha`, `streamer_beta`; handles `@example_clips`;
  domains `example.com`.
- Never commit secrets or a real `.env`. `.env.example` holds placeholders only.
- Tests never contact real platforms and never post anything. Use `httpx.MockTransport` and fakes.
- Never log tokens. Secrets go through the vault (`ctx.secrets`) and the logging redaction.
- Code must work on Windows, macOS and Linux with Python 3.11+.
