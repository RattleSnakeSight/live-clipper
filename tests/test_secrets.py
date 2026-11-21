import os
import stat
import sys
import types

import pytest

from live_clipper.logging_setup import redact
from live_clipper.paths import get_paths
from live_clipper.secrets import Vault


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    # Never touch the developer's real OS keyring from tests.
    monkeypatch.setitem(sys.modules, "keyring", None)


def test_roundtrip_and_persistence():
    d = get_paths().secrets
    v = Vault(d)
    assert v.get("twitch") is None
    v.set("twitch", {"client_id": "cid", "client_secret": "sec-value-1"})
    v.set("youtube", {"refresh_token": "rt-value-2"})
    assert v.get("twitch") == {"client_id": "cid", "client_secret": "sec-value-1"}
    assert sorted(Vault(d).names()) == ["twitch", "youtube"]
    v.delete("twitch")
    v.delete("never-existed")
    assert Vault(d).names() == ["youtube"]
    assert b"rt-value-2" not in (d / "vault.json.enc").read_bytes()


def test_key_file_fallback_with_restrictive_permissions():
    d = get_paths().secrets
    Vault(d).set("x", {"v": "y"})
    key = d / "vault.key"
    assert key.exists()
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE((d / "vault.json.enc").stat().st_mode) == 0o600


def test_values_registered_with_redactor_on_set_and_load():
    d = get_paths().secrets
    Vault(d).set("svc", {"nested": {"token": "set-secret-abc"}})
    assert redact("x set-secret-abc y") == "x *** y"
    # Simulate a fresh process: the value must be registered when loaded.
    import live_clipper.logging_setup as ls
    ls._secrets.clear()
    assert Vault(d).get("svc") is not None
    assert redact("x set-secret-abc y") == "x *** y"


def test_uses_keyring_when_available(monkeypatch):
    store = {}
    fake = types.SimpleNamespace(
        get_password=lambda s, u: store.get((s, u)),
        set_password=lambda s, u, p: store.__setitem__((s, u), p),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    d = get_paths().secrets
    Vault(d).set("a", {"b": "c"})
    assert not (d / "vault.key").exists()
    assert len(store) == 1
    assert Vault(d).get("a") == {"b": "c"}


def test_broken_keyring_falls_back_to_file(monkeypatch):
    def boom(*a):
        raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=boom, set_password=boom))
    d = get_paths().secrets
    Vault(d).set("a", {"b": "c"})
    assert (d / "vault.key").exists()
