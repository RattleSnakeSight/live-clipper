"""Disk cap, backpressure and cleanup for local clip files. Driven by the wizard's storage settings.

Ported from the original maintenance logic (values and order kept), minus the Drive cold tier.

Rules that never bend: clips that are queued, held, uploading or simply not posted yet are never deleted.
Files this module does not know about are never deleted either, except temporary render files.

Cleanup order when a pass runs (each step only while space is still needed, except steps 1 and 2, which
always run):
  1. temporary render files older than an hour
  2. trash entries past their keep time
  3. posted clips older than the retention period go to trash (restorable until step 2 purges them)
  4. needed space = max(usage - cap, critical free - free); trash purged early, oldest first
  5. originals (source.ts) of posted clips, oldest first (when `clean_first` is "originals")
  6. whole posted clips, oldest first
"""

import asyncio
import fnmatch
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from .db import register_migrations
from .services import AppContext

log = logging.getLogger(__name__)
MB = 1024**2
GB = 1024**3

MIN_FREE_MB = 4096  # `lo`: below this no new clip starts
CRITICAL_FREE_MB = 1536  # `crit`: below this the worst state, trash purged early
MAX_ACTIVE_CLIPS = 6
TEMP_PATTERNS = ("*.part.mp4", "hook_*.txt", "audio_pre.wav", "audio_tail.wav")
TEMP_MIN_AGE_S = 3600
TRASH_KEEP_DEFAULT_H = 168
CYCLE_S = 60
ORIGINAL_NAME = "source.ts"
TERMINAL = {"published", "failed", "cancelled", "canceled", "skipped"}
TRASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}-\d{1,12}$")
SAFE_ID = re.compile(r"[^A-Za-z0-9_]")

register_migrations("maintenance", ["""
CREATE TABLE maintenance_clip(
    clip_id TEXT NOT NULL, unit_path TEXT NOT NULL, created_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'kept', trash_path TEXT, trash_at REAL, purge_after REAL,
    PRIMARY KEY(clip_id, unit_path));
CREATE TABLE maintenance_delivery(
    delivery_id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, destination TEXT, state TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX maintenance_delivery_clip ON maintenance_delivery(clip_id);
"""])


@dataclass
class Action:
    action: str  # delete_temp | purge_trash | trash_clip | delete_original | delete_clip
    path: str
    clip_id: str | None
    bytes: int
    reason: str

    def public(self, root: Path) -> dict:
        d = asdict(self)
        try:
            d["path"] = Path(self.path).relative_to(root).as_posix()
        except ValueError:
            d["path"] = Path(self.path).name
        return d


def _size(p: Path) -> int:
    try:
        if p.is_symlink():
            return 0
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dirpath, _, names in os.walk(p):
            for n in names:
                f = Path(dirpath, n)
                if not f.is_symlink():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total
    except OSError:
        return 0


