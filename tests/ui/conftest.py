"""Playwright fixtures: a restartable mock API server per test and one headless browser."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # the ui extra is not installed; browser tests skip
    sync_playwright = None

sys.path.insert(0, str(Path(__file__).parent))
from mock_api import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """Runs mock_api in a thread. restart() keeps the state file, like an app restart or update."""

    def __init__(self, state_file: Path, seed: bool):
        self.state_file, self.seed = state_file, seed
        self.port = _free_port()
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        app = create_app(self.state_file, self.seed, self.port)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("mock server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=10)
            self._server = None

    def restart(self) -> None:
        self.stop()
        self.start()


@pytest.fixture(scope="session")
def browser():
    if sync_playwright is None:
        pytest.skip("install the ui extra: pip install -e .[ui]")
    try:
        with sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as e:  # browser binary missing
                pytest.skip(f"chromium not installed (python -m playwright install chromium): {e}")
            yield b
            b.close()
    except RuntimeError as e:  # e.g. an event loop already running
        pytest.skip(str(e))


def _make_server(tmp_path, seed: bool, state: dict | None = None):
    import json

    f = tmp_path / "state.json"
    if state is not None:
        f.write_text(json.dumps(state))
    srv = MockServer(f, seed)
    srv.start()
    return srv


DONE = {"app_state": {"setup.completed": True, "tour.state": "skipped", "tour.version": 1}, "answers": {}}


@pytest.fixture
def fresh_server(tmp_path):
    """First launch: nothing set up, no data."""
    srv = _make_server(tmp_path, seed=False)
    yield srv
    srv.stop()


@pytest.fixture
def seeded_server(tmp_path):
    """Set up, tour already dismissed, demo channels/clips/rules present."""
    srv = _make_server(tmp_path, seed=True, state=DONE)
    yield srv
    srv.stop()


@pytest.fixture
def make_page(browser):
    contexts = []

    def make(width: int = 1280, height: int = 860, reduced_motion: bool = True):
        ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True,
                                  reduced_motion="reduce" if reduced_motion else "no-preference")
        contexts.append(ctx)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.js_errors = errors
        page.set_default_timeout(8000)
        return page

    yield make
    for c in contexts:
        c.close()
