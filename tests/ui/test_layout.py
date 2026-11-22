"""Layout rules that were broken once: a hidden field stays hidden, phone controls are finger-sized, the
dashboard leads with what waits on the user, the phone clip list keeps its title and status, and the tour
card does not cover what it explains."""

import json

import pytest
from conftest import DONE, MockServer
from playwright.sync_api import expect
from test_help_coverage import ROUTES

pytestmark = pytest.mark.ui

# controls and stand-alone links (links inside a sentence are exempt); a checkbox or radio is hit through
# its label
CONTROLS = ("button, a.btn, a.act, a.rowlink, .tabbar a, input:not([type=hidden]), select, textarea, "
            "summary, [role=tab]")
UNDER_44 = """(controls) => [...document.querySelectorAll(controls)]
  .filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'
      && !el.closest('[hidden]');
  })
  .map(el => {
    const boxed = 'input[type=checkbox]:not(.switch), input[type=radio]';
    const viaLabel = el.matches(boxed) && el.closest('label');
    const r = (viaLabel || el).getBoundingClientRect();
    return [el.outerHTML.slice(0, 120), Math.round(r.width), Math.round(r.height)];
  })
  .filter(([, w, h]) => Math.min(w, h) < 44)"""


def test_a_hidden_field_is_not_shown(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    webhook = page.get_by_label("What is the webhook address?")
    method = page.get_by_label("A webhook (ntfy, Discord or other)")
    expect(method).to_be_visible()  # the form is drawn; a missing element would also count as hidden
    expect(webhook).to_be_hidden()  # only asked when notifications go to a webhook
    method.check()
    expect(webhook).to_be_visible()

    page.goto(seeded_server.url + "/#/channels/new")
    every = page.get_by_label("Check the title every (seconds)")
    expect(page.get_by_label("Streamer link")).to_be_visible()
    expect(every).to_be_hidden()
    page.get_by_label("Only when the title matches").check()
    expect(every).to_be_visible()
    assert not page.js_errors


def test_phone_controls_are_finger_sized(seeded_server, make_page):
    page = make_page(width=390, height=844)
    for r in ROUTES:
        page.goto(f"{seeded_server.url}/#/{r}")
        expect(page.locator("main h1")).to_be_visible()
        page.wait_for_timeout(200)
        small = page.evaluate(UNDER_44, CONTROLS)
        assert not small, f"{r}: controls under 44 px on a phone: {small[:5]}"
    # the bottom bar marks the current screen and opens the full menu
    page.goto(f"{seeded_server.url}/#/clips/1")
    bar = page.locator(".tabbar")
    expect(bar.get_by_role("link", name="Clips")).to_have_attribute("aria-current", "page")
    bar.get_by_role("link", name="Channels").click()
    expect(page.locator("main h1")).to_have_text("Channels")


def test_dashboard_leads_with_what_waits_on_you(seeded_server, make_page, tmp_path):
    page = make_page(width=390, height=844)
    page.goto(seeded_server.url + "/#/dashboard")
    todo = page.locator("#attention")
    expect(todo).to_contain_text("2 clips waiting for you to post")
    rows = page.locator("#channel-cards article")
    expect(rows.first).to_contain_text("Needs action")  # the channel with a problem is listed first
    assert todo.bounding_box()["y"] < rows.first.bounding_box()["y"] < 844  # both on the first screen
    todo.get_by_role("link", name="Review clips").click()
    expect(page).to_have_url(f"{seeded_server.url}/#/clips")

    # nothing waiting: the strip is not there at all
    state = tmp_path / "empty" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps(DONE))
    empty = MockServer(state, seed=False)
    empty.start()
    try:
        page.goto(empty.url + "/#/dashboard")
        expect(page.get_by_text("No channels yet").first).to_be_visible()
        expect(page.locator("#attention")).to_be_hidden()
    finally:
        empty.stop()
    assert not page.js_errors


def test_phone_clip_library_keeps_title_and_status(seeded_server, make_page):
    page = make_page(width=390, height=844)
    page.goto(seeded_server.url + "/#/clips")
    row = page.locator("#clip-library tbody tr").first
    title, status = row.get_by_role("link"), row.locator(".pill")
    expect(status).to_be_visible()
    assert title.bounding_box()["width"] >= 120  # was one syllable per line
    box = status.bounding_box()
    assert box["x"] + box["width"] <= 390  # was cut off at the edge
    expect(row).to_contain_text("ago")  # channel, age and length moved under the title
    expect(page.locator("#clip-library").get_by_role("columnheader", name="Channel")).to_be_hidden()


def test_tour_card_does_not_cover_its_target(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    page.get_by_role("button", name="Replay tutorial").click()
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".tour-card .count")).to_contain_text("Step 2 of")
    page.wait_for_timeout(300)
    overlap = page.evaluate("""() => {
        const a = document.querySelector('.tour-card').getBoundingClientRect();
        const b = document.querySelector('.tour-spot').getBoundingClientRect();
        return Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left))
             * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
    }""")
    assert overlap == 0


def test_settings_switch_saves_a_yes_or_no_answer(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    form = page.locator("#settings-notify")
    login = form.get_by_role("switch", name="Start Live Clipper automatically when you log in?")
    expect(login).not_to_be_checked()
    login.check()
    form.get_by_role("button", name="Save").click()
    expect(page.locator("#toasts")).to_contain_text("Notifications and startup saved")
    assert json.loads(seeded_server.state_file.read_text())["answers"]["service.start_at_login"] is True
