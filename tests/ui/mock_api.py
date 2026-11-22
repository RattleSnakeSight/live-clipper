"""Fake of the app's HTTP API (same routes and shapes as the real services) that serves live_clipper/web.

Used by the fast Playwright UI tests with seeded demo data; tests/ui/test_real_app.py drives the real app. To stay
in step with the real API the fake reuses the real pieces that need no network or media: the setup questions
(`setup_service.build_questions`), channel URL parsing and source descriptions (monitor), the selection
formats/rules router (`SelectService` over a throwaway SQLite file), the editing presets (`PresetStore`) and the
destination requirements (publish). Channels, clips, deliveries and connections are in-memory fixtures.

App state and setup answers persist to a JSON file, so a test can stop the server and start a new one on the same
file to simulate an app restart or update. Everything else is rebuilt from fixtures on each start.

Run by hand: python -m tests.ui.mock_api --port 8799 --seed
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

import live_clipper
from live_clipper import formats as F
from live_clipper import presets as P
from live_clipper.agent_setup import AGENTS, TARGETS, spec_view
from live_clipper.db import Database
from live_clipper.monitor_service import parse_channel_url
from live_clipper.presets import SHIPPED, PresetStore
from live_clipper.publish.requirements import requirements_for
from live_clipper.publish.service import SETTING_KEYS, check_settings
from live_clipper.select_service import SelectService
from live_clipper.setup_service import build_questions
from live_clipper.sources import OFFICIAL, PLATFORMS, STREAM_PROBE, describe
from live_clipper.sources import oauth as source_oauth

WEB_DIR = Path(live_clipper.__file__).parent / "web"
NOW = time.time()

# --- setup questions (the real list for this fake computer) -------------------------------------------------

HARDWARE = {"cpu": {"model": "Example 8-core processor", "cores": 8}, "ram_bytes": 16 * 1024**3,
            "disk": {"path": "/data", "free_bytes": 400 * 1024**3}, "gpu": {"kind": "none", "name": None, "vram_mb": None},
            "ffmpeg": {"found": True, "path": "/usr/bin/ffmpeg", "version": "7.0"},
            "ffprobe": {"found": True, "path": "/usr/bin/ffprobe", "version": "7.0"},
            "streamlink": {"found": False, "path": None, "version": None}, "os": "Linux 6.8", "python": "3.13.0"}
QUESTIONS = build_questions(HARDWARE, "/data/live-clipper/media")
QIDX = {q["id"]: q for q in QUESTIONS}


def _cond_ok(c: dict, a: dict) -> bool:
    v = a.get(c["id"])
    if "equals" in c:
        return v == c["equals"]
    if "not_equals" in c:
        return v != c["not_equals"]
    if "in" in c:
        return v in c["in"]
    if "contains_any" in c:
        return bool(set(v or []) & set(c["contains_any"]))
    if "not_empty" in c:
        return bool(v)
    return True


def _visible(q: dict, a: dict) -> bool:
    return all(_cond_ok(c, a) for c in q["show_if"])


def _validate(q: dict, v: Any) -> str | None:
    """setup_service.validate_value without touching the file system for paths."""
    t, val = q["type"], q["validation"]
    opts = {o["value"] for o in q["options"]}
    if t == "boolean":
        if not isinstance(v, bool):
            return "Answer yes or no."
        return "This needs a yes." if val.get("must_be") and v is not True else None
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, int | float):
            return "Enter a number."
        if not val["min"] <= v <= val["max"]:
            return f"Enter a number from {val['min']} to {val['max']}."
        return None
    if t == "select":
        return None if v in opts else "Pick one of the listed options."
    if t == "multiselect":
        if not isinstance(v, list) or any(x not in opts for x in v):
            return "Pick from the listed options."
        if "none" in v and len(v) > 1:
            return "'Nowhere' cannot be combined with other choices."
        return "Pick at least one." if len(v) < val.get("min_items", 0) else None
    if t == "url":
        if v and not re.match(r"^https?://[^/\s]+", str(v)):
            return "Enter a full address starting with http:// or https://."
        return None
    if t == "path":
        return None if str(v).strip() else "Enter a full folder path."
    return None


# --- fixtures ---------------------------------------------------------------------------------------------

GUIDE = {
    "first-run": ("First run", "# First run\n\nThe setup asks a few questions. You can change every answer later "
                  "in **Settings**.\n\n## What happens next\n\n1. Add a channel.\n2. Connect a source.\n"
                  "3. Wait for a clip.\n\nSee [Monitoring streams](monitoring.md) for details.\n\n"
                  "<script>alert('x')</script>\n\n| Step | Where |\n|---|---|\n| Add channel | Channels |\n"),
    "monitoring": ("Monitoring streams", "# Monitoring streams\n\nEach channel shows its state on the dashboard.\n\n"
                   "- `waiting`: watching for the stream to start\n- `recording`: capturing now\n\n"
                   "[Unsafe link](javascript:alert(1)) stays plain text.\n"),
    "updating": ("Updating", "# Updating\n\nYour data folder is kept when you update.\n\n```\npip install --upgrade live-clipper\n```\n"),
}
PROVIDERS = ("youtube", "tiktok", "instagram", "test")
TEST_LABEL = requirements_for("test")["label"]


def _destinations(port: int) -> dict:
    out = {}
    for i, prov in enumerate(PROVIDERS, start=1):
        req = requirements_for(prov, port)
        per_day = (req.get("quotas") or {}).get("per_day")
        out[i] = {"id": i, "provider": prov, "label": req["label"], "test": prov == "test", "account": None,
                  "status": "disconnected", "status_detail": "", "enabled": False, "rights_acknowledged": False,
                  "rights_ack_at": None, "credentials_set": [], "app_credentials_set": [], "settings": {},
                  "requirements": req, "quota": {"used_today": 0, "limit": per_day} if per_day else None}
    return out


def _dest_view(d: dict) -> dict:
    if d["test"]:
        d["setup"] = {"state": "active" if d["enabled"] else "ready_to_enable", "steps": []}
        return d
    has = bool(d["app_credentials_set"]) or d["status"] == "connected"
    steps = [{"id": "app_credentials", "done": has}, {"id": "connect", "done": d["status"] == "connected"},
             {"id": "rights_acknowledgement", "done": d["rights_acknowledged"]}, {"id": "enable", "done": d["enabled"]}]
    state = ("needs_credentials" if not has else "needs_connect" if d["status"] != "connected"
             else "needs_acknowledgement" if not d["rights_acknowledged"] else "ready_to_enable" if not d["enabled"]
             else "active")
    d["setup"] = {"state": state, "steps": steps}
    return d


def _seed_channels() -> dict:
    """monitor_service.channel_view shapes."""
    rows = [
        (1, "twitch", "streamer_alpha", "recording", "", "", "", OFFICIAL["twitch"], True),
        (2, "kick", "streamer_beta", "needs_action", "Kick public API livestreams failed: credentials rejected",
         "Reconnect Kick on the Sources page", "#/sources/kick", STREAM_PROBE, True),
        (3, "youtube", "@streamer_gamma", "waiting", "waiting for the stream to start", "", "", STREAM_PROBE, True),
        (4, "twitch", "streamer_delta", "disabled", "not monitoring", "", "", OFFICIAL["twitch"], False),
    ]
    out = {}
    for id_, p, login, st, detail, fix, link, src, armed in rows:
        url = {"twitch": f"https://www.twitch.tv/{login}", "kick": f"https://kick.com/{login}",
               "youtube": f"https://www.youtube.com/{login}"}[p]
        out[id_] = {"id": id_, "url": url, "platform": p, "login": login, "display_name": login,
                    "watch_mode": "ungated", "poll_seconds": 120, "format_id": None, "publish_platforms": [],
                    "enabled": armed, "state": st, "detail": detail, "fix": fix, "fix_link": link,
                    "status_source": src, "gate_change_pending": False}
    return out


SRT = "1\n00:00:00,000 --> 00:00:02,500\nNo way he made that jump\n"


def _seed_clips() -> tuple[dict, dict]:
    """edit_service clip views: the shared store row plus `media` and `rendering`."""
    kinds = [("ready", "held"), ("ready", "qualified"), ("ready", "qualified"), ("failed", None), ("rendering", None),
             ("ready", "held")]
    clips, deliveries = {}, {}
    for i, (status, decision) in enumerate(kinds, start=1):
        title = f"Chase ends in a harbour crash {i}"
        sel = {"en": {"decision": decision, "reasons": ["gameplay not confirmed"] if decision == "held" else [],
                      "format_id": "gta-vi", "matched_rules": [], "at": NOW}} if decision else {}
        files = {"duration": 28.0 + i, "trim_in": 18.0, "source": "source.ts", "preview": "preview.mp4"}
        if status == "ready":
            files["final"] = {"en": "final_en.mp4"}
        base = f"/api/clips/{i}/media"
        clips[i] = {"id": i, "channel_id": (i % 3) + 1, "dedupe_key": f"k{i}", "created_at": NOW - i * 900,
                    "updated_at": NOW - i * 800, "status": status, "kind": "chat_spike",
                    "trigger": {"reason": "Chat rate jumped to 4 times its usual level.", "format_id": "gta-vi",
                                "selection": sel},
                    "title": "", "source_lang": "en",
                    "captions": {"en": {"status": "ready", "title": title, "title_base": title, "srt": "captions/en.srt",
                                        "final_status": "ready" if status == "ready" else "pending"}},
                    "layout": {}, "timings": {}, "files": files, "error": "render failed: ffmpeg exited 1" if status == "failed" else "",
                    "preset_id": "default", "overrides": {},
                    "media": {"preview": f"{base}/preview", "thumbnail": f"{base}/thumbnail",
                              "final": {"en": f"{base}/final?lang=en"} if status == "ready" else {}},
                    "rendering": status == "rendering"}
        dstate = {1: "held", 2: "published", 3: "queued", 6: "held"}.get(i)
        if i == 4:
            dstate = "failed"
        for dest_id in ((4, 1) if dstate == "held" else (4,) if dstate else ()):
            n = len(deliveries) + 1
            deliveries[n] = {"id": n, "clip_id": i, "destination_id": dest_id, "language": "en", "status": dstate,
                             "mode": "manual", "attempts": 1 if dstate in ("published", "failed") else 0,
                             "outcome": "public" if dstate == "published" else None,
                             "url": "https://example.com/clip" if dstate == "published" else None, "post_id": None,
                             "last_error": "Upload refused: quota used up for today." if dstate == "failed" else None,
                             "next_attempt_at": None, "created_at": NOW - i * 800, "updated_at": NOW - i * 700,
                             "history": []}
    # a delivery whose upload may or may not have landed: the user checks the account and confirms
    n = len(deliveries) + 1
    deliveries[n] = {"id": n, "clip_id": 5, "destination_id": 1, "language": "en", "status": "needs_confirmation",
                     "mode": "manual", "attempts": 1, "outcome": None, "url": None, "post_id": None,
                     "last_error": None, "next_attempt_at": None, "created_at": NOW - 4000, "updated_at": NOW - 3900,
                     "history": []}
    return clips, deliveries


SEED_RULE = {"id": "hold-long-kick", "name": "Hold long Kick clips", "enabled": True,
             "conditions": {"platforms": ["kick"]}, "duration": {"min_s": 40},
             "actions": {"decision": "hold", "window_post": 15}}


# --- app --------------------------------------------------------------------------------------------------

def create_app(state_file: Path, seed: bool = False, port: int = 8799) -> FastAPI:
    state_file = Path(state_file)
    store: dict = json.loads(state_file.read_text()) if state_file.exists() else {}
    store.setdefault("app_state", {})
    store.setdefault("answers", {})

    def save() -> None:  # atomic, so readers never see a half-written file
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(store))
        for _ in range(50):  # Windows refuses the replace while a reader has the file open
            try:
                tmp.replace(state_file)
                return
            except PermissionError:
                time.sleep(0.02)
        tmp.replace(state_file)

    # formats, rules and presets: the real code over a fresh database for every start
    sql = Database(state_file.parent / f"mock-{port}-{time.time_ns()}.sqlite3")
    sql.migrate()
    F.seed(sql)
    presets = PresetStore(sql)
    presets.seed()
    if seed:
        F.create_rule(sql, SEED_RULE)
    select = SelectService()
    select.ctx = SimpleNamespace(db=sql, bus=SimpleNamespace(publish=lambda *a: None))
    select._sync_active = lambda: None  # the selection core's active format is process-global; leave it alone

    db: dict = {
        "channels": _seed_channels() if seed else {},
        "destinations": _destinations(port),
        "mode": "manual" if seed else None,
        "sources": {"twitch": {"client_id": "app-id", "client_secret": "app-secret", "access_token": "tok"} if seed else {},
                    "kick": {}, "youtube": {}},
        "pending": {},
        "notifications": [{"id": 1, "ts": NOW - 60, "kind": "notify", "title": "Clip ready",
                           "body": "streamer_alpha: Chase ends in a harbour crash 1"}] if seed else [],
    }
    db["clips"], db["deliveries"] = _seed_clips() if seed else ({}, {})
    if seed:
        db["destinations"][1].update(status="connected", account="@example_clips", app_credentials_set=["client_id"],
                                     rights_acknowledged=True, rights_ack_at=NOW, enabled=True)
        db["destinations"][4].update(status="connected", account="test", enabled=True)
    ids = {"channel": 100, "delivery": 100}

    app = FastAPI()

    def nf(what: str):
        raise HTTPException(404, f"{what} not found")

    # core
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": live_clipper.__version__, "uptime": 12.5, "pid": 1}

    @app.get("/api/status")
    def status():
        armed = [c for c in db["channels"].values() if c["enabled"]]
        return {"version": live_clipper.__version__, "uptime": 12.5, "pid": 1, "services": {
            "monitor": {"state": "running", "channels": len(db["channels"]), "armed": len(armed), "recording": 1},
            "publish": {"started": True, "mode": mode(), "eligible_destinations": ["youtube"], "deliveries": {}},
            "maintenance": {"state": "healthy", "paused": False}}}

    @app.get("/api/app-state/{key}")
    def get_state(key: str):
        return {"key": key, "value": store["app_state"].get(key)}

    @app.put("/api/app-state/{key}")
    def put_state(key: str, body: dict = Body(...)):
        store["app_state"][key] = body.get("value")
        save()
        return {"key": key, "value": body.get("value")}

    # setup (ops)
    @app.get("/api/setup/hardware")
    def hardware():
        return HARDWARE

    @app.get("/api/setup/questions")
    def questions():
        return {"questions": QUESTIONS}

    def answers() -> dict:  # posting.mode is the publish mode, as in setup_service.STORED_AS
        a = dict(store["answers"])
        if db["mode"] is not None:
            a["posting.mode"] = db["mode"]
        return a

    def setup_state():
        a = answers()
        eff = {q["id"]: a.get(q["id"], q["default"]) for q in QUESTIONS}
        shown = dict(a)
        if shown.get("notify.webhook_url"):
            shown["notify.webhook_url"] = re.sub(r"^(https?://[^/]+).*$", r"\1/...", shown["notify.webhook_url"])
        return {"completed": bool(store["app_state"].get("setup.completed")),
                "completed_at": store["app_state"].get("setup.completed_at"), "answers": shown,
                "remaining": [q["id"] for q in QUESTIONS if _visible(q, eff) and q["id"] not in a]}

    @app.get("/api/setup/state")
    def get_setup_state():
        return setup_state()

    def persist(clean: dict) -> None:
        for k, v in clean.items():
            if k == "posting.mode":
                db["mode"] = v
            store["answers"][k] = v
        save()

    @app.put("/api/setup/answers")
    def put_answers(body: dict = Body(...)):
        given = body.get("answers", body)
        errors = {}
        for k, v in given.items():
            if k not in QIDX:
                errors[k] = "Unknown question."
            elif err := _validate(QIDX[k], v):
                errors[k] = err
        if errors:
            raise HTTPException(422, {"errors": errors})
        persist(given)
        return setup_state()

    @app.post("/api/setup/complete")
    def complete():
        a = answers()
        eff = {q["id"]: a.get(q["id"], q["default"]) for q in QUESTIONS}
        missing, fill = {}, {}
        for q in QUESTIONS:
            if _visible(q, eff):
                err = _validate(q, eff[q["id"]])
                if not err and q["validation"].get("required") and q["type"] in ("url", "path") and not eff[q["id"]]:
                    err = "Needs an answer."
                if err:
                    missing[q["id"]] = err
                elif q["id"] not in a:
                    fill[q["id"]] = eff[q["id"]]
        if missing:
            raise HTTPException(422, {"errors": missing})
        persist(fill)
        store["app_state"]["setup.completed"] = True
        store["app_state"]["setup.completed_at"] = time.time()
        save()
        return setup_state()

    @app.get("/api/storage")
    def storage():
        a = {q["id"]: store["answers"].get(q["id"], q["default"]) for q in QUESTIONS}
        cap = a["storage.cap_gb"] * 1024**3
        return {"dir": a["storage.dir"], "usage_bytes": 38 * 1024**3, "cap_bytes": cap, "free_bytes": 400 * 1024**3,
                "over_cap": False, "breakdown": {"clips": 30 * 1024**3, "trash": 1024**3, "temporary": 2 * 1024**3,
                                                 "other": 5 * 1024**3},
                "protected_bytes": 4 * 1024**3, "retention_days": a["storage.retention_days"],
                "clean_first": a["storage.clean_first"], "rules": a["storage.cleanup_rules"],
                "health": {"state": "healthy", "reasons": []}, "next_cleanup": []}

    @app.post("/api/storage/cleanup")
    def cleanup(body: dict = Body(default={})):
        return {"dry_run": body.get("dry_run", True), "actions": [{"kind": "delete_temp", "path": "tmp/seg-001.ts",
                                                                   "bytes": 52428800}], "freed_bytes": 52428800}

    @app.get("/api/notifications")
    def notifications(since: int = 0):
        items = [n for n in db["notifications"] if n["id"] > since]
        return {"items": items, "latest": items[-1]["id"] if items else since}

    @app.get("/api/diagnostics")
    def diagnostics():
        return {"version": live_clipper.__version__, "uptime": 12.5, "services": status()["services"],
                "restarts": {"count": 1, "recent": [{"ts": NOW - 3600, "message": "worker exited, restarting",
                                                     "data": {"code": 1}}]},
                "disk": {"free_bytes": 400 * 1024**3, "total_bytes": 1000 * 1024**3},
                "ffmpeg": {"found": True, "version": "7.0"}, "ffprobe": {"found": True, "version": "7.0"},
                "log_tail": ["2026-11-20 10:00:01 INFO monitor: streamer_alpha is live",
                             "2026-11-20 10:00:02 INFO capture: started, access_token=[redacted]"]}

    @app.get("/api/guide")
    def guide_index():
        return {"pages": [{"slug": s, "title": t} for s, (t, _) in GUIDE.items()]}

    @app.get("/api/guide/{slug}")
    def guide_page(slug: str):
        if slug not in GUIDE:
            nf("guide page")
        return PlainTextResponse(GUIDE[slug][1], media_type="text/markdown")

    # monitor (monitor_service routes; armed == enabled)
    def view(c: dict) -> dict:
        if not c["enabled"]:
            c.update(state="disabled", detail="not monitoring", fix="", fix_link="")
        elif c["state"] == "disabled":
            c.update(state="waiting", detail="waiting for the stream to start")
        connected = bool(db["sources"].get(c["platform"]))
        c["status_source"] = ("creator-provided feed" if c["platform"] == "custom"
                              else OFFICIAL[c["platform"]] if connected else STREAM_PROBE)
        return c

    def channel(cid: int) -> dict:
        return db["channels"].get(cid) or nf("channel")

    @app.get("/api/channels")
    def channels():
        return [view(c) for c in db["channels"].values()]

    def gate(body: dict, cur: dict) -> None:
        if "watch_mode" in body and body["watch_mode"] is not None:
            if body["watch_mode"] not in ("ungated", "topic_gated"):
                raise HTTPException(422, "watch_mode must be one of ungated, topic_gated")
            cur["watch_mode"] = body["watch_mode"]
        if body.get("poll_seconds") is not None:
            cur["poll_seconds"] = max(60, min(900, int(body["poll_seconds"])))

    @app.post("/api/channels", status_code=201)
    def add_channel(body: dict = Body(...)):
        try:
            platform, login, _src = parse_channel_url(str(body.get("url", "")))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if any(c["platform"] == platform and c["login"] == login for c in db["channels"].values()):
            raise HTTPException(409, f"{platform}/{login} is already added")
        ids["channel"] += 1
        c = {"id": ids["channel"], "url": body["url"].strip(), "platform": platform, "login": login,
             "display_name": (body.get("display_name") or "").strip() or login, "watch_mode": "ungated", "poll_seconds": 120,
             "format_id": body.get("format_id"), "publish_platforms": body.get("publish_platforms") or [],
             "enabled": bool(body.get("enabled", True)), "state": "waiting", "detail": "", "fix": "", "fix_link": "",
             "status_source": STREAM_PROBE, "gate_change_pending": False}
        gate(body, c)
        db["channels"][c["id"]] = c
        return view(c)

    @app.get("/api/channels/{cid}")
    def get_channel(cid: int):
        return view(channel(cid))

    @app.put("/api/channels/{cid}")
    def put_channel(cid: int, body: dict = Body(...)):
        c = channel(cid)
        gate(body, c)
        for k in ("format_id", "publish_platforms"):
            if k in body:
                c[k] = body[k]
        if body.get("display_name") is not None:
            c["display_name"] = body["display_name"].strip() or c["login"]
        if body.get("enabled") is not None:
            c["enabled"] = bool(body["enabled"])
        return view(c)

    @app.delete("/api/channels/{cid}", status_code=204)
    def del_channel(cid: int):
        channel(cid)
        db["channels"].pop(cid)
        return Response(status_code=204)

    @app.post("/api/channels/{cid}/arm")
    def arm(cid: int):
        c = channel(cid)
        c["enabled"] = True
        return view(c)

    @app.post("/api/channels/{cid}/disarm")
    def disarm(cid: int):
        c = channel(cid)
        c["enabled"] = False
        return view(c)

    @app.get("/api/channels/{cid}/events")
    def channel_events(cid: int):
        return [{"id": 1, "ts": NOW - 120, "level": "info", "clip_id": None, "message": "state new -> waiting"}]

    def source(p: str) -> dict:
        d = describe(p, db["sources"][p], SimpleNamespace(kick_webhook_url=""), None, db["pending"].get(p))
        if p in source_oauth.PROVIDERS:
            d["redirect_uri"] = source_oauth.redirect_uri(p, port)
        return d

    @app.get("/api/sources")
    def sources():
        return [source(p) for p in PLATFORMS]

    @app.post("/api/sources/{p}/connect")
    def connect_source(p: str, body: dict = Body(default={})):
        if p not in PLATFORMS:
            nf("platform")
        m = body.get("method", "credentials")
        if m == "device" and p == "twitch":
            if not body.get("client_id"):
                raise HTTPException(422, "client_id is required")
            db["pending"][p] = {"kind": "device", "user_code": "WDJB-MJHT", "verification_uri": "https://example.com/activate",
                                "expires_at": time.time() + 1800, "state": "waiting"}
            return db["pending"][p]
        if m == "oauth" and p in source_oauth.PROVIDERS:
            if not body.get("client_id"):
                raise HTTPException(422, "client_id is required")
            return {"kind": "oauth", "authorize_url": f"https://example.com/oauth/{p}/authorize",
                    "redirect_uri": source_oauth.redirect_uri(p, port)}
        if m != "credentials":
            raise HTTPException(422, f"method {m!r} is not available for {p}")
        if p == "youtube":
            if not body.get("api_key"):
                raise HTTPException(422, "api_key is required")
            db["sources"][p] = {"api_key": body["api_key"]}
        else:
            if not (body.get("client_id") and body.get("client_secret")):
                raise HTTPException(422, "client_id and client_secret are required")
            db["sources"][p] = {"client_id": body["client_id"], "client_secret": body["client_secret"],
                                "access_token": "app-token"}
        return source(p)

    @app.delete("/api/sources/{p}", status_code=204)
    def disconnect_source(p: str):
        if p not in PLATFORMS:
            nf("platform")
        db["sources"][p] = {}
        db["pending"].pop(p, None)
        return Response(status_code=204)

    # edit (edit_service routes)
    def clip(cid: int) -> dict:
        return db["clips"].get(cid) or nf("clip")

    @app.get("/api/clips")
    def clips(channel_id: int | None = None, limit: int = 50, offset: int = 0):
        rows = sorted(db["clips"].values(), key=lambda c: -c["id"])
        if channel_id:
            rows = [c for c in rows if c["channel_id"] == channel_id]
        return rows[offset:offset + limit]

    @app.get("/api/clips/{cid}")
    def get_clip(cid: int):
        c = clip(cid)
        base = presets.get(c["preset_id"]) or P.shipped()
        return {**c, "srt": {lg: SRT for lg, x in c["captions"].items() if x.get("srt")},
                "values": P.merge(base, c.get("overrides"))}

    @app.get("/api/clips/{cid}/media/{kind}")
    def clip_media(cid: int, kind: str):
        return Response(status_code=404)

    @app.post("/api/clips/{cid}/render")
    def render(cid: int, body: dict = Body(default={})):
        c = clip(cid)
        if body.get("trim") is not None:
            a, b = body["trim"]
            if b - a < 2:
                raise HTTPException(400, "trimmed clip would be shorter than 2 s")
        if body.get("preset_id") is not None and presets.get(body["preset_id"]) is None:
            raise HTTPException(400, f"unknown preset {body['preset_id']!r}")
        if c["status"] not in ("ready", "preview_ready"):
            raise HTTPException(409, f"clip is {c['status']}; edit it when it is ready")
        for lg, t in (body.get("title") or {}).items():
            c["captions"][lg]["title"] = t
        if body.get("preset_id"):
            c["preset_id"] = body["preset_id"]
        if body.get("overrides") is not None:
            if not isinstance(body["overrides"], dict):
                raise HTTPException(400, "overrides must be an object of preset values")
            merged = P.merge(c.get("overrides") or {}, body["overrides"])
            try:
                P.validate(merged, base=presets.get(c["preset_id"]))
            except ValidationError as e:
                raise bad(e) from None
            c["overrides"] = merged
        c.update(status="rendering", rendering=True)
        return {"ok": True, "status": "rendering"}

    @app.post("/api/clips/{cid}/review")
    def review(cid: int, body: dict = Body(...)):
        c = clip(cid)
        if not isinstance(body.get("approved"), bool):
            raise HTTPException(400, "approved must be true or false")
        for s in c["trigger"]["selection"].values():
            s["decision"] = "qualified" if body["approved"] else "rejected"
        return {"evidence": {"state": "approved" if body["approved"] else "rejected"},
                "published": list(c["files"].get("final", {})) if body["approved"] else [],
                "selection": c["trigger"]["selection"]}

    @app.post("/api/clips/{cid}/publish")
    def publish(cid: int, body: dict = Body(default={})):
        finals = list(clip(cid)["files"].get("final", {}))
        if not finals:
            raise HTTPException(409, "no ready final to publish")
        return {"published": finals}

    # presets (edit_service routes over the real PresetStore)
    def bad(e: ValidationError):
        return HTTPException(400, {"code": "invalid", "errors": json.loads(e.json(include_url=False))})

    @app.get("/api/presets")
    def list_presets():
        return presets.list()

    @app.get("/api/presets/schema")
    def preset_schema():
        return P.Preset.model_json_schema()

    @app.get("/api/presets/{pid}")
    def get_preset(pid: str):
        return presets.get(pid) or nf("preset")

    @app.get("/api/presets/{pid}/export")
    def export_preset(pid: str):
        return presets.get(pid) or nf("preset")

    @app.post("/api/presets", status_code=201)
    def create_preset(body: dict = Body(...)):
        try:
            return presets.create(body)
        except ValidationError as e:
            raise bad(e) from None
        except KeyError:
            raise HTTPException(409, "a preset with this id exists") from None

    @app.post("/api/presets/import", status_code=201)
    def import_preset(body: Any = Body(...), replace: bool = False):
        try:
            return [presets.import_(x, replace=replace) for x in (body if isinstance(body, list) else [body])]
        except ValidationError as e:
            raise bad(e) from None

    @app.put("/api/presets/{pid}")
    def put_preset(pid: str, body: dict = Body(...)):
        try:
            return presets.update(pid, body)
        except ValidationError as e:
            raise bad(e) from None
        except LookupError:
            nf("preset")

    @app.delete("/api/presets/{pid}")
    def del_preset(pid: str):
        if pid in SHIPPED:
            raise HTTPException(400, "built-in presets cannot be deleted")
        presets.delete(pid)
        return {"ok": True}

    @app.post("/api/presets/{pid}/duplicate", status_code=201)
    def dup_preset(pid: str):
        return presets.duplicate(pid)

    @app.post("/api/presets/{pid}/reset")
    def reset_preset(pid: str):
        try:
            return presets.reset(pid)
        except PermissionError as e:
            raise HTTPException(400, str(e)) from None

    # formats and rules: the real select router
    app.include_router(select.router)

    # publish (same shapes as live_clipper/publish/service.py)
    dests = db["destinations"]

    def mode() -> str:
        return db["mode"] or "auto"  # the worker's default

    def dest(did: int) -> dict:
        return dests.get(did) or nf("destination")

    @app.get("/api/destinations")
    def destinations():
        return {"destinations": [_dest_view(d) for d in dests.values()]}

    @app.get("/api/destinations/{did}")
    def get_destination(did: int):
        return _dest_view(dest(did))

    @app.put("/api/destinations/{did}/credentials")
    def put_credentials(did: int, body: dict = Body(...)):
        d = dest(did)
        if d["test"]:
            raise HTTPException(400, "the test destination has no credentials")
        d["app_credentials_set"] = sorted({*d["app_credentials_set"], *(k for k, v in body.items() if v)})
        return {"app_credentials_set": d["app_credentials_set"]}

    @app.put("/api/destinations/{did}/settings")
    def put_settings(did: int, body: dict = Body(...)):
        d = dest(did)
        bad_keys = [k for k in body if k not in SETTING_KEYS]
        if bad_keys:
            raise HTTPException(400, f"unsupported setting(s): {bad_keys}")
        if errs := check_settings(body):
            raise HTTPException(400, {"message": "; ".join(errs.values()), "errors": errs})
        st = {**d["settings"], **{k: v for k, v in body.items() if v not in ("", None)}}
        for k, v in body.items():
            if v in ("", None):
                st.pop(k, None)
        d["settings"] = st
        return _dest_view(d)

    @app.post("/api/destinations/{did}/connect")
    def connect_destination(did: int):
        d = dest(did)
        if d["test"]:
            d.update(status="connected", account="test")
            return {"action": "verified", "ok": True, "account": "test"}
        if not d["app_credentials_set"]:
            raise HTTPException(400, "enter this app's client ID (and secret) first, or use the token form")
        d.update(status="connected", account="@example_clips")  # the fake skips the browser round trip
        return {"action": "authorize", "authorize_url": f"https://example.com/oauth/{d['provider']}/authorize"}

    @app.post("/api/destinations/{did}/disconnect")
    def disconnect_destination(did: int):
        d = dest(did)
        d.update(status="disconnected", account=None, enabled=False)
        return _dest_view(d)

    @app.put("/api/destinations/{did}/enabled")
    def set_enabled(did: int, body: dict = Body(...)):
        d = dest(did)
        if body.get("enabled") and not d["test"]:
            if d["status"] != "connected":
                raise HTTPException(409, "connect the destination before enabling it")
            if not d["rights_acknowledged"]:
                raise HTTPException(409, "acknowledge that you have the streamer's permission first")
        d["enabled"] = bool(body.get("enabled"))
        return _dest_view(d)

    @app.put("/api/destinations/{did}/acknowledge")
    def acknowledge(did: int, body: dict = Body(...)):
        d = dest(did)
        ack = bool(body.get("acknowledged"))
        d.update(rights_acknowledged=ack, rights_ack_at=time.time() if ack else None)
        if not ack:
            d["enabled"] = False
        return _dest_view(d)

    @app.post("/api/destinations/instagram/token")
    def instagram_token(body: dict = Body(...)):
        if len(str(body.get("access_token", "")).strip()) < 16:
            raise HTTPException(400, "that does not look like an access token")
        d = next(x for x in dests.values() if x["provider"] == "instagram")
        d.update(status="connected", account="@example_clips")
        return _dest_view(d)

    @app.get("/api/publish/mode")
    def get_mode():
        return {"mode": mode(), "modes": ["auto", "manual"],
                "detail": "auto posts finished clips to every connected, enabled destination; manual holds them "
                          "until you click Post"}

    @app.put("/api/publish/mode")
    def put_mode(body: dict = Body(...)):
        if body.get("mode") not in ("auto", "manual"):
            raise HTTPException(400, "mode must be auto or manual")
        db["mode"] = body["mode"]
        return {"mode": db["mode"]}

    @app.get("/api/publish/test-mode")
    def get_test_mode():
        return {"enabled": dests[4]["enabled"], "label": TEST_LABEL}

    @app.put("/api/publish/test-mode")
    def put_test_mode(body: dict = Body(...)):
        dests[4].update(enabled=bool(body.get("enabled")), status="connected", account="test")
        return {"enabled": dests[4]["enabled"], "label": TEST_LABEL}

    def delivery_view(d: dict) -> dict:
        t = dests[d["destination_id"]]
        return {**d, "destination": t["provider"], "destination_label": t["label"], "test": t["test"],
                "real": not t["test"]}

    @app.get("/api/publish/deliveries")
    def deliveries(clip_id: str | None = None, status: str | None = None):
        rows = sorted(db["deliveries"].values(), key=lambda d: -d["id"])
        if clip_id is not None:
            rows = [d for d in rows if str(d["clip_id"]) == clip_id]
        if status:
            rows = [d for d in rows if d["status"] in status.split(",")]
        return {"deliveries": [delivery_view(d) for d in rows]}

    def _post(d: dict) -> dict:
        t = dests[d["destination_id"]]
        if t["status"] != "connected" or not t["enabled"]:
            raise HTTPException(403, f"{t['label']} is not connected and enabled")
        d.update(status="published", url="https://example.com/posted", attempts=d.get("attempts", 0) + 1,
                 updated_at=time.time())
        return delivery_view(d)

    @app.post("/api/publish/deliveries/{did}/post")
    def post_held(did: int):
        d = db["deliveries"].get(did) or nf("delivery")
        if d["status"] != "held":
            raise HTTPException(409, "only held deliveries can be posted")
        return _post(d)

    @app.post("/api/publish/deliveries/{did}/confirm-posted")
    def confirm_posted(did: int, body: dict = Body(default={})):
        d = db["deliveries"].get(did) or nf("delivery")
        if d["status"] not in ("needs_confirmation", "uncertain"):
            raise HTTPException(409, "delivery is not awaiting confirmation")
        url = str(body.get("url") or "").strip()
        if url and not re.match(r"^https?://[^\s/]+", url):
            msg = "Enter the full address of the post, starting with https://."
            raise HTTPException(400, {"message": msg, "errors": {"url": msg}})
        d.update(status="published", outcome="confirmed by user", url=url or d["url"], updated_at=time.time())
        return delivery_view(d)

    @app.post("/api/publish/deliveries/{did}/retry")
    def retry(did: int):
        d = db["deliveries"].get(did) or nf("delivery")
        d.update(status="queued", last_error=None)
        return delivery_view(d)

    @app.post("/api/publish/deliveries/{did}/cancel")
    def cancel(did: int):
        d = db["deliveries"].get(did) or nf("delivery")
        d.update(status="cancelled")
        return delivery_view(d)

    @app.post("/api/publish/post")
    def post_clip(body: dict = Body(...)):
        c = clip(int(body.get("clip_id", 0)))
        dest(int(body.get("destination_id", 0)))
        ids["delivery"] += 1
        d = {"id": ids["delivery"], "clip_id": c["id"], "destination_id": int(body["destination_id"]),
             "language": body.get("language", "en"), "status": "queued", "mode": "manual", "attempts": 0,
             "outcome": None, "url": None, "post_id": None, "last_error": None, "next_attempt_at": None,
             "created_at": time.time(), "updated_at": time.time(), "history": []}
        db["deliveries"][d["id"]] = d
        return _post(d)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # agent-assisted setup: a paired agent whose helper is not running, so requests stay pending
    agent_reqs: list[dict] = []

    @app.get("/api/agent")
    def agent_overview():
        return {"paired": {"agent": "claude", "agent_label": AGENTS["claude"], "since": NOW},
                "helper": {"online": False, "last_seen": None}, "targets": [spec_view(t) for t in TARGETS],
                "source_kinds": ["env", "file", "keyring"]}

    @app.get("/api/agent/requests")
    def agent_requests(target: str = ""):
        return {"requests": [r for r in reversed(agent_reqs) if not target or r["target"] == target]}

    @app.post("/api/agent/requests", status_code=201)
    def agent_request(body: dict = Body(...)):
        if body.get("target") not in TARGETS:
            raise HTTPException(422, "unknown platform")
        if body.get("source", {}).get("kind") != "env" and not body["source"].get("ref", "").strip():
            raise HTTPException(422, "choose the file or the credential manager entry to read")
        if any(r["target"] == body["target"] and r["status"] == "pending" for r in agent_reqs):
            raise HTTPException(409, "a request for this platform is already open; wait or cancel it")
        r = {"id": len(agent_reqs) + 1, "target": body["target"], "label": TARGETS[body["target"]]["label"],
             "source": body["source"], "status": "pending", "detail": "", "saved": [], "missing": [],
             "created_at": time.time(), "updated_at": time.time(), "expires_at": time.time() + 600}
        agent_reqs.append(r)
        return r

    @app.post("/api/agent/requests/{rid}/cancel")
    def agent_cancel(rid: int):
        r = next((x for x in agent_reqs if x["id"] == rid), None) or nf("request")
        r.update(status="cancelled", detail="You cancelled this request.")
        return r

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--state", default="mock-state.json")
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    uvicorn.run(create_app(Path(a.state), a.seed, a.port), host="127.0.0.1", port=a.port)
