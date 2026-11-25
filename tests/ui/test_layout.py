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
    page.once("dialog", lambda d: d.accept())

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


def test_theme_is_set_before_the_app_script_runs(seeded_server, make_page):
    """No flash: theme-boot.js sets the theme from the cookie before the app's own script loads."""
    page = make_page()
    page.emulate_media(color_scheme="dark")
    page.route("**/js/app.js", lambda route: route.abort())  # only the pre-paint script may act
    page.goto(seeded_server.url + "/#/dashboard")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.context.add_cookies([{"name": "lc_theme", "value": "light", "url": seeded_server.url}])
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "oklch(0.965 0.006 200)"  # light ground before any module ran


def test_settings_theme_choice_applies_and_is_saved(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    box = page.locator("#settings-appearance")
    expect(box.get_by_label("Automatic")).to_be_checked()
    box.get_by_label("Light").check()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert json.loads(seeded_server.state_file.read_text())["app_state"]["ui.theme"] == "light"
    page.goto(seeded_server.url + "/#/clips")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.goto(seeded_server.url + "/#/settings")
    page.locator("#settings-appearance").get_by_label("Dark").check()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_ask_my_agent_request_waits_and_can_be_cancelled(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/destinations")
    # the helper is an option of the keys step, one click away from the fields it fills
    page.locator("#dest-tiktok > details.connect > summary").click()
    page.locator("#dest-tiktok").get_by_text("Or let my agent fill these in").click()
    panel = page.locator("#dest-tiktok .agent-panel")
    expect(panel).to_contain_text("Paired with Claude Code")
    expect(panel).to_contain_text("TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET")
    expect(panel.get_by_label("File or entry name")).to_be_hidden()  # environment needs no location
    panel.get_by_label("A file I choose").check()
    panel.get_by_role("button", name="Ask my agent").click()
    expect(panel.locator(".field .error")).to_contain_text("choose the file")  # a file needs its path
    panel.get_by_label("File or entry name").fill("/data/keys.env")
    panel.get_by_role("button", name="Ask my agent").click()
    status = panel.locator(".agent-status")
    expect(status).to_contain_text("Waiting for your agent")
    expect(panel.get_by_role("button", name="Ask my agent")).to_be_disabled()
    status.get_by_role("button", name="Cancel").click()
    expect(status).to_contain_text("Cancelled")
    expect(status).to_contain_text("enter the keys yourself")
    assert not page.js_errors


def test_manual_fallback_points_to_the_right_form(seeded_server, fresh_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/settings")
    box = page.locator("#settings-agent")
    box.get_by_label("Platform").select_option("destination:tiktok")
    panel = box.locator(".agent-panel")
    panel.get_by_role("button", name="Ask my agent").click()
    panel.get_by_role("button", name="Cancel").click()
    link = panel.get_by_role("link", name="enter the keys yourself on Destinations")
    link.click()
    expect(page.locator("main h1")).to_have_text("Destinations")

    # the wizard cannot be left half done: it says where to go once finished, and keeps the answers
    page.goto(fresh_server.url + "/#/setup/review")
    keys = page.locator("#wizard-accounts")
    keys.locator("summary").first.click()
    panel = keys.locator(".agent-panel").first
    panel.get_by_role("button", name="Ask my agent").click()
    panel.get_by_role("button", name="Cancel").click()
    expect(panel).to_contain_text("once setup is finished")
    assert not page.js_errors


def test_custom_accent_supports_keyboard_pointer_and_save_failure(seeded_server, make_page):
    page = make_page(width=390)
    page.goto(seeded_server.url + "/#/settings")
    panel = page.locator("#settings-appearance")
    panel.get_by_text("Customize accent colour", exact=True).click()
    wheel = panel.get_by_role("slider")
    wheel.press("ArrowRight")
    expect(wheel).to_have_attribute("aria-valuenow", "203")
    saved_hue = "async h => (await (await fetch('/api/app-state/ui.accent')).json()).value?.hue === h"
    page.wait_for_function(saved_hue, arg=203)
    page.reload()
    panel.get_by_text("Customize accent colour", exact=True).click()
    expect(wheel).to_have_attribute("aria-valuenow", "203")
    wheel.click(position={"x": 122, "y": 66})
    expect(wheel).to_have_attribute("aria-valuenow", "0")
    page.wait_for_function(saved_hue, arg=0)
    hex_input = panel.get_by_role("textbox", name="Accent colour as a hex code")
    hex_input.fill("bad")
    hex_input.press("Tab")
    expect(hex_input).to_have_attribute("aria-invalid", "true")
    page.route("**/api/app-state/ui.accent", lambda r: r.fulfill(status=503, body="Unavailable"))
    panel.get_by_role("button", name="Reset to teal").click()
    expect(page.locator("#toasts")).to_contain_text("Accent could not be saved")
    expect(wheel).to_have_attribute("aria-valuenow", "0")
