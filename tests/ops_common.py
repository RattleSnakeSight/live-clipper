import sys

import pytest
from fastapi.testclient import TestClient

from live_clipper.paths import get_paths
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)


def make_ctx():
    paths = get_paths()
    return build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))


def client(ctx, *services):
    ctx.services.extend(services)
    return TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"})
