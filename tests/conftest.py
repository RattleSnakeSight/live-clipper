import os
import sys
from pathlib import Path

import pytest
from fake_upstream import FakeUpstream

from live_clipper import __version__, update_gate

# The update check never leaves this machine in tests: app processes started by a test load
# tests/update_shim/sitecustomize.py, which points the check at this fake upstream ("current" unless a test
# changes it), and in-process code is pointed there directly.
UPSTREAM = FakeUpstream(__version__)
os.environ["LC_TEST_UPSTREAM"] = UPSTREAM.url
# the checkout too, so the shim can import the app whatever way it was installed
HERE = Path(__file__).parent
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(HERE / "update_shim"), str(HERE.parent), *filter(None, [os.environ.get("PYTHONPATH")])])
update_gate.project_manifest_url = update_gate.manifest_url  # kept for the test of the real address
update_gate.manifest_url = lambda: os.environ["LC_TEST_UPSTREAM"]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data dir so nothing touches the real one."""
    root = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(root))
    monkeypatch.setitem(sys.modules, "keyring", None)  # vault key file, never the OS keyring
    return root


@pytest.fixture
def upstream():
    """The shared fake upstream, put back to "current" and a clean count after the test."""
    UPSTREAM.version, UPSTREAM.mode, UPSTREAM.hits, UPSTREAM.not_modified = __version__, "", 0, 0
    yield UPSTREAM
    UPSTREAM.version, UPSTREAM.mode = __version__, ""
