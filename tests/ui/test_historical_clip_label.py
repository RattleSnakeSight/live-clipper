"""The clip's retained source label is visible even after its channel is removed."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from live_clipper.db import Database
from tests.test_channel_identity import services

pytestmark = pytest.mark.ui


def test_kept_clip_names_original_creator_in_library_detail_and_dashboard(tmp_path, seeded_server, make_page):
    db = Database(tmp_path / "channels.sqlite")
    db.migrate()
    api, engine = services(tmp_path, db)
    with api:
        params = {"url": "twitch.tv/oldsource", "display_name": "Original Creator", "enabled": False}
        cid = api.post("/api/channels", json=params).json()["id"]
        clip = engine.store.create_clip(cid, "fixture", "manual", {}, title="Historical clip")
        assert api.delete(f"/api/channels/{cid}").status_code == 204
        replacement = api.post("/api/channels", json={**params, "display_name": "Current Creator"})
        assert replacement.status_code == 201

        page = make_page()
        def api_get(route):
            url = urlsplit(route.request.url)
            result = api.get(url.path, params=url.query)
            route.fulfill(status=result.status_code, json=result.json())

        page.route("**/api/channels", api_get)
        page.route("**/api/clips?*", api_get)
        page.route(f"**/api/clips/{clip}", api_get)
        page.goto(seeded_server.url + "/#/clips")
        library = page.locator("#clip-library")
        expect(library.get_by_role("cell", name="Original Creator")).to_be_visible()
        library.get_by_role("link", name="Historical clip").press("Enter")
        expect(page.locator("main .title-row")).to_contain_text("Original Creator")
        page.locator("#sidebar a[data-route='dashboard']").press("Enter")
        expect(page.locator("#rail")).to_contain_text("Original Creator")
