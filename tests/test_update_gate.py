"""Required update check and gate: version source, comparison, failures, the gate in the HTTP app, and real
launches of the app against a fake upstream (tests/fake_upstream.py; no test leaves this machine)."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_clipper import __version__, update_gate
from live_clipper.paths import get_paths
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context
from live_clipper.update_gate import CheckFailed, UpdateGate, fetch_latest, is_newer, parse_version

BASE = "http://127.0.0.1:8765"
NEWER = "9.0.0"


# --- version source and comparison -----------------------------------------------------------------------


def test_numeric_and_pep440_ordering():
    assert is_newer("0.10.0", "0.9.0")
    assert is_newer("0.3.0", "0.3.0.dev1")  # a development build is older than its release
    assert is_newer("1.0", "0.99.99")
    assert not is_newer("0.3.0", "0.3.0")  # same version: documentation-only commits change nothing
    assert not is_newer("0.2.0", "0.3.0")  # this copy is newer than main (a candidate or development build)
    assert not is_newer("0.4.0rc1", "0.3.0")  # pre-releases on main are never offered
    assert not is_newer("0.4.0.dev0", "0.3.0")
    assert not is_newer("not-a-version", "0.3.0")


def test_parse_reads_only_the_version_line():
    assert parse_version('"""doc"""\n__version__ = "0.12.1"\n') == "0.12.1"
    for bad in ("", "<html>", '__version__ = "x y"', '__version__ = "1.0; import os"', "VERSION = '1.0'"):
        with pytest.raises(CheckFailed) as e:
            parse_version(bad)
        assert e.value.code == "bad_response"


def test_the_real_version_file_matches_the_pattern_the_check_reads():
    # live_clipper/__init__.py on main is the release manifest every installed copy reads
    text = open(update_gate.__file__.replace("update_gate.py", "__init__.py"), encoding="utf-8").read()
    assert parse_version(text) == __version__


def test_manifest_address_is_built_from_the_project_url():
    url = update_gate.project_manifest_url()  # the shipped function, before conftest pointed it at the fake
    owner_repo = update_gate.REPO_URL.removeprefix("https://github.com/")
    assert url == f"https://raw.githubusercontent.com/{owner_repo}/main/live_clipper/__init__.py"
    assert update_gate.REPO_URL.count("/") == 4 and "LC_TEST" not in open(update_gate.__file__).read()


def test_fetch_results(upstream):
    assert fetch_latest() == (__version__, fetch_latest()[1])
    upstream.version = NEWER
    version, etag = fetch_latest()
    assert version == NEWER and etag
    assert fetch_latest(etag) == (None, etag)  # unchanged: a 304, nothing to read
    assert upstream.not_modified == 1
    for mode, code in (("429", "rate_limited"), ("500", "unavailable"), ("huge", "bad_response"),
                       ("garbage", "bad_response"), ("drop", "offline")):
        upstream.mode = mode
        with pytest.raises(CheckFailed) as e:
            fetch_latest()
        assert e.value.code == code, mode


def test_fetch_timeout_and_no_server(upstream, monkeypatch):
    real = update_gate.TIMEOUT
    monkeypatch.setattr(update_gate, "TIMEOUT", httpx.Timeout(0.5))
    upstream.mode = "slow"
    with pytest.raises(CheckFailed) as e:
        fetch_latest()
    assert e.value.code == "timeout"
    monkeypatch.setattr(update_gate, "TIMEOUT", real)  # Windows takes about 2 s to refuse a closed port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    monkeypatch.setattr(update_gate, "manifest_url", lambda: f"http://127.0.0.1:{dead}/x")
    with pytest.raises(CheckFailed) as e:
        fetch_latest()
    assert e.value.code == "offline"


@pytest.mark.parametrize("proxy", ["socks5://someone:hunter2-secret@127.0.0.1:1080",
                                   "ftp://someone:hunter2-secret@127.0.0.1:21", "http://[hunter2-secret",
                                   "http://someone:hunter2-secret@proxy.example:notaport",
                                   "hunter2-secret:8080"])
def test_an_unusable_system_proxy_is_a_redacted_failure(monkeypatch, caplog, home, proxy):
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": proxy})
    monkeypatch.setattr(update_gate, "manifest_url", lambda: "https://example.com/v.py")  # not loopback
    if proxy.startswith("socks5"):  # the normal install has no socksio; make sure of it here
        monkeypatch.setitem(sys.modules, "socksio", None)
    with pytest.raises(CheckFailed) as e:
        fetch_latest()
    assert e.value.code == "proxy" and "hunter2" not in str(e.value) and e.value.__cause__ is None
    v = run(UpdateGate(get_paths().state, clock=Clock()).check("open"))  # a fresh start: recovery screen
    assert v["state"] == "unknown" and v["error"] == "proxy"
    assert "hunter2" not in caplog.text


def test_request_is_anonymous(upstream, monkeypatch, tmp_path):
    seen = {}
    real = httpx.Client.stream

    def spy(self, method, url, **kw):
        seen.update(headers=dict(self.headers), url=str(url), cookies=len(self.cookies))
        return real(self, method, url, **kw)

    monkeypatch.setattr(httpx.Client, "stream", spy)
    monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))
    (tmp_path / "netrc").write_text("machine 127.0.0.1 login someone password secret\n")
    fetch_latest()
    names = {k.lower() for k in seen["headers"]}
    assert names <= {"user-agent", "accept", "accept-encoding", "connection"}
    assert seen["headers"]["user-agent"] == "live-clipper-update-check" and seen["cookies"] == 0
    assert "?" not in seen["url"]


# --- the gate state machine ------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Upstream:
    """In-process fetch stand-in: answers with `version`, or raises CheckFailed(fail)."""

    def __init__(self, version=__version__):
        self.version, self.calls, self.delay = version, 0, 0.0
        self.fail: str | None = None

    def __call__(self, etag):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise CheckFailed(self.fail)
        return self.version, None


def gate_for(home, up, clock, version=__version__):
    return UpdateGate(get_paths().state, version=version, fetch=up, clock=clock)


def run(coro):
    return asyncio.run(coro)


def test_two_immediate_opens_each_check_and_see_a_new_release(home):
    up, clock = Upstream(), Clock()
    g = gate_for(home, up, clock)

    async def go():
        first = await g.check("open")
        up.version = NEWER  # released between the two opens
        clock.t += 1
        second = await g.check("open")
        return first, second

    first, second = run(go())
    assert first["state"] == "current" and second["state"] == "pending" and second["available"] == NEWER
    assert up.calls == 2


def test_overlapping_checks_share_one_request(home):
    up, clock = Upstream(), Clock()
    up.delay = 0.3
    g = gate_for(home, up, clock)

    async def go():
        return await asyncio.gather(*(g.check("open") for _ in range(5)))

    assert {v["state"] for v in run(go())} == {"current"} and up.calls == 1


def test_a_launch_checks_once_and_every_browser_open_asks_again(home):
    up, clock = Upstream(), Clock()
    g = gate_for(home, up, clock)

    async def go():
        await g.check("timer")  # the worker's own start check
        clock.t += 2
        await g.check("after_start")  # the CLI that started it
        assert up.calls == 1
        for n in (2, 3):  # the tab it opens, then the same saved sign-in address opened again at once
            if n == 3:
                up.version = NEWER
            clock.t += 0.5
            v = await g.check("open")
            assert up.calls == n
        assert v["state"] == "pending" and v["available"] == NEWER
        await g.check("retry")  # Check again pressed at once reuses the answer it just got
        assert up.calls == 3
        clock.t += 3
        await g.check("retry")
        assert up.calls == 4

    run(go())


def test_an_unexpected_error_is_unknown_not_stuck_checking(home):
    clock = Clock()

    def broken(etag):
        raise ValueError("malformed proxy setting")

    v = run(gate_for(home, broken, clock).check("open"))
    assert v["state"] == "unknown" and v["error"] == "unavailable"


def test_a_full_disk_still_closes_the_gate_and_restarts(home, monkeypatch):
    up, clock = Upstream(), Clock()
    g = gate_for(home, up, clock)
    restarts = []
    g.on_restart = lambda: restarts.append(True)

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    async def go():
        await g.check("open")
        monkeypatch.setattr(type(g.file), "write_text", full)
        up.version = NEWER
        clock.t += 1
        assert (await g.check("open"))["state"] == "pending"
        await asyncio.sleep(0.7)

    run(go())
    assert restarts == [True]


def test_unknown_blocks_then_a_later_check_starts_services(home):
    up, clock = Upstream(), Clock()
    up.fail = "offline"
    g = gate_for(home, up, clock)
    started = []

    async def on_current():
        started.append(True)

    g.on_current = on_current

    async def go():
        v = await g.check("timer")
        assert v["state"] == "unknown" and v["error"] == "offline" and v["available"] is None
        assert g.next_check - clock.t == update_gate.RETRY[0]
        assert not started
        up.fail = None
        clock.t += 3
        assert (await g.check("retry"))["state"] == "current"

    run(go())
    assert started == [True]


def test_pending_is_saved_and_survives_an_outage_and_restart(home):
    up, clock = Upstream(NEWER), Clock()
    run(gate_for(home, up, clock).check("open"))
    saved = json.loads((get_paths().state / "update.json").read_text())
    assert saved["available"] == NEWER and set(saved) == {"available", "installed", "checked_at"}

    up.fail = "timeout"
    clock.t += 3600
    g = gate_for(home, up, clock)

    async def go():
        await g.start()
        try:
            await asyncio.sleep(0.05)
            return await g.check("open")
        finally:
            await g.stop()

    v = run(go())
    assert v["state"] == "pending" and v["available"] == NEWER and v["error"] == "timeout"


def test_saved_pending_clears_once_this_copy_is_that_version(home):
    up, clock = Upstream(NEWER), Clock()
    run(gate_for(home, up, clock).check("open"))
    g = gate_for(home, up, clock, version=NEWER)  # after the upgrade
    g._load()
    assert g.state == "checking" and not (get_paths().state / "update.json").exists()


def test_a_release_while_running_restarts_into_the_gate_and_a_failure_does_not(home):
    up, clock = Upstream(), Clock()
    g = gate_for(home, up, clock)
    restarts = []
    g.on_restart = lambda: restarts.append(True)

    async def go():
        await g.check("open")
        assert g.services_started
        up.fail = "offline"
        clock.t += 10
        assert (await g.check("open"))["state"] == "current"  # verified at start; keeps running
        up.fail, up.version = None, NEWER
        clock.t += 10
        assert (await g.check("open"))["state"] == "pending"
        await asyncio.sleep(0.7)

    run(go())
    assert restarts == [True]


# --- the gate in the HTTP app ----------------------------------------------------------------------------


class Work:
    """Stands in for the app's services: records start and stop, offers one operation and a websocket."""

    name = "work"

    def __init__(self):
        self.events = []
        self.router = APIRouter(prefix="/api/work")
        self.router.post("/run")(lambda: {"ran": True})

        @self.router.websocket("/ws")
        async def ws(websocket):
            await websocket.accept()
            await websocket.close()

    async def start(self, ctx):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")

    def status(self):
        return {"running": "start" in self.events}


