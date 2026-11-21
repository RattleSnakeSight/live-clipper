"""First-run wizard backend: hardware detection, the question list (single source of truth), answers, state.

Answers are stored in `ctx.settings` under the question id (for example `storage.cap_gb`), so any subsystem
can read them directly. The webhook URL is a secret and goes to the vault instead (`notifications`).
"""

import ctypes
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response

from . import autostart
from .services import AppContext

log = logging.getLogger(__name__)
GB = 1024**3
WEBHOOK_SECRET = "notifications"


# --- hardware -------------------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 8) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _cpu_model(run: Callable = _run) -> str:
    if sys.platform == "darwin":
        out = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out and out.strip():
            return out.strip()
    elif sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _ram_bytes(run: Callable = _run) -> int:
    try:
        if os.name == "nt":
            class MemStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                            ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong),
                            ("ext", ctypes.c_ulonglong)]
            st = MemStatus()
            st.length = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return int(st.total)
        if sys.platform == "darwin":
            out = run(["sysctl", "-n", "hw.memsize"])
            return int(out.strip()) if out else 0
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def _tool(name: str, run: Callable, configured: str | None = None) -> dict:
    path = configured if configured and Path(configured).exists() else shutil.which(name)
    if not path:
        return {"found": False, "path": None, "version": None}
    out = run([path, "-version" if name.startswith("ff") else "--version"]) or ""
    m = re.search(r"(?:version|streamlink)\s+([^\s,]+)", out.splitlines()[0] if out else "")
    return {"found": True, "path": path, "version": m.group(1) if m else (out.split("\n")[0] or None)}


def _gpu(run: Callable) -> dict:
    out = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]) if shutil.which(
        "nvidia-smi") else None
    if out and out.strip():
        name, _, mem = out.strip().splitlines()[0].partition(",")
        m = re.search(r"(\d+)", mem)
        return {"kind": "nvidia", "name": name.strip(), "vram_mb": int(m.group(1)) if m else None}
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return {"kind": "apple", "name": "Apple Silicon", "vram_mb": None}
    return {"kind": "none", "name": None, "vram_mb": None}


def detect_hardware(storage_dir: Path | str, run: Callable = _run, ffmpeg_path: str | None = None) -> dict:
    d = Path(storage_dir)
    probe = d
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = 0
    return {
        "cpu": {"model": _cpu_model(run), "cores": os.cpu_count() or 1},
        "ram_bytes": _ram_bytes(run),
        "disk": {"path": str(d), "free_bytes": free},
        "gpu": _gpu(run),
        "ffmpeg": _tool("ffmpeg", run, ffmpeg_path),
        "ffprobe": _tool("ffprobe", run),
        "streamlink": _tool("streamlink", run),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }


# --- defaults from hardware --------------------------------------------------------------------------

