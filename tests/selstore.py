"""Test-only SQLite store for the selection modules (candidates, moments, winners, freshness).

It mirrors the subset of the reference job store these modules call (q, q1, x, update, merge_json, tx,
add_channel, add_destination, channel, channels, create_clip, clip, log) and the tables they query, with the
same
DDL and semantics. JSON columns are decoded on read and encoded by update(). The integrator supplies the real
store adapter; this file only exists so the selection tests run against real SQL.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL, source_url TEXT,
  settings TEXT NOT NULL DEFAULT '{}', armed INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, UNIQUE(platform, name));
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, status TEXT NOT NULL, kind TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT '{}', window_start REAL, window_end REAL, action_time REAL,
  delay REAL, epoch INTEGER, truncated TEXT DEFAULT '', title TEXT DEFAULT '',
  source_lang TEXT DEFAULT '', captions TEXT NOT NULL DEFAULT '{}',
  layout TEXT NOT NULL DEFAULT '{}', timings TEXT NOT NULL DEFAULT '{}',
  files TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '', updated_at REAL);
CREATE TABLE IF NOT EXISTS destinations (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
  account TEXT DEFAULT '', settings TEXT NOT NULL DEFAULT '{}', status TEXT DEFAULT 'disconnected',
  status_detail TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL, destination_id INTEGER NOT NULL,
  language TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'manual', attempts INTEGER NOT NULL DEFAULT 0,
  provider_ref TEXT DEFAULT '', provider_post_id TEXT DEFAULT '', provider_url TEXT DEFAULT '',
  outcome TEXT DEFAULT '', last_error TEXT DEFAULT '', next_attempt_at REAL DEFAULT 0,
  history TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  reconcile INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
  media_version INTEGER NOT NULL DEFAULT 0, blocked_from TEXT NOT NULL DEFAULT '',
  claimed_at REAL NOT NULL DEFAULT 0, account_key TEXT NOT NULL DEFAULT '', public_at REAL NOT NULL DEFAULT 0,
  UNIQUE(clip_id, destination_id, language, version));
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, channel_id INTEGER,
  clip_id INTEGER, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vod_imports (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  media TEXT NOT NULL DEFAULT '{}', chat TEXT NOT NULL DEFAULT '{}', media_fp TEXT NOT NULL DEFAULT '',
  chat_fp TEXT NOT NULL DEFAULT '', vod_start REAL, offset_s REAL NOT NULL DEFAULT 0, timeline_base REAL,
  coverage TEXT NOT NULL DEFAULT '{}', triggers TEXT NOT NULL DEFAULT '[]', clip_ids TEXT NOT NULL DEFAULT
  '[]',
  progress TEXT NOT NULL DEFAULT '{}', clips_created INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT
  '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, finished_at REAL,
  auto_import INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS clip_ledger (
  clip_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, creator_key TEXT NOT NULL, moment_id TEXT NOT
  NULL,
  moment_creator TEXT NOT NULL, action_time REAL, window_start REAL, window_end REAL, dedupe_key TEXT NOT
  NULL,
  source_lang TEXT NOT NULL DEFAULT '', fingerprints TEXT NOT NULL DEFAULT '{}', clip_created_at REAL NOT
  NULL,
  confirmed_at REAL NOT NULL, retired_at REAL NOT NULL, reason TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS clip_ledger_dedupe ON clip_ledger(dedupe_key);
CREATE TABLE IF NOT EXISTS recovery_evidence (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', account_key TEXT NOT NULL
  DEFAULT '',
  provider_post_id TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '', clip_id INTEGER, channel_id
  INTEGER,
  at REAL, state TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '{}', imported INTEGER NOT NULL DEFAULT
  0,
  source_sha256 TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS clip_descriptors (
  clip_id INTEGER PRIMARY KEY, moment_id TEXT NOT NULL DEFAULT '', creator_key TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '', source_fp TEXT NOT NULL DEFAULT '', features TEXT NOT NULL DEFAULT '{}',
  labels TEXT NOT NULL DEFAULT '[]', missing TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE VIEW IF NOT EXISTS clip_channels AS
  SELECT id, channel_id FROM clips UNION ALL SELECT clip_id AS id, channel_id FROM clip_ledger;
"""

