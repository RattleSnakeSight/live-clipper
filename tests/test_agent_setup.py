"""Agent-assisted key setup: pairing, request lifecycle, the helper's source lookup and secret hygiene.

Fake credentials only; platforms are httpx.MockTransport fakes, nothing leaves the machine.
"""

import asyncio
import json
import sys
import threading
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from live_clipper import agent_helper, agent_setup, source_status
from live_clipper.agent_setup import AgentSetupService
from live_clipper.capture import Capture
from live_clipper.logging_setup import setup_logging
from live_clipper.monitor_service import MonitorService
from live_clipper.paths import get_paths
from live_clipper.publish.sandbox_server import shutdown_sandbox
from live_clipper.publish.service import PublishService
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context

BASE = "http://127.0.0.1:8765"
# synthetic markers; none of them may show up in a response, a log line or the request list
KEY, SECRET, TW_SECRET = "FAKEKEY-agent-111", "FAKESECRET-agent-222", "good-FAKE-twitch-333"  # gitleaks:allow


@pytest.fixture
def app(monkeypatch):
    def twitch(req: httpx.Request):
        if req.url.host == "id.twitch.tv":
            ok = f"client_secret={TW_SECRET}" in req.content.decode()
            return (
                httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
                if ok
                else httpx.Response(400, json={"message": "invalid client"})
            )
        return httpx.Response(200, json={"data": []})

    async def start(self):
        self._task = asyncio.get_running_loop().create_future()

    async def stop(self):
        self._task = None

    async def probe(url, timeout=40):
        return {"state": "offline", "detail": "fake probe"}

    monkeypatch.setattr(Capture, "start", start)
    monkeypatch.setattr(Capture, "stop", stop)
    monkeypatch.setattr(source_status, "streamlink_status", probe)
    monkeypatch.setattr(agent_setup, "CLAIM_WAIT", 0.3)
    paths = get_paths()
    ctx = build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))
    setup_logging(paths.logs, console=False)
    agent = AgentSetupService()
    ctx.services[:] = [MonitorService(transport=httpx.MockTransport(twitch)), PublishService(), agent]
    with TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        c.ctx, c.agent = ctx, agent
        yield c
    shutdown_sandbox()


def paired(c, agent="claude"):
    p = agent_setup.pair(c.ctx.paths.state, agent)
    return {agent_helper.HEADER: p["secret"]}


def ask(c, target, kind="env", ref=""):
    r = c.post("/api/agent/requests", json={"target": target, "source": {"kind": kind, "ref": ref}})
    assert r.status_code == 201, r.text
    return r.json()


def claim(c, helper):
    r = c.post("/api/agent/helper/claim", json={}, headers=helper)
    assert r.status_code == 200, r.text
    return r.json()["request"]


def answer(c, helper, req, environ):
    """The helper's own lookup and reply, through the app's HTTP API."""
    values, missing = agent_helper.resolve(req, environ)
    return c.post(
        f"/api/agent/helper/requests/{req['id']}/result",
        json={"values": values, "missing": missing},
        headers=helper,
    )


def no_secrets_anywhere(c, *texts):
    blob = "\n".join(texts) + c.get("/api/agent/requests").text + c.get("/api/agent").text
    for log in c.ctx.paths.logs.glob("*.log"):
        blob += log.read_text(encoding="utf-8", errors="replace")
    for marker in (KEY, SECRET, TW_SECRET):
        assert marker not in blob


def test_without_pairing_the_customer_is_sent_to_manual_setup(app):
    assert app.get("/api/agent").json()["paired"] is None
    r = app.post("/api/agent/requests", json={"target": "destination:tiktok", "source": {"kind": "env"}})
    assert r.status_code == 409 and "by hand" in r.json()["detail"]


def test_env_lookup_saves_destination_keys_and_shows_names_only(app):
    helper = paired(app)
    assert app.get("/api/agent").json()["paired"]["agent_label"] == "Claude Code"
    ask(app, "destination:tiktok")
    req = claim(app, helper)
    assert req["target"] == "destination:tiktok" and req["status"] == "claimed"
    assert [f["env"] for f in req["fields"]] == ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"]
    r = answer(app, helper, req, {"TIKTOK_CLIENT_KEY": KEY, "TIKTOK_CLIENT_SECRET": SECRET, "PATH": "x"})
    assert r.json()["status"] == "done" and r.json()["saved"] == ["client_key", "client_secret"]
    pub = next(s for s in app.ctx.services if s.name == "publish")
    assert pub.secrets.get("app", "tiktok") == {"client_key": KEY, "client_secret": SECRET}
    tiktok = next(d for d in app.get("/api/destinations").json()["destinations"] if d["provider"] == "tiktok")
    assert tiktok["app_credentials_set"] == [
        "client_key",
        "client_secret",
    ]  # the existing integration sees them
    no_secrets_anywhere(app, r.text)