def hardware_defaults(hw: dict) -> dict:
    cores, ram_gb = hw["cpu"]["cores"], hw["ram_bytes"] / GB
    gpu = hw["gpu"]
    if gpu["kind"] == "nvidia":
        vram = (gpu.get("vram_mb") or 0) / 1024
        device, compute = "cuda", "float16"
        model = "large-v3" if vram >= 10 else "medium" if vram >= 6 else "small"
    else:
        device, compute = "cpu", "int8"
        model = "small" if ram_gb >= 16 and cores >= 8 else "tiny" if 0 < ram_gb < 6 else "base"
    recordings = max(1, min(8, cores // 2, int(ram_gb // 4) or 1))
    free_gb = hw["disk"]["free_bytes"] / GB
    return {
        "processing.asr_device": device, "processing.asr_compute_type": compute,
        "processing.asr_model": model, "processing.max_concurrent_recordings": recordings,
        "processing.render_threads": max(1, min(8, cores // 2)),
        "storage.cap_gb": int(min(50, max(5, free_gb * 0.25))),
    }


# --- questions ------------------------------------------------------------------------------------------

def _opts(*pairs: tuple[str, str]) -> list[dict]:
    return [{"value": v, "label": label} for v, label in pairs]


POST_DESTS = ["youtube", "tiktok", "instagram"]
WEBHOOK_METHODS = ["webhook", "both"]


def build_questions(hw: dict, default_dir: str) -> list[dict]:
    d = hardware_defaults(hw)
    gpu_note = {"nvidia": f"Found an NVIDIA GPU ({hw['gpu']['name']}).", "apple": "Found Apple Silicon."}.get(
        hw["gpu"]["kind"], "No supported GPU found, so the processor does the work.")
    webhook_on = {"id": "notify.method", "in": WEBHOOK_METHODS}

    def q(id, title, help, type, default=None, options=None, validation=None, show_if=None):
        return {"id": id, "title": title, "help": help, "type": type, "default": default,
                "options": options or [], "validation": validation or {}, "show_if": show_if or []}

    return [
        q("setup.usage", "How will you use Live Clipper?",
          "This decides which reminders you see; it does not change how the app works.", "select", "personal",
          _opts(("personal", "Clipping my own channel"),
                ("permission", "Clipping other streamers, with their permission"),
                ("testing", "Just trying it out")), {"required": True}),
        q("setup.permission_confirmed", "Do you have permission to clip and post their streams?",
          "Only clip and post content from streamers who said yes.", "boolean", False,
          validation={"required": True, "must_be": True},
          show_if=[{"id": "setup.usage", "equals": "permission"}]),
        q("platforms.enabled", "Which platforms do you want to watch?",
          "Pick the sites where the streams you clip are broadcast.", "multiselect", ["twitch"],
          _opts(("twitch", "Twitch"), ("kick", "Kick"), ("youtube", "YouTube")), {"min_items": 1}),
        q("storage.dir", "Where should clips and recordings be stored?",
          "Pick a folder on a drive with plenty of free space.", "path", default_dir, validation={"required": True}),
        q("storage.cap_gb", "How much disk space may Live Clipper use (GB)?",
          "When this is reached, the app stops starting new clips and cleans up old posted ones.", "number",
          d["storage.cap_gb"], validation={"min": 1, "max": 4096, "integer": True}),
        q("storage.retention_days", "How many days should posted clips be kept?",
          "Clips that are already posted are deleted after this long. Unposted clips are never deleted.",
          "number", 14, validation={"min": 1, "max": 365, "integer": True}),
        q("storage.clean_first", "What should be deleted first when the disk is full?",
          "Original recordings take the most space, and finished clips are small.", "select", "originals",
          _opts(("originals", "Original recordings of posted clips, then posted clips"),
                ("posted_clips", "Whole posted clips, oldest first"))),
        q("storage.cleanup_rules", "Which automatic cleanups do you want?",
          "Temporary files are always cleaned. Clips waiting to be posted are always kept.", "multiselect",
          ["expire_posted", "clean_when_full"],
          _opts(("expire_posted", "Delete posted clips after the retention period"),
                ("clean_when_full", "Delete oldest posted clips when the disk cap is reached"))),
        q("processing.asr_device", "Where should speech recognition run?", gpu_note, "select",
          d["processing.asr_device"],
          _opts(("cpu", "Processor"), ("cuda", "NVIDIA GPU"))),
        q("processing.asr_model", "How accurate should speech recognition be?",
          "Bigger models hear better but need more memory and time.", "select", d["processing.asr_model"],
          _opts(("tiny", "Tiny (fastest)"), ("base", "Base"), ("small", "Small"), ("medium", "Medium"),
                ("large-v3", "Large (needs a strong GPU)"))),
        q("processing.asr_compute_type", "Which number precision should speech recognition use?",
          "The default fits your hardware; change it only if you know why.", "select",
          d["processing.asr_compute_type"],
          _opts(("int8", "int8 (small and fast on processors)"), ("float16", "float16 (GPU)"),
                ("float32", "float32 (most compatible, slowest)"))),
        q("processing.max_concurrent_recordings", "How many streams may be recorded at the same time?",
          "Each recording uses processor, memory and disk.", "number", d["processing.max_concurrent_recordings"],
          validation={"min": 1, "max": 16, "integer": True}),
        q("processing.render_threads", "How many processor threads may clip rendering use?",
          "More threads render faster but slow down everything else on this computer.", "number",
          d["processing.render_threads"], validation={"min": 1, "max": 32, "integer": True}),
        q("posting.destinations", "Where do you want to post clips?",
          "Pick none to review clips and post them yourself.", "multiselect", ["none"],
          _opts(("youtube", "YouTube Shorts"), ("tiktok", "TikTok"), ("instagram", "Instagram Reels"),
                ("none", "Nowhere, I only want the files")), {"min_items": 1}),
        q("posting.mode", "Should clips be posted automatically?",
          "Manual means every clip waits for your approval.", "select", "manual",
          _opts(("manual", "No, I approve each clip"), ("auto", "Yes, post as soon as a clip is ready")),
          show_if=[{"id": "posting.destinations", "contains_any": POST_DESTS}]),
        q("notify.method", "How should Live Clipper tell you about new clips and problems?",
          "Browser notifications only show while the app is open in a tab.", "select", "browser",
          _opts(("none", "Do not notify me"), ("browser", "Browser notifications"),
                ("webhook", "A webhook (ntfy, Discord or other)"), ("both", "Browser and webhook"))),
        q("notify.webhook_url", "What is the webhook address?",
          "Paste an ntfy topic URL, a Discord webhook URL, or any address that accepts a JSON POST.", "url", "",
          validation={"required": True, "schemes": ["http", "https"]}, show_if=[webhook_on]),
        q("notify.webhook_format", "Which message format does the webhook expect?",
          "Auto picks Discord or ntfy from the address and sends JSON otherwise.", "select", "auto",
          _opts(("auto", "Detect from the address"), ("generic", "JSON"), ("ntfy", "ntfy"),
                ("discord", "Discord")), show_if=[webhook_on]),
        q("service.start_at_login", "Start Live Clipper automatically when you log in?",
          "Runs quietly in the background so recording is not missed after a restart.", "boolean", False),
    ]


def _cond_ok(cond: dict, answers: dict) -> bool:
    v = answers.get(cond["id"])
    if "equals" in cond:
        return v == cond["equals"]
    if "not_equals" in cond:
        return v != cond["not_equals"]
    if "in" in cond:
        return v in cond["in"]
    if "contains_any" in cond:
        return bool(set(v or []) & set(cond["contains_any"]))
    if "not_empty" in cond:
        return bool(v)
    return True


def is_visible(question: dict, answers: dict) -> bool:
    return all(_cond_ok(c, answers) for c in question["show_if"])


def validate_value(q: dict, value: Any) -> tuple[Any, str | None]:
    """Return (clean_value, error)."""
    t, v, o = q["type"], q["validation"], {x["value"] for x in q["options"]}
    if t == "boolean":
        if not isinstance(value, bool):
            return value, "Answer yes or no."
        if v.get("must_be") and value is not True:
            return value, "This needs a yes."
        return value, None
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return value, "Enter a number."
        if v.get("integer") and int(value) != value:
            return value, "Enter a whole number."
        if not v["min"] <= value <= v["max"]:
            return value, f"Enter a number from {v['min']} to {v['max']}."
        return int(value) if v.get("integer") else value, None
    if t == "select":
        return (value, None) if value in o else (value, "Pick one of the listed options.")
    if t == "multiselect":
        if not isinstance(value, list) or any(x not in o for x in value) or len(set(value)) != len(value):
            return value, "Pick from the listed options."
        if len(value) < v.get("min_items", 0):
            return value, "Pick at least one."
        if "none" in value and len(value) > 1 and "none" in o:
            return value, "'Nowhere' cannot be combined with other choices."
        return value, None
    if not isinstance(value, str):
        return value, "Enter text."
    value = value.strip()
    if t == "url":
        parts = urlsplit(value)
        if parts.scheme not in v["schemes"] or not parts.netloc:
            return value, "Enter a full address starting with http:// or https://."
    if t == "path":
        return _check_dir(value)
    return value, None


def _check_dir(value: str) -> tuple[str, str | None]:
    p = Path(value).expanduser()
    if not value or not p.is_absolute():
        return value, "Enter a full folder path."
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        return value, "Live Clipper cannot write to that folder."
    return str(p), None


def mask_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/..." if parts.netloc else ""


# --- in-app guide ---------------------------------------------------------------------------------------

GUIDE_DIR = Path(__file__).parent / "guide"
GUIDE_PAGES = [
    ("install", "Install"), ("first-run", "First run"), ("platforms", "Platforms"),
    ("monitoring", "Monitoring streams"), ("formats-and-presets", "Formats and presets"),
    ("editing", "Editing clips"), ("posting", "Posting"), ("troubleshooting", "Troubleshooting"),
    ("updating", "Updating"), ("uninstalling", "Uninstalling"), ("privacy", "Privacy"),
]


def guide_index() -> dict:
    return {"pages": [{"slug": s, "title": t} for s, t in GUIDE_PAGES]}


def guide_page(slug: str) -> Response:
    if slug not in dict(GUIDE_PAGES):  # only known slugs ever reach the filesystem
        raise HTTPException(404, "No such guide page")
    return Response((GUIDE_DIR / f"{slug}.md").read_text(encoding="utf-8"), media_type="text/markdown")


# --- service --------------------------------------------------------------------------------------------

class SetupService:
    name = "setup"

    def __init__(self, run: Callable = _run, autostart_mod=autostart):
        self._run, self._autostart = run, autostart_mod
        self.ctx: AppContext | None = None
        self.router = APIRouter()
        r = APIRouter(prefix="/api/setup")
        r.get("/hardware")(self.hardware)
        r.get("/questions")(self.questions)
        r.put("/answers")(self.put_answers)
        r.get("/state")(self.state)
        r.post("/complete")(self.complete)
        self.router.include_router(r)
        self.router.get("/api/guide")(guide_index)
        self.router.get("/api/guide/{slug}")(guide_page)

    async def start(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {"completed": bool(self.ctx and self.ctx.app_state.get("setup.completed", False))}

    def _storage_dir(self) -> str:
        return self.ctx.settings.get("storage.dir") or str(self.ctx.paths.media)

    def _hw(self) -> dict:
        return detect_hardware(self._storage_dir(), self._run, self.ctx.settings.get("ffmpeg_path"))

    def _questions(self) -> list[dict]:
        return build_questions(self._hw(), str(self.ctx.paths.media))

    def _answers(self) -> dict:
        out = {}
        for q in self._questions():
            val = self.ctx.settings.get(q["id"])
            if val is not None:
                out[q["id"]] = val
        if self.ctx.secrets.get(WEBHOOK_SECRET):
            out["notify.webhook_url"] = self.ctx.secrets.get(WEBHOOK_SECRET)["url"]
        return out

    def hardware(self) -> dict:
        return self._hw()

    def questions(self) -> dict:
        return {"questions": self._questions()}

    def put_answers(self, body: dict) -> dict:
        qs = {q["id"]: q for q in self._questions()}
        answers = body.get("answers", body)
        clean, errors = {}, {}
        for key, value in answers.items():
            if key not in qs:
                errors[key] = "Unknown question."
                continue
            clean[key], err = validate_value(qs[key], value)
            if err:
                errors[key] = err
        if errors:
            raise HTTPException(422, {"errors": errors})
        self._persist(clean)
        return self.state()

    def _persist(self, clean: dict) -> None:
        for key, value in clean.items():
            if key == "notify.webhook_url":
                self.ctx.secrets.set(WEBHOOK_SECRET, {"url": value})
            else:
                self.ctx.settings.set(key, value)

    def state(self) -> dict:
        answers, qs = self._answers(), self._questions()
        effective = {q["id"]: answers.get(q["id"], q["default"]) for q in qs}
        shown = {**answers}
        if "notify.webhook_url" in shown:
            shown["notify.webhook_url"] = mask_url(shown["notify.webhook_url"])
        remaining = [q["id"] for q in qs if is_visible(q, effective) and q["id"] not in answers]
        return {"completed": bool(self.ctx.app_state.get("setup.completed", False)),
                "completed_at": self.ctx.app_state.get("setup.completed_at"),
                "answers": shown, "remaining": remaining}

    def complete(self) -> dict:
        answers, qs = self._answers(), self._questions()
        effective = {q["id"]: answers.get(q["id"], q["default"]) for q in qs}
        missing, fill = {}, {}
        for q in qs:
            if not is_visible(q, effective):
                continue
            val = effective[q["id"]]
            clean, err = validate_value(q, val)
            if err or (q["validation"].get("required") and q["type"] in ("url", "path") and not clean):
                missing[q["id"]] = err or "Needs an answer."
            elif q["id"] not in answers:
                fill[q["id"]] = clean
        if missing:
            raise HTTPException(422, {"errors": missing})
        self._persist(fill)
        if effective.get("service.start_at_login"):
            try:
                self._autostart.install()
            except Exception as e:
                log.warning("could not enable start at login: %s", e)
                self.ctx.db.event("warning", "setup", "could not enable start at login")
        elif "service.start_at_login" in answers and self._autostart.is_installed():
            try:
                self._autostart.uninstall()
            except Exception as e:
                log.warning("could not disable start at login: %s", e)
        self.ctx.app_state.set("setup.completed", True)
        self.ctx.app_state.set("setup.completed_at", time.time())
        return self.state()
