"""GET /api/diagnostics: what the Status page shows. Everything is redacted and read-only."""

import json
import shutil
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter

from .logging_setup import redact
from .services import AppContext
from .setup_service import _run, _tool

TAIL_LINES = 200


def tail(path: Path, n: int = TAIL_LINES) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return [redact(line.rstrip("\n")) for line in deque(f, maxlen=n)]
    except OSError:
        return []


class DiagnosticsService:
    name = "diagnostics"

    def __init__(self):
        self.ctx: AppContext | None = None
        self.router = APIRouter(prefix="/api/diagnostics")
        self.router.get("")(self.get)

    async def start(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {}

    def get(self) -> dict:
        ctx = self.ctx
        services = {}
        for svc in ctx.services:
            try:
                services[svc.name] = svc.status()
            except Exception as e:
                services[svc.name] = {"error": redact(str(e))}
        rows = ctx.db.execute("SELECT ts, message, data_json FROM events WHERE source='supervisor' "
                              "AND message LIKE '%restarting%' ORDER BY id DESC").fetchall()
        recent = [{"ts": r[0], "message": redact(r[1]), "data": json.loads(r[2]) if r[2] else None}
                  for r in rows[:5]]
        usage = shutil.disk_usage(ctx.paths.root)
        return {
            "version": ctx.version, "uptime": round(time.time() - ctx.started_at, 1),
            "services": services,
            "restarts": {"count": len(rows), "recent": recent},
            "disk": {"free_bytes": usage.free, "total_bytes": usage.total},
            "ffmpeg": _tool("ffmpeg", _run, ctx.settings.get("ffmpeg_path")),
            "ffprobe": _tool("ffprobe", _run),
            "log_tail": tail(ctx.paths.logs / "live-clipper.log"),
        }
