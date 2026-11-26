"""The pipeline store: channels, clips and their log, with the original `Store` API, on the app database.

One store serves the whole pipeline: the engine (monitor), cutting/rendering (edit) and the selection functions
(select) read and write the same clip rows. Code addresses the original logical table names
(`store.update("clips", ...)`, `store.q("SELECT ... FROM clips ...")`); they are rewritten to the tables of the
`monitor` namespace:

  channels, clips, log, clip_ledger, clip_descriptors, vod_imports, recovery_evidence -> monitor_<name>
  clip_channels           -> view over clips + clip_ledger (as in the original)
  destinations, deliveries -> read-only views over the publishing tables (publish_destinations,
                              publish_deliveries) with the original column names; the labelled test
                              destination appears as the original's local `sandbox` provider
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

def _recover_missing_publish_channel(conn: sqlite3.Connection) -> None:
    """Recover native finals with a missing channel without claiming standalone publish payloads."""
    rows = conn.execute("""
        SELECT p.clip_id, p.language, p.payload, m.channel_id, ch.platform, ch.publish_platforms, live.files
        FROM publish_clips p JOIN monitor_clip_channels m ON m.id=p.clip_id
        LEFT JOIN monitor_channels ch ON ch.id=m.channel_id
        LEFT JOIN monitor_clips live ON live.id=m.id
        LEFT JOIN monitor_clip_ledger ledger ON ledger.clip_id=m.id
        WHERE p.channel_id IS NULL AND typeof(p.clip_id)='integer'
          AND p.created_at>=COALESCE(live.created_at, ledger.clip_created_at)
    """).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        # A standalone producer may use the same integer id. Match the monitored final's path as well.
        final = (json.loads(row["files"] or "{}").get("final") or {}).get(row["language"])
        final = final or f"final_{row['language']}.mp4"  # the ledger outlives the file metadata
        path = str(payload.get("path") or "").replace("\\", "/")
        if path.split("/")[-3:] != ["clips", str(row["clip_id"]), final]:
            continue
        source = row["channel_id"]
        saved = payload.get("channel_id")
        allowed = json.loads(row["publish_platforms"] or "[]")
        if not row["platform"] or saved not in (None, source):
            payload["destinations"] = []
        elif allowed:
            chosen = payload.get("destinations")
            payload["destinations"] = allowed if chosen is None else [d for d in chosen if d in allowed]
        payload["channel_id"] = source
        conn.execute("UPDATE publish_clips SET channel_id=?, payload=? WHERE clip_id=? AND language=?",
                     (source, json.dumps(payload), row["clip_id"], row["language"]))


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
  SELECT id, CASE provider WHEN 'test' THEN 'sandbox' ELSE provider END AS provider, label, account, settings,
         status, status_detail, created_at, created_at AS updated_at, enabled, rights_ack_at
  FROM publish_destinations;
CREATE VIEW monitor_deliveries AS
  SELECT id, clip_id, destination_id, language, idempotency_key, status, mode, attempts, provider_ref,
         provider_post_id, provider_url, outcome, last_error, next_attempt_at, history, created_at, updated_at,
         reconcile, version, media_version, blocked_from, claimed_at, '' AS account_key,
         COALESCE(public_at, 0) AS public_at FROM publish_deliveries;
""",
        # v3: retain clip attribution and channel permissions after removal.
        """
ALTER TABLE monitor_channels RENAME TO monitor_old_channels;
CREATE TABLE monitor_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, name TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
  settings TEXT NOT NULL DEFAULT '{}', armed INTEGER NOT NULL DEFAULT 0,
  format_id TEXT, publish_platforms TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, retired_at REAL);
INSERT INTO monitor_channels(id,platform,name,source_url,url,display_name,settings,armed,format_id,publish_platforms,created_at)
  SELECT id,platform,name,source_url,url,display_name,settings,armed,format_id,publish_platforms,created_at
  FROM monitor_old_channels;
DROP TABLE monitor_old_channels;
CREATE UNIQUE INDEX monitor_active_login ON monitor_channels(platform,name) WHERE retired_at IS NULL;

-- Reserve ids that outlived their channel, including publisher-only records and permissions.
-- A retired row without a platform or login has no recoverable attribution.
INSERT INTO monitor_channels(id,platform,name,created_at,retired_at)
  SELECT DISTINCT CAST(ref AS INTEGER),'','',0,0 FROM (
    SELECT channel_id AS ref FROM monitor_clip_channels UNION ALL SELECT channel_id FROM monitor_vod_imports
    UNION ALL SELECT channel_id FROM monitor_log UNION ALL SELECT channel_id FROM monitor_recovery_evidence
    UNION ALL SELECT channel_id FROM publish_clips
    UNION ALL SELECT j.key FROM settings_kv s, json_each(s.value_json) j WHERE s.key='publish.channel_platforms'
  ) WHERE CAST(ref AS INTEGER)>0 AND CAST(CAST(ref AS INTEGER) AS TEXT)=CAST(ref AS TEXT)
    AND NOT EXISTS (SELECT 1 FROM monitor_channels c WHERE c.id=ref);

-- A clip older than the registered channel already shared a reused number before this upgrade.
-- Keep the current channel id and give the identifiable older history a separate unknown identity.
CREATE TEMP TABLE split_channel AS
  SELECT c.id AS original,c.created_at AS since,
         (SELECT COALESCE(MAX(id),0) FROM monitor_channels) + ROW_NUMBER() OVER (ORDER BY c.id) AS historical
  FROM monitor_channels c WHERE c.retired_at IS NULL AND (
       EXISTS (SELECT 1 FROM monitor_clips m WHERE m.channel_id=c.id AND m.created_at<c.created_at)
    OR EXISTS (SELECT 1 FROM monitor_clip_ledger l WHERE l.channel_id=c.id AND l.clip_created_at<c.created_at)
    OR EXISTS (SELECT 1 FROM publish_clips p WHERE p.channel_id=c.id AND p.created_at<c.created_at)
    OR EXISTS (SELECT 1 FROM monitor_vod_imports v WHERE v.channel_id=c.id AND v.created_at<c.created_at));
INSERT INTO monitor_channels(id,platform,name,created_at,retired_at) SELECT historical,'','',0,0 FROM split_channel;
UPDATE monitor_clips SET channel_id=s.historical FROM split_channel s
  WHERE channel_id=s.original AND created_at<s.since;
UPDATE monitor_clip_ledger SET channel_id=s.historical FROM split_channel s
  WHERE channel_id=s.original AND clip_created_at<s.since;
UPDATE monitor_vod_imports SET channel_id=s.historical FROM split_channel s
  WHERE channel_id=s.original AND created_at<s.since;
UPDATE monitor_log SET channel_id=s.historical FROM split_channel s
  WHERE channel_id=s.original AND (ts<s.since OR EXISTS
    (SELECT 1 FROM monitor_clip_channels m WHERE m.id=monitor_log.clip_id AND m.channel_id=s.historical));
UPDATE monitor_recovery_evidence SET channel_id=s.historical FROM split_channel s
  WHERE channel_id=s.original AND (created_at<s.since OR EXISTS
    (SELECT 1 FROM monitor_clip_channels m WHERE m.id=monitor_recovery_evidence.clip_id AND m.channel_id=s.historical));
UPDATE publish_clips SET channel_id=s.historical FROM split_channel s
  WHERE publish_clips.channel_id=s.original AND (publish_clips.created_at<s.since OR EXISTS
    (SELECT 1 FROM monitor_clip_channels m WHERE m.id=publish_clips.clip_id AND m.channel_id=s.historical));
DROP TABLE split_channel;
-- Pending deliveries consult the persisted payload again before sending.
UPDATE publish_clips SET payload=json_set(payload,'$.channel_id',CAST(channel_id AS INTEGER),'$.destinations',json('[]'))
  WHERE EXISTS (SELECT 1 FROM monitor_channels c WHERE c.id=publish_clips.channel_id AND c.platform='');
""",
        _recover_missing_publish_channel,  # v4: recover channel provenance for older native finals
    ],
)