def test_source_keys_go_through_the_platform_check(app):
    helper = paired(app, "codex")
    ask(app, "source:twitch")
    bad = answer(app, helper, claim(app, helper), {"TWITCH_CLIENT_ID": KEY, "TWITCH_CLIENT_SECRET": SECRET})
    assert bad.json()["status"] == "invalid" and "did not accept" in bad.json()["detail"]
    sources = lambda: {x["platform"]: x for x in app.get("/api/sources").json()}  # noqa: E731
    assert not sources()["twitch"]["connected"]
    ask(app, "source:twitch")
    ok = answer(app, helper, claim(app, helper), {"TWITCH_CLIENT_ID": KEY, "TWITCH_CLIENT_SECRET": TW_SECRET})
    assert ok.json()["status"] == "done"
    assert sources()["twitch"]["connected"]
    no_secrets_anywhere(app, bad.text, ok.text)


def test_partial_keys_save_nothing_and_name_what_is_missing(app):
    helper = paired(app)
    ask(app, "destination:tiktok")
    r = answer(app, helper, claim(app, helper), {"TIKTOK_CLIENT_KEY": KEY})
    assert r.json()["status"] == "missing" and r.json()["missing"] == ["client_secret"]
    pub = next(s for s in app.ctx.services if s.name == "publish")
    assert pub.secrets.get("app", "tiktok") is None


def test_file_and_credential_manager_sources(app, tmp_path, monkeypatch):
    helper = paired(app)
    env = tmp_path / "keys.env"
    env.write_text(f"# mine\nexport TIKTOK_CLIENT_KEY='{KEY}'\nTIKTOK_CLIENT_SECRET={SECRET}\nOTHER=1\n")
    ask(app, "destination:tiktok", "file", str(env))
    assert answer(app, helper, claim(app, helper), {}).json()["status"] == "done"
    js = tmp_path / "keys.json"
    js.write_text(json.dumps({"INSTAGRAM_CLIENT_ID": KEY, "INSTAGRAM_CLIENT_SECRET": SECRET}))
    ask(app, "destination:instagram", "file", str(js))
    assert answer(app, helper, claim(app, helper), {}).json()["status"] == "done"

    # a missing file and no keyring support: the helper reports why, nothing is saved
    app.headers.update(helper)  # from here the client plays the helper, as `agent serve` does
    ask(app, "destination:youtube", "file", str(tmp_path / "nope.env"))
    req = claim(app, helper)
    out = agent_helper.handle(app, req, lambda s: None)
    assert out["status"] == "failed" and out["detail"] == agent_setup.SOURCE_ERRORS["not_found"]
    ask(app, "destination:youtube", "keyring", "live-clipper-test")
    out = agent_helper.handle(app, claim(app, helper), lambda s: None)
    assert out["status"] == "failed" and out["detail"] == agent_setup.SOURCE_ERRORS["no_keyring"]
    store = {("live-clipper-test", "YOUTUBE_CLIENT_ID"): KEY}
    monkeypatch.setitem(
        sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, n: store.get((s, n)))
    )
    ask(app, "destination:youtube", "keyring", "live-clipper-test")
    said = []
    out = agent_helper.handle(app, claim(app, helper), said.append)
    assert out["status"] == "done" and out["saved"] == ["client_id"]  # the YouTube secret is optional
    assert KEY not in said[0] and "client_id" in said[0]
    no_secrets_anywhere(app, json.dumps(out))


def test_only_the_paired_helper_can_claim_or_answer(app):
    assert app.post("/api/agent/helper/claim", json={}).status_code == 409  # nothing paired yet
    helper = paired(app)
    ask(app, "destination:tiktok")
    assert app.post("/api/agent/helper/claim", json={}).status_code == 403  # the browser has only the token
    assert (
        app.post("/api/agent/helper/claim", json={}, headers={agent_helper.HEADER: "wrong"}).status_code
        == 403
    )
    no_token = TestClient(app.app, base_url=BASE)
    assert no_token.post("/api/agent/helper/claim", json={}, headers=helper).status_code == 401
    req = claim(app, helper)
    r = app.post(
        f"/api/agent/helper/requests/{req['id']}/result",
        json={"values": {}},
        headers={agent_helper.HEADER: "x"},
    )
    assert r.status_code == 403
    # re-pairing replaces the secret: the old helper is locked out
    paired(app, "codex")
    assert app.post("/api/agent/helper/claim", json={}, headers=helper).status_code == 403


