"""Browser tests against the real app, run as `python -m live_clipper --home <tmp> start --foreground`.

Nothing here may reach the internet. The browser aborts and records every non-loopback request (the test
fails if there was one), and the app arms no channel: a channel that is not armed is never polled, probed,
recorded or chatted with (monitor `Engine.arm` is the only thing that starts that work), and no account is
connected.

Updates: the app has no version override to simulate a newer build, so an "update" here is a stop and a fresh
start on the same data folder. That is what carries everything across a real update too: the data folder
lives outside the install (docs/ARCHITECTURE.md, "Data dir"), and the wizard, tour and settings state is in
its database.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import expect
from test_help_coverage import assert_editors_show_schema_descriptions

pytestmark = pytest.mark.ui

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
SCREENS = ["dashboard", "channels", "channels/new", "sources", "clips", "formats", "formats/gta-vi",
           "presets", "presets/default", "destinations", "settings", "status", "guide", "guide/first-run"]
MISSING_HELP = """() => [...document.querySelectorAll(
    'button, input:not([type=hidden]), select, textarea, [role=switch], a.btn, summary')]
  .filter(el => {
    const r = el.getBoundingClientRect();
    return (r.width > 0 || r.height > 0) && getComputedStyle(el).visibility !== 'hidden'
      && !el.closest('[hidden]');
  })
  .filter(el => !(el.dataset.help || '').trim())
  .map(el => el.outerHTML.slice(0, 200))"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RealApp:
    """One data folder; start() and stop() like a user running `live-clipper start` / `live-clipper stop`."""

    def __init__(self, home: Path):
        self.home = home
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""

    def _cli(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "live_clipper", "--home", str(self.home), *args]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.port = _free_port()
        log = open(self.home.parent / f"{self.home.name}-{self.port}.out", "wb")  # closed with the process
        cmd = self._cli("start", "--foreground", "--no-browser", "--port", str(self.port))
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the app exited with {self.proc.returncode}")
            try:
                if httpx.get(f"{self.url}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("the app did not answer /api/health")
        self.token = (self.home / "state" / "token").read_text().strip()
        # services start in the server's lifespan; wait until the ones the UI needs answer
        while time.time() < deadline:
            s = self.api("/status")
            if {"setup", "monitor", "select", "edit", "publish"} <= set(s.get("services", {})):
                return
            time.sleep(0.3)
        raise RuntimeError("services did not start")

    def stop(self) -> None:
        if not self.proc:
            return
        subprocess.run(self._cli("stop"), timeout=60, capture_output=True)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def api(self, path: str):
        r = httpx.get(f"{self.url}/api{path}", headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
        r.raise_for_status()
        return r.json()

    def app_state(self, key: str):
        return self.api(f"/app-state/{key}")["value"]

    def wait_state(self, page, key: str, value) -> None:
        """Poll while letting the page run: sync Playwright handles routed requests (the network guard)
        only while the test is inside a Playwright call."""
        for _ in range(100):
            if self.app_state(key) == value:
                return
            page.wait_for_timeout(50)
        raise AssertionError(f"{key} is {self.app_state(key)!r}, expected {value!r}")


@pytest.fixture
def make_app(tmp_path):
    apps: list[RealApp] = []

    def make(name: str) -> RealApp:
        home = tmp_path / name
        home.mkdir()
        app = RealApp(home)
        apps.append(app)
        app.start()
        return app

    yield make
    for a in apps:
        a.stop()


@pytest.fixture
def offline_page(make_page):
    """A page whose browser context aborts every request that leaves this computer, and records it."""
    pages = []

    def make():
        page = make_page()
        page.external = []

        def guard(route):
            url = route.request.url
            host = urlparse(url).hostname or ""
            if url.startswith(("data:", "blob:")) or host in LOOPBACK:
                route.continue_()
            else:
                page.external.append(url)
                route.abort()

        page.context.route("**/*", guard)
        pages.append(page)
        return page

    yield make
    for p in pages:
        assert not p.external, f"the UI tried to reach the network: {p.external}"


def tour(page):
    return page.locator(".tour-card")


def open_app(page, app: RealApp) -> None:
    page.goto(f"{app.url}/?t={app.token}")  # sets the sign-in cookie and redirects to /


def next_q(page) -> None:
    before = page.locator(".wizard").inner_text()
    page.get_by_role("button", name="Next").or_(page.get_by_role("button", name="Review answers")).click()
    page.wait_for_function("t => document.querySelector('.wizard').innerText !== t", arg=before)


def run_wizard(page, answers: dict | None = None) -> list[str]:
    """Answer every question (defaults unless `answers` has a callback for its title), then finish."""
    answers = answers or {}
    expect(page.get_by_role("heading", name="Set up Live Clipper")).to_be_visible()
    seen = []
    for _ in range(40):
        h2 = page.locator(".question h2")
        if not h2.count():
            break
        title = h2.inner_text()
        seen.append(title)
        if title in answers:
            answers[title](page)
        next_q(page)
    expect(page.get_by_role("heading", name="Check your answers")).to_be_visible()
    page.get_by_role("button", name="Finish setup").click()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    return seen


def check_help(page, where: str) -> None:
    page.wait_for_timeout(250)
    missing = page.evaluate(MISSING_HELP)
    assert not missing, f"controls without data-help on {where}: {missing}"


def post_destinations(page):
    page.get_by_label("Nowhere, I only want the files").uncheck()
    page.get_by_label("YouTube Shorts").check()


def manual_posting(page):
    expect(page.get_by_label("Yes, post as soon as a clip is ready")).to_be_checked()  # the default
    page.get_by_label("No, I approve each clip").check()


def test_first_run_tour_restart_and_replay_on_the_real_app(make_app, offline_page):
    app = make_app("profile-a")
    page = offline_page()
    open_app(page, app)
    expect(page).to_have_url(f"{app.url}/#/setup")

    seen = run_wizard(page, {"Where do you want to post clips?": post_destinations,
                             "Should clips be posted automatically?": manual_posting})
    assert "Should clips be posted automatically?" in seen
    assert app.app_state("setup.completed") is True
    # the answers reached the settings the services read
    assert app.api("/publish/mode")["mode"] == "manual"
    assert app.api("/status")["services"]["edit"]["asr"]["device"] in ("cpu", "cuda")

    # the tour starts on its own after the wizard; run it to the end
    expect(tour(page)).to_contain_text("Welcome to Live Clipper")
    total = int(tour(page).locator(".count").inner_text().split(" of ")[1])
    for i in range(total - 1):
        expect(tour(page).locator(".count")).to_have_text(f"Step {i + 1} of {total}")
        page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Finish").click()
    expect(tour(page)).to_have_count(0)
    app.wait_state(page, "tour.state", "completed")
    app.wait_state(page, "tour.version", 1)

    page.reload()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)

    # stop and start again on the same data folder (this is also what an update does)
    app.stop()
    app.start()
    open_app(page, app)
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)
    expect(page.get_by_role("heading", name="Set up Live Clipper")).to_have_count(0)
    assert app.app_state("setup.completed") is True and app.app_state("tour.state") == "completed"
    page.goto(f"{app.url}/#/destinations")
    expect(page.get_by_label("Manual")).to_be_checked()  # the wizard's posting answer survived the restart

    # Settings > Replay tutorial, then Skip tour: remembered across a restart
    page.goto(f"{app.url}/#/settings")
    page.get_by_role("button", name="Replay tutorial").click()
    expect(tour(page)).to_contain_text("Welcome to Live Clipper")
    page.get_by_role("button", name="Skip tour").click()
    expect(tour(page)).to_have_count(0)
    app.wait_state(page, "tour.state", "skipped")
    app.stop()
    app.start()
    open_app(page, app)
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)
    assert app.app_state("tour.state") == "skipped"
    assert not page.js_errors


