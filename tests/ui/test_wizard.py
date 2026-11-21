import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.ui
PERMISSION_Q = "Do you have permission to clip and post their streams?"


def state(srv):
    return json.loads(srv.state_file.read_text())


def next_q(page, wait=True):
    """Press Next and wait until the next question (or the review page) is shown."""
    before = page.locator(".wizard").inner_text()
    page.get_by_role("button", name="Next").or_(page.get_by_role("button", name="Review answers")).click()
    if wait:
        page.wait_for_function("t => document.querySelector('.wizard').innerText !== t", arg=before)


def test_wizard_full_flow_with_conditions_resume_and_review(fresh_server, make_page):
    page = make_page()
    page.goto(fresh_server.url + "/")
    expect(page).to_have_url(f"{fresh_server.url}/#/setup")
    expect(page.get_by_role("heading", name="Set up Live Clipper")).to_be_visible()
    expect(page.get_by_text("Found on this computer")).to_be_visible()
    expect(page.locator(".sidebar")).to_be_hidden()

    # first question: one plain explanation under the title
    expect(page.get_by_role("heading", name="How will you use Live Clipper?")).to_be_visible()
    expect(page.locator(".question > p.help")).to_have_text(
        "If you clip other streamers, the next question asks you to confirm their permission.")
    expect(page.get_by_text("Question 1 of")).to_be_visible()

    # show_if: the permission question appears only for "permission"
    page.get_by_label("Clipping other streamers, with their permission").check()
    next_q(page)
    expect(page.get_by_role("heading", name=PERMISSION_Q)).to_be_visible()
    next_q(page)  # default is No: blocked client-side (error text appears)
    expect(page.locator(".question .error")).to_have_text("This needs a yes to continue.")
    page.get_by_label("Yes").check()
    next_q(page)
    expect(page.get_by_role("heading", name="Which platforms do you want to watch?")).to_be_visible()
    assert state(fresh_server)["answers"]["setup.permission_confirmed"] is True

    # Back works, and a reload resumes at the first unanswered question
    page.get_by_role("button", name="Back").click()
    expect(page.get_by_role("heading", name=PERMISSION_Q)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Which platforms do you want to watch?")).to_be_visible()

    page.get_by_label("Kick").check()
    next_q(page)  # storage.dir
    next_q(page)  # storage.cap_gb
    cap = page.get_by_label("How much disk space may Live Clipper use (GB)?")
    expect(cap).to_have_value("50")  # from the detected hardware: a quarter of the free space, at most 50
    cap.fill("0")
    next_q(page)  # error text changes the page
    expect(page.locator(".question .error")).to_contain_text("from 1 to 4096")
    cap.fill("200")
    next_q(page)

    # answer the rest; turn on the webhook to reveal its conditional questions
    seen = []
    for _ in range(30):
        h2 = page.locator(".question h2")
        if not h2.count():
            break
        title = h2.inner_text()
        seen.append(title)
        if title.startswith("How should Live Clipper tell you"):
            page.get_by_label("A webhook (ntfy, Discord or other)").check()
        if title == "What is the webhook address?":
            page.locator(".question input").fill("https://ntfy.example.com/my-topic")
        next_q(page)
    assert "What is the webhook address?" in seen
    assert "Should clips be posted automatically?" not in seen  # posting.destinations is "none"

    expect(page.get_by_role("heading", name="Check your answers")).to_be_visible()
    expect(page.locator(".review")).to_contain_text("Kick")
    page.locator(".review dd").filter(has_text="200").get_by_role("button", name="Change").click()
    expect(page.get_by_label("How much disk space may Live Clipper use (GB)?")).to_have_value("200")

    page.goto(fresh_server.url + "/#/setup/review")
    page.get_by_role("button", name="Finish setup").click()
    expect(page).to_have_url(f"{fresh_server.url}/#/dashboard")
    s = state(fresh_server)
    assert s["app_state"]["setup.completed"] is True
    assert s["answers"]["storage.cap_gb"] == 200
    # the tour starts right after the wizard on first use
    expect(page.get_by_role("dialog")).to_contain_text("Welcome to Live Clipper")
    assert not page.js_errors
