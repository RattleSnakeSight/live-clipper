"""The integration glue without media: one store for monitor/edit/select, the selection gate in front of
edit.clip_ready, effective values feeding the cut window and preset, ops maintenance hooks, and publishing's
say before storage cleanup deletes a clip."""

from __future__ import annotations

import asyncio
import time
import types

from monitor_env import env
from test_maintenance import Env

from live_clipper import formats
from live_clipper.engine import Engine
from live_clipper.events import Trigger
from live_clipper.pipeline import Selection, evidence
from live_clipper.services import EventBus

QUALIFIED = {"state": "qualified", "kind": "opinion", "reason": "substantive GTA VI talk", "evidence_s": 3.0,
             "signals": {"speech": {"gta_mentions": [{"t": 3.0, "term": "gta 6"}]}, "visual": {},
                         "game_context": "GTA VI named in the clip's speech"},
             "window": None, "version": 1}
ASR_EN = {"speech_status": "speech", "language": "en", "original_check": {"status": "pass", "reason": "ok"}}


def setup(tmp_path, **settings):
    s, store, sec = env(tmp_path, **settings)
    bus, seen = EventBus(), []
    bus.subscribe("*", lambda t, p: seen.append((t, p)))
    return s, store, Selection(store.db, store, bus), seen


def clip(store, ch, score=4.0, editorial=QUALIFIED, asr=ASR_EN, kind="chat_spike", **trig):
    t = {"kind": kind, "score": score, "reason": "Chat spike", "sample_messages": ["no way", "lol"],
         "editorial": editorial, **trig}
    cid = store.create_clip(ch, f"{ch}:{kind}:{time.time_ns()}", kind, t, action_time=time.time(),
                            window_start=time.time() - 18, window_end=time.time() + 12, status="ready")
    store.update("clips", cid, captions={"asr": asr, "en": {"title": "A title", "title_base": "A title"}},
                 files={"final": {"en": "final_en.mp4"}, "duration": 30.0})
    return cid


def test_one_store_serves_monitor_edit_and_select_tables(tmp_path):
    s, store, sel, _ = setup(tmp_path)
    ch = store.add_channel("custom", "rtmp://x/a", "rtmp://x/a", display_name="streamer_alpha")
    cid = clip(store, ch)
    assert store.q1("SELECT preset_id, overrides FROM clips WHERE id=?", cid) == {"preset_id": "default",
                                                                                "overrides": {}}
    # selection's tables and views exist (ledger, descriptors, imports, evidence, clip_channels, deliveries)
    for t in ("clip_ledger", "clip_descriptors", "vod_imports", "recovery_evidence", "clip_channels",
              "destinations", "deliveries"):
        store.q(f"SELECT * FROM {t} LIMIT 1")
    assert store.q("SELECT id, channel_id FROM clip_channels") == [{"id": cid, "channel_id": ch}]
    # a clip id still referenced by a publishing row is never reused
    store.db.execute("INSERT INTO publish_deliveries(clip_id, destination_id, language, idempotency_key, status, "
                     "created_at, updated_at) VALUES (40, 1, 'en', 'k', 'published', 0, 0)")
    assert store.create_clip(ch, "next", "manual", {}) == 41


def test_gate_qualifies_holds_and_rejects_like_the_original_rules(tmp_path):
    s, store, sel, seen = setup(tmp_path)
    ch = store.add_channel("custom", "rtmp://x/a", "rtmp://x/a", display_name="streamer_alpha")
    assert sel.gate(clip(store, ch), "en")["decision"] == "qualified"
    low = sel.gate(clip(store, ch, score=2.2), "en")       # first-crossing score under the 3.0 threshold
    assert low["decision"] == "held" and "below this auto-post rule's threshold (3x)" in low["reasons"][0]
    held = sel.gate(clip(store, ch, editorial=None), "en")  # no editorial verdict yet
    assert held["decision"] == "held" and "awaiting gameplay approval" in held["reasons"][0]
    rej = dict(QUALIFIED, state="rejected", kind="", reason="camera view")
    assert sel.gate(clip(store, ch, editorial=rej), "en")["decision"] == "rejected"
    es = sel.gate(clip(store, ch), "es")                   # a translation is manual-only
    assert es["decision"] == "held" and "translation" in es["reasons"][0]
    moments = [p for t, p in seen if t == "select.moment"]
    assert [m["decision"] for m in moments] == ["qualified", "held", "held", "rejected", "held"]
    assert {"moment_id", "channel_id", "decision", "score", "reasons"} <= set(moments[0])


