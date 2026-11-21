"""The pipeline store: channels, clips and their log, with the original `Store` API, on the app database.

One store serves the whole pipeline: the engine (monitor), cutting/rendering (edit) and the selection functions
(select) read and write the same clip rows. Code addresses the original logical table names
(`store.update("clips", ...)`, `store.q("SELECT ... FROM clips ...")`); they are rewritten to the tables of the
`monitor` namespace:

  channels, clips, log, clip_ledger, clip_descriptors, vod_imports, recovery_evidence -> monitor_<name>
  clip_channels           -> view over clips + clip_ledger (as in the original)
  destinations, deliveries -> read-only views over the publishing tables (publish_destinations,
                              publish_deliveries) with the original column names
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any

from . import formats as _formats  # noqa: F401  (select_* tables the selection resolver reads)
from . import presets as _presets  # noqa: F401  (edit_presets)
from .db import Database, register_migrations
from .publish import store as _publish_store  # noqa: F401  (publish_* tables behind the delivery views)

log = logging.getLogger(__name__)

register_migrations(
    "monitor",
    [
        """
CREATE TABLE monitor_channels (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
  settings TEXT NOT NULL DEFAULT '{}', armed INTEGER NOT NULL DEFAULT 0,
  format_id TEXT, publish_platforms TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL, UNIQUE(platform, name));
CREATE TABLE monitor_clips (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, status TEXT NOT NULL, kind TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT '{}', window_start REAL, window_end REAL, action_time REAL,
  delay REAL, epoch INTEGER, truncated TEXT DEFAULT '', title TEXT DEFAULT '',
  source_lang TEXT DEFAULT '', captions TEXT NOT NULL DEFAULT '{}',
  layout TEXT NOT NULL DEFAULT '{}', timings TEXT NOT NULL DEFAULT '{}',
  files TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '', updated_at REAL);
CREATE TABLE monitor_log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, channel_id INTEGER,
  clip_id INTEGER, message TEXT NOT NULL);
CREATE INDEX monitor_log_channel ON monitor_log(channel_id, id);
CREATE TABLE monitor_clip_ledger (
  clip_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
  retired_at REAL NOT NULL);
""",
        # v2: one clip store for monitor, edit and select: the clip's editing preset and per-clip overrides, the
        # original ledger/descriptor/import/evidence tables the selection functions query, and the views
        """
