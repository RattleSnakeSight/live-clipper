"""Worker stand-in for test_crash_recovery: arms 3 channels, cuts one clip, then waits to be killed."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.modules["keyring"] = None  # vault key file only
sys.path.insert(0, str(Path(__file__).parent))

from live_clipper.events import Trigger  # noqa: E402
from live_clipper.monitor_service import MonitorService  # noqa: E402
from live_clipper.paths import get_paths  # noqa: E402
from live_clipper.services import build_context  # noqa: E402

CHANNELS = [  # (name, watch gate, format_id, publish_platforms)
    ("streamer_alpha", {"enabled": False, "poll_seconds": 120}, "gta-vi", ["youtube"]),
    ("streamer_beta", {"enabled": True, "poll_seconds": 300}, None, []),
    ("streamer_gamma", {"enabled": False, "poll_seconds": 120}, "custom-format", ["tiktok", "youtube"]),
]


def prepare(ctx) -> None:
    ctx.settings.set("monitor.min_free_mb", 0)
    ctx.settings.set("monitor.topic_observe_seconds", 0)
    ctx.settings.set("ffmpeg_path", "missing-ffmpeg-for-test")  # no media processes in this test


async def main() -> None:
    paths = get_paths()
    ctx = build_context(paths, port=8765, token="test")
    prepare(ctx)
    svc = MonitorService()
    await svc.start(ctx)
    e = svc.engine

    async def cut_done_then_hang(m, clip_id, start, end, action=None):
        d = e.clip_dir(clip_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "preview.mp4").write_bytes(b"x")
        (d / "source.ts").write_bytes(b"x")
        e.store.update("clips", clip_id, status="preview_ready")
        await asyncio.Event().wait()  # post-cut work in progress when killed

    e._process = cut_done_then_hang
    ids = []
    for name, gate, fmt, pubs in CHANNELS:
        url = f"srt://127.0.0.1:9/{name}"
        cid = e.store.add_channel(
            "custom",
            url,
            url,
            {"topic_gate": gate},
            url=url,
            display_name=name,
            format_id=fmt,
            publish_platforms=pubs,
        )
        e.arm(cid)
        ids.append(cid)
    now = time.time()
    trig = Trigger(
        channel_id=ids[0],
        reason="Manual clip",
        kind="manual",
        burst_start=now,
        action_time=now - 3,
        delay=3.0,
        detected_at=now,
    )
    clip = e.on_trigger(e.monitors[ids[0]], trig)
    while e.store.clip(clip)["status"] != "preview_ready":
        await asyncio.sleep(0.02)
    print(json.dumps({"ids": ids, "clip": clip, "action_time": trig.action_time}), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