def test_rules_and_overrides_feed_the_gate_title_and_destinations(tmp_path):
    s, store, sel, _ = setup(tmp_path)
    db = store.db
    ch = store.add_channel("custom", "rtmp://x/a", "rtmp://x/a", display_name="streamer_alpha",
                           publish_platforms=["youtube", "tiktok"])
    formats.create_rule(db, {"id": "low-bar", "name": "Low bar", "streamers": {"channels": ["streamer_alpha"]},
                             "actions": {"threshold": 2.0, "title_template": "{channel}: {title}",
                                         "destinations": ["youtube", "instagram"]}})
    c = clip(store, ch, score=2.2)
    d = sel.gate(c, "en")
    assert d["decision"] == "qualified" and d["result"]["matched_rules"] == [{"id": "low-bar", "name": "Low bar"}]
    row = store.clip(c)
    assert sel.apply_template(d["result"], row, "A title") == "streamer_alpha: A title"
    fields = sel.publish_fields(row, "en", d["result"])
    assert fields["destinations"] == ["youtube"]            # the rule's list within the channel's platforms
    assert "streamer_alpha" in fields["description"] and fields["format_id"] == "gta-vi"
    formats.create_rule(db, {"id": "skip", "name": "Skip manual", "scenario": {"trigger_types": ["manual"]},
                             "actions": {"decision": "skip"}})
    assert sel.gate(clip(store, ch, kind="manual"), "en")["decision"] == "rejected"
    store.merge_json("clips", c, "trigger", {"override": {"scoring.min_score": 9.0}})  # per-clip layer wins
    assert sel.gate(c, "en")["decision"] == "held"
    assert evidence(store.clip(c)) == ["chat", "speech", "statement"]


def test_effective_values_feed_the_engine_window_preset_and_detector(tmp_path):
    s, store, _, _ = setup(tmp_path)
    eng = Engine(s, store, None)
    db = store.db
    formats.create_format(db, {"id": "short", "name": "Short", "window": {"pre": 8, "post": 6},
                               "detector": {"sensitivity": "high"},
                               "match": {"title_regex": r"(?i)\bkart\b", "category_regex": "(?i)racing"}})
    eng.edit.presets.create({"id": "compact", "name": "Compact", "encode": {"final_height": 960}})
    formats.create_rule(db, {"id": "r", "name": "r", "conditions": {"platforms": ["custom"]},
                             "actions": {"preset_id": "compact", "window_post": 9}})
    ch = store.add_channel("custom", "rtmp://x/b", "rtmp://x/b", format_id="short")
    started = []

    async def go():
        eng.arm(ch)
        m = eng.monitors[ch]
        assert m.detector.cfg.sensitivity == "high"          # the channel's format drives the detector
        m.note_status({"state": "live", "title": "Kart cup finals", "category": "Sports"}, 60, 60)
        assert m.topic_evidence()["state"] == "live_gta"      # ... and the topic match (title regex)
        m.note_status({"state": "live", "title": "GTA 6 tonight", "category": "Racing"}, 60, 60)
        assert m.topic_evidence()["state"] == "live_gta_category"

        async def no_cut(*a):
            started.append(a[1:])
        eng._process = no_cut
        now = time.time()
        cid = eng.on_trigger(m, Trigger(channel_id=ch, reason="r", kind="chat_spike", burst_start=now,
                                        action_time=now - 4, delay=4.0, detected_at=now))
        await asyncio.sleep(0)
        await eng.stop()
        return cid

    cid = asyncio.run(go())
    c = store.clip(cid)
    assert (c["window_start"], c["window_end"]) == (c["action_time"] - 8, c["action_time"] + 9)
    assert c["preset_id"] == "compact" and c["overrides"]["window"] == {"pre_roll_s": 8.0, "post_roll_s": 9.0}
    assert c["trigger"]["format_id"] == "short" and started == [(cid, c["window_start"], c["window_end"],
                                                                 c["action_time"])]
    assert eng.edit.for_clip(cid).settings.final_height == 960


