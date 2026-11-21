"""Fixtures shared by the publishing tests: app context, publisher, local test provider, fake clips."""
import itertools
import os
import sys
import time

import httpx
import pytest

from live_clipper.paths import get_paths
from live_clipper.publish.sandbox_server import SandboxServer
from live_clipper.publish.store import Store
from live_clipper.publish.vault import Secrets
from live_clipper.publish.worker import Publisher
from live_clipper.services import build_context

FAST = {"poll_interval": 0.0, "backoff_base": 0.01, "timeout": 2.0}
_n = itertools.count(1)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)


def make_ctx(port=8765):
    return build_context(get_paths(), port=port, token="t" * 40)


def make_clip(ctx, channel_id="streamer_alpha", language="en", size=2000, write=True):
    cid = next(_n)
    path = get_paths().media / f"clip{cid}_{language}.mp4"
    if write:
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(size))
    return {"clip_id": cid, "channel_id": channel_id, "path": str(path), "language": language,
            "title": f"Heist ({language})", "description": "A synthetic clip", "hashtags": ["gaming", "#clips"],
            "duration_s": 20.0, "width": 1080, "height": 1920, "format_id": "default", "preset_id": "default"}


def admin(sb, path, body=None):
    r = httpx.post(sb.url + path, json=body or {}, timeout=5)
    r.raise_for_status()
    return r.json()


def videos(sb, tag=None):
    st = httpx.get(sb.url + "/_admin/state", timeout=5).json()
    return [v for v in st["videos"].values() if tag is None or v["tag"] == tag]


@pytest.fixture
def sb(tmp_path):
    s = SandboxServer(tmp_path / "sbx" / "state.json", processing_delay=0.05).start()
    yield s
    s.stop()


class World:
    """ctx + store + secrets + a Publisher, with helpers to add a connected test-provider destination."""

    def __init__(self, sb, clock=None, **pub_kw):
        self.ctx, self.sb = make_ctx(), sb
        self.store, self.secrets = Store(self.ctx.db), Secrets(self.ctx.secrets)
        self.events, self.notes = [], []
        self._alias = True
        self.pub = Publisher(self.store, self.ctx.settings, self.secrets, clock=clock or time.time,
                             notify=lambda t, b: self.notes.append((t, b)), emit=self.events.append, **pub_kw)
        self.pub.factories["youtube"] = self.pub.factories["test"]      # a 'youtube' row talks to the local provider

    def dest(self, extra=None, refresh=True, provider="test", ack=True, enabled=True):
        did = self.pub.add_destination(provider, "Test", {"base_url": self.sb.url, **FAST, **(extra or {})})
        tok = admin(self.sb, "/_admin/issue_token")
        if not refresh:
            tok.pop("refresh_token")
        self.pub.save_tokens(did, tok)
        assert self.run(self.pub.connect(did))["ok"]
        self.store.update("destinations", did, enabled=int(enabled), rights_ack_at=time.time() if ack else None)
        return did

    def row(self, did):
        return self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)

    def run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def drain(self, **kw):
        return self.run(self.pub.drain(**kw))


@pytest.fixture
def world(sb):
    return World(sb)
