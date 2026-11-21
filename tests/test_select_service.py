"""HTTP API of the select service: formats and rules CRUD, import/export, reset, reorder, effective-value
preview."""
from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from live_clipper import formats, registry
from live_clipper.paths import get_paths
from live_clipper.select_service import SelectService
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context, load_services

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)


@pytest.fixture
def client():
    paths = get_paths()
    ctx = build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))
    svc = SelectService()
    ctx.services.append(svc)
    seen = []
    ctx.bus.subscribe("select.*", lambda topic, payload: seen.append((topic, payload)))
    with TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        c.seen = seen
        yield c
    formats.set_active(None)


def test_registered_in_registry():
    assert "live_clipper.select_service:SelectService" in registry.SERVICES
    assert [s.name for s in load_services(["live_clipper.select_service:SelectService"])] == ["select"]


def test_formats_crud_export_import_reset(client):
    r = client.get("/api/formats").json()
    assert r["active"] == "gta-vi" and [f["id"] for f in r["formats"]] == ["gta-vi"]
    assert client.get("/api/status").json()["services"]["select"]["active_format"] == "gta-vi"
    exp = client.get("/api/formats/gta-vi/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    doc = exp.json()
    doc.update(id="mine", name="Mine")
    doc["scoring"]["min_score"] = 1.5
    assert client.post("/api/formats/import", content=json.dumps(doc)).status_code == 201
    assert client.post("/api/formats/import", content=json.dumps(doc)).status_code == 409
    assert client.post("/api/formats/import?overwrite=true", content=json.dumps(doc)).status_code == 201
    assert client.get("/api/formats/mine").json()["scoring"]["min_score"] == 1.5
    r = client.put("/api/formats/mine", json={"name": "Mine v2", "window": {"pre": 20}})
    assert r.status_code == 200 and r.json()["window"] == {"pre": 20.0, "post": 12.0}
    assert client.post("/api/formats/mine/activate").json() == {"active": "mine"}
    assert formats.active().id == "mine"                                # the core follows the active format
    dup = client.post("/api/formats/mine/duplicate", json={"id": "mine-b"})
    assert dup.status_code == 201 and dup.json()["name"] == "Mine v2 (copy)"
    assert client.delete("/api/formats/mine").json() == {"deleted": "mine"}
    assert formats.active().id == "gta-vi"
    assert client.get("/api/formats/mine").status_code == 404
    client.put("/api/formats/gta-vi", json={"scoring": {"min_score": 9}})
    assert client.post("/api/formats/gta-vi/reset").json()["scoring"]["min_score"] == 3.0
    assert client.delete("/api/formats/gta-vi").status_code == 409
    assert ("select.formats_changed", {"what": "active", "id": "mine"}) in client.seen


def test_validation_errors_are_422_with_locations(client):
    r = client.post("/api/formats/import", content=b"{oops")
    assert r.status_code == 422 and "line 1" in r.json()["detail"]["message"]
    r = client.post("/api/formats", json={"id": "x", "name": "x", "keywords": {"strong": "("}, "bogus": 1})
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["detail"]["errors"]}
    assert locs == {"keywords.strong", "bogus"}
    assert client.put("/api/formats/gta-vi", json={"id": "other"}).status_code == 422


def test_rules_crud_reorder_and_preview(client):
    a = {"id": "kick-five", "name": "Kick needs 5x", "conditions": {"platforms": ["kick"]},
         "actions": {"threshold": 5}}
    b = {"id": "beta-hold", "name": "Hold beta", "streamers": {"channels": ["streamer_beta"]},
         "actions": {"decision": "hold", "threshold": 8}}
    assert client.post("/api/rules", json=a).status_code == 201
    assert client.post("/api/rules", json=b).status_code == 201
    assert client.post("/api/rules", json=b).status_code == 409
    assert [x["id"] for x in client.get("/api/rules").json()["rules"]] == ["kick-five", "beta-hold"]
    body = {"channel": "streamer_beta", "context": {"platform": "kick"},
            "clip": {"override": {"window.pre": 30}}}
    eff = client.post("/api/formats/preview-effective", json=body).json()
    assert eff["values"]["scoring.min_score"] == 8.0 and eff["values"]["output.decision"] == "hold"
    assert eff["trail"]["window.pre"]["set_by"] == "override"
    assert client.post("/api/rules/reorder", json={"ids": ["beta-hold", "kick-five"]}).status_code == 200
    eff = client.post("/api/formats/preview-effective", json=body).json()
    assert eff["values"]["scoring.min_score"] == 5.0
    assert client.post("/api/rules/reorder", json={"ids": ["beta-hold"]}).status_code == 422
    r = client.put("/api/rules/kick-five", json={**a, "enabled": False})
    assert r.status_code == 200 and client.get("/api/rules/kick-five").json()["enabled"] is False
    assert client.delete("/api/rules/kick-five").json() == {"deleted": "kick-five"}
    assert client.get("/api/rules/kick-five").status_code == 404
    bad = client.post("/api/rules", json={"id": "r", "name": "r", "actions": {"window_post": -1}})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"][0]["loc"] == "actions.window_post"
    r = client.post("/api/formats/preview-effective", json={"clip": {"override": {"nope": 1}}})
    assert r.status_code == 422


def test_schema_endpoints_describe_every_field(client):
    for path in ("/api/formats/schema", "/api/rules/schema"):
        schema = client.get(path).json()
        assert schema["properties"] and schema["$defs"]
    fmt = client.get("/api/formats/schema").json()
    assert fmt["properties"]["scoring"]["description"]
    assert fmt["$defs"]["Scoring"]["properties"]["min_score"]["description"]
