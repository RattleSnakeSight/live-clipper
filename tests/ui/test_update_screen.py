"""The update gate in the browser: no screen when current, a blocking "Update pending" screen with versions,
steps and Check again when a newer release exists, a distinct recovery screen when the check fails, and the
switch to the gate when a release appears while the app is open."""

import json

import pytest
from conftest import DONE, _make_server
from playwright.sync_api import expect
from test_layout import CONTROLS, UNDER_44

pytestmark = pytest.mark.ui

PENDING = {"state": "pending", "available": "9.0.0", "method": "pipx", "agent": "claude"}
UNKNOWN = {"state": "unknown", "available": None, "error": "offline", "checked_at": None}


@pytest.fixture
def server(tmp_path):
    servers = []

    def make(update=None, theme=None):
        state = json.loads(json.dumps(DONE))
        if update:
            state["update"] = update
        if theme:
            state["app_state"]["ui.theme"] = theme
        folder = tmp_path / str(len(servers))
        folder.mkdir()
        srv = _make_server(folder, seed=True, state=state)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


def checks(srv):
    return json.loads(srv.state_file.read_text()).get("update_checks", [])


def test_current_shows_the_app_and_checks_once_per_open(server, make_page):
    srv = server()
    page = make_page()
    page.goto(srv.url + "/#/dashboard")
    expect(page.locator("h1")).to_have_text("Dashboard")
    expect(page.locator(".gate")).to_have_count(0)
    assert checks(srv) == ["open"]
    page.reload()
    expect(page.locator("h1")).to_have_text("Dashboard")
    assert checks(srv) == ["open", "open"]  # every open asks again
    assert not page.js_errors


def test_first_run_wizard_still_opens_when_current(tmp_path, make_page):
    srv = _make_server(tmp_path, seed=False)
    try:
        page = make_page()
        page.goto(srv.url + "/")
        expect(page).to_have_url(srv.url + "/#/setup")
        expect(page.locator(".gate")).to_have_count(0)
    finally:
        srv.stop()


def test_pending_blocks_the_app_and_explains_the_update(server, make_page):
    srv = server(PENDING)
    page = make_page()
    page.goto(srv.url + "/#/channels")
    heading = page.locator("h1")
    expect(heading).to_have_text("Update pending")
    expect(heading).to_be_focused()
    expect(page).to_have_title("Update pending - Live Clipper")
    gate = page.locator(".gate")
    expect(gate.locator(".gate-versions")).to_contain_text("Installed")
    expect(gate.locator(".gate-versions .available dd")).to_have_text("9.0.0")
    expect(gate.locator("code").first).to_have_text("live-clipper update")
    expect(gate).to_contain_text("pipx upgrade live-clipper")
    expect(gate).to_contain_text("Claude Code, the agent that installed Live Clipper")
    expect(gate).to_contain_text("live-clipper start")
    expect(gate).to_contain_text("kept")
    for hidden in ("#sidebar", ".tabbar", ".topbar-end"):
        expect(page.locator(hidden)).to_be_hidden()
    # moving around the app does not get past the screen
    page.goto(srv.url + "/#/clips")
    expect(page.locator("h1")).to_have_text("Update pending")
    expect(page.locator(".view")).to_have_count(0)
    assert not page.js_errors


def test_keyboard_reaches_every_action(server, make_page):
    srv = server(PENDING)
    page = make_page()
    page.goto(srv.url + "/")
    expect(page.locator("h1")).to_be_focused()
    reached = []
    for _ in range(8):
        page.keyboard.press("Tab")
        reached.append(page.evaluate("document.activeElement.getAttribute('aria-label') || "
                                     "document.activeElement.textContent"))
    assert "Copy live-clipper update" in reached and "Copy live-clipper start" in reached
    assert "Check again" in reached


def test_check_again_reports_and_opens_the_app_once_updated(server, make_page):
    srv = server(PENDING)
    page = make_page()
    page.goto(srv.url + "/")
    page.get_by_role("button", name="Check again").click()
    expect(page.locator(".gate-status")).to_contain_text("Still pending")
    expect(page.get_by_role("button", name="Check again")).to_be_focused()
    state = json.loads(srv.state_file.read_text())
    state["update_next"] = {"state": "current"}
    srv.state_file.write_text(json.dumps(state))
    srv.restart()  # the mock reads its state file at start
    page.get_by_role("button", name="Check again").click()
    expect(page.locator("h1")).to_have_text("Dashboard")
    assert checks(srv)[-2:] == ["retry", "open"]


def test_unknown_is_a_distinct_recovery_screen(server, make_page):
    srv = server(UNKNOWN)
    page = make_page()
    page.goto(srv.url + "/")
    expect(page.locator("h1")).to_have_text("Can't check for updates")
    gate = page.locator(".gate")
    expect(gate).not_to_contain_text("Update pending")
    expect(gate).not_to_contain_text("9.0.0")
    expect(gate).to_contain_text("no connection")
    expect(page.get_by_role("button", name="Check again")).to_be_visible()
    page.locator(".gate-more summary").click()
    expect(gate.locator(".gate-more code").first).to_have_text("live-clipper update")


def test_a_release_while_open_switches_to_the_gate(server, make_page):
    srv = server()
    page = make_page()
    page.goto(srv.url + "/#/dashboard")
    expect(page.locator("h1")).to_have_text("Dashboard")
    page.request.put(srv.url + "/api/_mock/update", data=PENDING)
    page.goto(srv.url + "/#/channels")  # the next request is refused with 423
    expect(page.locator("h1")).to_have_text("Update pending", timeout=10000)
    expect(page.locator("h1")).to_be_focused()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_phone_layout_and_theme(server, make_page, theme):
    srv = server(PENDING, theme=theme)
    page = make_page(390, 844)
    page.goto(srv.url + "/")
    expect(page.locator("h1")).to_have_text("Update pending")
    assert page.evaluate("document.documentElement.dataset.theme") == theme
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate(UNDER_44, CONTROLS) == []