ALTER TABLE monitor_clips ADD COLUMN preset_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE monitor_clips ADD COLUMN overrides TEXT NOT NULL DEFAULT '{}';
CREATE INDEX monitor_clips_channel ON monitor_clips(channel_id, id);
DROP TABLE monitor_clip_ledger;
CREATE TABLE monitor_clip_ledger (
  clip_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, creator_key TEXT NOT NULL, moment_id TEXT NOT NULL,
  moment_creator TEXT NOT NULL, action_time REAL, window_start REAL, window_end REAL, dedupe_key TEXT NOT NULL,
  source_lang TEXT NOT NULL DEFAULT '', fingerprints TEXT NOT NULL DEFAULT '{}', clip_created_at REAL NOT NULL,
  confirmed_at REAL NOT NULL, retired_at REAL NOT NULL, reason TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX monitor_clip_ledger_dedupe ON monitor_clip_ledger(dedupe_key);
CREATE TABLE monitor_clip_descriptors (
  clip_id INTEGER PRIMARY KEY, moment_id TEXT NOT NULL DEFAULT '', creator_key TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '', source_fp TEXT NOT NULL DEFAULT '', features TEXT NOT NULL DEFAULT '{}',
  labels TEXT NOT NULL DEFAULT '[]', missing TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE TABLE monitor_vod_imports (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  media TEXT NOT NULL DEFAULT '{}', chat TEXT NOT NULL DEFAULT '{}', media_fp TEXT NOT NULL DEFAULT '',
  chat_fp TEXT NOT NULL DEFAULT '', vod_start REAL, offset_s REAL NOT NULL DEFAULT 0, timeline_base REAL,
  coverage TEXT NOT NULL DEFAULT '{}', triggers TEXT NOT NULL DEFAULT '[]', clip_ids TEXT NOT NULL DEFAULT '[]',
  progress TEXT NOT NULL DEFAULT '{}', clips_created INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, finished_at REAL,
  auto_import INTEGER NOT NULL DEFAULT 0);
CREATE TABLE monitor_recovery_evidence (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', account_key TEXT NOT NULL DEFAULT '',
  provider_post_id TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '', clip_id INTEGER, channel_id INTEGER,
  at REAL, state TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '{}', imported INTEGER NOT NULL DEFAULT 0,
  source_sha256 TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE VIEW monitor_clip_channels AS
  SELECT id, channel_id FROM monitor_clips UNION ALL SELECT clip_id AS id, channel_id FROM monitor_clip_ledger;
CREATE VIEW monitor_destinations AS
  SELECT id, provider, label, account, settings, status, status_detail, created_at, created_at AS updated_at,
         enabled, rights_ack_at FROM publish_destinations;
CREATE VIEW monitor_deliveries AS
  SELECT id, clip_id, destination_id, language, idempotency_key, status, mode, attempts, provider_ref,
         provider_post_id, provider_url, outcome, last_error, next_attempt_at, history, created_at, updated_at,
         reconcile, version, media_version, blocked_from, claimed_at, '' AS account_key,
         COALESCE(public_at, 0) AS public_at FROM publish_deliveries;
""",
    ],
)

JSON_COLS = {"settings", "trigger", "captions", "layout", "timings", "files", "history", "evidence", "media",
             "chat", "coverage", "triggers", "clip_ids", "progress", "publish_platforms", "overrides"}
LIST_COLS = {"history", "publish_platforms"}
LOG_KEEP = 2000
TABLES = ("channels", "clips", "log", "clip_ledger", "clip_descriptors", "vod_imports", "recovery_evidence",
          "clip_channels", "destinations", "deliveries")
_TABLE = re.compile(rf"\b(FROM|INTO|UPDATE|JOIN)\s+({'|'.join(TABLES)})\b", re.I)


def _sql(sql: str) -> str:
    return _TABLE.sub(lambda m: f"{m.group(1)} monitor_{m.group(2)}", sql)


class Store:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._in_tx = False

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else ([] if k in LIST_COLS else {})
        return d

    @contextlib.contextmanager
    def tx(self):
        """One write transaction (BEGIN IMMEDIATE); nested use joins the outer one."""
        with self._lock:
            outer = not self._in_tx
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
                self._in_tx = True
            try:
                yield self
                if outer:
                    self.db.execute("COMMIT")
            except BaseException:
                if outer:
                    self.db.execute("ROLLBACK")
                raise
            finally:
                if outer:
                    self._in_tx = False

    def q(self, sql: str, *args) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self.db.execute(_sql(sql), args).fetchall()]

    def q1(self, sql: str, *args) -> dict | None:
        with self._lock:
            return self._row(self.db.execute(_sql(sql), args).fetchone())

    def x(self, sql: str, *args) -> int:
        with self._lock:
            cur = self.db.execute(_sql(sql), args)
            return cur.lastrowid or cur.rowcount

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        if table == "clips":
            fields["updated_at"] = time.time()
        cols = [f"{k}=?" for k in fields]
        vals = [json.dumps(v) if k in JSON_COLS else v for k, v in fields.items()]
        self.x(f"UPDATE {table} SET {', '.join(cols)} WHERE id=?", *vals, row_id)

    def merge_json(self, table: str, row_id: int, col: str, patch: dict) -> dict:
        """Atomic read-modify-write of a JSON column (one level deep)."""
        with self._lock:
            row = self.q1(f"SELECT {col} FROM {table} WHERE id=?", row_id)
            cur = (row or {}).get(col) or {}
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(cur.get(k), dict):
                    cur[k] = {**cur[k], **v}
                else:
                    cur[k] = v
            self.update(table, row_id, **{col: cur})
            return cur

    # ---- channels --------------------------------------------------------
    def add_channel(
        self, platform: str, name: str, source_url: str = "", settings: dict | None = None, **extra: Any
    ) -> int:
        cid = self.x(
            "INSERT INTO channels(platform,name,source_url,settings,created_at) VALUES(?,?,?,?,?)",
            platform,
            name,
            source_url,
            json.dumps(settings or {}),
            time.time(),
        )
        self.update("channels", cid, **extra)
        return cid

    def channel(self, cid: int) -> dict | None:
        return self.q1("SELECT * FROM channels WHERE id=?", cid)

    def channels(self) -> list[dict]:
        return self.q("SELECT * FROM channels ORDER BY id")

    # ---- clips -----------------------------------------------------------
    def create_clip(self, channel_id: int, dedupe_key: str, kind: str, trigger: dict, **fields) -> int | None:
        """Insert a clip; None if the dedupe key exists (idempotent), also for a retired clip (ledger)."""
        now = time.time()
        if self.q1("SELECT 1 FROM clip_ledger WHERE dedupe_key=?", dedupe_key):
            return None
        try:
            with self.tx():  # never reuse an id the ledger or a delivery still references
                used = (
                    self.q1(
                        "SELECT MAX(m) AS m FROM (SELECT MAX(id) AS m FROM clips UNION ALL "
                        "SELECT MAX(clip_id) FROM clip_ledger UNION ALL SELECT MAX(clip_id) FROM deliveries)"
                    )["m"]
                    or 0
                )
                cid = used + 1
                self.x(
                    "INSERT INTO clips(id,channel_id,dedupe_key,created_at,updated_at,status,kind,trigger) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    cid,
                    channel_id,
                    dedupe_key,
                    now,
                    now,
                    "pending",
                    kind,
                    json.dumps(trigger),
                )
        except sqlite3.IntegrityError:
            return None
        self.update("clips", cid, **fields)
        return cid

    def clip(self, cid: int) -> dict | None:
        return self.q1("SELECT * FROM clips WHERE id=?", cid)

    def clips(self, channel_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        if channel_id is None:
            return self.q("SELECT * FROM clips ORDER BY id DESC LIMIT ? OFFSET ?", limit, offset)
        return self.q(
            "SELECT * FROM clips WHERE channel_id=? ORDER BY id DESC LIMIT ? OFFSET ?", channel_id, limit, offset
        )

    def mark(self, clip_id: int, key: str, ts: float | None = None) -> None:
        """Record a latency milestone (first write wins)."""
        with self._lock:
            t = self.q1("SELECT timings FROM clips WHERE id=?", clip_id)["timings"]
            if key not in t:
                t[key] = ts if ts is not None else time.time()
                self.update("clips", clip_id, timings=t)

    # ---- log -------------------------------------------------------------
    def log(
        self, message: str, level: str = "info", channel_id: int | None = None, clip_id: int | None = None
    ) -> None:
        with self._lock:
            rid = self.x(
                "INSERT INTO log(ts,level,channel_id,clip_id,message) VALUES(?,?,?,?,?)",
                time.time(),
                level,
                channel_id,
                clip_id,
                message[:500],
            )
            if rid % 200 == 0:
                self.x("DELETE FROM log WHERE id < ?", rid - LOG_KEEP)
        if level in ("warn", "error"):
            log.log(logging.ERROR if level == "error" else logging.WARNING, "%s", message[:500])
