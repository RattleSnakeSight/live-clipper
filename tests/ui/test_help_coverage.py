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


# --- every format, rule and preset field has its own plain description --------------------------------------

def _described(model, prefix=""):
    """(path, description) for every field of a pydantic model, nested models included."""
    from pydantic import BaseModel

    for name, f in model.model_fields.items():
        yield f"{prefix}{name}", f.description
        types = [f.annotation, *getattr(f.annotation, "__args__", ())]
        for t in types:
            for inner in (t, *getattr(t, "__args__", ())):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    yield from _described(inner, f"{prefix}{name}.")


def test_every_model_field_has_a_specific_description():
    from live_clipper.formats import Rule, SelectionFormat
    from live_clipper.presets import Preset

    seen = 0
    for model in (SelectionFormat, Rule, Preset):
        for path, desc in _described(model):
            seen += 1
            assert desc and desc.strip(), f"{model.__name__}.{path} has no description"
            label = f"{model.__name__}.{path}"
            assert len(desc) >= 20 and desc.rstrip().endswith("."), f"{label}: {desc!r} is no sentence"
            assert not desc.startswith("Sets "), f"{model.__name__}.{path} has generic help"
    assert seen > 150


EDITOR_HELP = """async (kind) => {
    const docs = await import('/js/views/docs.js');
    const schema = await (await fetch(`/api/${kind}/schema`)).json();
    const want = docs.schemaHelp(schema);
    const bad = [];
    let n = 0;
    for (const f of document.querySelectorAll('#doc-editor [data-path]')) {
        const help = f.querySelector(':scope > .help, :scope > label + .help');
        const input = f.tagName === 'FIELDSET' ? null : f.querySelector('input, select, textarea');
        if (!help) continue;
        n++;
        const path = f.dataset.path;
        const text = help.textContent.trim();
        if (!text || text.startsWith('Sets ') || text !== want(path)) bad.push([path, text, want(path)]);
        const ih = input ? (input.dataset.help || '') : null;
        if (input && !ih.startsWith(want(path))) bad.push([path, 'input help', ih]);
    }
    return {n, bad};
}"""

RULE_HELP = """async () => {
    const schema = await (await fetch('/api/rules/schema')).json();
    const all = new Set();
    const walk = (n) => {
        if (n.description) all.add(n.description);
        Object.values(n.properties || {}).forEach(walk);
    };
    walk(schema);
    Object.values(schema.$defs || {}).forEach(walk);
    const bad = [];
    for (const el of document.querySelectorAll('#rule-editor [data-help]')) {
        const t = el.dataset.help;
        if (el.matches('button, a') || el.closest('.error')) continue;
        if (!all.has(t)) bad.push(t);
    }
    return bad;
}"""


def assert_editors_show_schema_descriptions(page, base_url):
    """Format and preset editors show the API's description of each field; so does the rule editor."""
    for kind, ident in (("formats", "gta-vi"), ("presets", "default")):
        page.goto(f"{base_url}/#/{kind}/{ident}")
        expect(page.locator("#doc-editor [data-path]").first).to_be_visible()
        res = page.evaluate(EDITOR_HELP, kind)
        assert res["n"] > 20, res
        assert not res["bad"], f"{kind}: help differs from the schema description: {res['bad'][:5]}"
    page.goto(f"{base_url}/#/formats")
    page.locator("#rule-list-panel").get_by_role("button", name="Add rule").click()
    expect(page.locator("#rule-editor")).to_be_visible()
    bad = page.evaluate(RULE_HELP)
    assert not bad, f"rule editor help that is not a schema description: {bad}"
    page.locator("#rule-editor").get_by_role("button", name="Cancel").click()


def test_editors_show_each_fields_own_description(seeded_server, make_page):
    page = make_page()
    assert_editors_show_schema_descriptions(page, seeded_server.url)
    assert not page.js_errors