def test_ops_maintenance_gates_clips_and_pauses_captures(tmp_path):
    s, store, _, _ = setup(tmp_path)
    eng = Engine(s, store, None)
    ops = types.SimpleNamespace(captures_paused=False, admit_clip=lambda n: (False, "storage cap reached"))
    eng.maintenance.ops = ops
    assert eng.maintenance.admit_clip() == "storage cap reached"
    ops.admit_clip = lambda n: (True, None)
    assert eng.maintenance.admit_clip() == ""

    class FC:
        def __init__(self):
            self._task, self.stops, self.starts = None, 0, 0
            self.health = types.SimpleNamespace(detail="")

        def start(self):
            self.starts += 1
            self._task = asyncio.get_running_loop().create_future()

        async def stop(self):
            self.stops += 1
            self._task = None

    async def go():
        fc = FC()
        fc.start()
        eng.monitors[1] = types.SimpleNamespace(id=1, capture=fc, gated=False, gate_open=True,
                                                   media_on=True, slot_wait=False)
        ops.captures_paused = True
        await eng.maintenance.enforce_producers()
        assert fc.stops == 1 and eng.maintenance.paused == {1}
        await eng.maintenance.enforce_producers()             # still paused by ops: no restart
        assert fc.starts == 1
        ops.captures_paused = False
        await eng.maintenance.enforce_producers()
        assert fc.starts == 2 and not eng.maintenance.paused
        eng.monitors.clear()

    asyncio.run(go())


def test_storage_cleanup_asks_publishing_before_deleting(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 365})
    clips = [e.clip(i, 400, posted_days_ago=10 - i) for i in (1, 2, 3)]
    asked = []

    class Pub:
        def holds_provider_bytes(self, clip_id):
            asked.append(clip_id)
            return [{"id": 7, "status": "uncertain"}] if clip_id == 1 else []

    e.ctx.services.append(types.SimpleNamespace(name="publish", pub=Pub()))
    acts = e.m.plan()
    assert [(a.action, a.clip_id) for a in acts] == [("delete_clip", "1")]
    e.m.execute(acts)
    assert asked == [1] and clips[0].exists()              # an upload may still need clip 1's file


def test_publishing_honours_the_clip_destinations_from_selection():
    from live_clipper.publish.worker import Publisher

    pub = types.SimpleNamespace(settings={})
    clip = {"clip_id": 3, "channel_id": 1, "destinations": ["youtube"]}
    assert Publisher.platform_reason(pub, clip, {"provider": "youtube"}) is None
    assert "only for: youtube" in Publisher.platform_reason(pub, clip, {"provider": "tiktok"})
    assert Publisher.platform_reason(pub, clip, {"provider": "test"}) is None   # the test destination
    assert Publisher.platform_reason(pub, {**clip, "destinations": None}, {"provider": "tiktok"}) is None


def test_selection_reads_publishing_rows_through_the_store_views(tmp_path):
    from live_clipper import candidates

    s, store, _, _ = setup(tmp_path)
    ch = store.add_channel("custom", "rtmp://x/a", "rtmp://x/a")
    c = clip(store, ch)
    db = store.db
    for i, (prov, label) in enumerate((("youtube", "Clips channel"), ("test", "Test destination")), 1):
        db.execute("INSERT INTO publish_destinations(id, provider, label, created_at) VALUES (?, ?, ?, 0)",
                   (i, prov, label))
        db.execute("INSERT INTO publish_deliveries(clip_id, destination_id, language, idempotency_key, status, "
                   "created_at, updated_at) VALUES (?, ?, 'en', ?, 'published', 0, 0)", (c, i, f"k{i}"))
    assert candidates.started_on(store, c) == ["Clips channel"]   # the test destination never counts
    d = store.q1("SELECT * FROM deliveries WHERE destination_id=1")
    assert d["history"] == [] and d["account_key"] == "" and d["public_at"] == 0


def test_rule_evidence_uses_the_channels_format_not_the_active_one(tmp_path):
    s, store, sel, _ = setup(tmp_path)
    db = store.db
    formats.duplicate_format(db, formats.active_id(db), "own", "Own")
    own = formats.get_format(db, "own")
    formats.update_format(db, "own", {"topic_states": {**own["topic_states"], "title": "live_own_topic"}})
    formats.create_rule(db, {"id": "topic-bar", "name": "Topic bar", "scenario": {"evidence": ["topic"]},
                             "actions": {"threshold": 0.5}})
    ch = store.add_channel("custom", "rtmp://x/a", "rtmp://x/a", display_name="streamer_alpha")
    store.update("channels", ch, format_id="own")
    cid = clip(store, ch, topic={"state": "live_own_topic"})
    res = sel.resolve(ch, store.clip(cid))
    assert res["format_id"] == "own" and [r["id"] for r in res["matched_rules"]] == ["topic-bar"]
