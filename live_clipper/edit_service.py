"""Edit subsystem: the engine object the media code expects, and the HTTP API for presets, clips, clip media
and operator re-renders.

The media modules (clipper, postprocess, render, captions) keep the original design's `engine` duck type:
`store`, `settings`, `clip_dir`, `render_lock`, `emit`, `publisher`, ... `EditEngine` provides it on top of
the app context; `for_clip()` gives a view whose `settings` come from that clip's effective preset values.
Clips live in the shared pipeline store (`live_clipper.store.Store`); `selection` (live_clipper.pipeline)
decides which finished clips are handed to publishing.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import presets as P
from .pipeline import Selection
from .presets import EditSettings, PresetStore
from .store import Store

log = logging.getLogger(__name__)
LANGS = ("en", "es")


class _Publisher:
    """publisher.on_language_ready(clip_id, lang) is called when a final is ready: publish edit.clip_ready."""

    def __init__(self, engine: EditEngine):
        self.e = engine

    def on_language_ready(self, clip_id: int, lang: str, force: bool = False) -> bool:
        """Hand a ready final to publishing when selection qualifies it (force: an explicit user publish)."""
        c = self.e.store.clip(clip_id)
        name = ((c or {}).get("files", {}).get("final") or {}).get(lang)
        if not c or not name or self.e.bus is None:
            return False
        from .render import size_for

        sel, extra = self.e.selection, {}
        if sel is not None:
            res = None
            if not force:
                d = sel.gate(clip_id, lang)
                if d["decision"] != "qualified":
                    return False
                res = d["result"]
            extra = sel.publish_fields(c, lang, res)
        w, h = size_for(self.e.for_clip(c).settings.final_height)
        trig = c.get("trigger") or {}
        title = ((c["captions"] or {}).get(lang) or {}).get("title") or c.get("title") or ""
        self.e.bus.publish(
            "edit.clip_ready",
            {
                "clip_id": clip_id,
                "channel_id": c["channel_id"],
                "path": str(self.e.clip_dir(clip_id) / name),
                "language": lang,
                "title": title,
                "description": trig.get("description") or title,
                "hashtags": trig.get("hashtags") or [],
                "duration_s": float(c["files"].get("duration") or 0),
                "width": w,
                "height": h,
                "format_id": trig.get("format_id"),
                "preset_id": c.get("preset_id"),
                **extra,
            },
        )
        return True


class _ClipEngine:
    """A per-clip view of the engine: own `settings`, everything else (store, locks, captioner) shared."""

    def __init__(self, base: EditEngine, settings: EditSettings):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "settings", settings)

    def __getattr__(self, k):
        return getattr(self._base, k)

    def __setattr__(self, k, v):  # e.g. postprocess caches engine.captioner: keep it shared
        setattr(self._base, k, v)


class _NoKV:
    def get(self, key, default=None):
        return default


class EditEngine:
    def __init__(self, paths, db, settings_kv=None, bus=None, max_media_jobs: int = 2, *, store=None,
                 clips_dir=None, models_dir=None):
        self.paths, self.kv, self.bus = paths, settings_kv or _NoKV(), bus
        self.store = store or Store(db)
        self.presets = PresetStore(self.store.db)
        media = self.kv.get("storage.dir") or (paths.media if paths else None)
        self.clips_dir = Path(clips_dir or Path(media) / "clips")
        self.models_dir = Path(models_dir or Path(paths.root) / "models")
        self.selection = Selection(self.store.db, self.store, bus)
        self.captioner = None
        self._captioner_lock = threading.Lock()
        self.early_asr: dict = {}
        self.asr_pool = ThreadPoolExecutor(1, thread_name_prefix="live-clipper-asr")
        self.cold = None
        self._slots = asyncio.Semaphore(max_media_jobs)  # bounded concurrent final encodes
        self.maintenance = self
        self.publisher = _Publisher(self)
        self._locks: dict[int, asyncio.Lock] = {}
        self.clip_tasks: dict[int, asyncio.Task] = {}

    # ---- duck type used by the media code ---------------------------------------------------------------
    def media_slot(self):
        return self._slots

    def clip_dir(self, clip_id: int) -> Path:
        return self.clips_dir / str(clip_id)

    def render_lock(self, clip_id: int) -> asyncio.Lock:
        return self._locks.setdefault(clip_id, asyncio.Lock())

    def emit(self, kind: str, payload: dict) -> None:
        if self.bus is not None:
            self.bus.publish(f"edit.{kind}", payload)

    def asr_settings(self) -> dict:
        return {
            "model": self.kv.get("asr.model", "small"),
            "device": self.kv.get("asr.device", "auto"),
            "compute_type": self.kv.get("asr.compute_type"),
        }

    def values_for(
        self, clip: dict | None = None, preset_id: str | None = None, overrides: dict | None = None
    ) -> dict:
        """Effective values: preset (clip's, else default) <- clip overrides <- extra overrides."""
        pid = preset_id or (clip or {}).get("preset_id") or P.DEFAULT_ID
        base = self.presets.get(pid) or self.presets.get(P.DEFAULT_ID) or P.shipped()
        return P.merge(P.merge(base, (clip or {}).get("overrides")), overrides)

    def settings_for(self, values: dict) -> EditSettings:
        return EditSettings(values, self.models_dir, self.asr_settings())

    @property
    def settings(self) -> EditSettings:
        return self.settings_for(self.values_for())

    def for_clip(self, clip, preset_id: str | None = None, overrides: dict | None = None) -> _ClipEngine:
        clip = self.store.clip(clip) if isinstance(clip, int) else clip
        return _ClipEngine(self, self.settings_for(self.values_for(clip, preset_id, overrides)))

    def busy(self, clip_id: int) -> bool:
        t = self.clip_tasks.get(clip_id)
        return t is not None and not t.done()

    def spawn(self, clip_id: int, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        self.clip_tasks[clip_id] = t
        t.add_done_callback(
            lambda _t: self.clip_tasks.pop(clip_id, None) if self.clip_tasks.get(clip_id) is _t else None
        )
        return t

    # ---- the live cut path (port of the original Engine._process) -------------------------------------
    def create_clip(
        self,
        channel_id: int,
        window_start: float,
        window_end: float,
        action_time: float,
        *,
        kind: str = "chat_spike",
        trigger: dict | None = None,
        layout: dict | None = None,
        preset_id: str = P.DEFAULT_ID,
        overrides: dict | None = None,
        dedupe_key: str | None = None,
        delay: float | None = None,
    ) -> int | None:
        """A new clip row (None: the dedupe key exists); layout = the channel's own layout, else the preset's
        (see new_clip_layout). The dedupe key defaults to the original `<channel>:<kind>:<action second>`."""
        s = self.settings_for(self.values_for(None, preset_id, overrides))
        return self.store.create_clip(
            channel_id,
            dedupe_key or f"{channel_id}:{kind}:{int(action_time)}",
            kind,
            trigger or {},
            delay=delay,
            window_start=window_start,
            window_end=window_end,
            action_time=action_time,
            layout=s.new_clip_layout(layout),
            preset_id=preset_id,
            overrides=overrides or {},
        )

    async def process(
        self, buffers, channel_id: int, clip_id: int, start: float, end: float, action: float | None = None
    ) -> None:
        """Cut [start, end] from the rolling buffer (preview, audio, source), then captions and finals."""
        from .clipper import cut_clip

        d = self.clip_dir(clip_id)
        eng = self.for_clip(clip_id)
        s = eng.settings
        try:
            self.store.update("clips", clip_id, status="cutting")
            prog: dict = {}

            def on_event(k: str, ts: float) -> None:
                if k in ("fed_until", "clip_start", "trim_in"):
                    prog[k] = ts
                    if k == "clip_start":
                        asyncio.get_running_loop().create_task(self._early_asr(eng, clip_id, d, prog))
                else:
                    self.store.mark(clip_id, k, ts)

            res = await cut_clip(
                buffers,
                channel_id,
                clip_id,
                start,
                end,
                d,
                layout=(self.store.clip(clip_id) or {}).get("layout") or {},
                out_h=s.preview_height,
                preset=s.x264_preset,
                crf=s.preview_crf,
                on_event=on_event,
                prefer=action,
                fps=s.output_fps,
            )
            if not res.ok:
                self._fail(clip_id, res.error or "cut failed")
                self.store.log(f"clip {clip_id} cut failed: {res.error[:200]}", "error", channel_id, clip_id)
                return
            self.store.mark(clip_id, "preview", res.done_at)
            if res.layout is not None:  # the auto decision, reused by every later render
                self.store.update("clips", clip_id, layout=res.layout)
            self.store.update(
                "clips",
                clip_id,
                status="preview_ready",
                window_start=res.start,
                window_end=res.end,
                epoch=res.epoch,
                truncated=res.truncated,
                files={
                    **res.files,
                    "source_t0": res.source_t0,
                    "trim_in": res.trim_in,
                    "duration": res.duration,
                },
            )
            self.store.log(
                f"clip {clip_id} playable ({res.duration:.1f}s, encode tail {res.encode_tail_s:.2f}s)"
                + (f" truncated: {res.truncated}" if res.truncated else ""),
                channel_id,
                clip_id,
            )
            self.emit("clip", {"id": clip_id})
            from .postprocess import post_cut

            await post_cut(eng, clip_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.store.log(
                f"clip {clip_id} failed: {traceback.format_exc()[-600:]}", "error", clip_id=clip_id
            )
            self._fail(clip_id, f"{type(e).__name__}: {e}")

    async def resume(self, clip_id: int) -> None:
        """Post-cut work of a clip whose cut finished before a restart (captions, title, finals)."""
        from .postprocess import post_cut

        try:
            await post_cut(self.for_clip(clip_id), clip_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.store.log(
                f"clip {clip_id} failed: {traceback.format_exc()[-600:]}", "error", clip_id=clip_id
            )
            self._fail(clip_id, f"{type(e).__name__}: {e}")

    def publish(self, clip_id: int, lang: str | None = None) -> list[str]:
        """The user's explicit publish of ready finals, whatever the selection decided."""
        c = self.store.clip(clip_id) or {}
        langs = [lang] if lang else list((c.get("files") or {}).get("final") or {})
        return [lg for lg in langs if self.publisher.on_language_ready(clip_id, lg, force=True)]

    async def _early_asr(self, eng, clip_id: int, d: Path, prog: dict) -> None:
        from .postprocess import early_transcribe

        try:
            await early_transcribe(eng, clip_id, d, prog)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.early_asr.pop(clip_id, None)
            self.store.log(f"clip {clip_id}: early transcription skipped: {e}", "warn", clip_id=clip_id)

    def _fail(self, clip_id: int, error: str) -> None:
        """Failed clips keep their row (for the operator) but free their media."""
        self.store.update("clips", clip_id, status="failed", error=error[:500])
        self.early_asr.pop(clip_id, None)
        shutil.rmtree(self.clip_dir(clip_id), ignore_errors=True)

    # ---- operator re-render / edit ----------------------------------------------------------------------
    async def rerender(
        self, clip_id: int, edit: dict | None, preset_id: str | None, overrides: dict | None, lang: str | None
    ) -> None:
        """The original edit path (transactional apply_edit) plus preset changes and on-request finals."""
        from . import postprocess

        clip = self.store.clip(clip_id)
        merged = P.merge(clip.get("overrides") or {}, overrides)
        eng = self.for_clip({**clip, "overrides": merged}, preset_id)
        self.store.merge_json("clips", clip_id, "files", {"editing": True})
        try:
            if preset_id is not None or overrides is not None:
                # the choice is stored first: what gets published (edit.clip_ready) describes these renders
                self.store.update(
                    "clips",
                    clip_id,
                    **({"preset_id": preset_id} if preset_id else {}),
                    **({"overrides": merged} if overrides is not None else {}),
                )
            if edit:
                await postprocess.apply_edit(eng, clip_id, **edit)
            if preset_id is not None or overrides is not None:
                if not edit or not ({"layout", "trim"} & set(edit)):
                    await postprocess.rerender_preview(eng, clip_id)
                caps = self.store.clip(clip_id)["captions"] or {}
                for lg in LANGS:
                    if (caps.get(lg) or {}).get("final_status") == "ready" and lg != lang:
                        await postprocess.render_final(eng, clip_id, lg)
            if lang:
                self.store.merge_json("clips", clip_id, "captions", {lang: {"final_status": "rendering"}})
                await postprocess.render_final(eng, clip_id, lang)
        except Exception as e:
            self.store.log(f"clip {clip_id}: edit failed: {e}", "error", clip_id=clip_id)
        finally:
            self.store.merge_json("clips", clip_id, "files", {"editing": False})
            self.emit("clip", {"id": clip_id})

    def recover(self) -> None:
        """Startup: finish or roll back edits interrupted by a crash (postprocess.recover_edit)."""
        from .postprocess import recover_edit

        for c in self.store.clips(limit=1_000_000):
            if (c["files"] or {}).get("editing") or (self.clip_dir(c["id"]) / ".edit").exists():
                recover_edit(self, c["id"])

    def asr_status(self) -> dict:
        a = self.asr_settings()
        root = self.models_dir / "whisper"
        have = importlib.util.find_spec("faster_whisper") is not None
        cached = root.is_dir() and any(a["model"] in p.name for p in root.iterdir())
        return {
            "available": have,
            "model": a["model"],
            "device": a["device"],
            "compute_type": a["compute_type"],
            "model_downloaded": cached,
            "download_root": str(root),
            "detail": ("ready" if cached else "the model downloads on the first clip with audio")
            if have
            else "faster-whisper is not installed: clips get no captions",
        }


MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}


class EditService:
    name = "edit"

    def __init__(self):
        self.engine: EditEngine | None = None
        self.router = self._router()

    async def start(self, ctx) -> None:
        ff = ctx.settings.get("ffmpeg_path")
        if ff:  # a folder or the ffmpeg binary itself
            folder = str(Path(ff).parent if Path(ff).is_file() else Path(ff))
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        self.engine = EditEngine(ctx.paths, ctx.db, ctx.settings, ctx.bus)
        self.engine.presets.seed()
        self.engine.recover()

    async def stop(self) -> None:
        if not self.engine:
            return
        for t in list(self.engine.clip_tasks.values()):
            t.cancel()
        self.engine.asr_pool.shutdown(wait=False, cancel_futures=True)

    def status(self) -> dict:
        if not self.engine:
            return {"running": False}
        return {
            "running": True,
            "rendering": sorted(self.engine.clip_tasks),
            "asr": self.engine.asr_status(),
            "ffmpeg": shutil.which("ffmpeg"),
        }

    # ---- HTTP -----------------------------------------------------------------------------------------
    def _e(self) -> EditEngine:
        if self.engine is None:
            raise HTTPException(503, "the edit service is not started")
        return self.engine

    def _clip(self, cid: int) -> dict:
        c = self._e().store.clip(cid)
        if not c:
            raise HTTPException(404, "clip not found")
        return c

    def _router(self) -> APIRouter:
        r = APIRouter()

        def presets() -> PresetStore:
            return self._e().presets

        def bad(e: ValidationError):
            return HTTPException(400, {"code": "invalid", "errors": json.loads(e.json(include_url=False))})

        @r.get("/api/presets")
        def list_presets():
            return presets().list()

        @r.get("/api/presets/{pid}")
        def get_preset(pid: str):
            return presets().get(pid) or _404("preset")

        @r.post("/api/presets", status_code=201)
        def create_preset(body: dict = Body(...)):
            try:
                return presets().create(body)
            except ValidationError as e:
                raise bad(e) from None
            except KeyError:
                raise HTTPException(409, "a preset with this id exists") from None

        @r.put("/api/presets/{pid}")
        def update_preset(pid: str, body: dict = Body(...)):
            try:
                return presets().update(pid, body)
            except ValidationError as e:
                raise bad(e) from None
            except LookupError:
                raise HTTPException(404, "preset not found") from None

        @r.delete("/api/presets/{pid}")
        def delete_preset(pid: str):
            try:
                presets().delete(pid)
            except PermissionError as e:
                raise HTTPException(400, str(e)) from None
            except LookupError:
                raise HTTPException(404, "preset not found") from None
            return {"ok": True}

        @r.post("/api/presets/{pid}/duplicate", status_code=201)
        def duplicate_preset(pid: str, body: dict = Body(default={})):
            try:
                return presets().duplicate(pid, (body or {}).get("name"))
            except LookupError:
                raise HTTPException(404, "preset not found") from None
            except ValidationError as e:
                raise bad(e) from None

        @r.post("/api/presets/{pid}/reset")
        def reset_preset(pid: str):
            try:
                return presets().reset(pid)
            except PermissionError as e:
                raise HTTPException(400, str(e)) from None

        @r.get("/api/presets/{pid}/export")
        def export_preset(pid: str):
            d = presets().get(pid) or _404("preset")
            return JSONResponse(
                d, headers={"Content-Disposition": f'attachment; filename="{pid}.preset.json"'}
            )

        @r.post("/api/presets/import", status_code=201)
        def import_preset(body: dict | list = Body(...), replace: bool = False):
            items = body if isinstance(body, list) else [body]
            try:
                return [presets().import_(x, replace=replace) for x in items if isinstance(x, dict)] or _400()
            except ValidationError as e:
                raise bad(e) from None

        @r.get("/api/clips")
        def list_clips(channel_id: int | None = None, limit: int = 50, offset: int = 0):
            return [
                self._view(c)
                for c in self._e().store.clips(channel_id, max(1, min(limit, 500)), max(0, offset))
            ]

        @r.get("/api/clips/{cid}")
        def get_clip(cid: int):
            c = self._clip(cid)
            v = self._view(c)
            d = self._e().clip_dir(cid)
            v["srt"] = {
                lg: (d / x["srt"]).read_text(encoding="utf-8")
                for lg, x in (c["captions"] or {}).items()
                if isinstance(x, dict) and x.get("srt") and (d / x["srt"]).is_file()
            }
            v["values"] = self._e().values_for(c)
            return v

        @r.get("/api/clips/{cid}/media/{kind}")
        async def clip_media(
            cid: int, kind: Literal["preview", "final", "thumbnail"], lang: str | None = None
        ):
            from .postprocess import EditError, contained

            c = self._clip(cid)
            d = self._e().clip_dir(cid)
            f = c["files"] or {}
            if kind == "preview":
                name = f.get("preview")
            elif kind == "final":
                finals = f.get("final") or {}
                name = finals.get(lang or c.get("source_lang") or "") or (
                    None if lang else next(iter(finals.values()), None)
                )
            else:
                name = await asyncio.to_thread(_thumbnail, d, f)
            try:
                p = contained(d, name) if name else None
            except EditError:
                p = None
            if p is None or not p.is_file():
                raise HTTPException(404, f"no {kind} for this clip")
            return FileResponse(p, media_type=MEDIA_TYPES.get(p.suffix, "application/octet-stream"))

        @r.post("/api/clips/{cid}/render")
        async def render_clip(cid: int, body: dict = Body(default={})):
            """Re-render with a per-clip override: trim [a, b] (seconds from the clip start), layout, srt
            {lang: text}, title {lang: text}, preset_id, overrides (partial preset values), lang (render that
            language's final on request)."""
            from .postprocess import EditError, validate_edit

            e = self._e()
            c = self._clip(cid)
            body = dict(body or {})
            preset_id, overrides, lang = (
                body.pop("preset_id", None),
                body.pop("overrides", None),
                body.pop("lang", None),
            )
            if preset_id is not None and presets().get(preset_id) is None:
                raise HTTPException(400, f"unknown preset {preset_id!r}")
            if overrides is not None and not isinstance(overrides, dict):
                raise HTTPException(400, "overrides must be an object of preset values")
            if lang not in (None, *LANGS):
                raise HTTPException(400, "lang must be en or es")
            cap = (c["captions"] or {}).get(lang) or {} if lang else {}
            if lang and cap.get("status") not in ("ready", "no_speech", "no_audio"):
                raise HTTPException(400, f"{lang} captions are not ready ({cap.get('status', 'missing')})")
            final = ((c["files"] or {}).get("final") or {}).get(lang) if lang else None
            if (
                final
                and cap.get("final_status") == "ready"
                and (e.clip_dir(cid) / final).is_file()
                and not body
                and preset_id is None
                and overrides is None
            ):
                return {"ok": True, "status": "already_rendered"}
            if e.busy(cid) or (c["files"] or {}).get("editing"):
                raise HTTPException(409, "another edit or render of this clip is running")
            if c["status"] not in ("ready", "preview_ready"):
                raise HTTPException(409, f"clip is {c['status']}; edit it when it is ready")
            try:
                view = e.for_clip({**c, "overrides": P.merge(c.get("overrides") or {}, overrides)}, preset_id)
            except ValidationError as err:
                raise bad(err) from None
            edit = None
            if body:
                try:
                    edit = await asyncio.to_thread(validate_edit, view, c, body)
                except EditError as err:
                    raise HTTPException(400, str(err)) from None
            elif preset_id is None and overrides is None and lang is None:
                raise HTTPException(
                    400, "nothing to change: send trim, layout, srt, title, preset_id, overrides or lang"
                )
            e.spawn(cid, e.rerender(cid, edit, preset_id, overrides, lang))
            return {"ok": True, "status": "rendering"}

        return r

    def _view(self, c: dict) -> dict:
        f = c["files"] or {}
        base = f"/api/clips/{c['id']}/media"
        media = {
            "preview": f"{base}/preview" if f.get("preview") else None,
            "thumbnail": f"{base}/thumbnail",
            "final": {lg: f"{base}/final?lang={lg}" for lg in (f.get("final") or {})},
        }
        return {**c, "media": media, "rendering": self._e().busy(c["id"])}


def _404(what: str):
    raise HTTPException(404, f"{what} not found")


def _400():
    raise HTTPException(400, "nothing to import")


def _thumbnail(d: Path, files: dict) -> str | None:
    """A JPEG poster frame for the UI, made once from the preview (or a final). The original produced none."""
    out = d / "thumbnail.jpg"
    if out.is_file():
        return out.name
    src = files.get("preview") or next(iter((files.get("final") or {}).values()), None)
    if not src or not (d / src).is_file():
        return None
    from .proc import LOW_PRIO

    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "1.0",
                "-i",
                str(d / src),
                "-frames:v",
                "1",
                "-vf",
                "scale=360:-2",
                str(out),
            ],
            capture_output=True,
            timeout=30,
            **LOW_PRIO,
        )
    return out.name if out.is_file() else None
