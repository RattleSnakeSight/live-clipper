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
RGB = r"^#[0-9A-Fa-f]{6}$"


class Window(_M):
    pre_roll_s: float = Field(ge=0, le=120,
        description="Seconds of video kept before the moment chat reacted to.")
    post_roll_s: float = Field(ge=0, le=120, description="Seconds of video kept after the moment.")
    max_clip_seconds: float = Field(
        ge=2, le=600, description="Longest clip you can make by trimming one by hand, in seconds.")


class Encode(_M):
    preview_height: int = Field(
        ge=240, le=4320,
            description="Height in pixels of the quick preview video. Width follows the vertical shape.")
    final_height: int = Field(
        ge=240, le=4320,
            description="Height in pixels of the finished video that gets posted. 1920 gives 1080x1920.")
    x264_preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"
    ] = Field(
        description="Encoder speed. Slower settings make smaller files at the same quality but take longer.")
    preview_crf: int = Field(
        ge=0, le=51,
            description="Quality of the preview video. Lower is sharper and bigger; 23 is a fair default.")
    render_crf: int = Field(
        ge=0, le=51,
            description="Quality of the finished video. Lower is sharper and bigger; 18 to 23 looks good.")
    output_fps: int = Field(
        ge=0, le=240,
            description=(
                "Most frames per second in the output. 0 means no limit; slower sources are never padded."))
    accurate_seek: bool = Field(
        description=(
            "Cut exactly at the start time. Off keeps the original faster cut, which can start a moment "
            "late."))


class Layout(_M):
    mode: Literal["auto", "fit", "crop", "split", "stack"] = Field(
        description=(
            "How the stream is framed in the vertical video: auto picks for you, fit shows the whole frame, "
            "crop cuts the centre, split and stack place facecam and gameplay together."))
    zoom: float | None = Field(None, ge=1.0, le=4.0, description="Zoom into the picture. 1 is no zoom.")
    dy: float | None = Field(
        None, ge=-0.5, le=0.5,
            description="Move the picture up (negative) or down (positive), as a share of its height.")
    cx: float | None = Field(
        None, ge=0.0, le=1.0,
            description="Horizontal centre of a crop, from 0 (left edge) to 1 (right edge).")
    facecam: list[float] | None = Field(
        None, min_length=4, max_length=4,
        description="Where the webcam sits in the stream frame: left, top, width, height, each from 0 to 1.")
    gameplay: list[float] | None = Field(
        None, min_length=4, max_length=4,
        description=(
            "Where the gameplay sits in the stream frame: left, top, width, height, each from 0 to 1."))
    facecam_frac: float | None = Field(
        None, ge=0.15, le=0.6,
            description="Share of the video height given to the webcam panel in split layouts.")
    pin: bool | None = Field(
        None, description="Keep this layout fixed instead of letting automatic detection change it.")


class Facecam(_M):
    detect: bool = Field(
        description="Let the auto layout look for a webcam in the frame. Off shows the whole frame.")
    split_frac: float = Field(
        ge=0.15, le=0.6,
            description="Share of the video height used by the webcam panel when a webcam is found.")


class Captions(_M):
    font: str = Field(
        min_length=1, max_length=64, pattern=r"^[^,\r\n]+$",
        description="Font name for the captions, as installed on this computer. No commas.")
    bold: bool = Field(description="Draw the captions in bold.")
    size_frac: float = Field(gt=0, le=0.3, description="Caption text size as a share of the video width.")
    outline_frac: float = Field(
        ge=0.0, le=1.0,
            description="Thickness of the outline around caption letters, as a share of the video width.")
    shadow_frac: float = Field(
        ge=0.0, le=1.0,
            description="Size of the shadow behind caption letters, as a share of the video width.")
    margin_v_frac: float = Field(
        ge=0.0, le=1.0,
            description="Distance of the captions from the screen edge, as a share of the video height.")
    margin_lr_frac: float = Field(
        ge=0, le=0.4,
            description=(
                "Space kept free on the left and right of the captions, as a share of the video width."))
    alignment: int = Field(
        ge=1, le=9,
            description=(
                "Where captions sit, as a number-pad position: 2 bottom centre, 5 middle, 8 top centre."))
    primary: str = Field(pattern=RGB, description="Caption text colour as #RRGGBB.")
    outline_color: str = Field(pattern=RGB,
        description="Colour of the outline around caption letters, as #RRGGBB.")
    back_color: str = Field(pattern=RGB,
        description="Colour of the shadow behind caption letters, as #RRGGBB.")


class Title(_M):
    duration_s: float = Field(
        ge=0, le=60,
            description=(
                "Seconds the title text shows at the start of the clip. 0 turns the title overlay off."))
    wrap_chars: int = Field(ge=8, le=80,
        description="Characters per line before the title wraps to the next line.")


class Languages(_M):
    render: Literal["original", "both"] = Field(
        description=(
            "Which finished videos are made automatically: the spoken language only, or English and "
            "Spanish."))


class Preset(_M):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
                    description=(
                        "Short unique name used in files and rules: lower-case letters, digits, dashes."))
    name: str = Field(min_length=1, max_length=80, description="The name shown in the app.")
    description: str = Field("", max_length=500, description="One line about what this look is for.")
    window: Window = Field(description="How much video goes around each moment.")
    encode: Encode = Field(description="Size, quality and speed of the videos made.")
    layout: Layout = Field(description="How the stream is framed in the vertical video.")
    facecam: Facecam = Field(description="Whether and how a webcam panel is placed above the gameplay.")
    captions: Captions = Field(description="Look of the burned-in subtitles.")
    title: Title = Field(description="The title text shown over the start of the clip.")
    languages: Languages = Field(description="Which language versions are rendered.")


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
