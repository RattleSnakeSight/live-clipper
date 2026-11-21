"""Editing presets as data: every cut/render parameter of the original design, validated with pydantic,
stored in SQLite (namespace `edit`), importable/exportable as JSON.

The shipped `presets/default.json` holds the original values. A stored or imported preset may be partial: it
is merged key by key onto the shipped default before validation, so a customer file only carries what it
changes.
`settings_for(values)` turns effective values (preset -> rules -> per-clip override, merged by the selection
side's resolver and handed over as a plain dict) into the settings object the media code renders with.
"""

from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .db import register_migrations
from .render import SPLIT_FRAC

DEFAULT_ID = "default"
SHIPPED = {DEFAULT_ID: Path(__file__).parent / "presets" / "default.json"}

# the `edit` namespace: editing presets (clips live in the shared pipeline store, live_clipper.store)
register_migrations(
    "edit",
    [
        """
CREATE TABLE IF NOT EXISTS edit_presets(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, data_json TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""
    ],
)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


Frac = Field(ge=0.0, le=1.0)


class Window(_M):
    pre_roll_s: float = Field(ge=0, le=120)  # seconds before the estimated action
    post_roll_s: float = Field(ge=0, le=120)
    max_clip_seconds: float = Field(ge=2, le=600)  # limit for operator trims (live windows are pre + post)


class Encode(_M):
    preview_height: int = Field(ge=240, le=4320)
    final_height: int = Field(ge=240, le=4320)
    x264_preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"
    ]
    preview_crf: int = Field(ge=0, le=51)  # live-cut preview
    render_crf: int = Field(ge=0, le=51)  # finals and re-rendered previews
    output_fps: int = Field(ge=0, le=240)  # cap (0 = none); a slower source is never duplicated
    accurate_seek: bool  # False keeps the original input-side seek (see DEVIATIONS)


class Layout(_M):
    mode: Literal["auto", "fit", "crop", "split", "stack"]
    zoom: float | None = Field(None, ge=1.0, le=4.0)
    dy: float | None = Field(None, ge=-0.5, le=0.5)
    cx: float | None = Field(None, ge=0.0, le=1.0)
    facecam: list[float] | None = Field(None, min_length=4, max_length=4)
    gameplay: list[float] | None = Field(None, min_length=4, max_length=4)
    facecam_frac: float | None = Field(None, ge=0.15, le=0.6)
    pin: bool | None = None


class Facecam(_M):
    detect: bool  # auto layout may look for a webcam inset (else whole frame)
    split_frac: float = Field(ge=0.15, le=0.6)  # share of the height for the facecam panel


class Captions(_M):
    font: str = Field(min_length=1, max_length=64, pattern=r"^[^,\r\n]+$")  # ASS style field: no commas
    bold: bool
    size_frac: float = Field(gt=0, le=0.3)  # of the output width
    outline_frac: float = Frac
    shadow_frac: float = Frac
    margin_v_frac: float = Frac  # of the output height
    margin_lr_frac: float = Field(ge=0, le=0.4)
    alignment: int = Field(ge=1, le=9)  # ASS numpad alignment (2 = bottom centre)
    primary: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    back_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class Title(_M):
    duration_s: float = Field(ge=0, le=60)  # hook overlay shown for the first N seconds
    wrap_chars: int = Field(ge=8, le=80)


class Languages(_M):
    render: Literal["original", "both"]  # automatic finals: original language only, or en + es


class Preset(_M):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=500)
    window: Window
    encode: Encode
    layout: Layout
    facecam: Facecam
    captions: Captions
    title: Title
    languages: Languages


@lru_cache
def shipped(preset_id: str = DEFAULT_ID) -> dict:
    return json.loads(SHIPPED[preset_id].read_text(encoding="utf-8"))


def merge(base: dict, patch: dict | None) -> dict:
    """Key-by-key merge (nested dicts merged, everything else replaced)."""
    out = dict(base)
    for k, v in (patch or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate(data: dict, base: dict | None = None) -> dict:
    """A complete, validated preset dict from a partial one (pydantic ValidationError on bad input)."""
    m = Preset.model_validate(merge(base or shipped(), data))
    return m.model_dump(mode="json", exclude_none=True)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56]
    return s or "preset"


# ---------------------------------------------------------------- storage
class PresetStore:
    def __init__(self, db):
        self.db = db

    def seed(self) -> None:
        """Insert shipped presets that are missing; never overwrites a customer's edits."""
        now = time.time()
        for pid in SHIPPED:
            d = validate(shipped(pid))
            self.db.execute(
                "INSERT OR IGNORE INTO edit_presets VALUES (?, ?, ?, 1, ?, ?)",
                (pid, d["name"], json.dumps(d), now, now),
            )

    def list(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT data_json, builtin, updated_at FROM edit_presets ORDER BY builtin DESC, id"
        )
        return [{**json.loads(r[0]), "builtin": bool(r[1]), "updated_at": r[2]} for r in rows]

    def get(self, pid: str) -> dict | None:
        r = self.db.execute("SELECT data_json FROM edit_presets WHERE id=?", (pid,)).fetchone()
        return json.loads(r[0]) if r else None

    def unique_id(self, want: str) -> str:
        pid, n = want, 2
        while self.get(pid) is not None:
            pid = f"{want[:58]}-{n}"
            n += 1
        return pid

    def create(self, data: dict) -> dict:
        data = dict(data)
        data["id"] = data.get("id") or self.unique_id(slug(data.get("name") or "preset"))
        d = validate(data)
        if self.get(d["id"]) is not None:
            raise KeyError(d["id"])
        now = time.time()
        self.db.execute(
            "INSERT INTO edit_presets VALUES (?, ?, ?, 0, ?, ?)",
            (d["id"], d["name"], json.dumps(d), now, now),
        )
        return d

    def update(self, pid: str, data: dict) -> dict:
        cur = self.get(pid)
        if cur is None:
            raise LookupError(pid)
        d = validate({**data, "id": pid}, base=cur)
        self.db.execute(
            "UPDATE edit_presets SET name=?, data_json=?, updated_at=? WHERE id=?",
            (d["name"], json.dumps(d), time.time(), pid),
        )
        return d

    def delete(self, pid: str) -> None:
        if pid in SHIPPED:
            raise PermissionError("a built-in preset cannot be deleted (reset it instead)")
        if self.db.execute("DELETE FROM edit_presets WHERE id=?", (pid,)).rowcount == 0:
            raise LookupError(pid)

    def duplicate(self, pid: str, name: str | None = None) -> dict:
        cur = self.get(pid)
        if cur is None:
            raise LookupError(pid)
        name = name or f"{cur['name']} (copy)"
        return self.create({**cur, "id": self.unique_id(slug(name)), "name": name[:80]})

    def reset(self, pid: str) -> dict:
        if pid not in SHIPPED:
            raise PermissionError("only built-in presets can be reset")
        d = validate(shipped(pid))
        self.db.execute(
            "UPDATE edit_presets SET name=?, data_json=?, updated_at=? WHERE id=?",
            (d["name"], json.dumps(d), time.time(), pid),
        )
        return d

    def import_(self, data: dict, replace: bool = False) -> dict:
        """Import one preset file. An existing id is replaced only with replace=True, else renamed."""
        data = dict(data)
        data.pop("builtin", None)
        data.pop("updated_at", None)
        pid = data.get("id") or slug(data.get("name") or "preset")
        if replace and self.get(pid) is not None:
            return self.update(pid, validate({**data, "id": pid}))
        return self.create({**data, "id": self.unique_id(pid)})


# ---------------------------------------------------------------- effective values -> render settings
class EditSettings:
    """What the media code reads as `engine.settings`; defaults are the original constants."""

    def __init__(self, values: dict, models_dir: Path, asr: dict | None = None):
        v = validate(values)
        w, e, t = v["window"], v["encode"], v["title"]
        self.values = v
        self.clip_pre_roll, self.clip_post_roll = w["pre_roll_s"], w["post_roll_s"]
        self.max_clip_seconds = w["max_clip_seconds"]
        self.preview_height, self.final_height = e["preview_height"], e["final_height"]
        self.x264_preset, self.output_fps = e["x264_preset"], e["output_fps"]
        self.preview_crf, self.render_crf = e["preview_crf"], e["render_crf"]
        self.accurate_seek = e["accurate_seek"]
        self.title_secs, self.title_wrap = t["duration_s"], t["wrap_chars"]
        self.render_languages = v["languages"]["render"]
        asr = asr or {}
        self.whisper_model = asr.get("model") or "small"
        self.whisper_device = asr.get("device") or "auto"
        self.whisper_compute_type = asr.get("compute_type") or None
        self.asr_threads = int(asr.get("threads") or max(1, (os.cpu_count() or 2) - 1))
        self.models_dir = Path(models_dir)

    def caption_style(self, play_res) -> dict:
        """ASS style for this resolution; the shipped values give exactly subs.default_style(play_res)."""
        c = self.values["captions"]
        w, h = play_res
        return {
            "font": c["font"],
            "bold": c["bold"],
            "size": round(w * c["size_frac"]),
            "outline": max(2.0, round(w * c["outline_frac"], 1)),
            "shadow": max(1.0, round(w * c["shadow_frac"], 1)),
            "margin_v": round(h * c["margin_v_frac"]),
            "margin_l": round(w * c["margin_lr_frac"]),
            "margin_r": round(w * c["margin_lr_frac"]),
            "alignment": c["alignment"],
            "primary": c["primary"],
            "outline_color": c["outline_color"],
            "back_color": c["back_color"],
        }

    def window_for(self, action_time: float) -> tuple[float, float]:
        """Clip window around the estimated action (chat: burst onset - reaction delay)."""
        return action_time - self.clip_pre_roll, action_time + self.clip_post_roll

    def new_clip_layout(self, channel_layout: dict | None = None) -> dict:
        """Layout a NEW clip starts with: the channel's own layout if it set one, else the preset's."""
        from .render import is_legacy_default

        L = dict(channel_layout) if not is_legacy_default(channel_layout) else dict(self.values["layout"])
        if L.get("mode") == "auto":
            if not self.values["facecam"]["detect"]:
                return {"mode": "fit", "pin": True}
            frac = self.values["facecam"]["split_frac"]
            if frac != SPLIT_FRAC and "facecam_frac" not in L:
                L["facecam_frac"] = frac
        return L