def test_second_profile_skips_the_tour_on_first_run(make_app, offline_page):
    app = make_app("profile-b")
    page = offline_page()
    open_app(page, app)
    seen = run_wizard(page)
    assert "Should clips be posted automatically?" not in seen  # no destination picked
    assert app.api("/publish/mode")["mode"] == "auto"  # the default: automatic once a destination is on
    expect(tour(page)).to_contain_text("Welcome to Live Clipper")
    page.get_by_role("button", name="Skip tour").click()
    expect(tour(page)).to_have_count(0)
    app.wait_state(page, "tour.state", "skipped")
    page.reload()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)
    assert not page.js_errors


def test_real_screens_channels_rules_destinations_and_help(make_app, offline_page, tmp_path):
    app = make_app("profile-c")
    page = offline_page()
    open_app(page, app)
    run_wizard(page)
    page.get_by_role("button", name="Skip tour").click()
    expect(tour(page)).to_have_count(0)
    app.wait_state(page, "tour.version", 1)  # the tour then returns to the dashboard
    expect(page).to_have_url(f"{app.url}/#/dashboard")

    # a Twitch channel without credentials, not watched: the stream-probe label, no polling, no network
    page.goto(f"{app.url}/#/channels/new")
    page.get_by_label("Streamer link").fill("https://www.twitch.tv/streamer_alpha")
    expect(page.get_by_text("Found: Twitch channel streamer_alpha")).to_be_visible()
    page.get_by_label("Watch this channel").uncheck()
    page.get_by_role("button", name="Add channel").click()
    expect(page).to_have_url(f"{app.url}/#/channels")
    row = page.locator("#channel-list li").filter(has_text="streamer_alpha")
    expect(row).to_contain_text("Twitch")
    expect(row).to_contain_text("Disabled")
    page.goto(f"{app.url}/#/dashboard")
    card = page.locator("#channel-cards article").filter(has_text="streamer_alpha")
    expect(card).to_contain_text("Status from the stream probe (no official API connected; slower)")
    expect(card.get_by_role("button", name="Start watching")).to_be_visible()
    channels = app.api("/channels")
    assert [(c["platform"], c["login"], c["enabled"], c["state"]) for c in channels] == [
        ("twitch", "streamer_alpha", False, "disabled")]
    assert channels[0]["status_source"].startswith("stream probe")
    # the same link again is refused at the field
    page.goto(f"{app.url}/#/channels/new")
    page.get_by_label("Streamer link").fill("https://www.twitch.tv/streamer_alpha")
    page.get_by_label("Watch this channel").uncheck()
    page.get_by_role("button", name="Add channel").click()
    expect(page.locator("#channel-form .field .error").first).to_contain_text("already added")
    # edit keeps the link and saves the watch mode
    cid = channels[0]["id"]
    page.goto(f"{app.url}/#/channels/{cid}")
    page.get_by_label("Only when the title matches").check()
    page.get_by_role("button", name="Save changes").click()
    expect(page).to_have_url(f"{app.url}/#/channels")
    assert app.api(f"/channels/{cid}")["watch_mode"] == "topic_gated"

    # sources: the stream probe is named; each way to connect shows its own action and nothing else
    page.goto(f"{app.url}/#/sources")
    twitch = page.locator("#source-twitch")
    expect(twitch).to_contain_text("Stream probe (no official API connected)")
    expect(twitch.locator("input[type=password]")).to_have_count(0)  # "no account" asks for nothing
    twitch.get_by_label("Sign in with a code").check()
    expect(twitch.get_by_role("button", name="Get a Twitch code")).to_be_visible()
    expect(twitch.get_by_role("button", name="Save Twitch keys")).to_have_count(0)
    kick = page.locator("#source-kick")
    kick.locator("summary").filter(has_text="Connect Kick").click()
    kick.get_by_label("Sign in with Kick").check()
    expect(kick).to_contain_text(f"http://localhost:{app.port}/api/oauth/source/kick/callback")
    expect(kick).to_contain_text("http://127.0.0.1:8788/webhooks/kick")  # where a tunnel forwards Kick chat
    youtube = page.locator("#source-youtube")
    youtube.locator("summary").filter(has_text="Connect YouTube").click()
    youtube.get_by_label("API key (public data)").check()
    expect(youtube.get_by_role("button", name="Save YouTube key")).to_be_visible()
    expect(youtube).to_contain_text("cannot upload")

    # destinations: the steps with the exact local redirect addresses; Metricool is named and asks for nothing
    page.goto(f"{app.url}/#/destinations")
    dests = (("youtube", "YouTube Shorts"), ("tiktok", "TikTok"), ("instagram", "Instagram Reels"))
    page.locator("#dest-instagram").get_by_label("Sign in with your Meta app").check()
    for provider, label in dests:
        sec = page.locator(f"#dest-{provider}")
        expect(sec.get_by_role("heading", name=label, exact=True)).to_be_visible()
        expect(sec).to_contain_text(f"http://127.0.0.1:{app.port}/api/oauth/{provider}/callback")
        expect(sec).to_contain_text("What fails without each step")
        expect(sec.locator("details.step[open]")).to_have_count(1)  # only the next step is open
    for provider in ("tiktok", "instagram"):
        route = page.locator(f"#routes-{provider} li[data-route=metricool]")
        expect(route).to_contain_text("Needs a separately installed sender")
        expect(route.locator("input, button, select, textarea")).to_have_count(0)
    expect(page.locator("#dest-youtube")).to_contain_text("Metricool is not needed")
    expect(page.get_by_label("Automatic")).to_be_checked()
    # destination settings go through the real publish API, with its per-field errors shown inline
    ig = page.locator("#dest-instagram form[data-settings=instagram]")
    settings = page.locator("form[data-settings=instagram]")
    page.locator("#dest-instagram details").filter(has=settings).locator("> summary").click()
    ig.get_by_label("Public video folder address (https)").fill("http://example.com/clips")
    ig.get_by_role("button", name="Save settings").click()
    expect(ig.locator(".field").filter(has_text="Public video folder address").locator(".error")).to_contain_text("https://")
    ig.get_by_label("Public video folder address (https)").fill("https://example.com/clips")
    ig.get_by_label("Login type").select_option("facebook")
    ig.get_by_role("button", name="Save settings").click()
    expect(page.locator("#toasts")).to_contain_text("Settings saved")
    ig_dest = next(d for d in app.api("/destinations")["destinations"] if d["provider"] == "instagram")
    assert ig_dest["settings"] == {"login": "facebook", "video_base_url": "https://example.com/clips"}
    # formats, rules and presets show the schema's description for every field
    assert_editors_show_schema_descriptions(page, app.url)

    # rules and the effective-settings preview against the real selection API
    page.goto(f"{app.url}/#/formats")
    panel = page.locator("#rule-list-panel")
    panel.get_by_role("button", name="Add rule").click()
    check_help(page, "rule editor")
    page.get_by_label("Rule name").fill("Twitch longer ending")
    page.locator("#rule-editor").get_by_label("Twitch").check()
    page.get_by_label("Seconds after the moment").fill("20")
    page.locator("#rule-editor").get_by_role("button", name="Add rule").click()
    expect(panel.locator(".rule-list > li")).to_have_count(1)
    assert app.api("/rules")["rules"][0]["actions"]["window_post"] == 20
    tool = page.locator("#preview-tool")
    tool.get_by_label("Platform").select_option("twitch")
    tool.get_by_role("button", name="Show effective settings").click()
    post = tool.locator("tr").filter(has_text="window.post")
    expect(post).to_contain_text("Rule")
    expect(post).to_contain_text("Twitch longer ending")
    expect(tool.locator("tr").filter(has_text="window.pre")).to_contain_text("Built-in")

    # format export and import through the real routes
    row = page.locator("#format-list li").filter(has_text="GTA VI")
    with page.expect_download() as dl:
        row.get_by_role("button", name="Export").click()
    doc = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))
    doc.update(id="my-format", name="My format")
    f = tmp_path / "my-format.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    with page.expect_file_chooser() as fc:
        page.get_by_role("button", name="Import file").click()
    fc.value.set_files(str(f))
    expect(page.locator("#format-list")).to_contain_text('Imported "My format"')
    assert "my-format" in [x["id"] for x in app.api("/formats")["formats"]]

    # the API reference is drawn locally from the app's own route list; this page's guard fails the test on
    # any request that leaves the computer
    page.goto(f"{app.url}/api/docs")
    expect(page.get_by_role("heading", name="API reference")).to_be_visible()
    routes = page.locator("#routes summary")
    expect(routes.filter(has_text="/api/publish/mode").first).to_be_visible()
    page.get_by_label("Filter routes").fill("publish/mode")
    expect(routes.filter(has_text="/api/channels").first).to_be_hidden()
    mode = routes.filter(has_text="/api/publish/mode").filter(has_text="PUT")
    mode.click()
    expect(page.locator("#routes details[open]")).to_contain_text("Request body")
    expect(page.locator("#state")).to_contain_text("routes shown")

    # a guide page
    page.goto(f"{app.url}/#/guide/first-run")
    expect(page.locator("#guide-main article").get_by_role("heading", name="First run")).to_be_visible()

    # every visible control on every real screen has its one-line help, including the setup questions
    for r in [*SCREENS, f"channels/{cid}"]:
        page.goto(f"{app.url}/#/{r}")
        expect(page.locator("main h1")).to_be_visible()
        check_help(page, r)
    page.goto(f"{app.url}/#/setup")
    for i in range(40):
        check_help(page, f"setup page {i + 1}")
        nxt = page.get_by_role("button", name="Next").or_(page.get_by_role("button", name="Review answers"))
        if not nxt.count():
            break
        next_q(page)

    # nothing was armed, polled or probed by the app itself
    mon = app.api("/status")["services"]["monitor"]
    assert mon["armed"] == 0 and all(s["calls"] == 0 for s in mon["sources"].values())
    log = (app.home / "logs" / "live-clipper.log").read_text(encoding="utf-8", errors="replace")
    assert not re.search(r"streamlink|twitch\.tv/streamer_alpha.*(probe|capture)", log, re.I)
    assert not page.js_errors