class Maintenance:
    def __init__(self, ctx: AppContext, clock=time.time, free_fn=None):
        self.ctx, self.clock = ctx, clock
        self._free = free_fn or (lambda p: shutil.disk_usage(p).free)
        self.captures_paused = False

    # --- config ---
    def config(self) -> dict:
        s = self.ctx.settings
        rules = s.get("storage.cleanup_rules", ["expire_posted", "clean_when_full"])
        return {
            "dir": Path(s.get("storage.dir") or self.ctx.paths.media),
            "cap_bytes": int(float(s.get("storage.cap_gb", 20)) * GB),  # original default 20480 MB
            "retention_days": float(s.get("storage.retention_days", 14)),
            "clean_first": s.get("storage.clean_first", "originals"),
            "rules": set(rules),
            "trash_keep_h": min(720, max(24, float(s.get("storage.trash_keep_hours", TRASH_KEEP_DEFAULT_H)))),
            "lo": MIN_FREE_MB * MB,
            "crit": min(MIN_FREE_MB, CRITICAL_FREE_MB) * MB,
            "max_active": int(s.get("storage.max_active_clips", MAX_ACTIVE_CLIPS)),
        }

    # --- bookkeeping from bus events ---
    def unit_for(self, clip_id: str, path: Path, root: Path) -> Path:
        """A clip dir if the file sits in a dir named after the clip, else the file itself."""
        parent = path.parent
        try:
            inside = parent.resolve() != root.resolve() and parent.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        return parent if inside and str(clip_id) in parent.name else path

    def record_clip(self, clip_id, path: str) -> None:
        cfg = self.config()
        p = Path(path)
        unit = self.unit_for(str(clip_id), p, cfg["dir"])
        self.ctx.db.execute("INSERT OR IGNORE INTO maintenance_clip(clip_id, unit_path, created_at) VALUES(?,?,?)",
                            (str(clip_id), str(unit), self.clock()))

    def record_delivery(self, p: dict) -> None:
        if p.get("clip_id") is None or p.get("state") is None:
            return
        self.ctx.db.execute("INSERT OR REPLACE INTO maintenance_delivery VALUES(?,?,?,?,?)",
                            (str(p.get("delivery_id")), str(p["clip_id"]), str(p.get("destination", "")),
                             str(p["state"]).lower(), self.clock()))

    def posted_at(self, clip_id: str) -> float | None:
        """Time of the last publish if every delivery is finished and at least one published, else None."""
        rows = self.ctx.db.execute("SELECT state, ts FROM maintenance_delivery WHERE clip_id=?",
                                   (str(clip_id),)).fetchall()
        if not rows or any(r[0] not in TERMINAL for r in rows):
            return None
        pub = [r[1] for r in rows if r[0] == "published"]
        return max(pub) if pub else None

    def publisher_holds(self, clip_id) -> bool:
        """Ask publishing (Publisher.holds_provider_bytes) whether an upload may still need this clip's file."""
        for svc in self.ctx.services:
            pub = getattr(svc, "pub", None) if getattr(svc, "name", "") == "publish" else None
            if pub is not None:
                cid = int(clip_id) if str(clip_id).isdigit() else clip_id
                try:
                    return bool(pub.holds_provider_bytes(cid))
                except Exception:
                    log.exception("could not ask publishing about clip %s; keeping it", clip_id)
                    return True
        return False

    # --- scan and plan ---
    def scan(self) -> dict:
        cfg = self.config()
        root = cfg["dir"]
        root.mkdir(parents=True, exist_ok=True)
        now = self.clock()
        units, trash, temp, seen = [], [], [], set()
        rows = self.ctx.db.execute("SELECT clip_id, unit_path, created_at, state, trash_path, purge_after "
                                   "FROM maintenance_clip WHERE state IN ('kept','trash')").fetchall()
        for cid, unit, created, state, tpath, purge_after in rows:
            p = Path(tpath if state == "trash" else unit)
            if not (p.exists() or p.is_symlink()):
                self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE clip_id=? AND unit_path=?",
                                    (cid, unit))
                continue
            seen.add(p)
            entry = {"clip_id": cid, "path": p, "bytes": _size(p), "created": created}
            if state == "trash":
                trash.append({**entry, "purge_after": purge_after or now})
            else:
                entry["posted_at"] = self.posted_at(cid)
                orig = p / ORIGINAL_NAME
                entry["original"] = (orig, _size(orig)) if p.is_dir() and orig.is_file() else None
                units.append(entry)
        total = 0
        for dirpath, _, names in os.walk(root):
            for n in names:
                f = Path(dirpath, n)
                if f.is_symlink():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                total += st.st_size
                if any(fnmatch.fnmatch(n, pat) for pat in TEMP_PATTERNS) and now - st.st_mtime >= TEMP_MIN_AGE_S:
                    temp.append({"path": f, "bytes": st.st_size})
        kept = sum(u["bytes"] for u in units)
        trash_b = sum(t["bytes"] for t in trash)
        try:
            free = self._free(root)
        except OSError:
            free = 0
        return {"cfg": cfg, "units": units, "trash": trash, "temp": temp, "total": total, "free": free,
                "kept_bytes": kept, "trash_bytes": trash_b, "temp_bytes": sum(t["bytes"] for t in temp),
                "protected_bytes": sum(u["bytes"] for u in units if u["posted_at"] is None)}

    def plan(self, scan: dict | None = None) -> list[Action]:
        sc = scan or self.scan()
        cfg, now = sc["cfg"], self.clock()
        acts: list[Action] = []
        freed = 0
        for t in sorted(sc["temp"], key=lambda t: str(t["path"])):
            acts.append(Action("delete_temp", str(t["path"]), None, t["bytes"], "temporary render file"))
            freed += t["bytes"]
        planned_trash = set()
        for t in sorted(sc["trash"], key=lambda t: t["purge_after"]):
            if t["purge_after"] <= now:
                acts.append(Action("purge_trash", str(t["path"]), t["clip_id"], t["bytes"], "trash keep time passed"))
                planned_trash.add(t["path"])
                freed += t["bytes"]
        posted = sorted((u for u in sc["units"] if u["posted_at"] is not None), key=lambda u: u["posted_at"])
        if "expire_posted" in cfg["rules"]:
            for u in posted:
                if u["posted_at"] + cfg["retention_days"] * 86400 <= now:
                    acts.append(Action("trash_clip", str(u["path"]), u["clip_id"], 0,
                                       f"posted more than {cfg['retention_days']:g} days ago"))
        need = max(sc["total"] - freed - cfg["cap_bytes"], cfg["crit"] - sc["free"])

        def short() -> bool:
            return need - freed_extra[0] > 0

        freed_extra = [0]
        if need > 0:
            for t in sorted(sc["trash"], key=lambda t: t["created"]):
                if short() and t["path"] not in planned_trash:
                    acts.append(Action("purge_trash", str(t["path"]), t["clip_id"], t["bytes"], "disk cap reached"))
                    freed_extra[0] += t["bytes"]
            if "clean_when_full" in cfg["rules"]:
                remaining = {u["path"]: u["bytes"] for u in posted}
                if cfg["clean_first"] == "originals":
                    for u in posted:
                        if short() and u["original"]:
                            acts.append(Action("delete_original", str(u["original"][0]), u["clip_id"],
                                               u["original"][1], "disk cap reached"))
                            freed_extra[0] += u["original"][1]
                            remaining[u["path"]] -= u["original"][1]
                for u in posted:
                    if not short():
                        break
                    acts = [x for x in acts if not (x.action == "trash_clip" and x.path == str(u["path"]))]
                    acts.append(Action("delete_clip", str(u["path"]), u["clip_id"], remaining[u["path"]],
                                       "disk cap reached"))
                    freed_extra[0] += remaining[u["path"]]
        return acts

    # --- execution ---
    def _inside(self, p: Path, root: Path) -> bool:
        try:
            return p.parent.resolve().is_relative_to(root.resolve())
        except OSError:
            return False

    def _rm(self, p: Path) -> None:
        if p.is_symlink() or p.is_file():
            p.unlink(missing_ok=True)
        else:
            shutil.rmtree(p, ignore_errors=True)

    def execute(self, acts: list[Action]) -> dict:
        cfg = self.config()
        root, now = cfg["dir"], self.clock()
        trash_dir = root / "trash"
        done, freed, errors = 0, 0, []
        for a in acts:
            p = Path(a.path)
            try:
                if a.action == "delete_temp":
                    p.unlink(missing_ok=True)
                elif a.action == "purge_trash":
                    if not (p.parent == trash_dir and TRASH_RE.match(p.name) and not p.is_symlink()):
                        log.warning("refusing to purge unexpected trash path %s", p.name)
                        continue
                    self._rm(p)
                    self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE trash_path=?", (str(p),))
                else:
                    if a.clip_id is not None and self.posted_at(a.clip_id) is None:
                        continue  # not (or no longer) fully posted: protected
                    if a.clip_id is not None and self.publisher_holds(a.clip_id):
                        continue  # a provider may still read or resume an upload of this file
                    if not self._inside(p, root):
                        continue
                    if a.action == "delete_original":
                        p.unlink(missing_ok=True)
                    elif a.action == "delete_clip":
                        self._rm(p)
                        self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE clip_id=? AND unit_path=?",
                                            (a.clip_id, str(p)))
                    elif a.action == "trash_clip":
                        trash_dir.mkdir(parents=True, exist_ok=True)
                        stamp, safe = int(now), SAFE_ID.sub("_", a.clip_id)[:64]
                        while (dest := trash_dir / f"{safe}-{stamp}").exists():
                            stamp += 1
                        os.rename(p, dest)
                        self.ctx.db.execute(
                            "UPDATE maintenance_clip SET state='trash', trash_path=?, trash_at=?, purge_after=? "
                            "WHERE clip_id=? AND unit_path=?",
                            (str(dest), now, now + cfg["trash_keep_h"] * 3600, a.clip_id, str(p)))
                done += 1
                freed += a.bytes
            except OSError as e:
                errors.append(f"{a.action} {p.name}: {e.__class__.__name__}")
        return {"done": done, "freed_bytes": freed, "errors": errors}

    def cleanup(self, dry_run: bool = True) -> dict:
        sc = self.scan()
        acts = self.plan(sc)
        out = {"dry_run": dry_run, "actions": [a.public(sc["cfg"]["dir"]) for a in acts]}
        if not dry_run:
            out.update(self.execute(acts))
        return out

    def cycle(self) -> dict:
        return self.cleanup(dry_run=False)

    # --- backpressure ---
    def admit_clip(self, active_clips: int = 0) -> tuple[bool, str | None]:
        """Same order as the original: low free space, paused captures, over the cap, too many active clips."""
        sc = self.scan()
        cfg = sc["cfg"]
        if sc["free"] < cfg["lo"]:
            return False, "low free disk space"
        if self.captures_paused:
            return False, "recording paused for disk space"
        if sc["total"] > cfg["cap_bytes"]:
            return False, "storage cap reached; posted clips are cleaned automatically, unposted ones are kept"
        if active_clips >= cfg["max_active"]:
            return False, "too many clips in progress"
        return True, None

    def health(self, sc: dict | None = None, active_clips: int = 0) -> dict:
        sc = sc or self.scan()
        cfg, reasons, state = sc["cfg"], [], "healthy"
        if sc["free"] < cfg["crit"]:
            state, reasons = "degraded", ["free disk space is critically low"]
        elif self.captures_paused:
            state, reasons = "degraded", ["recording paused for disk space"]
        else:
            if sc["free"] < cfg["lo"]:
                reasons.append("free disk space is low")
            if sc["total"] > cfg["cap_bytes"]:
                reasons.append("storage cap reached")
            if active_clips >= cfg["max_active"]:
                reasons.append("too many clips in progress")
            if reasons:
                state = "backpressure"
        return {"state": state, "reasons": reasons}

    def enforce_producers(self) -> bool | None:
        """Pause below critical free space, resume at the low mark. Returns the new paused flag on change."""
        sc = self.scan()
        if not self.captures_paused and sc["free"] < sc["cfg"]["crit"]:
            self.captures_paused = True
            return True
        if self.captures_paused and sc["free"] >= sc["cfg"]["lo"]:
            self.captures_paused = False
            return False
        return None

    def report(self) -> dict:
        sc = self.scan()
        cfg = sc["cfg"]
        other = max(0, sc["total"] - sc["kept_bytes"] - sc["trash_bytes"] - sc["temp_bytes"])
        return {
            "dir": str(cfg["dir"]), "usage_bytes": sc["total"], "cap_bytes": cfg["cap_bytes"],
            "free_bytes": sc["free"], "over_cap": sc["total"] > cfg["cap_bytes"],
            "breakdown": {"clips": sc["kept_bytes"], "trash": sc["trash_bytes"], "temporary": sc["temp_bytes"],
                          "other": other},
            "protected_bytes": sc["protected_bytes"],
            "retention_days": cfg["retention_days"], "clean_first": cfg["clean_first"],
            "rules": sorted(cfg["rules"]), "health": self.health(sc),
            "next_cleanup": [a.public(cfg["dir"]) for a in self.plan(sc)][:20],
        }


