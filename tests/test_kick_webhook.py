import types

from starlette.testclient import TestClient

from live_clipper.kick_webhook import MAX_BODY, create_webhook_app


class FakeKickChat:
    platform = "kick"

    def __init__(self, code):
        self.code, self.bodies = code, []

    async def handle_webhook(self, headers, body):
        self.bodies.append(body)
        return self.code


def test_webhook_listener_routes_to_kick_adapters_only():
    engine = types.SimpleNamespace(monitors={}, probes={})
    c = TestClient(create_webhook_app(engine))
    assert c.post("/webhooks/kick", content=b"{}").status_code == 404           # no armed Kick channel
    assert c.post("/webhooks/kick", content=b"x" * (MAX_BODY + 1)).status_code == 413
    ok, bad = FakeKickChat(200), FakeKickChat(401)
    engine.monitors = {1: types.SimpleNamespace(chat=ok), 2: types.SimpleNamespace(chat=bad),
                       3: types.SimpleNamespace(chat=None)}
    assert c.post("/webhooks/kick", content=b"{}").status_code == 200           # one adapter accepted it
    assert ok.bodies == bad.bodies == [b"{}"]
    assert c.get("/webhooks/kick").status_code == 405 and c.get("/api/status").status_code == 404
