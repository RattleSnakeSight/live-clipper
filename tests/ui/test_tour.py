import json
import time

import pytest
from conftest import MockServer
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

SET_UP_NO_TOUR = {"app_state": {"setup.completed": True}, "answers": {}}


@pytest.fixture
def setup_done_server(tmp_path):
    """Setup finished earlier, tour never shown (tour.state is null)."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps(SET_UP_NO_TOUR))
    srv = MockServer(f, seed=True)
    srv.start()
    yield srv
    srv.stop()


def app_state(srv):
    return json.loads(srv.state_file.read_text())["app_state"]


def wait_state(srv, **expected):
    """The UI saves asynchronously; poll the store until the keys have the expected values."""
    want = {k.replace("_", "."): v for k, v in expected.items()}
    for _ in range(100):
        got = app_state(srv)
        if all(got.get(k) == v for k, v in want.items()):
            return
        time.sleep(0.05)
    raise AssertionError(f"app state {app_state(srv)} never matched {want}")


def tour(page):
    return page.locator(".tour-card")


def test_tour_completed_profile_persists_across_reload_and_update(setup_done_server, make_page):
    srv = setup_done_server
    page = make_page()
    page.goto(srv.url + "/")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Welcome to Live Clipper")
    expect(dialog).to_have_attribute("aria-modal", "true")
    expect(page.get_by_role("button", name="Next")).to_be_focused()

    # focus stays inside the tour
    for _ in range(5):
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.closest('.tour-card') !== null")

    total = int(tour(page).locator(".count").inner_text().split(" of ")[1])
    seen_hashes = set()
    for i in range(total - 1):
        expect(tour(page).locator(".count")).to_have_text(f"Step {i + 1} of {total}")
        seen_hashes.add(page.evaluate("location.hash.split('/')[1]"))
        page.get_by_role("button", name="Next").click()
    expect(tour(page).locator(".count")).to_have_text(f"Step {total} of {total}")
    # every screen was visited by the tour
    assert {"dashboard", "channels", "sources", "clips", "formats", "presets", "destinations", "settings",
            "status", "guide"} <= seen_hashes
    page.get_by_role("button", name="Finish").click()
    expect(tour(page)).to_have_count(0)
    wait_state(srv, tour_state="completed", tour_version=1)

    # second launch: not shown again
    page.reload()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)

    # simulated update: new server process, same data store
    srv.restart()
    page2 = make_page()
    page2.goto(srv.url + "/")
    expect(page2.get_by_role("heading", name="Dashboard")).to_be_visible()
    page2.wait_for_timeout(600)
    expect(tour(page2)).to_have_count(0)
    expect(page2.get_by_role("heading", name="Set up Live Clipper")).to_have_count(0)  # nor is setup
    assert not page.js_errors and not page2.js_errors


def test_tour_skipped_profile_and_escape(setup_done_server, make_page):
    srv = setup_done_server
    page = make_page()
    page.goto(srv.url + "/")
    expect(tour(page)).to_contain_text("Welcome to Live Clipper")
    page.get_by_role("button", name="Next").click()
    expect(tour(page)).to_contain_text("Channel cards")
    # the step lists the controls in the highlighted area with their one-line help
    expect(tour(page).locator("ul")).to_contain_text("Stop watching")
    page.get_by_role("button", name="Skip tour").click()
    expect(tour(page)).to_have_count(0)
    wait_state(srv, tour_state="skipped", tour_version=1)
    page.reload()
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)

    srv.restart()
    page.goto(srv.url + "/")
    page.wait_for_timeout(600)
    expect(tour(page)).to_have_count(0)

    # Settings > Replay tutorial shows it again; Escape skips
    page.goto(srv.url + "/#/settings")
    page.get_by_role("button", name="Replay tutorial").click()
    expect(tour(page)).to_contain_text("Welcome to Live Clipper")
    page.keyboard.press("Escape")
    expect(tour(page)).to_have_count(0)
    wait_state(srv, tour_state="skipped", tour_version=1)


def test_replay_tutorial_from_settings_runs_to_completion(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    expect(page.get_by_role("heading", name="Settings")).to_be_visible()
    page.wait_for_timeout(400)
    expect(tour(page)).to_have_count(0)  # already skipped in this profile
    page.get_by_role("button", name="Replay tutorial").click()
    total = int(tour(page).locator(".count").inner_text().split(" of ")[1])
    for i in range(total - 1):
        expect(tour(page).locator(".count")).to_have_text(f"Step {i + 1} of {total}")
        page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Finish").click()
    expect(tour(page)).to_have_count(0)
    wait_state(seeded_server, tour_state="completed", tour_version=1)
    expect(page).to_have_url(f"{seeded_server.url}/#/settings")  # returns where it started
