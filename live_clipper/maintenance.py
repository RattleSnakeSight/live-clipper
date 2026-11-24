"""Disk cap, backpressure and cleanup for local clip files. Driven by the wizard's storage settings.

Ported from the original maintenance logic (values and order kept), minus the Drive cold tier.

Rules that never bend: clips that are queued, held, uploading or simply not posted yet are never deleted.
Files this module does not know about are never deleted either, except temporary render files.

What the user's own presses do (immediately, not on a schedule): Reject removes that clip's files, and Clean up
now also removes every fully posted clip and every clip the user rejected, found by its folder even when the
app never tracked it. A clip that is waiting, in flight, shared, approved or not decided keeps its files, and so
does a clip an outside sender (the shared claims table of a hand-patched installation) may still post or has
claimed and not confirmed. Posting to the test destination alone never makes a clip removable. The rows that
show what was sent (deliveries, claims, the clip row) always stay.

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
import sqlite3
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
APP_MARKER = ".live-clipper-data"  # ownership marker written into the app's storage folder
APP_SUBDIRS = ("clips", "tmp", "trash")
TEMP_DIRS = ("clips", "tmp")  # the only places temp files are swept: the app's own subfolders of the storage dir
TRASH_KEEP_DEFAULT_H = 168
CYCLE_S = 60
ORIGINAL_NAME = "source.ts"
TERMINAL = {"published", "failed", "cancelled", "canceled", "skipped"}
SENT = ("published", "private", "draft")             # a delivery that reached the platform and is settled
UNSENT = ("queued", "held", "paused", "blocked", "waiting_language")   # the provider was never asked
SETTLED = SENT + ("cancelled", "disallowed")
TRASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}-\d{1,12}$")
SAFE_ID = re.compile(r"[^A-Za-z0-9_]")


def is_link(p: Path) -> bool:
    """Symlink or Windows junction (never followed by the sweep)."""
    try:
        if p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
            return True
        return bool(getattr(os.lstat(p), "st_file_attributes", 0) & 0x400)  # reparse point (3.11 junctions)
    except OSError:
        return True

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

    def external_holds(self, clip_id) -> bool:
        return bool(self._external_hold(clip_id))

    # --- what a clip's deliveries say (publishing's own table, so it holds across restarts) ---
    def _deliveries(self, clip_id) -> list[dict]:
        try:
            rows = self.ctx.db.execute(
                "SELECT d.id, d.status, d.reconcile, COALESCE(t.provider, '') FROM publish_deliveries d "
                "LEFT JOIN publish_destinations t ON t.id=d.destination_id WHERE d.clip_id=?",
                (int(clip_id) if str(clip_id).isdigit() else clip_id,)).fetchall()
        except sqlite3.OperationalError:               # no publishing tables in this database
            return []
        return [{"id": r[0], "status": r[1], "reconcile": r[2], "provider": r[3]} for r in rows]

    def _posted(self, clip_id) -> bool:
        """Every delivery of the clip is settled and at least one reached its platform."""
        rows = self._deliveries(clip_id)
        if rows:
            real = [r for r in rows if r["provider"] != "test"]   # the test destination posts nothing anywhere
            return bool(real) and all(r["status"] in SETTLED for r in rows) and any(r["status"] in SENT for r in real)
        return self.posted_at(str(clip_id)) is not None

    def _rejected(self, clip_id) -> bool:
        if not str(clip_id).isdigit():
            return False
        from .store import Store
        c = Store(self.ctx.db).clip(int(clip_id)) or {}
        return (((c.get("trigger") or {}).get("review") or {}).get("gameplay") or {}).get("state") == "rejected"

    def _in_flight(self, clip_id) -> bool:
        """An upload of this clip may still need its file (or the publisher or an outside sender says so)."""
        for r in self._deliveries(clip_id):
            if r["status"] not in SETTLED and (r["status"] != "failed" or r["reconcile"]):
                return True
        return self.publisher_holds(clip_id) or bool(self._external_hold(clip_id))

    def _removable(self, clip_id) -> bool:
        """May this clip's files be deleted now? Posted and settled, or rejected by the user with nothing waiting."""
        if self._posted(clip_id):
            return not (self.publisher_holds(clip_id) or self._external_hold(clip_id))
        return self._rejected(clip_id) and not self._in_flight(clip_id) and not any(
            r["status"] in UNSENT for r in self._deliveries(clip_id))

    def _external_hold(self, clip_id) -> str:
        """Why an outside sender may still need this clip ("" = no reason). Reads the shared claims table that a
        hand-patched installation's separate sender writes before it submits: a claim it has not confirmed, or a
        clip it may still pick up (selection still qualifies it and a social network has no settled claim)."""
        if not str(clip_id).isdigit():
            return ""
        try:
            rows = self.ctx.db.execute("SELECT network, pending FROM local_english_claims WHERE clip_id=?",
                                       (int(clip_id),)).fetchall()
        except sqlite3.OperationalError:               # no such table: nothing outside sends from this database
            return ""
        if any(r[1] for r in rows):
            return "an outside sender has claimed this clip and has not confirmed the post"
        from .store import Store
        sel = ((Store(self.ctx.db).clip(int(clip_id)) or {}).get("trigger") or {}).get("selection") or {}
        if (sel.get("en") or {}).get("decision") == "qualified" and not {"instagram", "tiktok"} <= {r[0] for r in rows}:
            return "an outside sender may still post this clip to Instagram or TikTok"
        return ""

    def _publisher(self):
        for svc in self.ctx.services:
            pub = getattr(svc, "pub", None) if getattr(svc, "name", "") == "publish" else None
            if pub is not None:
                return pub
        return None

    def adopt_folders(self) -> int:
        """Track every clip folder under the store that the app never tracked (clips held or rejected by selection
        never reached posting, so nothing recorded their folder). Only folders named after an existing clip."""
        root = self.config()["dir"]
        n = 0
        try:
            ids = [r[0] for r in self.ctx.db.execute("SELECT id FROM monitor_clips").fetchall()]
        except sqlite3.OperationalError:
            return 0
        for cid in ids:
            d = root / "clips" / str(cid)
            if d.is_dir() and not is_link(d):
                cur = self.ctx.db.execute(
                    "INSERT INTO maintenance_clip(clip_id, unit_path, created_at) SELECT ?,?,? "
                    "WHERE NOT EXISTS (SELECT 1 FROM maintenance_clip WHERE clip_id=?)",
                    (str(cid), str(d), self.clock(), str(cid)))
                n += cur.rowcount
        return n

    def release_clip(self, clip_id, why: str) -> dict:
        """Delete one clip's folder now. Refuses a folder outside the store or one another clip shares. Keeps every
        database row; the clip row gets a note of what was removed and why."""
        cid, root = str(clip_id), self.config()["dir"]
        row = self.ctx.db.execute("SELECT unit_path FROM maintenance_clip WHERE clip_id=? AND state='kept'",
                                  (cid,)).fetchone()
        p = Path(row[0]) if row else root / "clips" / cid
        if is_link(p) or p == root or not self._inside(p, root):
            return {"removed": False, "freed_bytes": 0, "kept_because": "its folder is not inside the storage folder"}
        if self.ctx.db.execute("SELECT 1 FROM maintenance_clip WHERE unit_path=? AND clip_id<>? AND state='kept'",
                               (str(p), cid)).fetchone():
            return {"removed": False, "freed_bytes": 0, "kept_because": "the folder is shared with another clip"}
        before = _size(p) if p.exists() else 0
        self._rm(p)
        left = _size(p) if p.exists() else 0
        freed = before - left
        if p.exists():                                  # a file in use, or a refused delete: say so, keep the record
            return {"removed": False, "freed_bytes": freed,
                    "kept_because": "some files could not be deleted (they may be in use); the rest was removed"}
        if self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE clip_id=?", (cid,)).rowcount == 0:
            self.ctx.db.execute("INSERT OR IGNORE INTO maintenance_clip(clip_id, unit_path, created_at, state) "
                                "VALUES(?,?,?,'deleted')", (cid, str(p), self.clock()))
        if cid.isdigit():
            from .store import Store
            Store(self.ctx.db).merge_json("clips", int(cid), "trigger", {"media_removed": {
                "at": self.clock(), "why": why, "bytes": freed}})
        return {"removed": True, "freed_bytes": freed, "kept_because": ""}

    def _mark_rejected(self, cid: str) -> None:
        """The selection record says rejected for every language it holds: that is what a separate sender reads."""
        if not cid.isdigit():
            return
        from .store import Store
        s = Store(self.ctx.db)
        sel = ((s.clip(int(cid)) or {}).get("trigger") or {}).get("selection") or {}
        if sel:
            s.merge_json("clips", int(cid), "trigger", {"selection": {
                lg: {**(v if isinstance(v, dict) else {}), "decision": "rejected",
                     "reasons": ["rejected by the user"]} for lg, v in sel.items()}})

    def reject_clip(self, clip_id) -> dict:
        """The user pressed Reject: what was only waiting is cancelled, then the clip's files are removed unless an
        upload may still need them. Returns {"removed", "freed_bytes", "kept_because"}."""
        cid, pub = str(clip_id), self._publisher()
        self._mark_rejected(cid)                       # first, so an outside sender stops seeing it as qualified
        if pub is not None:
            for r in self._deliveries(cid):
                if r["status"] in UNSENT and not r["reconcile"]:
                    try:
                        pub.cancel(r["id"])
                    except Exception:                  # in flight meanwhile: the check below keeps the files
                        log.warning("could not cancel delivery %s of rejected clip %s", r["id"], cid)
        if self._in_flight(cid) or any(r["status"] in UNSENT for r in self._deliveries(cid)):
            return {"removed": False, "freed_bytes": 0, "kept_because": self._external_hold(cid) or (
                "an upload of this clip is still waiting or may have reached a platform; its files stay until that "
                "is settled")}
        return self.release_clip(cid, "rejected")

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
                    total += f.stat().st_size
                except OSError:
                    continue
        for sub in TEMP_DIRS:
            top = root / sub
            if is_link(top) or not top.is_dir():
                continue
            for dirpath, dirs, names in os.walk(top):
                dirs[:] = [d for d in dirs if not is_link(Path(dirpath, d))]
                for n in names:
                    f = Path(dirpath, n)
                    if is_link(f) or not any(fnmatch.fnmatch(n, pat) for pat in TEMP_PATTERNS):
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if now - st.st_mtime >= TEMP_MIN_AGE_S:
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

    def plan(self, scan: dict | None = None, manual: bool = False) -> list[Action]:
        """manual: the user pressed Clean up, so what is posted or rejected goes now, not after the retention time."""
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
        if manual:
            for u in sc["units"]:
                if any(x.action == "delete_clip" and x.path == str(u["path"]) for x in acts):
                    continue
                if self._removable(u["clip_id"]):
                    orig = str(u["original"][0]) if u["original"] else None
                    acts = [x for x in acts if not (x.action in ("trash_clip", "delete_original")
                                                    and x.path in (str(u["path"]), orig))]
                    why = "posted" if self._posted(u["clip_id"]) else "rejected"
                    acts.append(Action("delete_clip", str(u["path"]), u["clip_id"], u["bytes"],
                                       "you asked to clean up: " + why))
        return acts

    # --- execution ---
    def _inside(self, p: Path, root: Path) -> bool:
        try:
            return p.parent.resolve().is_relative_to(root.resolve())
        except OSError:
            return False

    def _temp_ok(self, p: Path, root: Path) -> bool:
        """A temp file to delete must be a plain file with a temp name inside one of the app's own subfolders."""
        if not any(fnmatch.fnmatch(p.name, pat) for pat in TEMP_PATTERNS) or is_link(p):
            return False
        try:
            for sub in TEMP_DIRS:
                top = root / sub
                if is_link(top) or not top.is_dir() or not self._inside(p, top):
                    continue
                q = p.parent
                while q != top and q != q.parent:
                    if is_link(q):
                        return False
                    q = q.parent
                if q != top:
                    return False
                return p.is_file()
        except OSError:
            return False
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
                    if not self._temp_ok(p, root):
                        log.warning("refusing to delete unexpected temp path %s", p.name)
                        continue
                    p.unlink(missing_ok=True)
                elif a.action == "purge_trash":
                    if not (p.parent == trash_dir and TRASH_RE.match(p.name) and not p.is_symlink()):
                        log.warning("refusing to purge unexpected trash path %s", p.name)
                        continue
                    self._rm(p)
                    self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE trash_path=?", (str(p),))
                else:
                    asked = a.action == "delete_clip" and a.reason.startswith("you asked to clean up")
                    if a.clip_id is not None and self.posted_at(a.clip_id) is None and not (
                            asked and self._removable(a.clip_id)):
                        continue  # not (or no longer) fully posted: protected
                    if a.clip_id is not None and (self.publisher_holds(a.clip_id) or self.external_holds(a.clip_id)):
                        continue  # a provider or an outside sender may still read or resume an upload of this file
                    if not self._inside(p, root):
                        continue
                    if a.action == "delete_original":
                        p.unlink(missing_ok=True)
                    elif a.action == "delete_clip":
                        before = _size(p)
                        self._rm(p)
                        if p.exists():
                            errors.append(f"delete_clip {p.name}: some files could not be deleted")
                            freed += before - _size(p)
                            continue
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

    @staticmethod
    def summary(acts: list[Action], sc: dict) -> dict:
        """What a cleanup is about to remove, in the customer's words: posted clips, rejected clips, and the rest
        (older posted clips past their retention time, trash, temporary render files), plus what stays."""
        cats = {"posted": [0, 0], "rejected": [0, 0], "other": [0, 0]}
        for a in acts:
            k = "rejected" if a.reason.endswith("rejected") else "posted" if a.reason.endswith("posted") else "other"
            cats[k][0] += 1
            cats[k][1] += a.bytes
        return {**{k: {"items": n, "bytes": b} for k, (n, b) in cats.items()}, "kept_bytes": sc["protected_bytes"]}

    def cleanup(self, dry_run: bool = True, manual: bool = False) -> dict:
        if manual:
            self.adopt_folders()
        sc = self.scan()
        acts = self.plan(sc, manual=manual)
        out = {"dry_run": dry_run, "actions": [a.public(sc["cfg"]["dir"]) for a in acts],
               "summary": self.summary(acts, sc), "would_free_bytes": sum(a.bytes for a in acts)}
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
        return self.m.cleanup(dry_run=body.dry_run, manual=True)   # a press of the button, not the timer
