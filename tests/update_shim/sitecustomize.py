"""Test-only shim, never shipped: Python runs this at start-up in every process whose PYTHONPATH includes this
folder (tests/conftest.py adds it, so the app processes that tests launch inherit it). It points the update
check at the fake upstream named by LC_TEST_UPSTREAM instead of the project's address. The installed app
has no such switch."""

import os

_upstream = os.environ.get("LC_TEST_UPSTREAM")
if _upstream:
    try:
        from live_clipper import update_gate
    except ImportError:  # a process without the app (pip, a helper tool)
        pass
    else:
        update_gate.manifest_url = lambda: _upstream
