import json
import re

import pytest
from conftest import DONE, MockServer
from playwright.sync_api import expect

pytestmark = pytest.mark.ui


@pytest.fixture
def empty_server(tmp_path):
    """Set up, tour dismissed, no channels yet."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps(DONE))
    srv = MockServer(f, seed=False)
    srv.start()
    yield srv
    srv.stop()


def test_add_channel_detects_platform_and_shows_on_dashboard(empty_server, make_page):
    page = make_page()
    page.goto(empty_server.url + "/#/dashboard")
    expect(page.get_by_text("No channels yet").first).to_be_visible()
    page.get_by_role("link", name="Add channel").first.click()
    expect(page.get_by_role("heading", name="Add a channel")).to_be_visible()

    link = page.get_by_label("Streamer link")
    link.fill("not a link")
    expect(page.get_by_text("does not look like a Twitch, Kick or YouTube channel link")).to_be_visible()
    page.get_by_role("button", name="Add channel").click()
    expect(link).to_have_attribute("aria-invalid", "true")

    link.fill("https://kick.com/streamer_beta")
    expect(page.get_by_text("Found: Kick channel streamer_beta")).to_be_visible()
    page.get_by_label("Only when the title matches").check()
    page.get_by_label("YouTube Shorts").check()
    page.get_by_role("button", name="Add channel").click()
    expect(page).to_have_url(f"{empty_server.url}/#/channels")
    row = page.locator("#channel-list li").filter(has_text="streamer_beta")
    expect(row).to_contain_text("Kick")
    expect(row).to_contain_text("only when the title matches")

    # a second channel, then stop watching the first from its dashboard card
    page.goto(empty_server.url + "/#/channels/new")
    page.get_by_label("Streamer link").fill("https://www.twitch.tv/streamer_alpha")
    page.get_by_role("button", name="Add channel").click()
    expect(page.locator("#channel-list li")).to_have_count(2)
    page.goto(empty_server.url + "/#/dashboard")
    cards = page.locator("#channel-cards article")
    expect(cards).to_have_count(2)
    beta = cards.filter(has_text="streamer_beta")
    expect(beta).to_contain_text("Waiting")
    expect(beta).to_contain_text("Status from the stream probe")
    beta.get_by_role("button", name="Stop watching").click()
    expect(beta).to_contain_text("Disabled")
    expect(beta.get_by_role("button", name="Start watching")).to_be_visible()
    assert not page.js_errors


def test_manual_post_from_review_queue(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips")
    queue = page.locator("#review-queue")
    expect(queue).to_contain_text("Waiting for your approval")
    row = queue.locator("li").filter(has_text="harbour crash 1")
    row.get_by_role("button", name="Post to Test destination").click()
    expect(page.locator("#toasts")).to_contain_text("Posted")
    expect(row.get_by_role("button", name="Post to Test destination")).to_have_count(0)
    expect(row.get_by_role("button", name="Post to YouTube Shorts")).to_be_visible()

    page.goto(seeded_server.url + "/#/clips/1")
    history = page.locator("#clip-editor")
    expect(history.locator("table")).to_contain_text("Posted")
    history.get_by_role("button", name="Post to YouTube Shorts").click()
    expect(history.get_by_role("link", name="Open post")).to_have_count(2)

    # switching to automatic hides the queue
    page.goto(seeded_server.url + "/#/destinations")
    page.get_by_label("Automatic").check()
    expect(page.locator("#toasts")).to_contain_text("automatically")
    page.goto(seeded_server.url + "/#/clips")
    expect(page.locator("#review-queue")).to_contain_text("Posting is automatic")
    assert not page.js_errors


def test_reject_refreshes_the_decision_and_post_controls(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips/1")
    decision = page.locator("section").filter(has=page.get_by_role("heading", name="Decision", exact=True))
    decision.get_by_role("button", name="Reject", exact=True).press("Enter")
    expect(decision.locator("li").first).to_contain_text("rejected")
    expect(decision.get_by_role("button", name="Post anyway")).to_be_hidden()
    expect(page.locator("#clip-editor [data-key^=post-]")).to_have_count(0)
    expect(page.locator("#clip-editor [data-key^=cancel-]")).to_have_count(0)
    page.goto(seeded_server.url + "/#/clips")
    queue = page.locator("#review-queue")
    expect(queue.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    expect(queue.locator('a[href="#/clips/1"]')).to_have_count(0)
    assert not page.js_errors


def test_clip_override_editor_renders_again(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips/2")
    page.get_by_label("Start (seconds)").fill("5")
    page.get_by_label("End (seconds)").fill("1")
    page.get_by_role("button", name="Render again").click()
    expect(page.locator("#render-err")).to_have_text("The end must be at least 2 seconds after the start.")
    page.get_by_label("End (seconds)").fill("20")
    page.get_by_label("Title (English)").fill("Harbour jump, fixed title")
    page.get_by_role("button", name="Render again").click()
    expect(page.locator(".title-row")).to_contain_text("Rendering")


def test_format_import_export_round_trip(seeded_server, make_page, tmp_path):
    page = make_page()
    page.goto(seeded_server.url + "/#/formats")
    row = page.locator("#format-list li").filter(has_text="GTA VI")
    with page.expect_download() as dl:
        row.get_by_role("button", name="Export").click()
    exported = json.loads(open(dl.value.path(), encoding="utf-8").read())
    assert exported["id"] == "gta-vi" and exported["window"]["pre"] == 18.0

    exported.update(id="my-format", name="My format")
    exported["window"]["pre"] = 9.5
    f = tmp_path / "my-format.json"
    f.write_text(json.dumps(exported))
    with page.expect_file_chooser() as fc:
        page.get_by_role("button", name="Import file").click()
    fc.value.set_files(str(f))
    expect(page.locator("#format-list")).to_contain_text('Imported "My format"')
    mine = page.locator("#format-list li").filter(has_text="My format")
    with page.expect_download() as dl2:
        mine.get_by_role("button", name="Export").click()
    assert json.loads(open(dl2.value.path(), encoding="utf-8").read()) == exported

    # edit in the form; a bad value is caught by the server and shown at the field
    mine.get_by_role("link", name="Edit").click()
    pre = page.locator('[data-path="window.pre"] input')
    window_section = page.locator("summary").filter(has_text="Window").first
    expect(pre).to_be_hidden()
    window_section.click()
    expect(pre).to_be_visible()
    expect(pre).to_have_value("9.5")
    pre.fill("-3")
    window_section.click()
    page.get_by_role("button", name="Save").first.click()
    expect(pre).to_be_visible()
    expect(page.locator('[data-path="window.pre"] .error')).to_contain_text("greater than or equal to 0")
    pre.fill("12")
    page.get_by_role("tab", name="JSON").click()
    raw = page.locator("#doc-json")
    assert json.loads(raw.input_value())["window"]["pre"] == 12
    raw.fill("{ broken")
    expect(page.locator("#doc-json-err")).to_contain_text("Not valid JSON")
    raw.fill(json.dumps({**exported, "window": {"pre": 12, "post": 12.0}}))
    page.get_by_role("button", name="Save").first.click()
    expect(page.locator("#doc-editor")).to_contain_text("Saved.")

    # duplicate and delete; a bad import file is refused with a message
    page.goto(seeded_server.url + "/#/formats")
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with page.expect_file_chooser() as fc:
        page.get_by_role("button", name="Import file").click()
    fc.value.set_files(str(bad))
    expect(page.locator("#format-list")).to_contain_text("not valid JSON")
    assert not page.js_errors


def test_rules_reorder_and_preview_effective(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/formats")
    panel = page.locator("#rule-list-panel")
    panel.get_by_role("button", name="Add rule").click()
    page.get_by_label("Rule name").fill("Twitch longer ending")
    page.locator("#rule-editor").get_by_label("Twitch").check()
    page.get_by_label("Seconds after the moment").fill("20")
    page.locator("#rule-editor").get_by_role("button", name="Add rule").click()
    items = panel.locator(".rule-list > li")
    expect(items).to_have_count(2)
    expect(items.nth(1)).to_contain_text("Twitch longer ending")
    panel.get_by_role("button", name="Move Twitch longer ending up").click()
    expect(items.nth(0)).to_contain_text("Twitch longer ending")
    expect(panel.get_by_role("button", name="Move Twitch longer ending down")).to_be_focused()

    tool = page.locator("#preview-tool")
    tool.get_by_label("Platform").select_option("kick")
    tool.get_by_label("Clip length (seconds)").fill("45")
    tool.get_by_role("button", name="Show effective settings").click()
    post = tool.locator("tr").filter(has_text="window.post")
    expect(post).to_contain_text("Rule")
    expect(post).to_contain_text("Hold long Kick clips")
    expect(tool.locator("tr").filter(has_text="window.pre")).to_contain_text("Built-in")
    expect(page.locator("#precedence")).to_contain_text("Changes to one clip")


def test_guide_renders_offline_markdown_safely(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/guide/first-run")
    article = page.locator("#guide-main article")
    expect(article.get_by_role("heading", name="First run")).to_be_visible()
    expect(article.locator("strong")).to_have_text("Settings")
    expect(article.locator("ol li")).to_have_count(3)
    expect(article.locator("table")).to_contain_text("Add channel")
    expect(article.locator("script")).to_have_count(0)
    expect(article).to_contain_text("<script>alert('x')</script>")  # shown as text
    article.get_by_role("link", name="Monitoring streams").click()
    expect(page).to_have_url(f"{seeded_server.url}/#/guide/monitoring")
    expect(article.locator("code").first).to_have_text("waiting")
    expect(article.get_by_role("link", name="Unsafe link")).to_have_count(0)
    expect(page.locator(".guide-layout nav [aria-current=page]")).to_have_text("Monitoring streams")
    assert not page.js_errors


def open_settings(page, provider):
    """The optional settings of a destination are one click away, under its setup steps."""
    inside = page.locator(f"form[data-settings={provider}]")
    card = page.locator(f"#dest-{provider}")
    panel = card.locator("> details.connect")
    if panel.get_attribute("open") is None:
        panel.locator("> summary").click()
    card.locator("details:not(.connect)").filter(has=inside).locator("> summary").click()
    return page.locator(f"#dest-{provider} form[data-settings={provider}]")


def test_destination_settings_form_validates_inline_and_saves(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/destinations")
    form = open_settings(page, "instagram")
    form.get_by_label("Login type").select_option("facebook")
    form.get_by_label("Public video folder address (https)").fill("http://example.com/clips")
    form.get_by_role("button", name="Save settings").click()
    field = form.locator(".field").filter(has_text="Public video folder address")
    expect(field.locator(".error")).to_contain_text("https://")
    expect(page.locator("#toasts")).not_to_contain_text("Settings saved")
    form.get_by_label("Public video folder address (https)").fill("https://example.com/clips")
    form.get_by_label("Also show the Reel in the profile feed").select_option("false")
    form.get_by_role("button", name="Save settings").click()
    expect(page.locator("#toasts")).to_contain_text("Settings saved")
    saved = page.request.get(seeded_server.url + "/api/destinations/3").json()["settings"]
    assert saved == {"login": "facebook", "video_base_url": "https://example.com/clips",
                     "share_to_feed": False}
    page.reload()
    form = open_settings(page, "instagram")
    expect(form.get_by_label("Login type")).to_have_value("facebook")
    expect(form.get_by_label("Public video folder address (https)")).to_have_value("https://example.com/clips")
    # YouTube and TikTok have their own fields; an empty field clears the setting
    yt = open_settings(page, "youtube")
    yt.get_by_label("Who can watch uploads").select_option("unlisted")
    yt.get_by_role("button", name="Save settings").click()
    expect(page.locator("#toasts")).to_contain_text("Settings saved")
    yt_saved = page.request.get(seeded_server.url + "/api/destinations/1").json()["settings"]
    assert yt_saved == {"privacy": "unlisted"}
    tiktok = open_settings(page, "tiktok")
    expect(tiktok.get_by_label("How clips are sent")).to_be_visible()
    assert not page.js_errors


def test_confirm_posted_takes_an_optional_post_address(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips/5")
    page.get_by_role("button", name="Confirm posted").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Post address (optional)").fill("not a link")
    dialog.get_by_role("button", name="Mark as posted").click()
    expect(dialog.locator(".error")).to_contain_text("https://")
    dialog.get_by_label("Post address (optional)").fill("https://example.com/p/42")
    dialog.get_by_role("button", name="Mark as posted").click()
    expect(page.locator("#toasts")).to_contain_text("Marked as posted")
    expect(page.get_by_role("link", name="Open post")).to_have_attribute("href", "https://example.com/p/42")
    assert not page.js_errors


def test_clip_editor_sends_only_the_changed_preset_values(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips/2")
    box = page.locator("#clip-look")
    expect(box.get_by_label("Video quality (CRF)")).to_have_value("21")
    box.get_by_label("Video quality (CRF)").fill("18")
    box.get_by_label("Caption position").select_option("8")
    box.get_by_label("Show the title over the start of the clip").uncheck()
    page.get_by_role("button", name="Render again").click()
    expect(page.locator(".title-row")).to_contain_text("Rendering")
    ov = page.request.get(seeded_server.url + "/api/clips/2").json()["overrides"]
    assert ov == {"encode": {"render_crf": 18}, "captions": {"alignment": 8}, "title": {"duration_s": 0}}
    # a value outside the preset's range is refused at the field
    page.goto(seeded_server.url + "/#/clips/1")
    page.get_by_label("Video quality (CRF)").fill("99")
    page.get_by_role("button", name="Render again").click()
    expect(page.locator("#render-err")).to_contain_text("less than or equal to 51")


def _without_final(route):
    """The real clip view, as it looks before its video is rendered: no finished file in any language."""
    response = route.fetch()
    body = response.json()
    body["media"]["final"] = {}
    route.fulfill(response=response, json=body)


def test_a_clip_without_a_finished_video_requests_none_and_offers_no_post(seeded_server, make_page):
    page = make_page()
    finals = []
    page.on("request", lambda r: "/media/final" in r.url and finals.append(r.url))
    page.route("**/api/clips/2", _without_final)
    page.goto(seeded_server.url + "/#/clips/2")
    expect(page.get_by_text("Where it was posted")).to_be_visible()
    expect(page.get_by_text("Also post to:")).to_have_count(0)
    expect(page.get_by_role("button", name="Post anyway")).to_have_count(0)
    expect(page.locator("#clip-editor video")).to_have_attribute("src", "/api/clips/2/media/preview")
    assert finals == []


def test_a_render_that_finishes_while_the_clip_is_open_shows_its_video_and_posting(seeded_server, make_page):
    page = make_page()
    page.route("**/api/clips/2", _without_final)
    page.goto(seeded_server.url + "/#/clips/2")
    expect(page.get_by_text("Where it was posted")).to_be_visible()
    expect(page.get_by_text("Also post to:")).to_have_count(0)
    finals = []
    page.on("request", lambda r: "/media/final" in r.url and finals.append(r.url))
    page.unroute("**/api/clips/2")  # the render finished: the next poll sees the video
    expect(page.get_by_text("Also post to:")).to_be_visible(timeout=12000)
    assert finals and finals[0].endswith("/api/clips/2/media/final?lang=en")  # (this fake serves no files)


@pytest.mark.parametrize("final", [None, "/api/clips/2/media/final?lang=es"])
def test_clip_without_preview_or_original_final(seeded_server, make_page, final):
    page = make_page()

    def clip_media(route):
        response = route.fetch()
        body = response.json()
        body["media"].update(preview=None, final={"es": final} if final else {})
        route.fulfill(response=response, json=body)

    page.route("**/api/clips/2", clip_media)
    page.goto(seeded_server.url + "/#/clips/2")
    expect(page.get_by_text("Where it was posted")).to_be_visible()
    video = page.locator("#clip-editor video")
    if final:
        expect(video).to_have_attribute("src", final)
    else:
        expect(page.get_by_text("The video is not ready yet.", exact=False)).to_be_visible()
        assert video.get_attribute("src") is None
    expect(page.get_by_text("Also post to:")).to_have_count(0)


@pytest.mark.parametrize("save_result", ["refused", "previous-format"])
def test_channel_format_save_failure_keeps_the_edit_and_can_retry(seeded_server, make_page, save_result):
    page = make_page()
    path = f"{seeded_server.url}/api/channels/1"
    original = page.request.put(path, data={"format_id": "gta-vi"}).json()
    page.goto(seeded_server.url + "/#/channels/1")
    fmt = page.get_by_label("Format", exact=True)
    fmt.select_option("")
    save = page.get_by_role("button", name="Save changes")
    if save_result == "refused":
        page.route(path, lambda route: route.fulfill(status=503, json={"detail": "Temporarily unavailable"}))
        message = "Temporarily unavailable"
    else:
        page.route(path, lambda route: route.fulfill(json=original))
        message = "Format was not saved"
    save.click()
    expect(page.locator("#channel-form > [role=alert]")).to_contain_text(message)
    expect(page.locator("#toasts")).not_to_contain_text("Channel saved.")
    expect(fmt).to_have_value("")
    assert page.request.get(path).json()["format_id"] == "gta-vi"
    page.unroute(path)
    save.click()
    expect(page.locator("#toasts")).to_contain_text("Channel saved.")
    assert page.request.get(path).json()["format_id"] is None


@pytest.mark.parametrize("catalog_state", ["unavailable", "missing-saved-format"])
def test_channel_preserves_format_and_explains_incomplete_choices(seeded_server, make_page, catalog_state):
    page = make_page()
    path = f"{seeded_server.url}/api/channels/1"
    page.request.put(path, data={"format_id": "gta-vi"})
    if catalog_state == "unavailable":
        page.route("**/api/formats", lambda route: route.fulfill(status=503, body="Unavailable"))
        message = "Could not load the format list. Reopen this form to try again."
    else:
        page.route("**/api/formats", lambda route: route.fulfill(json={"formats": []}))
        message = "The saved format gta-vi is no longer listed. Keep it or choose another format."
    page.goto(seeded_server.url + "/#/channels/1")
    fmt = page.get_by_label("Format", exact=True)
    expect(fmt).to_have_value("gta-vi")
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    expect(fmt).to_have_accessible_description(re.compile(re.escape(message)))
    page.get_by_label("Name shown in the app (optional)").fill("Updated name")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#toasts")).to_contain_text("Channel saved.")
    after = page.request.get(path).json()
    assert after["format_id"] == "gta-vi" and after["display_name"] == "Updated name"
    page.unroute("**/api/formats")
    page.goto(seeded_server.url + "/#/channels/1")
    expect(fmt).to_have_value("gta-vi")
    expect(page.get_by_text(message, exact=True)).to_have_count(0)
