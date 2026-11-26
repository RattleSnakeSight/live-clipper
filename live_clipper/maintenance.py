"""Disk cap, backpressure and cleanup for local clip files. Driven by the wizard's storage settings.

Ported from the original maintenance logic (values and order kept), minus the Drive cold tier.

Rules that never bend: clips that are queued, held, uploading or simply not posted yet are never deleted.
Files this module does not know about are never deleted either, except temporary render files.

What the user's own presses do (immediately, not on a schedule): Reject removes that clip's files, and Clean up
now also removes every fully posted clip and every clip the user rejected, found by its folder even when the
app never tracked it. A clip that is waiting, in flight, shared, approved or not decided keeps its files, and so
does a clip an outside sender (the shared claims table of a hand-patched installation) may still post or has
claimed and not confirmed. Posting to the test destination alone never makes a clip removable, for the button
and for the automatic passes alike. The rows that show what was sent (deliveries, claims, the clip row) always
stay.

Clean up now deletes only what the user confirmed: the preview (a dry run) remembers each item it listed, by what
it would do and by what is on the disk (the clip, the path, and the identity, sizes and times of its files). The
confirmation names that preview; it can be used once, for PLAN_TTL_S, and not after a restart. A manual deleting
run that names no preview is refused (PlanError "plan_required") and deletes nothing; only the timer's own pass
plans and runs at once. Each confirmed item runs only if a fresh plan still lists it the same way and its files
are unchanged, and only after the protections above are checked again; anything new is left for the next
preview, and the result says what was done, what was left and why.

A folder or file that another kept clip's tracked path shares (the same path, a path inside it, or a folder
holding it) is never moved or deleted as a clip, nor its original, by any pass: the clips that use it keep it.
A delivery whose destination is unknown (no provider recorded) never counts as a real post.

A clip being captured, cut or rendered right now (the edit engine's busy(clip_id), the monitor engine's clip
tasks) keeps its files whatever it is otherwise eligible for: no pass lists it, its folder or its original, nor a
temporary file in its folder, and the check is made again right before each removal (Reject included). It is
eligible again once that work finishes; a Reject deferred for it is completed by the next Clean up now.

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
import contextlib
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
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
TEST_DEST = "test"                                    # the local test destination: posts nothing anywhere
PLAN_TTL_S = 600                                      # how long a previewed cleanup may be confirmed
PLAN_MAX = 8                                          # previews remembered at once (several open tabs)
FILE_ACTIONS = ("delete_temp", "delete_original")     # remove one file; the other actions remove a folder
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


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _ancestors(n: str):
    """The folders above a normalized path, nearest first."""
    while (up := os.path.dirname(n)) and up != n:
        yield up
        n = up


def _fingerprint(p: Path, skip: frozenset = frozenset()) -> str:
    """What is on the disk at `p`: its own identity and, for a folder, every name, file identity, size and
    modification time inside it (links are listed, never followed). A path that was emptied and reused, or a
    file replaced by another of the same name, gives a different value. `skip` leaves out files an earlier item
    of the same plan removes first."""
    try:
        st = os.lstat(p)
    except OSError:
        return "missing"
    h = hashlib.sha256(f"{st.st_dev}:{st.st_ino}:{stat.S_IFMT(st.st_mode)}".encode())
    if is_link(p):
        return h.hexdigest()
    if stat.S_ISDIR(st.st_mode):
        for dirpath, dirs, names in os.walk(p):
            dirs.sort()
            for d in dirs:
                h.update(f"d|{Path(dirpath, d).relative_to(p).as_posix()}\n".encode())
            dirs[:] = [d for d in dirs if not is_link(Path(dirpath, d))]
            for n in sorted(names):
                f = Path(dirpath, n)
                if _norm(f) in skip:
                    continue
                try:
                    fs = os.lstat(f)
                except OSError:
                    continue
                h.update(f"f|{f.relative_to(p).as_posix()}|{fs.st_ino}|{fs.st_size}|{fs.st_mtime_ns}\n".encode())
    else:
        h.update(f"|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


class PlanError(Exception):
    """A manual run that cannot go ahead: no preview named (plan_required), or a confirmation that is unknown (or
    from before a restart), expired, already used, or changed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Maintenance:
    def __init__(self, ctx: AppContext, clock=time.time, free_fn=None):
        self.ctx, self.clock = ctx, clock
        self.media_dir = Path(ctx.settings.get("storage.dir") or ctx.paths.media)
        self._free = free_fn or (lambda p: shutil.disk_usage(p).free)
        self.captures_paused = False
        self._run_lock = threading.Lock()              # one deleting pass at a time (timer, button, old callers)
        self._plans_lock = threading.Lock()
        self._plans: dict[str, dict] = {}              # previews that may still be confirmed, in memory only
        self._used: dict[str, float] = {}              # confirmations already taken, to say so on a repeat

    # --- config ---
    def config(self) -> dict:
        s = self.ctx.settings
        rules = s.get("storage.cleanup_rules", ["expire_posted", "clean_when_full"])
        return {
            "dir": self.media_dir,
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
        dest = TEST_DEST if p.get("test") else str(p.get("destination", ""))
        self.ctx.db.execute("INSERT OR REPLACE INTO maintenance_delivery VALUES(?,?,?,?,?)",
                            (str(p.get("delivery_id")), str(p["clip_id"]), dest,
                             str(p["state"]).lower(), self.clock()))

    def posted_at(self, clip_id: str) -> float | None:
        """When the clip was last really posted, if it counts as posted, else None. Publishing's own table decides
        when it has rows for the clip: every delivery settled and at least one real one reached its platform.
        Otherwise the deliveries recorded from the bus: every one finished and at least one real one published.
        A delivery to the test destination posts nothing, so it never counts: a simulation must not make real
        media removable (by the button, the retention time or the disk cap). Nor does a delivery whose destination
        is unknown (removed, or no provider recorded): it must be settled like any other, but alone it is no post."""
        rows = self._deliveries(clip_id)
        if rows:
            real = [r for r in rows if r["provider"] not in (TEST_DEST, "")]
            if not (real and all(r["status"] in SETTLED for r in rows) and any(r["status"] in SENT for r in real)):
                return None
            return max(float(r["ts"] or 0) for r in real if r["status"] in SENT)
        rows = self.ctx.db.execute("SELECT state, ts, destination FROM maintenance_delivery WHERE clip_id=?",
                                   (str(clip_id),)).fetchall()
        if not rows or any(r[0] not in TERMINAL for r in rows):
            return None
        pub = [r[1] for r in rows if r[0] == "published" and r[2] not in (TEST_DEST, "", None)]
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
                "SELECT d.id, d.status, d.reconcile, COALESCE(t.provider, ''), d.updated_at FROM publish_deliveries d "
                "LEFT JOIN publish_destinations t ON t.id=d.destination_id WHERE d.clip_id=?",
                (int(clip_id) if str(clip_id).isdigit() else clip_id,)).fetchall()
        except sqlite3.OperationalError:               # no publishing tables in this database
            return []
        return [{"id": r[0], "status": r[1], "reconcile": r[2], "provider": r[3], "ts": r[4]} for r in rows]

    def _posted(self, clip_id) -> bool:
        """Every delivery of the clip is settled and at least one real one reached its platform (see posted_at)."""
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

    # --- folders shared by several clips ---
    def _claims(self) -> tuple[dict, dict]:
        """Where every kept clip's tracked files are: `exact` maps a normalized path to the clips tracked there,
        `below` a folder to the clips tracked somewhere inside it. Read fresh each time it is asked."""
        exact: dict[str, set] = {}
        below: dict[str, set] = {}
        for cid, unit in self.ctx.db.execute("SELECT clip_id, unit_path FROM maintenance_clip "
                                             "WHERE state='kept'").fetchall():
            n = _norm(unit)
            exact.setdefault(n, set()).add(str(cid))
            for up in _ancestors(n):
                below.setdefault(up, set()).add(str(cid))
        return exact, below

    def _shared_with(self, path, clip_id, claims: tuple | None = None) -> set:
        """The other kept clips whose tracked files `path` would take with it: tracked at the same path, inside
        it, or in a folder that holds it. Empty when only this clip uses it. `claims` (from _claims) serves a
        whole scan; without it the tracking table is read now, as the check right before a removal needs."""
        n, me = _norm(path), str(clip_id)
        if claims is not None:
            exact, below = claims
            others = set(exact.get(n, ())) | set(below.get(n, ()))
            for up in _ancestors(n):
                others |= exact.get(up, set())
            return others - {me}
        inside, others = os.path.join(n, ""), set()
        for cid, unit in self.ctx.db.execute("SELECT clip_id, unit_path FROM maintenance_clip "
                                             "WHERE state='kept' AND clip_id<>?", (me,)).fetchall():
            u = _norm(unit)
            if u == n or u.startswith(inside) or n.startswith(os.path.join(u, "")):
                others.add(str(cid))
        return others

    # --- clips being captured, cut or rendered right now ---
    def _rendering(self) -> tuple[set, set] | None:
        """The clips in use now and the folders they use. In use: the edit engine's busy(clip_id) for each of its
        clip tasks, and every clip task of the monitor engine (capture, cut, render). Their folders: the edit
        engine's clip_dir, the store's clips/<id> and every tracked path of the clip, normalized. Either engine may
        be absent (not started, tests). None when an engine could not be asked: then nothing tied to a clip goes."""
        edit = mon = None
        for svc in self.ctx.services:
            name = getattr(svc, "name", "")
            if name == "edit":
                edit = getattr(svc, "engine", None)
            elif name == "monitor":
                mon = getattr(svc, "engine", None)
        if edit is None:                               # the monitor's own edit engine, when no edit service runs
            edit = getattr(mon, "edit", None)
        try:
            ids = {k for k in list(getattr(edit, "clip_tasks", None) or {}) if edit.busy(k)}
            ids |= set(list(getattr(mon, "clip_tasks", None) or {}))
            dirs = set()
            for k in ids:
                dirs.add(_norm(self.media_dir / "clips" / str(k)))
                if hasattr(edit, "clip_dir"):
                    dirs.add(_norm(edit.clip_dir(k)))
                dirs |= {_norm(r[0]) for r in self.ctx.db.execute(
                    "SELECT unit_path FROM maintenance_clip WHERE clip_id=? AND state='kept'", (str(k),))}
        except Exception:
            log.exception("could not ask the edit or monitor engine which clips are in use; keeping them")
            return None
        return {str(k) for k in ids}, dirs

    @staticmethod
    def _in_use(path, clip_id, busy: tuple | None) -> bool:
        """Would removing `path` (of clip `clip_id`; None for a temporary file) take files a running clip task
        uses? The clip itself is busy, or the path is a busy folder, is inside one or holds one. `busy` comes from
        _rendering; None (the engines could not be asked) counts as in use."""
        if busy is None:
            return True
        ids, dirs = busy
        if clip_id is not None and str(clip_id) in ids:
            return True
        n = _norm(path)
        inside = os.path.join(n, "")
        return n in dirs or any(up in dirs for up in _ancestors(n)) or any(d.startswith(inside) for d in dirs)

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
        """Delete one clip's folder now. Refuses a folder outside the store or one another kept clip shares (the
        same folder, a file of it inside, or a folder holding it). Keeps every database row; the clip row gets a
        note of what was removed and why."""
        cid, root = str(clip_id), self.config()["dir"]
        row = self.ctx.db.execute("SELECT unit_path FROM maintenance_clip WHERE clip_id=? AND state='kept'",
                                  (cid,)).fetchone()
        p = Path(row[0]) if row else root / "clips" / cid
        if is_link(p) or p == root or not self._inside(p, root):
            return {"removed": False, "freed_bytes": 0, "kept_because": "its folder is not inside the storage folder"}
        if self._in_use(p, cid, self._rendering()):
            return {"removed": False, "freed_bytes": 0, "kept_because": "it is being rendered now"}
        if self._shared_with(p, cid):
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

    def reject_clip(self, clip_id, *, rendering: bool = False) -> dict:
        """The user pressed Reject: what was only waiting is cancelled, then the clip's files are removed unless an
        upload may still need them. Returns {"removed", "freed_bytes", "kept_because"}."""
        cid, pub = str(clip_id), self._publisher()
        if cid.isdigit() and not self._rejected(cid):
            from . import gameplay
            from .store import Store
            clips = Store(self.ctx.db)
            if clips.clip(int(cid)):
                gameplay.review(clips, int(cid), False)
        if pub is not None:
            pub.cancel_waiting(clip_id)
        if rendering:
            return {"removed": False, "freed_bytes": 0, "kept_because": "it is being rendered now"}
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
        claims = self._claims()
        busy = self._rendering()
        for cid, unit, created, state, tpath, purge_after in rows:
            p = Path(tpath if state == "trash" else unit)
            if not self._inside(p, root):
                continue  # Keep other media folders' records when switching drives, including offline drives.
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
                entry["shared"] = bool(self._shared_with(p, cid, claims))   # never a candidate, by any pass
                entry["busy"] = self._in_use(p, cid, busy)                   # nor while it is being rendered
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
                    if self._in_use(f, None, busy):                # a running render's own temporary file
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
        protected = {_norm(u["path"]): u["bytes"] for u in units
                     if u["posted_at"] is None or u["shared"] or u["busy"]}
        return {"cfg": cfg, "units": units, "trash": trash, "temp": temp, "total": total, "free": free,
                "kept_bytes": kept, "trash_bytes": trash_b, "temp_bytes": sum(t["bytes"] for t in temp),
                "protected_bytes": sum(protected.values())}

    def plan(self, scan: dict | None = None, manual: bool = False) -> list[Action]:
        """manual: the user pressed Clean up, so what is posted or rejected goes now, not after the retention time.
        A clip whose folder another kept clip shares, or that is being rendered now, is never listed (neither the
        folder nor its original)."""
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
        posted = sorted((u for u in sc["units"] if u["posted_at"] is not None and not u.get("shared", True)
                         and not u.get("busy", True)), key=lambda u: u["posted_at"])
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
                if u.get("shared", True) or u.get("busy", True) or any(
                        x.action == "delete_clip" and x.path == str(u["path"]) for x in acts):
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

    @staticmethod
    def _key(a: Action) -> tuple:
        """Which candidate an action is: what it does, to which clip and path, and why (the confirmation's words)."""
        return (a.action, _norm(a.path), a.clip_id, a.reason)

    def _identity(self, a: Action, skip: frozenset = frozenset()) -> str:
        """The candidate as it is now: its key, the tracking record of its clip, and what is on the disk."""
        row = None
        if a.clip_id is not None:
            p = Path(a.path)
            row = self.ctx.db.execute(
                "SELECT created_at, state, trash_at FROM maintenance_clip WHERE clip_id=? AND state IN ('kept','trash') "
                "AND (unit_path IN (?,?) OR trash_path=?) ORDER BY created_at, unit_path",
                (a.clip_id, str(p), str(p.parent), str(p))).fetchone()
        return hashlib.sha256(repr((self._key(a), tuple(row) if row else None,
                                    _fingerprint(Path(a.path), skip))).encode()).hexdigest()

    def _kept_reason(self, a: Action) -> str:
        """Why a clip action may not run now ("" = it may). Checked again right before each removal."""
        if a.clip_id is None:
            return ""
        asked = a.action == "delete_clip" and a.reason.startswith("you asked to clean up")
        if self.posted_at(a.clip_id) is None and not (asked and self._removable(a.clip_id)):
            return "it is not (or no longer) fully posted or rejected"
        p = Path(a.path)                               # an original is judged by the folder that holds it
        if self._shared_with(p.parent if a.action == "delete_original" else p, a.clip_id):
            return "the folder is shared with another clip"
        if self._in_use(p, a.clip_id, self._rendering()):
            return "it is being rendered now"
        if self.publisher_holds(a.clip_id):
            return "an upload of this clip may still need its files"
        if why := self._external_hold(a.clip_id):
            return why
        return ""

    def execute(self, acts: list[Action], expect: dict | None = None) -> dict:
        """Run the actions, checking every protection again first. `expect` (key -> identity) is what the user
        confirmed: an action whose candidate is not exactly that any more is left alone. Bytes freed are measured
        on the disk; `results` says, item by item, what was done, kept or failed and why."""
        cfg = self.config()
        root, now = cfg["dir"], self.clock()
        trash_dir = root / "trash"
        done, freed, errors, results = 0, 0, [], []

        def note(a: Action, outcome: str, why: str = "", got: int = 0) -> None:
            results.append({**a.public(root), "outcome": outcome, "why": why, "freed_bytes": got})

        for a in acts:
            p = Path(a.path)
            got = 0
            try:
                if expect is not None and expect.get(self._key(a)) != self._identity(a):
                    # An earlier action may have kept a temporary file because this folder is in use.
                    busy = self._in_use(p, a.clip_id, self._rendering())
                    note(a, "kept", "it is being rendered now" if busy else "it changed since the preview")
                    continue
                if a.action == "delete_temp":
                    if not self._temp_ok(p, root):
                        log.warning("refusing to delete unexpected temp path %s", p.name)
                        note(a, "kept", "it is not a temporary file in the app's folders")
                        continue
                    if self._in_use(p, None, self._rendering()):
                        note(a, "kept", "it is being rendered now")
                        continue
                    got = _size(p)
                    p.unlink(missing_ok=True)
                elif a.action == "purge_trash":
                    if not (p.parent == trash_dir and TRASH_RE.match(p.name) and not p.is_symlink()):
                        log.warning("refusing to purge unexpected trash path %s", p.name)
                        note(a, "kept", "it is not an entry of the app's trash")
                        continue
                    before = _size(p)
                    self._rm(p)
                    got = before - (_size(p) if p.exists() else 0)
                    if p.exists():
                        errors.append(f"purge_trash {p.name}: some files could not be deleted")
                        freed += got
                        note(a, "failed", "some files could not be deleted (they may be in use)", got)
                        continue
                    self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE trash_path=?", (str(p),))
                else:
                    if why := self._kept_reason(a):
                        note(a, "kept", why)  # protected now, whatever the plan said
                        continue
                    if not self._inside(p, root):
                        note(a, "kept", "it is not inside the storage folder")
                        continue
                    if a.action == "delete_original":
                        got = _size(p)
                        p.unlink(missing_ok=True)
                    elif a.action == "delete_clip":
                        before = _size(p)
                        self._rm(p)
                        got = before - (_size(p) if p.exists() else 0)
                        if p.exists():
                            errors.append(f"delete_clip {p.name}: some files could not be deleted")
                            freed += got
                            note(a, "failed", "some files could not be deleted (they may be in use)", got)
                            continue
                        self.ctx.db.execute("UPDATE maintenance_clip SET state='deleted' WHERE clip_id=? AND unit_path=?",
                                            (a.clip_id, str(p)))
                    elif a.action == "trash_clip":
                        trash_dir.mkdir(parents=True, exist_ok=True)
                        stamp, safe = int(now), SAFE_ID.sub("_", a.clip_id)[:64]
                        while (dest := trash_dir / f"{safe}-{stamp}").exists():
                            stamp += 1
                        os.rename(p, dest)                 # still on this disk: restorable, nothing freed yet
                        self.ctx.db.execute(
                            "UPDATE maintenance_clip SET state='trash', trash_path=?, trash_at=?, purge_after=? "
                            "WHERE clip_id=? AND unit_path=?",
                            (str(dest), now, now + cfg["trash_keep_h"] * 3600, a.clip_id, str(p)))
                done += 1
                freed += got
                note(a, "done", "", got)
            except OSError as e:
                errors.append(f"{a.action} {p.name}: {e.__class__.__name__}")
                note(a, "failed", e.__class__.__name__)
        return {"done": done, "freed_bytes": freed, "errors": errors, "results": results}

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

    def _report_run(self, out: dict, res: dict, left: list[dict], new: list[Action], sc: dict) -> dict:
        """The result of a deleting pass: the summary counts what was really removed and its measured bytes; `left`
        are confirmed items no fresh plan lists any more, `new` what a fresh plan lists that was not confirmed."""
        results = res["results"] + left
        done = [Action(r["action"], r["path"], r["clip_id"], r["freed_bytes"], r["reason"])
                for r in results if r["outcome"] == "done"]
        out.update(res, results=results, summary=self.summary(done, self.scan() if done else sc),
                   kept=sum(r["outcome"] == "kept" for r in results),
                   not_confirmed={"items": len(new), "bytes": sum(a.bytes for a in new)})
        return out

    def _remember(self, acts: list[Action], manual: bool, root: Path) -> tuple[str, float]:
        """Keep a preview so it can be confirmed: each item's key and identity, in the order shown."""
        items, removed_first = [], set()
        for a in acts:
            items.append({"key": self._key(a), "identity": self._identity(a, frozenset(removed_first)),
                          "public": a.public(root)})
            if a.action in FILE_ACTIONS:                   # a later folder item is checked without this file
                removed_first.add(_norm(a.path))
        pid, now = uuid.uuid4().hex, self.clock()
        with self._plans_lock:
            for k in [k for k, v in self._plans.items() if now - v["at"] > PLAN_TTL_S]:
                del self._plans[k]
            self._plans[pid] = {"at": now, "root": _norm(root), "manual": manual, "items": items}
            while len(self._plans) > PLAN_MAX:
                del self._plans[next(iter(self._plans))]
        return pid, now + PLAN_TTL_S

    def _take(self, plan_id: str) -> dict:
        """The confirmed preview, taken once: a second or simultaneous confirmation of it gets PlanError."""
        now = self.clock()
        with self._plans_lock:
            entry = self._plans.pop(plan_id, None)
            if entry is None:
                if plan_id in self._used:
                    raise PlanError("plan_used", "This cleanup was already run. Preview again to see what is left.")
                raise PlanError("plan_unknown", "This preview is no longer known (the app may have restarted). "
                                                "Preview again, then confirm.")
            if now - entry["at"] > PLAN_TTL_S:
                raise PlanError("plan_expired", "This preview is too old to confirm. Preview again, then confirm.")
            self._used[plan_id] = now
            while len(self._used) > 64:
                del self._used[next(iter(self._used))]
        return entry

    def _run_confirmed(self, plan_id: str) -> dict:
        entry = self._take(plan_id)
        with self._run_lock:
            if entry["manual"]:
                self.adopt_folders()
            sc = self.scan()
            root = sc["cfg"]["dir"]
            if _norm(root) != entry["root"]:
                raise PlanError("plan_changed", "The storage folder changed since the preview. Preview again.")
            fresh = {self._key(a): a for a in self.plan(sc, manual=entry["manual"])}
            confirmed = {tuple(i["key"]) for i in entry["items"]}
            run, left = [], []
            for i in entry["items"]:
                a = fresh.get(tuple(i["key"]))
                if a is None:
                    left.append({**i["public"], "outcome": "kept", "why": "it is no longer eligible", "freed_bytes": 0})
                else:
                    run.append(a)
            res = self.execute(run, expect={tuple(i["key"]): i["identity"] for i in entry["items"]})
            out = {"dry_run": False, "plan_id": plan_id, "actions": [a.public(root) for a in run],
                   "would_free_bytes": sum(a.bytes for a in run)}
            return self._report_run(out, res, left, [a for k, a in fresh.items() if k not in confirmed], sc)

    def cleanup(self, dry_run: bool = True, manual: bool = False, plan_id: str | None = None) -> dict:
        """dry_run: plan only, and remember the plan under `plan_id` so it can be confirmed. Not a dry run with a
        `plan_id`: run what that preview listed and nothing else. Not a dry run without one: only the timer's own
        pass (manual false) plans and runs at once; a manual one is refused with PlanError("plan_required") before
        anything is scanned or deleted, so every deletion the user asks for names the preview it confirms."""
        if plan_id is not None and not dry_run:
            return self._run_confirmed(plan_id)
        if manual and not dry_run:
            raise PlanError("plan_required", "Preview required: nothing was deleted. Preview the cleanup first, "
                                             "then confirm that preview.")
        with contextlib.nullcontext() if dry_run else self._run_lock:
            if manual:
                self.adopt_folders()
            sc = self.scan()
            acts = self.plan(sc, manual=manual)
            root = sc["cfg"]["dir"]
            out = {"dry_run": dry_run, "actions": [a.public(root) for a in acts],
                   "summary": self.summary(acts, sc), "would_free_bytes": sum(a.bytes for a in acts)}
            if dry_run:
                out["plan_id"], out["expires_at"] = self._remember(acts, manual, root)
                return out
            return self._report_run(out, self.execute(acts), [], [], sc)

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
            "dir": str(cfg["dir"]), "configured_dir": str(self.ctx.settings.get("storage.dir") or self.ctx.paths.media),
            "usage_bytes": sc["total"], "cap_bytes": cfg["cap_bytes"],
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
    plan_id: str | None = None      # with dry_run false: run only what this preview listed


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
        """A press of the button, not the timer. A deleting request must confirm a preview by its plan_id: without
        one (an older page, a script) it is refused with 409 "plan_required", and an unknown, expired, used or
        changed one is refused (409) too; nothing is deleted in either case."""
        try:
            return self.m.cleanup(dry_run=body.dry_run, manual=True, plan_id=body.plan_id)
        except PlanError as e:
            raise HTTPException(409, {"message": str(e), "code": e.code}) from None
