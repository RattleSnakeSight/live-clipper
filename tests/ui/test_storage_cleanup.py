"""Clean up now in Settings deletes only what the customer confirmed in the dialog.

Synthetic clips and the mock API exercise the same preview and confirmation contract as maintenance.py.
Between preview and confirmation the fixture can change a clip, add a hold, expire the plan or restart.
"""

from __future__ import annotations

import json

import pytest
from conftest import DONE, MockServer
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

MB = 1048576


@pytest.fixture
def server(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps(DONE))
    srv = MockServer(tmp_path / "state.json", seed=False)
    srv.start()
    yield srv
    srv.stop()


def open_dialog(make_page, srv, **before):
    """Open the cleanup confirmation and capture its requests. Apply `before` before the preview."""
    page = make_page()
    posted = []
    page.on("request", lambda r: posted.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/storage/cleanup") else None)
    page.goto(f"{srv.url}/#/settings")
    expect(page.locator("main h1")).to_be_visible()
    if before:
        mock(page, srv, **before)
    page.get_by_role("button", name="Clean up now").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("2 fully posted clips")
    expect(dialog).to_contain_text("Only these are deleted")
    return page, dialog, posted


def mock(page, srv, **change):
    return page.request.put(srv.url + "/api/_mock/cleanup", data=change).json()


def left(page, srv):
    return sorted(i["path"] for i in page.request.get(srv.url + "/api/_mock/cleanup").json()["items"])


def used(page):
    return page.locator("#settings-storage dl dd").nth(1)


def test_only_the_confirmed_items_are_deleted_and_the_figures_refresh(server, make_page):
    big = {"action": "delete_clip", "path": "clips/105", "clip_id": "105", "bytes": 3072 * MB,
           "reason": "you asked to clean up: rejected"}
    page, dialog, posted = open_dialog(make_page, server, add=[big])
    expect(dialog).to_contain_text("2 rejected clips")
    expect(used(page)).to_contain_text("38 GB of")
    mock(page, server, change=["clips/102"],                     # changed since the preview
         add=[{"action": "delete_clip", "path": "clips/104", "clip_id": "104", "bytes": 70 * MB,
               "reason": "you asked to clean up: posted"}])        # appeared since the preview
    dialog.get_by_role("button", name="Delete these files").click()
    said = page.locator("#settings-storage [role=status]").filter(has_text="Freed")
    expect(said).to_contain_text("Freed 3.3 GB: 1 fully posted clip (90 MB)")
    expect(said).to_contain_text("2 rejected clips")
    expect(said).to_contain_text("left in place: it changed since the preview")
    expect(said).to_contain_text("1 item that appeared after your preview was not part of what you confirmed")
    assert left(page, server) == ["clips/102", "clips/104"]
    expect(used(page)).to_contain_text("35 GB of")                # drawn again from GET /storage
    preview, confirm = posted
    assert preview == {"dry_run": True}
    assert confirm["dry_run"] is False and confirm["plan_id"]       # names the preview, never a bare run
    assert not page.js_errors


def test_an_item_held_after_the_preview_is_kept_and_named(server, make_page):
    page, dialog, _ = open_dialog(make_page, server)
    mock(page, server, hold=["clips/103"])
    dialog.get_by_role("button", name="Delete these files").click()
    said = page.locator("#settings-storage [role=status]").filter(has_text="Freed")
    expect(said).to_contain_text("an upload of this clip may still need its files")
    assert left(page, server) == ["clips/103"]


@pytest.mark.parametrize("how, message", [("expire", "too old to confirm"), ("restart", "no longer known")])
def test_a_confirmation_that_can_no_longer_run_deletes_nothing_and_says_so(server, make_page, how, message):
    page, dialog, _ = open_dialog(make_page, server)
    before = left(page, server)
    if how == "expire":
        mock(page, server, expire=True)
    else:
        server.restart()                                           # previews are not kept across a restart
    with page.expect_response("**/api/storage/cleanup") as resp:
        dialog.get_by_role("button", name="Delete these files").click()
    assert resp.value.status == 409
    expect(page.get_by_role("status", name="Cleanup result")).to_contain_text(message)
    assert left(page, server) == before
    expect(used(page)).to_contain_text("38 GB of")


def test_cancel_deletes_nothing(server, make_page):
    page, dialog, posted = open_dialog(make_page, server)
    before = left(page, server)
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    assert left(page, server) == before and [p.get("dry_run", True) for p in posted] == [True]
