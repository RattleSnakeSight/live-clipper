"""Parity adapter: drives the public cut/render code with the same calls as the original-design harness.

The external harness loads it with `--adapter adapter:PublicAdapter` (this folder on PYTHONPATH);
tests/parity/test_cutting_parity.py uses it too, so both measure exactly the same code path.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace


class _Store:
    def __init__(self, clip):
        self.c, self.updates = clip, []

    def clip(self, cid):
        return self.c

    def update(self, table, cid, **kw):
        self.updates.append(json.loads(json.dumps(kw, default=str)))
        self.c.update(kw)

    def mark(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass


class PublicAdapter:
    name = "public"

    def __init__(self):
        from live_clipper import buffer, clipper, facecam, postprocess, render
        from live_clipper.captions import pipeline, subs
        self.buffer, self.clipper, self.render, self.postprocess = buffer, clipper, render, postprocess
        self.subs, self.pipeline, self.facecam = subs, pipeline, facecam
        self.vod, self.vod_error = None, "VOD import is not part of the edit subsystem"

    def window_for(self, action: float, pre: float, post: float) -> tuple:
        from live_clipper.presets import EditSettings, merge, shipped
        s = EditSettings(merge(shipped(), {"window": {"pre_roll_s": pre, "post_roll_s": post}}), "models")
        return s.window_for(action), "live_clipper.presets.EditSettings.window_for"

    async def cut(self, segs, idx_cfg, T0, lat, start, end, out_dir, layout, out_h, preset, fps, prefer, timeout):
        b = self.buffer.BufferManager(Path(tempfile.gettempdir()) / "live-clipper-parity-buffer",
                                      buffer_seconds=1e9, ceiling_bytes=1 << 40)
        idx = b.index(1)
        jump_after = idx_cfg.get("pts_jump_after_segment")
        delay_after = idx_cfg.get("receipt_delay_after_segment")
        for i, s in enumerate(segs):
            ps, pe = s["pts_start"], s["pts_end"]
            recv = T0 + pe + lat
            if jump_after is not None and i >= jump_after:
                ps, pe = ps + idx_cfg["pts_jump_s"], pe + idx_cfg["pts_jump_s"]
            if delay_after is not None and i >= delay_after:
                recv += idx_cfg["receipt_delay_s"]
            idx.add(s["path"], ps, pe, recv, os.path.getsize(s["path"]))
        events = []
        res = await self.clipper.cut_clip(b, 1, 1, start, end, Path(out_dir), layout=layout, out_h=out_h,
                                          preset=preset, segment_timeout=timeout, prefer=prefer, fps=fps,
                                          on_event=lambda k, t: events.append([k, t]))
        epochs = [{"id": ep.id, "reason": ep.reason, "offset": ep.offset, "n": len(ep.segments)} for ep in idx.epochs]
        return res, events, epochs, idx

    async def render_clip(self, clip_dir: Path, clip: dict, out: Path, height: int, lang, title, settings: dict,
                          title_font: str | None):
        store = _Store(clip)
        eng = SimpleNamespace(clip_dir=lambda cid: clip_dir, store=store, maintenance=None, cold=None,
                              settings=SimpleNamespace(output_fps=settings["output_fps"],
                                                       x264_preset=settings["x264_preset"],
                                                       accurate_seek=settings.get("accurate_seek", False)))
        captured = []
        orig = self.postprocess.run_ffmpeg

        async def spy(args, cwd=None):
            captured.append({"args": list(args), "cwd": cwd})
            return await orig(args, cwd=cwd)
        self.postprocess.run_ffmpeg = spy
        try:
            rc, err = await self.postprocess.render_video(eng, clip, out, height=height, lang=lang, title=title)
        finally:
            self.postprocess.run_ffmpeg = orig
        return rc, err, captured, store.updates, clip.get("layout")

    def build_cues(self, segments):
        return self.subs.build_cues(segments)

    def captions_files(self, out_dir: Path, lang: str, cues, play_res):
        return self.pipeline._write(out_dir, lang, cues, None, tuple(play_res))

    def split_ass(self, src: Path, dst: Path, layout: dict) -> str:
        return self.render.split_ass(src, dst, layout)

    def make_title(self, transcript, chat, reason, lang):
        try:                                   # titles belong to the selection side
            from live_clipper.titles import make_title
        except ImportError:
            return None
        return make_title(transcript, chat, reason, lang=lang)
