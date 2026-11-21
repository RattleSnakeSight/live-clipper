# Contributing

Fixes, features and docs are welcome. Fork the repository, make your change on a branch and open a pull
request. The maintainers review and merge. CI has to pass first.

## Build, lint, test

```
python -m venv .venv
.venv/bin/pip install -e .[dev]      # Windows: .venv\Scripts\pip
ruff check .
pytest -q
```

Run both checks before you push. Some tests need ffmpeg on your PATH. Tests never use the network.

## Privacy rules

This is a public repository. Keep these out of code, tests, fixtures, docs, issues and commit messages:

- real names, usernames, e-mail addresses, local paths and hostnames
- real streamer or channel names, social handles and IDs
- tokens, keys, cookies and `.env` files
- logs you have not read through and cleaned

Use synthetic data: channels `streamer_alpha` and `streamer_beta`, handles `@example_clips`, domain
`example.com`.

## Code

- Python 3.11 or newer. Code has to work on Windows, macOS and Linux.
- Tests never contact real platforms and never post anything. Use `httpx.MockTransport` and fakes.
- Secrets go through the vault. Never log them.
- The agent skill exists twice: `live_clipper/skill/SKILL.md` (shipped) and `skills/live-clipper/SKILL.md`
  (for clones). If you edit one, copy it to the other. A test checks they match.

See [AGENTS.md](AGENTS.md) for the repository layout.