def test_dark_default_light_choice_and_agent_key_setup_on_the_real_app(make_app, offline_page, tmp_path):
    """The real app, the real `agent pair` / `agent serve` commands and a synthetic key file: the customer
    asks in the browser, the helper reads the file and the existing destination integration has the keys."""
    key, secret = "FAKEKEY-e2e-4711", "FAKESECRET-e2e-0815"  # gitleaks:allow (synthetic)
    app = make_app("profile-agent")
    page = offline_page()
    open_app(page, app)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"  # no preference: dark
    run_wizard(page)
    page.get_by_role("button", name="Skip tour").click()
    app.wait_state(page, "tour.version", 1)  # the tour then returns to the dashboard
    expect(page).to_have_url(f"{app.url}/#/dashboard")

    # Appearance: light applies at once and survives a reload and a restart
    page.goto(f"{app.url}/#/settings")
    page.locator("#settings-appearance").get_by_label("Light").check()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    app.wait_state(page, "ui.theme", "light")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")

    # pairing by the installing agent, then the request from the Destinations page
    pair = subprocess.run(app._cli("agent", "pair", "--agent", "codex"), capture_output=True, text=True,
                          timeout=60)
    assert pair.returncode == 0 and "Paired Codex" in pair.stdout
    keys = tmp_path / "tiktok.env"
    keys.write_text(f"TIKTOK_CLIENT_KEY={key}\nTIKTOK_CLIENT_SECRET={secret}\n", encoding="utf-8")
    page.goto(f"{app.url}/#/destinations")
    page.locator("#dest-tiktok").get_by_text("Or let my agent fill these in").click()
    panel = page.locator('#dest-tiktok .agent-panel')
    expect(panel).to_contain_text("Paired with Codex")
    panel.get_by_label("A file I choose").check()
    panel.get_by_label("File or entry name").fill(str(keys))
    panel.get_by_role("button", name="Ask my agent").click()
    expect(panel.locator(".agent-status")).to_contain_text("Waiting for your agent")

    # the helper, as the agent starts it; a dead proxy in its environment must not be used
    env = {**os.environ, "LIVE_CLIPPER_HOME": str(app.home), "HTTP_PROXY": "http://127.0.0.1:9",
           "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}
    cmd = [sys.executable, "-m", "live_clipper", "agent", "serve", "--once", "--max-wait", "60"]
    helper = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for _ in range(300):  # let the page poll while the helper works
        if helper.poll() is not None:
            break
        page.wait_for_timeout(100)
    out = helper.communicate(timeout=30)[0]
    assert helper.returncode == 0, out
    assert "found client_key, client_secret" in out and "result: done" in out
    expect(panel.locator(".agent-status")).to_contain_text("Saved: client_key, client_secret", timeout=10000)
    tiktok = next(d for d in app.api("/destinations")["destinations"] if d["provider"] == "tiktok")
    assert tiktok["app_credentials_set"] == ["client_key", "client_secret"]

    # still light after a restart; and no key in the helper's output, the page, the API or the logs
    app.stop()
    app.start()
    open_app(page, app)
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (app.home / "logs").glob("*.log"))
    seen = out + page.content() + json.dumps(app.api("/agent/requests"))
    seen += json.dumps(app.api("/destinations")) + logs
    assert key not in seen and secret not in seen
    assert not page.js_errors
