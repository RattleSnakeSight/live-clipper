import json

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
    page.get_by_label("Only when the topic matches").check()
    page.get_by_label("Test destination (nothing is posted)").check()
    page.get_by_role("button", name="Add channel").click()
    expect(page).to_have_url(f"{empty_server.url}/#/channels")
    row = page.locator("#channel-list li").filter(has_text="streamer_beta")
    expect(row).to_contain_text("Kick")
    expect(row).to_contain_text("only when the topic matches")

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
    expect(beta).to_contain_text("Status from the official Kick API")
    beta.get_by_role("button", name="Stop watching").click()
    expect(beta).to_contain_text("Offline")
    expect(beta.get_by_role("button", name="Start watching")).to_be_visible()
    assert not page.js_errors


def test_manual_post_from_review_queue(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips")
    queue = page.locator("#review-queue")
    expect(queue).to_contain_text("Waiting for your approval")
    row = queue.locator("li").filter(has_text="Clip 1")
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


def test_clip_override_editor_renders_again(seeded_server, make_page):
    page = make_page()
    page.goto(seeded_server.url + "/#/clips/2")
    page.get_by_label("Start (seconds)").fill("5")
    page.get_by_label("End (seconds)").fill("1")
    page.get_by_role("button", name="Render again").click()
    expect(page.locator("#render-err")).to_have_text("The end must be after the start.")
    page.get_by_label("End (seconds)").fill("20")
    page.get_by_label("Title", exact=True).fill("Harbour jump, fixed title")
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
    expect(pre).to_have_value("9.5")
    pre.fill("-3")
    page.get_by_role("button", name="Save").first.click()
    expect(page.locator('[data-path="window.pre"] .error')).to_contain_text("0 or more")
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
    page.locator("#rule-editor").get_by_label("Platform").select_option("twitch")
    page.get_by_label("Change settings").fill("window.post = 20")
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