class CleanupBody(BaseModel):
    dry_run: bool = True


class MaintenanceService:
    name = "maintenance"

    def __init__(self, clock=time.time, free_fn=None):
        self.clock, self.free_fn = clock, free_fn
        self.m: Maintenance | None = None
        self.ctx: AppContext | None = None
        self._task: asyncio.Task | None = None
        self._unsub: list = []
        self._last_state = "healthy"
        self.router = APIRouter(prefix="/api/storage")
        self.router.get("")(self.get_storage)
        self.router.post("/cleanup")(self.post_cleanup)

    async def start(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.m = Maintenance(ctx, self.clock, self.free_fn)
        self._unsub = [
            ctx.bus.subscribe("edit.clip_ready", self._clip_ready),
            ctx.bus.subscribe("publish.delivery", lambda _t, p: self.m.record_delivery(p)),
        ]
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for u in self._unsub:
            u()
        if self._task:
            self._task.cancel()

    def _clip_ready(self, _topic, p) -> None:
        if p.get("path") is not None and p.get("clip_id") is not None:
            self.m.record_clip(p["clip_id"], p["path"])

    def tick(self) -> dict:
        """One housekeeping pass: producers, cleanup, then tell the user when the state worsens."""
        self.m.enforce_producers()
        result = self.m.cycle()
        state = self.m.health()
        if state["state"] != self._last_state:
            if state["state"] != "healthy":
                self.ctx.notify("Disk space", "; ".join(state["reasons"]).capitalize() + ".")
            self._last_state = state["state"]
        return result

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                log.exception("storage cleanup pass failed")
            await asyncio.sleep(CYCLE_S)

    def status(self) -> dict:
        return {"state": self._last_state, "paused": bool(self.m and self.m.captures_paused)}

    def get_storage(self) -> dict:
        return self.m.report()

    def post_cleanup(self, body: CleanupBody) -> dict:
        return self.m.cleanup(dry_run=body.dry_run)