class Beat:
    name, router, essential = "heartbeat", None, True

    async def start(self, ctx):
        pass

    async def stop(self):
        pass

    def status(self):
        return {}


def gated_app(home, up):
    paths = get_paths()
    ctx = build_context(paths, port=8765, token=ensure_token(paths))
    work = Work()
    ctx.services[:] = [Beat(), work]
    ctx.update_gate = UpdateGate(paths.state, fetch=up)
    return TestClient(create_app(ctx), base_url=BASE), ctx, work


def wait_state(c, want, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = c.get("/api/update").json()["state"]
        if state == want:
            return
        time.sleep(0.02)
    raise AssertionError(f"state stayed {state}, wanted {want}")


def test_current_runs_everything(home):
    c, ctx, work = gated_app(home, Upstream())
    with c:
        c.headers["Authorization"] = f"Bearer {ctx.token}"
        wait_state(c, "current")
        assert work.events == ["start"]
        assert c.post("/api/work/run").json() == {"ran": True}
        assert c.get("/api/health").json()["update"] == "current"
    assert work.events == ["start", "stop"]


def test_pending_refuses_operations_but_keeps_health_status_and_retry(home):
    up = Upstream(NEWER)
    c, ctx, work = gated_app(home, up)
    with c:
        c.headers["Authorization"] = f"Bearer {ctx.token}"
        wait_state(c, "pending")
        r = c.post("/api/work/run")
        assert r.status_code == 423
        assert r.json()["update"]["available"] == NEWER and "Update pending" in r.json()["detail"]
        assert c.put("/api/app-state/x", json={"value": 1}).status_code == 423
        assert c.get("/api/app-state/ui.theme").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/api/work/ws"):
                pass
        assert c.get("/api/health").json()["update"] == "pending"
        status = c.get("/api/status").json()
        assert status["services"]["work"] == {"state": "paused", "reason": "update check"}
        assert status["update"]["state"] == "pending"
        assert c.post("/api/update/check", json={"kind": "retry"}).json()["state"] == "pending"
        assert c.get("/").status_code == 200 and c.get("/app.css").status_code == 200  # the gate screen
        # the token is still needed for everything that answers
        assert c.get("/api/update", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert work.events == []  # never started, so nothing to stop


def test_unknown_then_retry_resumes(home):
    up = Upstream()
    up.fail = "rate_limited"
    c, ctx, work = gated_app(home, up)
    with c:
        c.headers["Authorization"] = f"Bearer {ctx.token}"
        wait_state(c, "unknown")
        assert c.post("/api/work/run").status_code == 423
        assert c.get("/api/update").json()["error"] == "rate_limited"
        up.fail = None
        time.sleep(update_gate.RETRY_SPACING)
        assert c.post("/api/update/check", json={"kind": "retry"}).json()["state"] == "current"
        assert work.events == ["start"]
        assert c.post("/api/work/run").status_code == 200


def test_checking_holds_work_until_the_answer(home):
    up = Upstream()
    up.delay = 0.5
    c, ctx, work = gated_app(home, up)
    with c:
        c.headers["Authorization"] = f"Bearer {ctx.token}"
        r = c.post("/api/work/run")
        assert r.status_code == 423 and r.json()["update"]["state"] == "checking"
        assert work.events == []
        wait_state(c, "current")
        assert work.events == ["start"]


# --- real launches: CLI, supervisor, worker ----------------------------------------------------------------


def cli(*args, timeout=90):
    return subprocess.run([sys.executable, "-m", "live_clipper", *args], capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(port, method, path, **kw):
    token = (get_paths().state / "token").read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    return httpx.request(method, f"http://127.0.0.1:{port}{path}", headers=auth, timeout=20, **kw)


def wait_for(pred, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def test_launches_gate_and_resume(home, upstream):
    port = free_port()
    try:
        r = cli("start", "--no-browser", "--port", str(port))
        assert r.returncode == 0 and "Update pending" not in r.stdout, r.stdout + r.stderr
        assert api(port, "GET", "/api/health").json()["update"] == "current"
        assert api(port, "PUT", "/api/app-state/keep.me", json={"value": "yes"}).status_code == 200
        assert api(port, "GET", "/api/setup/state").status_code == 200
        hits = upstream.hits
        assert hits == 1, "a fresh start checks once: the worker's start check, reused by the CLI"

        # a release appears while it runs; the next open finds it, and the running worker restarts gated
        upstream.version = NEWER
        r = cli("start", "--no-browser")
        assert "already running" in r.stdout.lower() and f"Update pending: Live Clipper {NEWER}" in r.stdout
        assert "live-clipper update" in r.stdout
        pid = api(port, "GET", "/api/health").json()["pid"]
        assert wait_for(lambda: api(port, "GET", "/api/health").json()["pid"] != pid)  # a fresh, gated worker
        assert api(port, "GET", "/api/setup/state").status_code == 423
        status = api(port, "GET", "/api/status").json()
        assert status["update"]["state"] == "pending"
        assert status["services"]["monitor"] == {"state": "paused", "reason": "update check"}
        channel = {"url": "https://example.com/streamer_alpha"}
        assert api(port, "POST", "/api/channels", json=channel).status_code == 423
        assert api(port, "GET", "/api/app-state/keep.me").json()["value"] == "yes"  # data kept
        assert json.loads(cli("status").stdout)["update"] == "pending"

        # an outage and a restart: the confirmed update still holds
        cli("stop")
        upstream.mode = "drop"
        r = cli("start", "--no-browser", "--port", str(port))
        assert f"Update pending: Live Clipper {NEWER}" in r.stdout, r.stdout
        assert api(port, "GET", "/api/setup/state").status_code == 423

        # main goes back to this version (no newer release any more): Check again resumes normal work
        upstream.mode, upstream.version = "", __version__
        time.sleep(update_gate.RETRY_SPACING)
        assert api(port, "POST", "/api/update/check", json={"kind": "retry"}).json()["state"] == "current"
        assert wait_for(lambda: api(port, "GET", "/api/setup/state").status_code == 200)
        assert api(port, "GET", "/api/app-state/keep.me").json()["value"] == "yes"
    finally:
        cli("stop")


def test_offline_start_is_verification_only(home, upstream):
    port = free_port()
    upstream.mode = "500"
    try:
        r = cli("start", "--no-browser", "--port", str(port))
        assert r.returncode == 0 and "could not check for updates" in r.stdout.lower(), r.stdout
        assert "Update pending" not in r.stdout
        assert api(port, "GET", "/api/health").json()["update"] == "unknown"
        assert api(port, "GET", "/api/setup/state").status_code == 423
        upstream.mode = ""
        time.sleep(update_gate.RETRY_SPACING)
        assert api(port, "POST", "/api/update/check", json={"kind": "retry"}).json()["state"] == "current"
        assert api(port, "GET", "/api/setup/state").status_code == 200
    finally:
        cli("stop")


def test_a_usable_proxy_is_passed_on(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://proxy.example:3128"})
    assert update_gate._proxy("https://example.com/v.py") == "http://proxy.example:3128"
    assert update_gate._proxy("http://127.0.0.1:9/v.py") is None  # loopback never goes through it


def test_launch_ignores_a_broken_environment_proxy(home, upstream):
    """The CLI talks to its own app over loopback without environment proxies (they would see the token,
    and a dead one would make a healthy app look down)."""
    port = free_port()
    dead = "http://127.0.0.1:9"
    env = {**os.environ, "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead, "NO_PROXY": ""}
    try:
        cmd = [sys.executable, "-m", "live_clipper", "start", "--no-browser", "--port", str(port)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
        assert r.returncode == 0 and f"127.0.0.1:{port}/?t=" in r.stdout, r.stdout + r.stderr
        upstream.version = NEWER
        r = subprocess.run([sys.executable, "-m", "live_clipper", "start", "--no-browser"],
                           capture_output=True, text=True, timeout=90, env=env)
        assert f"Update pending: Live Clipper {NEWER}" in r.stdout, r.stdout + r.stderr
    finally:
        cli("stop")


def test_update_check_command(upstream):
    r = cli("update", "--check")
    assert r.returncode == 0 and f"Up to date: this is {__version__}" in r.stdout
    upstream.version = NEWER
    r = cli("update", "--check")
    assert r.returncode == 1 and f"Update pending: Live Clipper {NEWER}" in r.stdout
    upstream.mode = "429"
    r = cli("update", "--check")
    assert r.returncode == 2 and "too many requests" in r.stdout
