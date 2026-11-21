"""Every visible control has a one-line help text, the tour covers every screen, and nothing scrolls
sideways at 360 px."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

# routes plus sub-views; conditional screens (needs_action card, review queue, destination steps) are in the
# seeded data
ROUTES = [
    "dashboard", "channels", "channels/new", "channels/1", "sources", "clips", "clips/1", "formats",
    "formats/gta-vi", "presets", "presets/default", "destinations", "settings", "status", "guide",
    "guide/first-run",
]

MISSING_HELP = """() => [...document.querySelectorAll(
    'button, input:not([type=hidden]), select, textarea, [role=switch], a.btn, summary')]
  .filter(el => {
    const r = el.getBoundingClientRect();
    const shown = r.width > 0 || r.height > 0;
    return shown && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[hidden]');
  })
  .filter(el => !(el.dataset.help || '').trim())
  .map(el => el.outerHTML.slice(0, 200))"""

SCREENS = {"dashboard", "channels", "sources", "clips", "formats", "presets", "destinations", "settings",
           "status", "guide"}


def check(page, where):
    page.wait_for_timeout(250)
    missing = page.evaluate(MISSING_HELP)
    assert not missing, f"controls without data-help on {where}: {missing}"


def test_every_visible_control_has_help(seeded_server, make_page):
    page = make_page()
    for r in ROUTES:
        page.goto(f"{seeded_server.url}/#/{r}")
        expect(page.locator("main h1")).to_be_visible()
        check(page, r)

    # states reached by interaction
    page.goto(f"{seeded_server.url}/#/formats")
    page.locator("#rule-list-panel").get_by_role("button", name="Edit").first.click()
    expect(page.locator("#rule-editor")).to_be_visible()
    check(page, "rule editor")
    page.goto(f"{seeded_server.url}/#/formats/gta-vi")
    page.get_by_role("tab", name="JSON").click()
    check(page, "format JSON view")
    page.goto(f"{seeded_server.url}/#/channels")
    page.locator("#channel-list").get_by_role("button", name="Delete").first.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    check(page, "confirm dialog")
    page.keyboard.press("Escape")

    # every wizard question and the review page
    page.goto(f"{seeded_server.url}/#/setup")
    for i in range(40):
        check(page, f"setup page {i + 1}")
        nxt = page.get_by_role("button", name="Next").or_(page.get_by_role("button", name="Review answers"))
        if not nxt.count():
            break
        before = page.locator(".wizard").inner_text()
        nxt.click()
        page.wait_for_function("t => document.querySelector('.wizard').innerText !== t", arg=before)
    expect(page.get_by_role("heading", name="Check your answers")).to_be_visible()

    # the tour's own buttons
    page.goto(f"{seeded_server.url}/#/settings")
    page.get_by_role("button", name="Replay tutorial").click()
    page.get_by_role("button", name="Next").click()
    check(page, "tour")
    assert not page.js_errors


def test_tour_steps_reference_every_screen(seeded_server, make_page):
    page = make_page()
    page.goto(f"{seeded_server.url}/#/dashboard")
    expect(page.locator("main h1")).to_be_visible()
    info = page.evaluate("""async () => {
        const tour = await import('/js/tour.js');
        const app = await import('/js/app.js');
        const screens = new Set();
        for (const s of tour.STEPS) {
            const src = typeof s.route === 'function' ? s.route.toString() : s.route;
            for (const m of src.matchAll(/#\\/([a-z]+)/g)) screens.add(m[1]);
            if (!s.title || !s.text || s.text.includes('\\n')) return {bad: s.title};
        }
        return {screens: [...screens], routes: Object.keys(app.ROUTES)};
    }""")
    assert "bad" not in info, info
    routes = set(info["routes"]) - {"setup"}  # the wizard runs before the tour
    assert routes == SCREENS
    assert routes <= set(info["screens"]), f"tour misses {routes - set(info['screens'])}"


def test_small_screen_has_no_sideways_scroll_and_menu_works(seeded_server, make_page):
    page = make_page(width=360, height=740)
    for r in ROUTES:
        page.goto(f"{seeded_server.url}/#/{r}")
        expect(page.locator("main h1")).to_be_visible()
        page.wait_for_timeout(150)
        wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert wide <= 0, f"{r} is {wide}px wider than a 360px screen"
    page.goto(f"{seeded_server.url}/#/dashboard")
    menu = page.get_by_role("button", name="Menu")
    expect(menu).to_have_attribute("aria-expanded", "false")
    menu.click()
    expect(menu).to_have_attribute("aria-expanded", "true")
    page.locator(".sidebar").get_by_role("link", name="Sources").click()
    expect(page.locator("main h1")).to_have_text("Sources")
    expect(page.locator(".sidebar")).to_be_hidden()
