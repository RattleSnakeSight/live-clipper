"""Connecting accounts as a first-time customer: "Watch a stream" (Sources), "Post my clips" (Destinations).

Everything here runs against the mock API with synthetic accounts and fake provider answers. The provider's
sign-in tab is never opened: window.open is replaced, so no test leaves the computer.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import DONE, MockServer
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

PICKS = {"platforms.enabled": ["youtube", "kick"], "posting.destinations": ["youtube", "instagram", "tiktok"]}
NO_TAB = "window.open = (u) => { window.__opened = u; return null; }"
OVERFLOW = "() => document.documentElement.scrollWidth - innerWidth"
RETURN = "window.dispatchEvent(new Event('focus'))"  # the customer comes back from the provider's tab


@pytest.fixture
def server(tmp_path):
    """Setup finished with both intents picked; `start(state)` adds synthetic destination states."""
    made = []

    def start(seed: bool = False, **state):
        d = tmp_path / f"s{len(made)}"
        d.mkdir()
        (d / "state.json").write_text(json.dumps({**DONE, "answers": dict(PICKS), **state}))
        srv = MockServer(d / "state.json", seed)
        srv.start()
        made.append(srv)
        return srv

    yield start
    for s in made:
        s.stop()


def open_page(make_page, srv, route, **kw):
    page = make_page(**kw)
    page.add_init_script(NO_TAB)
    page.goto(f"{srv.url}/#/{route}")
    expect(page.locator("main h1")).to_be_visible()
    return page


def way(scope, title):
    """The radio button of one way to connect (its form and step list carry the same words)."""
    return scope.get_by_role("radio", name=re.compile("^" + re.escape(title)))


def opened(page) -> str:
    """The address of the provider tab the app tried to open."""
    page.wait_for_function("window.__opened")
    return page.evaluate("window.__opened")


def dest(srv, page, provider):
    return next(d for d in page.request.get(srv.url + "/api/destinations").json()["destinations"]
                if d["provider"] == provider)


def test_both_intents_are_visible_from_either_page(server, make_page):
    srv = server(seed=True)
    page = open_page(make_page, srv, "sources")
    nav = page.get_by_role("navigation", name="Account pages")
    watch, post = nav.get_by_role("link", name=re.compile("Watch a stream")), nav.get_by_role(
        "link", name=re.compile("Post my clips"))
    expect(watch).to_have_attribute("aria-current", "page")
    expect(watch).to_contain_text("1 of 3 connected")  # the seeded Twitch keys
    expect(post).to_contain_text("1 of 3 connected")   # the seeded YouTube channel, counted on its own
    post.click()
    expect(page.locator("main h1")).to_have_text("Destinations")
    nav = page.get_by_role("navigation", name="Account pages")
    post = nav.get_by_role("link", name=re.compile("Post my clips"))
    expect(post).to_have_attribute("aria-current", "page")
    assert not page.js_errors


def test_watch_youtube_shows_one_way_at_a_time_and_where_each_value_is(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "sources/youtube")
    card = page.locator("#source-youtube")
    expect(card.locator(".pill").first).to_have_text("No account connected")
    # the first choice needs nothing: no field is shown, only the way to the Channels page
    expect(way(card, "No account: paste a channel link")).to_be_checked()
    expect(card.locator(".route-panel input")).to_have_count(0)
    expect(card.get_by_role("link", name="Add a channel")).to_have_attribute("href", "#/channels/new")

    way(card, "API key (public data)").check()
    panel = card.locator(".route-panel")
    expect(panel.locator("> form input")).to_have_count(1)  # only the field this way needs
    expect(panel).to_contain_text("cannot upload")   # an API key is not the posting sign-in
    field = panel.locator(".field").filter(has=page.get_by_label("API key", exact=True))
    expect(field).to_contain_text('On the provider\'s page it is called "API key"')
    expect(field).to_contain_text("Identifies your Google Cloud project")
    link = panel.get_by_role("link", name="YouTube Data API v3 in the API Library")
    expect(link).to_have_attribute("href", "https://console.cloud.google.com/apis/library/youtube.googleapis.com")
    expect(link).to_have_attribute("target", "_blank")
    # quota and uncommon errors are one click away, not in the way
    more = panel.locator("details").filter(has_text="Limits, permissions and uncommon errors")
    expect(more).not_to_have_attribute("open", "")
    expect(more.get_by_text("quotaExceeded")).to_be_hidden()

    # an empty value is refused at the field, with the reason
    panel.get_by_role("button", name="Save YouTube key").click()
    expect(field.locator(".error")).to_contain_text("api_key is required")
    panel.get_by_label("API key", exact=True).fill("FAKE-KEY-000")
    panel.get_by_role("button", name="Save YouTube key").click()
    card = page.locator("#source-youtube")
    expect(card.locator(".pill").first).to_have_text("Connected")
    expect(card).to_contain_text("Connected with an API key")
    expect(card).to_contain_text("Saved on this computer: API key.")  # by the field's name, never its value
    assert "FAKE-KEY-000" not in page.content()  # the value is never shown again

    # coming back later resumes on the same way to connect
    page.reload()  # the link to this card opens its ways to connect
    expect(way(page.locator("#source-youtube"), "API key (public data)")).to_be_checked()
    assert not page.js_errors


def test_kick_shows_the_callback_and_the_webhook_address_with_copy(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "sources/kick")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    card = page.locator("#source-kick")
    way(card, "Sign in with Kick").check()
    callback = f"http://localhost:{srv.port}/api/oauth/source/kick/callback"
    box = card.locator(".callback").filter(has_text="Redirect address to register in Kick")
    expect(box.locator("code")).to_have_text(callback)
    expect(box).to_contain_text("Kick only accepts an exact match")  # why Kick needs it
    box.get_by_role("button", name="Copy").click()
    expect(page.locator("#toasts")).to_contain_text(re.compile("Address copied|The address is selected"))
    # the sign-in opens in another tab; closing it changes nothing and the button can be pressed again
    card.get_by_label("Client ID", exact=True).fill("kick-client")
    card.get_by_label("Client secret", exact=True).fill("kick-secret")
    card.get_by_role("button", name="Sign in to Kick").click()
    assert opened(page).startswith("https://example.com/oauth/kick/authorize")
    expect(card).to_contain_text("If you close that tab or press Cancel there, nothing changes")
    again = card.get_by_role("link", name="No new tab? Open the sign-in page here.")  # a blocked pop-up
    expect(again).to_have_attribute("href", re.compile("^https://example.com/oauth/kick/authorize"))
    expect(card.get_by_label("Client secret", exact=True)).to_have_value("")  # sent once, not kept here
    expect(card.locator(".pill").first).to_have_text("No account connected")
    # Kick chat is optional and says which local address a tunnel forwards to
    card.locator("#kick-chat > summary").click()
    expect(card.locator("#kick-chat code")).to_have_text("http://127.0.0.1:8788/webhooks/kick")
    assert not page.js_errors


def test_a_wrong_key_is_refused_with_what_to_check_and_nothing_is_saved(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "sources/kick")
    card = page.locator("#source-kick")
    way(card, "Client ID and secret").check()
    card.get_by_label("Client ID", exact=True).fill("kick-client")
    card.get_by_label("Client secret", exact=True).fill("wrong-secret")
    card.get_by_role("button", name="Save Kick keys").click()
    answer = card.locator(".route-panel > form [role=status]")  # right under the button
    expect(answer).to_contain_text("Kick rejected the client ID or secret")
    expect(answer).to_contain_text("Check the Client ID and secret")
    expect(card.locator(".pill").first).to_have_text("No account connected")
    expect(card.get_by_label("Client ID", exact=True)).to_have_value("kick-client")  # kept, to correct it
    assert "wrong-secret" not in card.inner_text()  # the masked value is never echoed in the message
    card.get_by_label("Client secret", exact=True).fill("right-secret")
    card.get_by_role("button", name="Save Kick keys").click()
    expect(page.locator("#source-kick .pill").first).to_have_text("Connected")
    assert not page.js_errors


def test_post_youtube_shorts_step_by_step_without_switching_anything_on(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    expect(card).to_contain_text("posts directly through Google")
    expect(card).to_contain_text("Metricool is not needed")
    expect(card.locator(".pill").first).to_have_text("Not connected")
    # only the next step is open; posting cannot be switched on yet
    expect(card.locator("details.step[open]")).to_have_count(1)
    one = card.locator("details.step[data-step='1']")
    expect(one).to_have_attribute("open", "")
    expect(one).to_contain_text("cannot upload")  # the API key from Sources is not enough
    expect(one.get_by_role("link", name="Google Auth platform: Clients").first).to_have_attribute(
        "href", "https://console.cloud.google.com/auth/clients")
    expect(one.locator("form[data-method] input")).to_have_count(2)  # only what this way needs
    expect(one.get_by_label("Client secret (optional)")).to_be_visible()

    # step 1: a saved key is not a connection
    one.get_by_role("button", name="Save keys").click()
    expect(one.locator(".field").first.locator(".error")).to_have_text("This value is needed.")
    one.get_by_label("Client ID", exact=True).fill("0000-fake.apps.googleusercontent.com")
    one.get_by_role("button", name="Save keys").click()
    card = page.locator("#dest-youtube")
    expect(card.locator(".pill").first).to_have_text("Keys saved, not signed in")
    assert dest(srv, page, "youtube")["status"] == "disconnected"

    # step 2: the sign-in, with what Google asks and where the answer comes back
    two = card.locator("details.step[data-step='2']")
    expect(two).to_have_attribute("open", "")
    expect(two).to_contain_text("Manage your YouTube videos")
    expect(two).to_contain_text(f"http://127.0.0.1:{srv.port}/api/oauth/youtube/callback")
    expect(two).to_contain_text("nothing to paste into Google")
    two.locator("summary").filter(has_text="If the sign-in shows an error").click()
    for case in ("redirect_uri_mismatch", "accessNotConfigured", "Step 3 shows the wrong channel"):
        expect(two.locator(".recover")).to_contain_text(case)
    two.get_by_role("button", name="Sign in with Google").click()
    assert opened(page).startswith("https://example.com/oauth/youtube/authorize")
    page.evaluate(RETURN)

    # step 3: the channel the provider reported, before anything can be switched on
    card = page.locator("#dest-youtube")
    expect(card.locator(".pill").first).to_have_text("Connected as @example_clips")
    three = card.locator("details.step[data-step='3']")
    expect(three).to_have_attribute("open", "")
    expect(three.locator(".identity")).to_contain_text("Channel the provider reported: @example_clips")
    expect(three.get_by_role("link", name="Open the channel to check")).to_have_attribute(
        "href", "https://www.youtube.com/channel/UC0000000000000000000000")
    d = dest(srv, page, "youtube")
    assert d["status"] == "connected" and d["enabled"] is False and d["rights_acknowledged"] is False
    sent = page.request.get(srv.url + "/api/publish/deliveries").json()["deliveries"]
    assert sent == []  # connecting sent nothing
    three.get_by_label(re.compile("permission to post them on @example_clips")).check()

    # step 4: on is a separate decision
    card = page.locator("#dest-youtube")
    sw = card.get_by_role("switch", name="Post to @example_clips")
    expect(sw).to_be_enabled()
    expect(sw).not_to_be_checked()
    expect(card.locator("header .pill").nth(1)).to_have_text("Posting off")
    assert dest(srv, page, "youtube")["enabled"] is False

    # the wrong channel: one button, keys and the acknowledgement stay
    card.locator("details.step[data-step='3'] > summary").click()
    card.get_by_role("button", name="Use a different channel").click()
    page.get_by_role("dialog").get_by_role("button", name="Disconnect").click()
    card = page.locator("#dest-youtube")
    expect(card.locator(".pill").first).to_have_text("Keys saved, not signed in")
    d = dest(srv, page, "youtube")
    assert d["app_credentials_set"] == ["client_id"] and d["rights_acknowledged"] is True and not d["enabled"]
    assert not page.js_errors


def test_checking_a_connection_keeps_posting_settings(server, make_page):
    srv = server(seed=True)  # YouTube connected, acknowledged and on; posting mode manual
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    before = dest(srv, page, "youtube")
    card.locator("details.step[data-step='2'] > summary").click()
    card.get_by_role("button", name="Check connection").click()
    expect(page.locator("#toasts")).to_contain_text("Nothing was posted")
    after = dest(srv, page, "youtube")
    for key in ("enabled", "rights_acknowledged", "settings", "account", "status"):
        assert after[key] == before[key]
    assert page.request.get(srv.url + "/api/publish/mode").json()["mode"] == "manual"
    assert not page.js_errors


def test_metricool_is_named_where_posting_is_decided_and_asks_for_nothing(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "destinations")
    for provider, native in (("tiktok", "your own TikTok developer app"),
                             ("instagram", "your own Meta developer app")):
        routes = page.locator(f"#routes-{provider}")
        expect(routes.locator("li[data-route=native]")).to_contain_text(native)
        expect(routes.locator("li[data-route=native] .pill")).to_have_text("Available here")
        out = routes.locator("li[data-route=metricool]")
        expect(out).to_contain_text("Metricool is a separate online service")
        expect(out.locator(".pill")).to_have_text("Needs a separately installed sender")
        expect(out).to_contain_text("nothing to connect or enter here")
        expect(out).to_contain_text("not sent twice")  # one sender per account
        expect(out.locator("input, button, select, textarea")).to_have_count(0)
    expect(page.locator("#dest-instagram")).to_contain_text("not been tried with a newly created Meta app")
    expect(page.locator("#routes-youtube")).to_have_count(0)
    # the one place with a Metricool choice is the preferred-route panel; the cards ask for nothing about it
    assert page.locator("#list-cards input[name*=metricool i], .stack input[name*=metricool i]").count() == 0

    # first-run setup says it at the question, and again on the last page when Instagram or TikTok is picked
    wiz = server(app_state={})
    page = open_page(make_page, wiz, "setup/review")
    expect(page.locator("#wizard-accounts")).to_contain_text("Watch a stream")
    expect(page.locator("#wizard-accounts")).to_contain_text("Post my clips")
    expect(page.locator("#wizard-metricool")).to_contain_text("not included in this installation")
    page.locator(".review dd [data-help*='Where do you want to post clips']").click()  # its Change button
    q = page.locator(".question")
    expect(q.locator(".eyebrow")).to_have_text("Post my clips")
    expect(q.locator("p.note")).to_contain_text("Metricool, an outside scheduling service")
    expect(q.locator("p.note")).to_contain_text("YouTube Shorts posts directly through Google")
    assert not page.js_errors


@pytest.mark.parametrize("detail, fix", [
    ("could not verify account: ValueError: YouTube Data API v3 is not enabled for this client's Google "
     "Cloud project (accessNotConfigured)", "press Enable"),
    ("token exchange failed: ValueError: Google token exchange failed: HTTP 401 invalid_client",
     "Desktop app client"),
    ("could not verify account: ValueError: this Google account has no YouTube channel", "Create a channel"),
])
def test_a_failed_sign_in_names_the_next_action(server, make_page, detail, fix):
    srv = server(destinations={"youtube": {"status": "error", "status_detail": detail,
                                           "app_credentials_set": ["client_id"]}})
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    expect(card.locator(".pill").first).to_have_text("Could not verify")
    note = card.locator("[data-problem=yes]")  # a lasting state: shown, not announced again on every look
    expect(note).to_contain_text(detail)
    expect(note).to_contain_text("What to do:")
    expect(note).to_contain_text(fix)
    expect(card.get_by_role("alert")).to_have_count(0)
    assert not page.js_errors


def test_a_cancelled_sign_in_can_be_started_again(server, make_page):
    srv = server(destinations={"youtube": {"status_detail": "authorization was not granted (access_denied)",
                                           "app_credentials_set": ["client_id"]}})
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    expect(card.locator("[data-problem=yes]")).to_contain_text("Nothing was changed. Press the sign-in")
    card.get_by_role("button", name="Sign in with Google").click()
    opened(page)
    page.evaluate(RETURN)
    expect(page.locator("#dest-youtube .pill").first).to_have_text("Connected as @example_clips")
    assert not page.js_errors


def test_instagram_asks_only_for_what_the_chosen_way_needs(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "destinations/instagram")
    one = page.locator("#dest-instagram details.step[data-step='1']")
    expect(way(one, "Paste a token from the Meta dashboard")).to_be_checked()
    expect(one.locator("form[data-method] input")).to_have_count(1)
    expect(one.get_by_label("Access token")).to_have_attribute("type", "password")  # masked
    way(one, "Sign in with your Meta app").check()
    expect(one.get_by_label("Instagram app ID")).to_be_visible()
    expect(one.get_by_label("Instagram app secret")).to_have_attribute("type", "password")
    expect(one.get_by_label("Access token")).to_have_count(0)
    expect(one.locator(".callback code")).to_have_text(f"http://127.0.0.1:{srv.port}/api/oauth/instagram/callback")
    # a wrong token is refused at the field and nothing is connected
    way(one, "Paste a token from the Meta dashboard").check()
    one.get_by_label("Access token").fill("short")
    one.get_by_role("button", name="Connect with token").click()
    expect(one.locator("form[data-method] .error")).to_contain_text("does not look like an access token")
    assert dest(srv, page, "instagram")["status"] == "disconnected"
    assert not page.js_errors


def test_keyboard_reaches_routes_steps_and_actions(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "sources/kick")
    card = page.locator("#source-kick")
    way(card, "No account: paste a channel link").focus()
    page.keyboard.press("ArrowDown")
    expect(way(card, "Client ID and secret")).to_be_checked()
    expect(card.locator(".route-panel")).to_have_attribute("data-route", "credentials")
    for _ in range(12):  # links and the copy button come first, in reading order
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.name") == "client_id":
            break
    assert page.evaluate("document.activeElement.name") == "client_id"

    page.goto(f"{srv.url}/#/destinations/youtube")
    two = page.locator("#dest-youtube details.step[data-step='2']")
    expect(two).not_to_have_attribute("open", "")
    two.locator("> summary").focus()
    page.keyboard.press("Enter")
    expect(two).to_have_attribute("open", "")
    # keyboard focus stays visible
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    assert not page.js_errors


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 390])
def test_both_pages_fit_phone_and_desktop_in_both_themes(server, make_page, width, theme):
    srv = server(seed=True, app_state={**DONE["app_state"], "ui.theme": theme})
    page = make_page(width=width, height=844)
    page.add_init_script(NO_TAB)
    for route in ("sources", "destinations"):
        page.goto(f"{srv.url}/#/{route}")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        page.wait_for_timeout(300)
        page.evaluate("document.querySelectorAll('main details').forEach((d) => { d.open = true; })")
        for radio in page.locator("main .connect input[type=radio][value=oauth]").all():
            radio.check()  # the ways to connect with the longest content: an address to copy and two fields
        assert page.evaluate(OVERFLOW) <= 0, f"{route} scrolls sideways at {width}px"
        clipped = page.evaluate("() => [...document.querySelectorAll('main code')]"
                                ".filter((c) => c.scrollWidth > c.clientWidth + 1).length")
        assert clipped == 0  # an address to copy is never cut off
    assert not page.js_errors


def test_a_link_to_a_card_lands_on_it_and_coming_back_keeps_your_place(server, make_page):
    srv = server(seed=True)
    page = open_page(make_page, srv, "destinations/tiktok")
    card = page.locator("#dest-tiktok")
    expect(card.get_by_role("heading", name="TikTok", exact=True)).to_be_focused()
    assert page.evaluate("scrollY") > 0  # the page did not jump back to the top
    # a saved setting is not "unsaved typing": the page still looks again when the customer comes back
    page.request.put(f"{srv.url}/api/destinations/2/settings", data={"tags": ["clips"]})
    limits = card.locator("details:not(.connect)").filter(has_text="What fails without each step")
    limits.locator("> summary").click()
    keys = {"client_key": "ck", "client_secret": "cs"}  # as if saved from another tab
    page.request.put(f"{srv.url}/api/destinations/2/credentials", data=keys)
    page.evaluate(RETURN)
    card = page.locator("#dest-tiktok")
    expect(card.locator(".pill").first).to_have_text("Keys saved, not signed in")  # it did look again
    limits = card.locator("details:not(.connect)").filter(has_text="What fails without each step")
    expect(limits).to_have_attribute("open", "")  # and what was opened is still open
    # an unsaved value keeps the card as it is until it is saved
    two = card.locator("details.step[data-step='2']")
    expect(two).to_have_attribute("open", "")
    page.locator("#dest-instagram > details.connect > summary").click()
    token = page.locator("#dest-instagram").get_by_label("Access token")
    token.fill("typed-not-saved-000000")
    page.request.post(f"{srv.url}/api/destinations/2/connect")
    page.evaluate(RETURN)
    page.wait_for_timeout(400)
    expect(token).to_have_value("typed-not-saved-000000")
    assert not page.js_errors


def test_keyboard_focus_stays_in_the_card_after_an_action(server, make_page):
    srv = server(destinations={"youtube": {"status": "connected", "account": "@example_clips",
                                           "app_credentials_set": ["client_id"]}})
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    ack = card.get_by_label(re.compile("permission to post them on @example_clips"))
    # The deep-link router focuses this heading after the card's controls first become visible.
    expect(card.get_by_role("heading", level=2)).to_be_focused()
    with page.expect_response(
        lambda r: r.request.method == "PUT" and r.url.endswith("/acknowledge")
    ) as saved:
        ack.press("Space")
    assert saved.value.ok
    expect(page.locator("#dest-youtube details.step[data-step='4']")).to_have_attribute("data-state", "next")
    inside = "document.activeElement.closest('section') && document.activeElement.closest('section').id"
    assert page.evaluate(inside) == "dest-youtube"  # not dropped to the top of the page
    four = page.locator("#dest-youtube details.step[data-step='4'] > summary")
    expect(four).to_be_focused()
    expect(four).to_contain_text("Do this next")
    assert not page.js_errors


def test_switching_on_after_an_account_change_names_the_new_account_first(server, make_page):
    moved = ("This sign-in belongs to a different account (@example_clips, id UC00) than the one posting was "
             "switched on for (Old Channel (id UC_old)). Posting was switched off and nothing was sent. "
             "Check the account, then switch posting on again.")
    state = {"status": "connected", "account": "@example_clips", "enabled": False, "status_detail": moved,
             "rights_acknowledged": True, "app_credentials_set": ["client_id"]}
    srv = server(destinations={"youtube": state})
    page = open_page(make_page, srv, "destinations/youtube")
    card = page.locator("#dest-youtube")
    expect(card.locator("[data-problem=yes]")).to_contain_text("different account")
    sw = card.get_by_role("switch", name="Post to @example_clips")
    sw.check()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Post to @example_clips?")
    expect(dialog).to_contain_text("stay where they are")
    dialog.get_by_role("button", name="Cancel").click()
    expect(sw).not_to_be_checked()
    assert dest(srv, page, "youtube")["enabled"] is False
    sw.check()
    page.get_by_role("dialog").get_by_role("button", name="Switch posting on").click()
    expect(page.locator("#dest-youtube header .pill").nth(1)).to_have_text("Posting on")
    assert dest(srv, page, "youtube")["enabled"] is True
    assert not page.js_errors


def test_metricool_is_preferred_never_assumed_connected_and_skippable(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "destinations")
    panel = page.locator("#social-route")
    expect(panel).to_contain_text("Metricool (preferred for Instagram and TikTok)")
    expect(panel).to_contain_text("Not connected")                    # preferred is not connected
    expect(panel.get_by_label("Through Metricool (preferred)")).to_be_checked()
    panel.get_by_label("Directly, or not at all (skip Metricool)").check()
    expect(panel).to_contain_text("Skipped")
    assert json.loads((srv.state_file).read_text()).get("social_route") == "direct"


def test_a_verified_sender_is_shown_as_connected_with_only_its_own_receipts(server, make_page):
    srv = server(social_sender={"networks": ["instagram", "tiktok"]}, social_confirmed={"instagram": 2})
    page = open_page(make_page, srv, "destinations")
    panel = page.locator("#social-route")
    expect(panel).to_contain_text("Connected: the local sender is running and verified")
    expect(panel).to_contain_text("instagram: 2 confirmed")
    expect(panel).to_contain_text("never direct or test posts")


def test_clean_up_now_says_what_it_deletes_first_and_reject_help_says_so(server, make_page):
    srv = server()
    page = open_page(make_page, srv, "settings")
    page.get_by_role("button", name="Preview cleanup").click()
    said = page.locator("[role=status]").filter(has_text="Would free")
    expect(said).to_contain_text("2 fully posted clips")
    expect(said).to_contain_text("1 rejected clip")
    expect(said).to_contain_text("Kept:")
    page.get_by_role("button", name="Clean up now").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("cannot be undone")
    expect(dialog).to_contain_text("2 fully posted clips")
    expect(dialog).to_contain_text("1 rejected clip")
    expect(dialog).to_contain_text("being posted, shared or not decided")
    expect(dialog.get_by_role("button", name="Delete these files")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