def test_cancel_expiry_revoke_and_one_claim_per_request(app):
    helper = paired(app)
    req = ask(app, "destination:tiktok")
    assert (
        app.post(
            "/api/agent/requests", json={"target": "destination:tiktok", "source": {"kind": "env"}}
        ).status_code
        == 409
    )
    got = []
    threads = [threading.Thread(target=lambda: got.append(claim(app, helper))) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(bool(x) for x in got) == [False, True]  # exactly one consumer gets it
    assert app.post(f"/api/agent/requests/{req['id']}/cancel").json()["status"] == "cancelled"
    late = answer(app, helper, got[0] or got[1], {"TIKTOK_CLIENT_KEY": KEY, "TIKTOK_CLIENT_SECRET": SECRET})
    assert late.status_code == 409  # no write after the customer cancelled
    pub = next(s for s in app.ctx.services if s.name == "publish")
    assert pub.secrets.get("app", "tiktok") is None

    req = ask(app, "destination:tiktok")
    app.ctx.db.execute("UPDATE agent_requests SET expires_at=? WHERE id=?", (time.time() - 1, req["id"]))
    assert claim(app, helper) is None
    assert app.get("/api/agent/requests").json()["requests"][0]["status"] == "expired"

    ask(app, "destination:instagram")
    assert app.delete("/api/agent/pairing").status_code == 204
    assert app.get("/api/agent/requests").json()["requests"][0]["status"] == "cancelled"
    assert app.post("/api/agent/helper/claim", json={}, headers=helper).status_code == 409


def test_values_for_another_platform_are_ignored(app):
    helper = paired(app)
    ask(app, "destination:tiktok")
    req = claim(app, helper)
    r = app.post(
        f"/api/agent/helper/requests/{req['id']}/result",
        headers=helper,
        json={"values": {"client_key": KEY, "client_secret": SECRET, "api_key": "x", "access_token": "y"}},
    )
    assert r.json()["saved"] == ["client_key", "client_secret"]
    pub = next(s for s in app.ctx.services if s.name == "publish")
    assert set(pub.secrets.get("app", "tiktok")) == {"client_key", "client_secret"}


def test_helper_refuses_a_request_for_other_keys_before_reading_anything(tmp_path, monkeypatch):
    """The helper looks up only its own list for the named platform; an altered request reads nothing."""
    opened = []
    real_read = agent_helper.read_file
    monkeypatch.setattr(agent_helper, "read_file", lambda p, n: opened.append(p) or real_read(p, n))
    good = {
        **agent_setup.spec_view("destination:tiktok"),
        "source": {"kind": "file", "ref": str(tmp_path / "k")},
    }
    for bad in (
        {**good, "target": "destination:other"},
        {**good, "fields": [{"name": "client_key", "env": "HOME", "required": True}]},
        {
            **good,
            "fields": good["fields"] + [{"name": "x", "env": "AWS_SECRET_ACCESS_KEY", "required": False}],
        },
    ):
        with pytest.raises(ValueError):
            agent_helper.resolve(bad, {"HOME": "x", "AWS_SECRET_ACCESS_KEY": "y"})
    assert opened == []
    env = {"TIKTOK_CLIENT_KEY": KEY, "TIKTOK_CLIENT_SECRET": SECRET, "HOME": "x"}
    assert agent_helper.resolve({**good, "source": {"kind": "env", "ref": ""}}, env)[0] == {
        "client_key": KEY,
        "client_secret": SECRET,
    }


def test_lifecycle_expiry_repair_restart_and_save_errors(app, monkeypatch):
    helper = paired(app)
    pub = next(s for s in app.ctx.services if s.name == "publish")
    keys = {"TIKTOK_CLIENT_KEY": KEY, "TIKTOK_CLIENT_SECRET": SECRET}

    # an answer after the deadline is refused although nobody listed the requests in between
    ask(app, "destination:tiktok")
    req = claim(app, helper)
    app.ctx.db.execute("UPDATE agent_requests SET expires_at=? WHERE id=?", (time.time() - 1, req["id"]))
    assert answer(app, helper, req, keys).status_code == 409
    assert pub.secrets.get("app", "tiktok") is None

    # a request made under one pairing is not handed to, or answered by, the next pairing
    ask(app, "destination:tiktok")
    other = paired(app, "codex")
    assert claim(app, other) is None
    assert app.get("/api/agent/requests").json()["requests"][0]["status"] == "cancelled"
    ask(app, "destination:tiktok")
    req = claim(app, other)
    helper = paired(app, "claude")  # re-paired while the request was claimed
    assert (
        app.post(
            f"/api/agent/helper/requests/{req['id']}/result", json={"values": {}}, headers=other
        ).status_code
        == 403
    )
    assert answer(app, helper, req, keys).status_code == 409

    # an unexpected error while saving ends the request with a fixed text, and the customer can ask again
    def boom(*a, **k):
        raise RuntimeError(f"disk full {SECRET}")

    real_update = pub.secrets.update
    pub.secrets.update = boom
    ask(app, "destination:tiktok")
    out = answer(app, helper, claim(app, helper), keys).json()
    assert out["status"] == "failed" and out["detail"] == agent_setup.NOT_SAVED
    pub.secrets.update = real_update
    ask(app, "destination:tiktok")  # not blocked by the failed one

    # a request being handled when the app stops is closed on the next start
    claim(app, helper)
    asyncio.run(app.agent.start(app.ctx))
    last = app.get("/api/agent/requests").json()["requests"][0]
    assert last["status"] == "failed" and "restarted" in last["detail"]
    no_secrets_anywhere(app, json.dumps(out))
