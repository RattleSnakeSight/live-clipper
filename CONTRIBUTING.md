# Contributing

Fixes, features and documentation are welcome. Fork the repository, make your change on a branch and open a
pull request. The maintainers review and merge; the CI checks below have to pass first.

## Set up a development copy

You need Python 3.11 or newer, Git, and FFmpeg with ffprobe on your PATH. Some tests render captions, which
need an FFmpeg build with the `ass` and `drawtext` filters (libass and freetype):

| System | FFmpeg |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (CI uses `choco install ffmpeg`) |
| macOS | `brew install ffmpeg-full`, then put `$(brew --prefix ffmpeg-full)/bin` on your PATH |
| Debian, Ubuntu | `sudo apt install ffmpeg` |

Then, from a clone:

```text
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ui]"
.venv/bin/python -m playwright install chromium
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`. On Linux, `playwright install
--with-deps chromium` also installs the browser's system libraries. The `dev` extra brings pytest and ruff;
the `ui` extra brings Playwright for the browser tests.

An editable install runs your working copy: `live-clipper start` serves your changes. Use a separate data
folder so your experiments never touch a real profile:

```text
live-clipper --home ./tmp-home start --no-browser --port 8790
```

## Run the checks

```text
ruff check .
python -m pytest -q
```

Both must pass before you push; CI runs exactly these on Linux, macOS and Windows with Python 3.11 and 3.13,
plus a gitleaks scan of the whole history. Tests never use the network.

Some tests skip on their own when a part is missing, and say why with `pytest -rs`:

- Browser tests (`tests/ui/`) need the `ui` extra and Chromium.
- Tests that cut and render video need FFmpeg and ffprobe.
- Speech recognition with a real model is opt-in, because it downloads a model:
  `LIVE_CLIPPER_ASR_TEST=1 python -m pytest tests/test_asr.py`.
- Translation tests need converted Opus-MT models in the test models folder and skip without them.

The update check never contacts GitHub in tests: `tests/conftest.py` points it at a local fake
(`tests/fake_upstream.py`) through a start-up shim in `tests/update_shim/`.

## Where to change what

| To change | Look in |
|---|---|
| Watching channels, live status, recording | `live_clipper/monitor_service.py`, `engine.py`, `sources/`, `capture.py` |
| Chat reading | `live_clipper/chat/` |
| Which moments become clips, titles | `detector.py`, `candidates.py`, `moments.py`, `editorial.py`, `titles.py`, `formats.py`, `formats/` |
| Cutting, captions, rendering, presets | `clipper.py`, `render.py`, `postprocess.py`, `captions/`, `presets.py`, `presets/` |
| Posting and destinations | `live_clipper/publish/` (setup steps shown in the app: `publish/requirements.py`) |
| Storage cleanup, notifications | `maintenance.py`, `notifications.py` |
| Setup questions, in-app guide list | `setup_service.py` |
| Update check | `update_gate.py` |
| Key helper | `agent_setup.py`, `agent_helper.py` |
| Command line | `cli.py`, `supervisor.py`, `worker.py` |
| Browser UI | `live_clipper/web/` (plain ES modules, no build step; see `docs/DESIGN.md`) |
| User guide | `live_clipper/guide/` |
| Agent skill | `live_clipper/skill/SKILL.md`, copied to `skills/live-clipper/SKILL.md` |

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the process model, the service plug-in interface, the
database and the auth model. [docs/CONTRACTS.md](docs/CONTRACTS.md) is the contract between subsystems, and
[docs/DEVIATIONS.md](docs/DEVIATIONS.md) records where behaviour differs from the design it was ported from.

## Documentation

- The user guide in `live_clipper/guide/` ships inside the app and is rendered by a small built-in Markdown
  renderer (`web/js/md.js`). Use headings, paragraphs, lists, tables, fenced code, bold, italic, inline code
  and `[text](url)` links. Link other guide pages as `[Title](page.md)`, without `#anchors`. Bare `<url>`
  links, HTML and images do not render, and a fenced code block inside a list ends the list.
  `tests/test_docs.py` checks this.
- A new guide page also needs a line in `GUIDE_PAGES` in `setup_service.py`.
- Keep instructions in one place and link to it. The README tester prompt and the one on the Install page
  must stay identical (a test checks it).
- Platform facts change: check them against the platform's own documentation, link the page, and write
  "not confirmed" where you could not confirm something.
- No em dashes in user-facing text.

## Privacy rules

This is a public repository. Keep these out of code, tests, fixtures, docs, issues and commit messages:

- real names, usernames, e-mail addresses, local paths and hostnames
- real streamer or channel names, social handles and IDs
- tokens, keys, cookies and `.env` files
- logs and screenshots you have not read through and cleaned

Use synthetic data: channels `streamer_alpha` and `streamer_beta`, handles `@example_clips`, domain
`example.com`.

## Code

- Code has to work on Windows, macOS and Linux with Python 3.11 or newer.
- Tests never contact real platforms and never post anything. Use `httpx.MockTransport` and fakes.
- Secrets go through the vault (`ctx.secrets`) and the logging redaction. Never log them.
- The agent skill exists twice: `live_clipper/skill/SKILL.md` (shipped) and `skills/live-clipper/SKILL.md`
  (for clones). Edit one and copy it to the other; a test checks they match byte for byte.
- `live_clipper/__init__.py` holds the version line that every installed copy reads to check for updates.
  Keep the file and the line format; see [AGENTS.md](AGENTS.md).

See [AGENTS.md](AGENTS.md) for the rules coding agents follow, which apply to everyone.
