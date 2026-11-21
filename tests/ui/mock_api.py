"""In-memory fake of the HTTP API contract (docs/CONTRACTS.md) that serves live_clipper/web.

Used by the Playwright UI tests while the real subsystems are built in parallel. App state and setup answers
persist to a JSON file, so a test can stop the server and start a new one on the same file to simulate an
app restart or update. Everything else is rebuilt from fixtures on each start.

Run by hand: python -m tests.ui.mock_api --port 8799 --seed
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import live_clipper

WEB_DIR = Path(live_clipper.__file__).parent / "web"
NOW = time.time()

# --- setup questions (same shape as GET /api/setup/questions) ---------------------------------------------

def _opts(*pairs):
    return [{"value": v, "label": label} for v, label in pairs]


def _q(id, title, help, type, default=None, options=None, validation=None, show_if=None):
    return {"id": id, "title": title, "help": help, "type": type, "default": default,
            "options": options or [], "validation": validation or {}, "show_if": show_if or []}


WEBHOOK_ON = {"id": "notify.method", "in": ["webhook", "both"]}
QUESTIONS = [
    _q("setup.usage", "How will you use Live Clipper?",
       "This decides which reminders you see; it does not change how the app works.", "select", "personal",
       _opts(("personal", "Clipping my own channel"), ("permission", "Clipping other streamers, with their permission"),
             ("testing", "Just trying it out")), {"required": True}),
    _q("setup.permission_confirmed", "Do you have permission to clip and post their streams?",
       "Only clip and post content from streamers who said yes.", "boolean", False,
       validation={"required": True, "must_be": True}, show_if=[{"id": "setup.usage", "equals": "permission"}]),
    _q("platforms.enabled", "Which platforms do you want to watch?",
       "Pick the sites where the streams you clip are broadcast.", "multiselect", ["twitch"],
       _opts(("twitch", "Twitch"), ("kick", "Kick"), ("youtube", "YouTube")), {"min_items": 1}),
    _q("storage.dir", "Where should clips and recordings be stored?",
       "Pick a folder on a drive with plenty of free space.", "path", "/data/live-clipper/media",
       validation={"required": True}),
    _q("storage.cap_gb", "How much disk space may Live Clipper use (GB)?",
       "When this is reached, the app stops starting new clips and cleans up old posted ones.", "number", 120,
       validation={"min": 1, "max": 4096, "integer": True}),
    _q("storage.retention_days", "How many days should posted clips be kept?",
       "Clips that are already posted are deleted after this long. Unposted clips are never deleted.",
       "number", 14, validation={"min": 1, "max": 365, "integer": True}),
    _q("storage.clean_first", "What should be deleted first when the disk is full?",
       "Original recordings take the most space, and finished clips are small.", "select", "originals",
       _opts(("originals", "Original recordings of posted clips, then posted clips"),
             ("posted_clips", "Whole posted clips, oldest first"))),
    _q("storage.cleanup_rules", "Which automatic cleanups do you want?",
       "Temporary files are always cleaned. Clips waiting to be posted are always kept.", "multiselect",
       ["expire_posted", "clean_when_full"],
       _opts(("expire_posted", "Delete posted clips after the retention period"),
             ("clean_when_full", "Delete oldest posted clips when the disk cap is reached"))),
    _q("processing.asr_device", "Where should speech recognition run?",
       "No supported GPU found, so the processor does the work.", "select", "cpu",
       _opts(("cpu", "Processor"), ("cuda", "NVIDIA GPU"))),
    _q("processing.asr_model", "How accurate should speech recognition be?",
       "Bigger models hear better but need more memory and time.", "select", "small",
       _opts(("tiny", "Tiny (fastest)"), ("base", "Base"), ("small", "Small"), ("medium", "Medium"),
             ("large-v3", "Large (needs a strong GPU)"))),
    _q("processing.max_concurrent_recordings", "How many streams may be recorded at the same time?",
       "Each recording uses processor, memory and disk.", "number", 2,
       validation={"min": 1, "max": 16, "integer": True}),
    _q("processing.render_threads", "How many processor threads may clip rendering use?",
       "More threads render faster but slow down everything else on this computer.", "number", 4,
       validation={"min": 1, "max": 32, "integer": True}),
    _q("posting.destinations", "Where do you want to post clips?",
       "Pick none to review clips and post them yourself.", "multiselect", ["none"],
       _opts(("youtube", "YouTube Shorts"), ("tiktok", "TikTok"), ("instagram", "Instagram Reels"),
             ("none", "Nowhere, I only want the files")), {"min_items": 1}),
    _q("posting.mode", "Should clips be posted automatically?", "Manual means every clip waits for your approval.",
       "select", "manual", _opts(("manual", "No, I approve each clip"), ("auto", "Yes, post as soon as a clip is ready")),
       show_if=[{"id": "posting.destinations", "contains_any": ["youtube", "tiktok", "instagram"]}]),
    _q("notify.method", "How should Live Clipper tell you about new clips and problems?",
       "Browser notifications only show while the app is open in a tab.", "select", "browser",
       _opts(("none", "Do not notify me"), ("browser", "Browser notifications"),
             ("webhook", "A webhook (ntfy, Discord or other)"), ("both", "Browser and webhook"))),
    _q("notify.webhook_url", "What is the webhook address?",
       "Paste an ntfy topic URL, a Discord webhook URL, or any address that accepts a JSON POST.", "url", "",
       validation={"required": True, "schemes": ["http", "https"]}, show_if=[WEBHOOK_ON]),
    _q("notify.webhook_format", "Which message format does the webhook expect?",
       "Auto picks Discord or ntfy from the address and sends JSON otherwise.", "select", "auto",
       _opts(("auto", "Detect from the address"), ("generic", "JSON"), ("ntfy", "ntfy"), ("discord", "Discord")),
       show_if=[WEBHOOK_ON]),
    _q("service.start_at_login", "Start Live Clipper automatically when you log in?",
       "Runs quietly in the background so recording is not missed after a restart.", "boolean", False),
]
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
        return "Pick at least one." if len(v) < val.get("min_items", 0) else None
    if t == "url":
        if v and not re.match(r"^https?://[^/\s]+", str(v)):
            return "Enter an address that starts with http:// or https://."
        return None
    if t == "path":
        return None if str(v).strip() else "Enter a folder."
    return None


# --- fixtures ---------------------------------------------------------------------------------------------

BUILTIN_FORMAT = {
    "id": "gta-vi", "name": "GTA VI", "version": 1,
    "description": "Built-in selection format: GTA VI gameplay, opinions and chat reactions.",
    "labels": {"game": "GTA VI", "game_short": "GTA"},
    "match": {"platforms": [], "title_regex": "(?i)\\bgta\\s*(6|vi)\\b", "gate_on_title": True},
    "detector": {"sensitivity": "medium", "window": 10.0, "reaction_delay": 4.0, "per_user_cap": 2},
    "window": {"pre": 18.0, "post": 12.0},
    "scoring": {"min_score": 3.0, "fresh_half_life_h": 12.0},
    "language": {"prefs": ["auto", "en", "es"], "min_window_prob": 0.5},
    "titles": {"max_len": 70, "languages": ["en", "es"]},
}
FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Short unique name used in files and rules."},
        "name": {"type": "string", "description": "Name shown in the app."},
        "description": {"type": "string", "description": "One line about what this format clips."},
        "detector": {"type": "object", "description": "How chat activity turns into a moment.", "properties": {
            "sensitivity": {"type": "string", "enum": ["low", "medium", "high"],
                            "description": "High makes more clips from smaller chat spikes."},
            "reaction_delay": {"type": "number", "description": "Seconds chat usually lags behind the action."},
        }},
        "window": {"type": "object", "description": "How much video goes around a moment.", "properties": {
            "pre": {"type": "number", "description": "Seconds kept before the moment."},
            "post": {"type": "number", "description": "Seconds kept after the moment."},
        }},
    },
}
BUILTIN_PRESET = {
    "id": "vertical-default", "name": "Vertical default", "version": 1,
    "description": "Built-in editing preset: 1080x1920, facecam above gameplay, burned-in captions.",
    "output": {"width": 1080, "height": 1920, "fps": 30},
    "layout": {"mode": "auto", "zoom": 1.0},
    "captions": {"enabled": True, "font_size": 64, "max_words_per_line": 4},
    "loudness": {"target_lufs": -14.0},
}
PRESET_SCHEMA = {"type": "object", "properties": {
    "layout": {"type": "object", "description": "How the stream is framed in a vertical clip.", "properties": {
        "mode": {"type": "string", "enum": ["auto", "split", "fit", "crop"],
                 "description": "Auto puts a detected facecam above the gameplay."}}},
    "captions": {"type": "object", "description": "Burned-in subtitles.", "properties": {
        "enabled": {"type": "boolean", "description": "Burn subtitles into the video."}}},
}}

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

def _step(text, verified=True):
    return {"text": text, "verified": verified}


# Trimmed copy of live_clipper/publish/requirements.py (same shape as the real `requirements` field).
REQUIREMENTS = {
    "youtube": {
        "label": "YouTube Shorts", "account": "Any Google account with a YouTube channel (post to your own channel).",
        "free": True,
        "developer_app_steps": [_step("Sign in to Google Cloud and create a project."),
                                _step("Enable \"YouTube Data API v3\" for it."),
                                _step("Create an OAuth client of type \"Desktop app\"."),
                                _step("Consent screen wording for Personal mode is not confirmed.", verified=False)],
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/youtube/callback",
        "redirect_note": "Desktop clients accept any loopback port; PKCE (S256) is always used.",
        "verification_audit": [_step("Consent screen in Testing: refresh tokens expire after 7 days.")],
        "quotas": {"summary": "About 100 uploads per day.", "per_day": 100, "tracked_locally": True},
        "fails_without": {"client_id": "Connect cannot start: Google rejects the authorization request.",
                          "scope_youtube.upload": "Upload is refused (403)."},
        "tos_rights": "Third-party streamer content needs the streamer's permission; you confirm that once per destination.",
        "notes": ["Shorts: vertical, up to 3 minutes."],
        "docs": ["https://developers.google.com/youtube/v3/guides/uploading_a_video"], "as_of": "2026-11-20",
    },
    "tiktok": {
        "label": "TikTok", "account": "A TikTok account you own; developer account at developers.tiktok.com.",
        "free": True,
        "developer_app_steps": [_step("Create an app and add Login Kit and Content Posting API."),
                                _step("Register the redirect URI below under Login Kit for desktop.")],
        "scopes": ["user.info.basic", "video.upload", "video.publish"],
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/tiktok/callback",
        "redirect_note": "Only localhost / 127.0.0.1 hosts.",
        "verification_audit": [_step("Unaudited apps: every direct post is private (SELF_ONLY).")],
        "quotas": {"summary": "About 15 posts/day per creator.", "per_day": 15, "tracked_locally": True},
        "fails_without": {"client_key": "Connect cannot start.", "audit": "Posts are private."},
        "tos_rights": "Confirm the streamer's permission once per destination.",
        "notes": [], "docs": ["https://developers.tiktok.com/doc/login-kit-desktop"], "as_of": "2026-11-20",
    },
    "instagram": {
        "label": "Instagram Reels", "account": "An Instagram professional account (Business or Creator).",
        "free": True,
        "developer_app_steps": [_step("Create a Meta developer app of type Business and add Instagram."),
                                _step("Click \"Generate token\" and paste the long-lived token into the token form.")],
        "scopes": {"instagram_login": ["instagram_business_basic", "instagram_business_content_publish"]},
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/instagram/callback",
        "redirect_note": "Configurable per destination because loopback is unverified for Meta.",
        "verification_audit": [_step("Own account only: App Review not required.")],
        "quotas": {"summary": "50 API posts per rolling 24 h.", "per_day": 50, "tracked_locally": False},
        "fails_without": {"token_refresh": "Long-lived tokens last 60 days."},
        "tos_rights": "No private mode exists: a published Reel is public.",
        "notes": [], "docs": [], "as_of": "2026-11-20",
    },
    "test": {
        "label": "Test destination — nothing is posted", "account": "None: a local fake provider on this machine.",
        "free": True, "developer_app_steps": [], "scopes": [], "redirect_uri": "", "redirect_note": "",
        "verification_audit": [], "quotas": {"summary": "none", "tracked_locally": False}, "fails_without": {},
        "tos_rights": "Nothing leaves this machine; no acknowledgement needed.", "notes": ["Never counted as a real post."],
        "docs": [], "as_of": "2026-11-20",
    },
}


def _destinations(port: int) -> dict:
    out = {}
    for i, prov in enumerate(("youtube", "tiktok", "instagram", "test"), start=1):
        req = copy.deepcopy(REQUIREMENTS[prov])
        req["redirect_uri"] = req["redirect_uri"].replace("{port}", str(port))
        out[i] = {"id": i, "provider": prov, "label": req["label"], "test": prov == "test", "account": None,
                  "status": "disconnected", "status_detail": "", "enabled": False, "rights_acknowledged": False,
                  "rights_ack_at": None, "credentials_set": [], "app_credentials_set": [], "settings": {},
                  "requirements": req, "quota": {"used_today": 0, "limit": req["quotas"]["per_day"]}
                  if req["quotas"].get("per_day") else None}
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
    rows = [
        (1, "twitch", "streamer_alpha", "recording", "", None, "official_api", True, True),
        (2, "kick", "streamer_beta", "needs_action", "Kick sign-in expired. Reconnect Kick to keep watching.",
         "#/sources", "stream_probe", True, True),
        (3, "youtube", "streamer_gamma", "waiting", "", None, "official_api", True, True),
        (4, "twitch", "streamer_delta", "disabled", "", None, "official_api", False, False),
    ]
    out = {}
    for id_, p, login, st, detail, fix, src, enabled, armed in rows:
        url = {"twitch": f"https://www.twitch.tv/{login}", "kick": f"https://kick.com/{login}",
               "youtube": f"https://www.youtube.com/@{login}"}[p]
        out[id_] = {"id": id_, "platform": p, "login": login, "url": url, "watch_mode": "always",
                    "format_id": "gta-vi", "publish_platforms": ["test"], "enabled": enabled, "armed": armed,
                    "state": st, "detail": detail, "fix_url": fix, "status_source": src,
                    "updated_at": NOW - 30}
    return out


def _seed_clips() -> tuple[dict, dict]:
    statuses = ["held", "queued", "posted", "failed", "rendering", "held"]
    clips, deliveries = {}, {}
    for i, st in enumerate(statuses, start=1):
        ch = ["streamer_alpha", "streamer_beta", "streamer_gamma"][i % 3]
        clips[i] = {"id": i, "channel_id": (i % 3) + 1, "channel": ch, "title": f"Chase ends in a harbour crash {i}",
                    "created_at": NOW - i * 900, "duration_s": 28 + i, "status": st, "format_id": "gta-vi",
                    "preset_id": "vertical-default", "language": "en", "captions": "No way he made that jump",
                    "trim_in": 0.0, "trim_out": 28.0 + i, "destinations": ["test", "youtube"],
                    "reason": "Chat rate jumped to 4 times its usual level."}
        dstate = {"posted": "published", "failed": "failed", "queued": "queued", "held": "held"}.get(st)
        for dest_id in ((4, 1) if st == "held" else (4,) if dstate else ()):
            n = len(deliveries) + 1
            deliveries[n] = {"id": n, "clip_id": i, "destination_id": dest_id, "language": "en", "status": dstate,
                             "mode": "manual", "attempts": 1 if dstate in ("published", "failed") else 0,
                             "url": "https://example.com/clip" if dstate == "published" else None,
                             "last_error": "Upload refused: quota used up for today." if st == "failed" else None,
                             "created_at": NOW - i * 800, "updated_at": NOW - i * 700}
    return clips, deliveries


PLATFORM_RE = [
    ("twitch", re.compile(r"^https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$")),
    ("kick", re.compile(r"^https?://(?:www\.)?kick\.com/([A-Za-z0-9_-]{2,40})/?$")),
    ("youtube", re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/(?:@|channel/|c/)([A-Za-z0-9_.-]+)/?$")),
]


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, p + "."))
        else:
            out[p] = v
    return out


def _check_doc(doc: Any, kind: str) -> list[dict]:
    errs = []
    if not isinstance(doc, dict):
        return [{"loc": ["body"], "msg": "Must be a JSON object."}]
    for k in ("id", "name"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            errs.append({"loc": ["body", k], "msg": "Field required."})
    if kind == "formats":
        sens = doc.get("detector", {}).get("sensitivity")
        if sens is not None and sens not in ("low", "medium", "high"):
            errs.append({"loc": ["body", "detector", "sensitivity"], "msg": "Must be low, medium or high."})
        for k in ("pre", "post"):
            v = doc.get("window", {}).get(k)
            if v is not None and (not isinstance(v, int | float) or v < 0):
                errs.append({"loc": ["body", "window", k], "msg": "Must be a number of seconds, 0 or more."})
    return errs


# --- app --------------------------------------------------------------------------------------------------

def create_app(state_file: Path, seed: bool = False, port: int = 8799) -> FastAPI:
    state_file = Path(state_file)
    store: dict = json.loads(state_file.read_text()) if state_file.exists() else {}
    store.setdefault("app_state", {})
    store.setdefault("answers", {})

    def save() -> None:
        state_file.write_text(json.dumps(store))

    db: dict = {
        "channels": _seed_channels() if seed else {},
        "formats": {"gta-vi": copy.deepcopy(BUILTIN_FORMAT)},
        "presets": {"vertical-default": copy.deepcopy(BUILTIN_PRESET)},
        "rules": [{"id": "r1", "name": "Hold long Kick clips", "enabled": True,
                   "conditions": {"platform": "kick", "min_duration_s": 40},
                   "actions": {"hold": True, "set": {"window.post": 15}}}] if seed else [],
        "destinations": _destinations(port),
        "mode": "manual",
        "sources": {
            "twitch": {"connected": seed, "account": "streamer_alpha" if seed else None},
            "kick": {"connected": False, "account": None},
            "youtube": {"connected": False, "account": None},
        },
        "notifications": [{"id": 1, "ts": NOW - 60, "kind": "notify", "title": "Clip ready",
                           "body": "streamer_alpha: Chase ends in a harbour crash 1"}] if seed else [],
    }
    db["clips"], db["deliveries"] = _seed_clips() if seed else ({}, {})
    if seed:
        db["destinations"][1].update(status="connected", account="@example_clips", app_credentials_set=["client_id"],
                                     rights_acknowledged=True, rights_ack_at=NOW, enabled=True)
        db["destinations"][4].update(status="connected", account="test", enabled=True)
    ids = {"channel": 100, "delivery": 100, "rule": 100}

    app = FastAPI()

    def nf(what: str):
        raise HTTPException(404, f"No such {what}")

    # core
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "uptime": 12.5, "pid": 1}

    @app.get("/api/status")
    def status():
        armed = [c for c in db["channels"].values() if c["armed"]]
        return {"version": "0.1.0", "uptime": 12.5, "pid": 1, "services": {
            "monitor": {"channels": len(db["channels"]), "armed": len(armed)},
            "publish": {"mode": db["mode"], "queued": sum(d["status"] == "queued" for d in db["deliveries"].values())},
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
        return {"cpu": {"model": "Example 8-core processor", "cores": 8}, "ram_bytes": 16 * 1024**3,
                "disk": {"path": "/data", "free_bytes": 400 * 1024**3}, "gpu": {"kind": "none", "name": None},
                "ffmpeg": {"found": True, "version": "7.0"}, "ffprobe": {"found": True, "version": "7.0"},
                "streamlink": {"found": False}, "os": "Linux 6.8", "python": "3.13.0"}

    @app.get("/api/setup/questions")
    def questions():
        return {"questions": QUESTIONS}

    def setup_state():
        a = store["answers"]
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

    @app.put("/api/setup/answers")
    def put_answers(body: dict = Body(...)):
        answers = body.get("answers", body)
        errors = {}
        for k, v in answers.items():
            if k not in QIDX:
                errors[k] = "Unknown question."
            elif err := _validate(QIDX[k], v):
                errors[k] = err
        if errors:
            raise HTTPException(422, {"errors": errors})
        store["answers"].update(answers)
        save()
        return setup_state()

    @app.post("/api/setup/complete")
    def complete():
        a = store["answers"]
        eff = {q["id"]: a.get(q["id"], q["default"]) for q in QUESTIONS}
        missing = {}
        for q in QUESTIONS:
            if _visible(q, eff):
                err = _validate(q, eff[q["id"]])
                if not err and q["validation"].get("required") and q["type"] in ("url", "path") and not eff[q["id"]]:
                    err = "Needs an answer."
                if err:
                    missing[q["id"]] = err
        if missing:
            raise HTTPException(422, {"errors": missing})
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
        return {"version": "0.1.0", "uptime": 12.5, "services": status()["services"],
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

    # monitor
    @app.get("/api/channels")
    def channels():
        return {"channels": list(db["channels"].values())}

    def channel_from(body: dict, existing: dict | None = None) -> dict:
        url = str(body.get("url", existing["url"] if existing else "")).strip()
        for p, rx in PLATFORM_RE:  # noqa: B007 (p is used after the loop)
            if m := rx.match(url):
                break
        else:
            raise HTTPException(422, {"errors": {"url": "Paste a Twitch, Kick or YouTube channel link."}})
        if body.get("watch_mode", "always") not in ("always", "topic"):
            raise HTTPException(422, {"errors": {"watch_mode": "Pick always or topic."}})
        c = dict(existing or {"armed": False, "state": "offline", "detail": "", "fix_url": None,
                              "status_source": "official_api", "updated_at": time.time()})
        enabled = bool(body.get("enabled", c.get("enabled", True)))
        c.update(platform=p, login=m.group(1), url=url, watch_mode=body.get("watch_mode", c.get("watch_mode", "always")),
                 format_id=body.get("format_id", c.get("format_id")),
                 publish_platforms=body.get("publish_platforms", c.get("publish_platforms", [])), enabled=enabled)
        if not enabled:
            c.update(state="disabled", armed=False)
        elif c["state"] == "disabled":
            c["state"] = "offline"
        return c

    @app.post("/api/channels")
    def add_channel(body: dict = Body(...)):
        ids["channel"] += 1
        c = channel_from(body)
        c["id"] = ids["channel"]
        if c["enabled"]:
            c.update(armed=True, state="waiting")
        db["channels"][c["id"]] = c
        return c

    @app.get("/api/channels/{cid}")
    def get_channel(cid: int):
        return db["channels"].get(cid) or nf("channel")

    @app.put("/api/channels/{cid}")
    def put_channel(cid: int, body: dict = Body(...)):
        if cid not in db["channels"]:
            nf("channel")
        db["channels"][cid] = channel_from(body, db["channels"][cid])
        return db["channels"][cid]

    @app.delete("/api/channels/{cid}")
    def del_channel(cid: int):
        db["channels"].pop(cid, None) or nf("channel")
        return {"ok": True}

    @app.post("/api/channels/{cid}/arm")
    def arm(cid: int):
        c = db["channels"].get(cid) or nf("channel")
        c.update(armed=True, state="waiting")
        return c

    @app.post("/api/channels/{cid}/disarm")
    def disarm(cid: int):
        c = db["channels"].get(cid) or nf("channel")
        c.update(armed=False, state="offline")
        return c

    @app.get("/api/channels/{cid}/events")
    def channel_events(cid: int):
        return {"events": [{"ts": NOW - 120, "message": "Stream went live"}]}

    SOURCE_META = {
        "twitch": ("Twitch", "device", [], "Uses the official Twitch API. Sign in with a code shown here."),
        "kick": ("Kick", "oauth", [{"name": "client_id", "label": "Client ID", "secret": False,
                                    "help": "From your Kick developer app."},
                                   {"name": "client_secret", "label": "Client secret", "secret": True,
                                    "help": "Stored encrypted on this computer."}], "Uses the official Kick API."),
        "youtube": ("YouTube", "credentials", [{"name": "api_key", "label": "API key", "secret": True,
                                                "help": "A YouTube Data API v3 key from Google Cloud."}],
                    "Checks live status with the YouTube Data API. Chat needs a Google sign-in."),
    }

    @app.get("/api/sources")
    def sources():
        out = []
        for p, (name, kind, fields, note) in SOURCE_META.items():
            s = db["sources"][p]
            on = s["connected"]
            out.append({"platform": p, "name": name, "connected": on, "account": s["account"], "note": note,
                        "connect": {"kind": kind, "fields": fields},
                        "enables": [
                            {"key": "live_status", "label": "Live status", "available": True,
                             "detail": "Official API" if on else "Stream check only, slower and less reliable"},
                            {"key": "chat", "label": "Chat", "available": on,
                             "detail": "Reads chat to find moments" if on else "Needs a connection"},
                            {"key": "capture", "label": "Capture", "available": True,
                             "detail": "Records the public stream; no connection needed"}]})
        return {"sources": out}

    @app.post("/api/sources/{p}/connect")
    def connect_source(p: str, body: dict = Body(default={})):
        if p not in db["sources"]:
            nf("source")
        kind = SOURCE_META[p][1]
        if kind == "device":
            db["sources"][p].update(connected=True, account="streamer_alpha")
            return {"status": "pending", "device": {"user_code": "WDJB-MJHT", "verification_uri": "https://example.com/activate",
                                                    "expires_in": 600}}
        if kind == "oauth":
            db["sources"][p].update(connected=True, account="streamer_beta")
            return {"status": "pending", "authorize_url": "https://example.com/oauth/authorize"}
        if not body.get("fields", {}).get("api_key"):
            raise HTTPException(422, {"errors": {"api_key": "Enter the API key."}})
        db["sources"][p].update(connected=True, account="API key")
        return {"status": "connected"}

    @app.delete("/api/sources/{p}")
    def disconnect_source(p: str):
        db["sources"].get(p) or nf("source")
        db["sources"][p].update(connected=False, account=None)
        return {"ok": True}

    # edit
    @app.get("/api/clips")
    def clips(status: str | None = None, channel_id: int | None = None, q: str | None = None):
        rows = sorted(db["clips"].values(), key=lambda c: -c["created_at"])
        if status:
            rows = [c for c in rows if c["status"] == status]
        if channel_id:
            rows = [c for c in rows if c["channel_id"] == channel_id]
        if q:
            rows = [c for c in rows if q.lower() in c["title"].lower()]
        return {"clips": rows}

    @app.get("/api/clips/{cid}")
    def clip(cid: int):
        return db["clips"].get(cid) or nf("clip")

    @app.get("/api/clips/{cid}/media/{kind}")
    def clip_media(cid: int, kind: str):
        return Response(status_code=404)

    @app.post("/api/clips/{cid}/render")
    def render(cid: int, body: dict = Body(...)):
        c = db["clips"].get(cid) or nf("clip")
        o = body.get("overrides", body)
        if "trim_in" in o and "trim_out" in o and o["trim_out"] <= o["trim_in"]:
            raise HTTPException(422, {"errors": {"trim_out": "End must be after start."}})
        c.update({k: v for k, v in o.items() if k in ("trim_in", "trim_out", "title", "captions", "preset_id")})
        c["status"] = "rendering"
        return c

    def doc_routes(kind: str, builtin: dict, schema: dict):
        coll = db[kind]

        @app.get(f"/api/{kind}")
        def list_docs():
            return {kind: [{"id": d["id"], "name": d["name"], "description": d.get("description", ""),
                            "builtin": d["id"] == builtin["id"]} for d in coll.values()]}

        @app.get(f"/api/{kind}/schema")
        def get_schema():
            return schema

        @app.get(f"/api/{kind}/{{did}}")
        def get_doc(did: str):
            return coll.get(did) or nf(kind[:-1])

        @app.post(f"/api/{kind}")
        def create_doc(body: Any = Body(...)):
            if errs := _check_doc(body, kind):
                raise HTTPException(422, errs)
            base, n = body["id"], 2
            while body["id"] in coll:
                body["id"] = f"{base}-{n}"
                n += 1
            coll[body["id"]] = body
            return body

        @app.put(f"/api/{kind}/{{did}}")
        def put_doc(did: str, body: Any = Body(...)):
            if did not in coll:
                nf(kind[:-1])
            if errs := _check_doc(body, kind):
                raise HTTPException(422, errs)
            body["id"] = did
            coll[did] = body
            return body

        @app.delete(f"/api/{kind}/{{did}}")
        def del_doc(did: str):
            if did == builtin["id"]:
                raise HTTPException(400, "The built-in one cannot be deleted. Use Reset instead.")
            coll.pop(did, None) or nf(kind[:-1])
            return {"ok": True}

        @app.post(f"/api/{kind}/{{did}}/duplicate")
        def dup_doc(did: str):
            d = copy.deepcopy(coll.get(did) or nf(kind[:-1]))
            d["name"] = f"{d['name']} copy"
            return create_doc(d)

        @app.post(f"/api/{kind}/{{did}}/reset")
        def reset_doc(did: str):
            if did != builtin["id"]:
                raise HTTPException(400, "Only the built-in one can be reset.")
            coll[did] = copy.deepcopy(builtin)
            return coll[did]

    @app.post("/api/formats/preview-effective")
    def preview(body: dict = Body(...)):
        layers: dict[str, tuple[Any, str, str]] = {}
        for k, v in _flatten(BUILTIN_FORMAT).items():
            layers[k] = (v, "builtin", "Built-in GTA VI format")
        fmt = db["formats"].get(body.get("format_id") or "gta-vi")
        if fmt and fmt["id"] != "gta-vi":
            for k, v in _flatten(fmt).items():
                if layers.get(k, (None,))[0] != v:
                    layers[k] = (v, "format", f"Your format: {fmt['name']}")
        ctx = body.get("context", {})
        for r in db["rules"]:
            cond = r.get("conditions", {})
            if not r.get("enabled", True):
                continue
            if cond.get("platform") and cond["platform"] != ctx.get("platform"):
                continue
            if cond.get("min_duration_s") and (body.get("duration_s") or 0) < cond["min_duration_s"]:
                continue
            for k, v in r.get("actions", {}).get("set", {}).items():
                layers[k] = (v, "rule", f"Rule: {r['name']}")
        clip = db["clips"].get(body.get("clip_id")) if body.get("clip_id") else None
        if clip:
            for k in ("title", "trim_in", "trim_out"):
                layers[f"clip.{k}"] = (clip[k], "override", f"Clip {clip['id']} override")
        return {"settings": [{"path": k, "value": v, "layer": layer, "source": src}
                             for k, (v, layer, src) in sorted(layers.items())]}

    @app.get("/api/rules")
    def rules():
        return {"rules": db["rules"]}

    @app.put("/api/rules")
    def put_rules(body: Any = Body(...)):
        rows = body.get("rules", body) if isinstance(body, dict) else body
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("name") for r in rows):
            raise HTTPException(422, [{"loc": ["body", "rules"], "msg": "Each rule needs a name."}])
        for r in rows:
            if not r.get("id"):
                ids["rule"] += 1
                r["id"] = f"r{ids['rule']}"
        db["rules"] = rows
        return {"rules": rows}

    @app.post("/api/rules")
    def add_rule(body: dict = Body(...)):
        if not body.get("name"):
            raise HTTPException(422, [{"loc": ["body", "name"], "msg": "Field required."}])
        ids["rule"] += 1
        body["id"] = f"r{ids['rule']}"
        db["rules"].append(body)
        return body

    @app.put("/api/rules/{rid}")
    def put_rule(rid: str, body: dict = Body(...)):
        for i, r in enumerate(db["rules"]):
            if r["id"] == rid:
                body["id"] = rid
                db["rules"][i] = body
                return body
        nf("rule")

    @app.delete("/api/rules/{rid}")
    def del_rule(rid: str):
        db["rules"] = [r for r in db["rules"] if r["id"] != rid]
        return {"ok": True}

    doc_routes("formats", BUILTIN_FORMAT, FORMAT_SCHEMA)
    doc_routes("presets", BUILTIN_PRESET, PRESET_SCHEMA)

    # publish (same shapes as live_clipper/publish/service.py)
    dests = db["destinations"]

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

    @app.post("/api/destinations/{did}/connect")
    def connect_destination(did: int):
        d = dest(did)
        if d["test"]:
            d.update(status="connected", account="test")
            return {"action": "verified", "ok": True, "account": "test"}
        if not d["app_credentials_set"]:
            raise HTTPException(400, "enter this app's client ID (and secret) first, or use the token form")
        d.update(status="connected", account="@example_clips")  # the mock skips the browser round trip
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
        return {"mode": db["mode"], "modes": ["auto", "manual"],
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
        return {"enabled": dests[4]["enabled"], "label": REQUIREMENTS["test"]["label"]}

    @app.put("/api/publish/test-mode")
    def put_test_mode(body: dict = Body(...)):
        dests[4].update(enabled=bool(body.get("enabled")), status="connected", account="test")
        return {"enabled": dests[4]["enabled"], "label": REQUIREMENTS["test"]["label"]}

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
        db["clips"][d["clip_id"]]["status"] = "posted"
        return delivery_view(d)

    @app.post("/api/publish/deliveries/{did}/post")
    def post_held(did: int):
        d = db["deliveries"].get(did) or nf("delivery")
        if d["status"] != "held":
            raise HTTPException(409, "only held deliveries can be posted")
        return _post(d)

    @app.post("/api/publish/deliveries/{did}/retry")
    def retry(did: int):
        d = db["deliveries"].get(did) or nf("delivery")
        d.update(status="queued", last_error=None)
        return delivery_view(d)

    @app.post("/api/publish/post")
    def post_clip(body: dict = Body(...)):
        c = db["clips"].get(int(body.get("clip_id", 0))) or nf("clip")
        dest(int(body.get("destination_id", 0)))
        ids["delivery"] += 1
        d = {"id": ids["delivery"], "clip_id": c["id"], "destination_id": int(body["destination_id"]),
             "language": body.get("language", "en"), "status": "queued", "mode": "manual", "attempts": 0,
             "url": None, "last_error": None, "created_at": time.time(), "updated_at": time.time()}
        db["deliveries"][d["id"]] = d
        return _post(d)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

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