JSON_COLS = {"settings", "trigger", "captions", "layout", "timings", "files", "history", "evidence", "media",
             "chat", "coverage", "triggers", "clip_ids", "progress", "publish_platforms", "overrides"}
LIST_COLS = {"history", "publish_platforms"}
# One joined lookup for the whole library, including retired or unavailable sources.
_CLIP_SELECT = """SELECT clip.*,
  COALESCE(NULLIF(ch.display_name,''), NULLIF(ch.name,''), 'Unknown historical channel') AS channel_name
  FROM clips clip LEFT JOIN channels ch ON ch.id=clip.channel_id"""
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
        """Merge with the latest row, including changes from a different subsystem's Store."""
        with self._lock, contextlib.nullcontext() if self.db.conn().in_transaction else self.db.tx():
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
        with self.tx():
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
        """Resolve clip provenance, including channels the user has removed."""
        return self.q1("SELECT * FROM channels WHERE id=? AND platform<>''", cid)

    def active_channel(self, cid: int) -> dict | None:
        return self.q1("SELECT * FROM channels WHERE id=? AND retired_at IS NULL", cid)

    def retire_channel(self, cid: int) -> None:
        self.update("channels", cid, retired_at=time.time(), armed=0)

    def channels(self) -> list[dict]:
        return self.q("SELECT * FROM channels WHERE retired_at IS NULL ORDER BY id")

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
        return self.q1(_CLIP_SELECT + " WHERE clip.id=?", cid)

    def clips(self, channel_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        if channel_id is None:
            return self.q(_CLIP_SELECT + " ORDER BY clip.id DESC LIMIT ? OFFSET ?", limit, offset)
        return self.q(
            _CLIP_SELECT + " WHERE clip.channel_id=? ORDER BY clip.id DESC LIMIT ? OFFSET ?", channel_id, limit, offset
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