JSON_COLS = {"settings", "trigger", "captions", "layout", "timings", "files", "history", "evidence",
             "media", "chat", "coverage", "triggers", "clip_ids", "progress"}
LOG_KEEP = 2000


class SelStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._in_tx = False
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else ({} if k != "history" else [])
        return d

    @contextlib.contextmanager
    def tx(self):
        """One SQLite write transaction (BEGIN IMMEDIATE). Re-entrant: nested use joins the outer
        transaction."""
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
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def q1(self, sql: str, *args) -> dict | None:
        with self._lock:
            return self._row(self.db.execute(sql, args).fetchone())

    def x(self, sql: str, *args) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        if table in ("clips", "deliveries"):
            fields["updated_at"] = time.time()
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in JSON_COLS else v)
        self.x(f"UPDATE {table} SET {', '.join(cols)} WHERE id=?", *vals, row_id)

    def merge_json(self, table: str, row_id: int, col: str, patch: dict) -> dict:
        """Atomic read-modify-write of a JSON column (one-level deep merge)."""
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

    def add_destination(self, provider: str, label: str, settings: dict, status: str = "disconnected") -> int:
        """New destination row with an id never used before by any delivery (a reused rowid would re-attach a
        deleted destination's kept claims to a new account)."""
        with self.tx():
            used = self.q1("SELECT MAX(m) AS m FROM (SELECT MAX(id) AS m FROM destinations UNION ALL "
                           "SELECT MAX(destination_id) FROM deliveries)")["m"] or 0
            now = time.time()
            self.x("INSERT INTO destinations(id,provider,label,settings,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?)", used + 1, provider, label, json.dumps(settings), status, now, now)
            return used + 1

    def add_channel(self, platform: str, name: str, source_url: str = "",
                    settings: dict | None = None) -> int:
        return self.x("INSERT INTO channels(platform,name,source_url,settings,created_at) VALUES(?,?,?,?,?)",
                      platform, name, source_url, json.dumps(settings or {}), time.time())

    def channel(self, cid: int) -> dict | None:
        return self.q1("SELECT * FROM channels WHERE id=?", cid)

    def channels(self) -> list[dict]:
        return self.q("SELECT * FROM channels ORDER BY id")

    def create_clip(self, channel_id: int, dedupe_key: str, kind: str, trigger: dict, **fields) -> int | None:
        """Insert a clip; None if the dedupe key already exists (idempotent), also for a clip that was already
        posted and removed (its key stays in clip_ledger)."""
        now = time.time()
        if self.q1("SELECT 1 FROM clip_ledger WHERE dedupe_key=?", dedupe_key):
            return None
        try:
            # never reuse an id that deliveries or the ledger still reference
            with self.tx():
                used = self.q1("SELECT MAX(m) AS m FROM (SELECT MAX(id) AS m FROM clips UNION ALL SELECT "
                               "MAX(clip_id) FROM clip_ledger UNION ALL SELECT MAX(clip_id) FROM "
                               "deliveries)")["m"] or 0
                cid = used + 1
                self.x("INSERT INTO clips(id,channel_id,dedupe_key,created_at,updated_at,status,kind,"
                       "trigger) VALUES(?,?,?,?,?,?,?,?)", cid, channel_id, dedupe_key, now, now, "pending",
                       kind,
                       json.dumps(trigger))
        except sqlite3.IntegrityError:
            return None
        self.update("clips", cid, **fields)
        return cid

    def clip(self, cid: int) -> dict | None:
        return self.q1("SELECT * FROM clips WHERE id=?", cid)

    def log(self, message: str, level: str = "info", channel_id: int | None = None,
            clip_id: int | None = None) -> None:
        with self._lock:
            rid = self.x("INSERT INTO log(ts,level,channel_id,clip_id,message) VALUES(?,?,?,?,?)",
                         time.time(), level, channel_id, clip_id, message[:500])
            if rid % 200 == 0:
                self.x("DELETE FROM log WHERE id < ?", rid - LOG_KEEP)
