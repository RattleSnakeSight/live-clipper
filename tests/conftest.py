import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data dir so nothing touches the real one."""
    root = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(root))
    return root
