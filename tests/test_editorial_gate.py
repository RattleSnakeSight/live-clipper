"""Editorial content gate (live_clipper.editorial) on the real rule: a stream title, category, greeting or
viewer-arrival surge never makes a clip postable; identifiable gameplay, a substantive comment or a reaction
with setup
and payoff does, even in a stream's opening minute. Ported from the reference suite (verdict fixtures with
synthetic
numpy frames).

Labeled regression fixture OPENING (synthetic, no media): a stream-opening short whose frames show a
full-frame webcam
(face about 0.37 of the frame height, the same place in every frame), a chat overlay scrolling arrival
greetings
("THE KING IS BACK", "LATE AS ALWAYS" repeated) and speech about sleep and food. No gameplay is visible."""
from __future__ import annotations

import numpy as np
import pytest

from live_clipper import editorial, gameplay

NOW = 1_900_000_000.0

OPENING_CHAT = {"kind": "chat_spike", "score": 3.4, "unique_users": 35,
                "reason": "Chat spike: 40 msgs/10s from 35 chatters (3.4x baseline 11.8); "
                          "top: late as always x9, "
                          "the king is back x3, goat x2",
                "sample_messages": ["THE KING IS BACK", "LATE AS ALWAYS", "STARTS STREAM W RED BULL ON FACE",
                                    "spent all night catching up on the GTA run", "GOAT", "LATE AS ALWAYS",
                                    "LATE AS ALWAYS", "viewer_one: LATE AS ALWAYS"],
                "topic": {"state": "live_gta_category", "category": "Grand Theft Auto VI", "title": "back"}}
OPENING_SPEECH = {"segments": [{"start": 6.0, "end": 9.0, "text": "I was up for like 30 to 30 hours today."},
                               {"start": 19.0, "end": 22.0, "text": "I have food coming in like now,"}]}


def _clip(trigger, dur=30.0, act_rel=18.0, since_start=40.0, t0=NOW - 600):
    return {"id": 1, "status": "ready", "action_time": t0 + act_rel, "window_start": t0,
            "window_end": t0 + dur,
            "timings": {"source_available": t0 + act_rel - since_start},
            "files": {"source": "source.ts", "source_t0": t0, "trim_in": 0.0, "duration": dur},
            "trigger": trigger}


def _rng_frames(n=8, h=180, w=320, seed=1, camera=False, overlay=False):
    """Synthetic frames: changing textured scenes (gameplay-like) or a static room (camera view), optional
    chat
    overlay column. A full-frame camera view comes with a persistent large face box."""
    rng = np.random.default_rng(seed)
    room = (rng.integers(40, 200, (h, w, 3))).astype(np.uint8)
    F = []
    for _ in range(n):
        if camera:
            f = room.copy()
        else:
            f = rng.integers(0, 255, (h // 6, w // 8, 3)).astype(np.uint8).repeat(6, 0).repeat(8, 1)
        if overlay:
            f[:, : w // 5] = rng.integers(0, 255, (h, w // 5, 3))
        F.append(f)
    faces = [[{"x": 0.42, "y": 0.18, "w": 0.2, "h": 0.37,
               "score": 0.93}] for _ in range(n)] if camera else None
    return np.stack(F), [2.0 + 3.6 * i for i in range(n)], faces


def test_stream_opening_camera_view_is_rejected_with_reason_and_evidence_time():
    F, times, faces = _rng_frames(camera=True, overlay=True)
    vis = editorial.visual_evidence(F, times, faces)
    assert vis["gameplay_pairs"] == 0 and vis["face_dominant_ratio"] == 1.0
    clip = _clip(OPENING_CHAT)
    v = editorial.decide(clip, vis, editorial.speech_evidence(OPENING_SPEECH),
                         editorial.chat_evidence(OPENING_CHAT))
    assert v["state"] == "rejected"
    assert v["reason"].startswith("stream opening (40 s after capture began)")
    assert "camera/just-chatting view with no gameplay" in v["reason"]
    assert "arriving-viewer greetings" in v["reason"] and "greetings/logistics" in v["reason"]
    assert v["evidence_s"] == pytest.approx(2.0)
    assert v["evidence_at"] == pytest.approx(clip["window_start"] + 2.0)
    # the category alone did not qualify it
    assert v["signals"]["game_context"] == "capture-time GTA VI category"
    clip["trigger"] = {**OPENING_CHAT, "editorial": v}
    g = gameplay.evidence(clip)
    assert g["state"] == "rejected" and "camera/just-chatting" in g["detail"]
    assert "[evidence at 2.0 s]" in g["detail"]


def test_a_genuine_reveal_in_the_opening_minute_qualifies():
    speech = {"segments": [{"start": 14.0, "end": 24.0,
                            "text": "Chat, GTA 6 just showed the whole Vice City map and "
                            "honestly it is way bigger than I thought, look at Leonida from up here, "
                            "this is insane"}]}
    chat = {"kind": "chat_spike", "score": 4.0, "sample_messages": ["no way", "W", "omg", "LETS GO",
                                                                    "insane"],
            "reason": "Chat spike: 50 msgs/10s; top: no way x6, w x5",
            "topic": {"state": "live_gta_category", "category": "Grand Theft Auto VI"}}
    F, times, faces = _rng_frames(camera=True)  # still on camera: the talk carries it
    clip = _clip(chat, since_start=35.0)
    v = editorial.decide(clip, editorial.visual_evidence(F, times, faces), editorial.speech_evidence(speech),
                         editorial.chat_evidence(chat))
    assert v["state"] == "qualified" and v["kind"] == "opinion"
    assert v["reason"].startswith("stream opening (35 s after capture began): substantive GTA VI talk")
    assert v["evidence_s"] == pytest.approx(15.0, abs=1.5)
    clip["trigger"] = {**chat, "editorial": v}
    assert gameplay.evidence(clip)["state"] == "approved"


def test_silent_gameplay_qualifies_on_visual_evidence_and_needs_setup_and_payoff():
    trig = {"kind": "chat_spike", "score": 3.5, "sample_messages": ["LMAO", "wasted", "no way", "KEKW"],
            "topic": {"state": "live_gta", "title": "GTA 6 day 2"}}
    F, times, _ = _rng_frames()
    vis = editorial.visual_evidence(F, times, [[] for _ in range(len(F))])
    assert vis["gameplay_ratio"] >= 0.5
    v = editorial.decide(_clip(trig), vis, editorial.speech_evidence({"segments": []}),
                         editorial.chat_evidence(trig))
    assert v["state"] == "qualified" and v["kind"] == "reaction" and "GTA VI" in v["reason"]
    edge = editorial.decide(_clip(trig, act_rel=1.0), vis, editorial.speech_evidence(None),
                            editorial.chat_evidence(trig))
    assert edge["state"] == "rejected" and "not enough setup" in edge["reason"]


def test_waiting_screen_greeting_surge_overlay_and_unidentified_game():
    greet = {"kind": "chat_spike", "score": 5, "sample_messages": ["hi", "hello", "hey chat", "first", "late",
                                                                   "hola", "yo"],
             "topic": {"state": "live_gta", "title": "GTA 6 starting soon"}}
    static = np.stack([np.full((180, 320, 3), 90, np.uint8)] * 8)
    v = editorial.decide(_clip(greet), editorial.visual_evidence(static, list(range(8)), None),
                         editorial.speech_evidence(None), editorial.chat_evidence(greet))
    assert v["state"] == "rejected" and "static or waiting screen" in v["reason"]
    assert "greetings" in v["reason"]
    F, times, _ = _rng_frames()
    vis = editorial.visual_evidence(F, times, None)
    over = editorial.decide(_clip(greet), vis, editorial.speech_evidence(None),
                            editorial.chat_evidence(greet))
    assert over["state"] == "rejected" and "greeting surge alone is not a moment" in over["reason"]
    # a scrolling chat overlay over a static picture is not gameplay (one column only)
    Fo, to, _ = _rng_frames(camera=True, overlay=True)
    assert editorial.visual_evidence(Fo, to, None)["gameplay_pairs"] == 0
    unknown_game = {"kind": "chat_spike", "score": 4, "sample_messages": ["lol", "no way"], "topic": {}}
    u = editorial.decide(_clip(unknown_game), vis, editorial.speech_evidence(None),
                         editorial.chat_evidence(unknown_game))
    assert u["state"] == "unknown" and "not identifiable" in u["reason"]


def test_game_character_faces_do_not_count_as_the_streamer_camera():
    F, times, _ = _rng_frames()
    moving = [[{"x": 0.1 + 0.1 * i, "y": 0.2, "w": 0.15, "h": 0.33, "score": 0.9}] for i in range(len(F))]
    vis = editorial.visual_evidence(F, times, moving)
    assert vis["face_dominant_ratio"] == 0 and vis["gameplay_ratio"] >= 0.5


def test_verdict_is_current_only_for_its_cut_and_version():
    trig = {"kind": "chat_spike", "sample_messages": ["LMAO"], "topic": {"state": "live_gta",
                                                                         "title": "GTA 6"}}
    clip = _clip(trig)
    F, times, _ = _rng_frames()
    v = editorial.decide(clip, editorial.visual_evidence(F, times, None), editorial.speech_evidence(None),
                         editorial.chat_evidence(trig), window=editorial.window_of(clip))
    clip["trigger"] = {**trig, "editorial": v}
    assert editorial.current(clip) == v
    clip["files"] = {**clip["files"], "trim_in": 2.0}                  # re-cut: the verdict is outdated
    assert editorial.current(clip) is None
    assert "older cut" in gameplay.evidence(clip)["detail"]
    off = {"trigger": {"topic": {"state": "live_off_topic"}}}
    assert "not a GTA gameplay moment" in gameplay.hold_reason(off)
