"""Unit tests: glossary, translation, cue building, subtitle formats (no whisper)."""
import os
import re
from pathlib import Path

import pytest

from live_clipper.captions import subs
from live_clipper.captions.asr import pick_language
from live_clipper.captions.glossary import Glossary
from live_clipper.captions.translate import Translator, distribute_words

MODELS = Path(os.environ.get("LIVE_CLIPPER_MODELS") or Path(__file__).resolve().parent.parent / "data" / "models")
needs_mt = pytest.mark.skipif(not (MODELS / "opus-mt-en-es" / "model.bin").exists(), reason="MT model missing")


@pytest.fixture(scope="module")
def mt():
    return Translator(MODELS)


def test_pick_language_restricted():
    assert pick_language({"en": 0.1, "es": 0.3, "fr": 0.6})[0] == "es"
    assert pick_language({"en": 0.2, "es": 0.1, "de": 0.7})[0] == "en"


def test_glossary_protect_restore_roundtrip():
    g = Glossary(["Big Smoke"])
    t, m = g.protect("Zentorno near Los Santos with @example_clips and Big Smoke")
    for orig in ("Zentorno", "Los Santos", "@example_clips", "Big Smoke"):
        assert orig not in t
    back, missing = Glossary.restore(t, m)
    assert back == "Zentorno near Los Santos with @example_clips and Big Smoke" and not missing


def test_glossary_missing_placeholder_reported():
    g = Glossary()
    t, m = g.protect("Lester and Franklin")
    _, missing = Glossary.restore("solo uno", m)
    assert len(missing) == 2


def test_correct_asr_snaps_near_miss():
    assert Glossary().correct_asr("the Zentorino is fast") == "the Zentorno is fast"


@needs_mt
def test_translate_en_es_protects_terms(mt):
    out = mt.translate_texts(["I'm taking the Zentorno through the LSPD roadblock, chat.",
                              "We rob Maze Bank with Lester and Franklin near Los Santos."], "en", "es")
    assert "Zentorno" in out[0] and "LSPD" in out[0]
    assert all(t in out[1] for t in ("Maze Bank", "Lester", "Franklin", "Los Santos"))


@needs_mt
def test_translate_es_en_and_phrase_map(mt):
    out = mt.translate_texts(["Voy a robar el banco Maze Bank con Lester, chat.",
                              "Tengo nivel de búsqueda cinco y la poli me persigue."], "es", "en")
    assert "Maze Bank" in out[0] and "Lester" in out[0] and "Maze Bank bank" not in out[0]
    assert "wanted level" in out[1] and "cops" in out[1]
    es = mt.translate_texts(["The cops hit me with a wanted level."], "en", "es")[0]
    assert "la poli" in es.lower() and "nivel de búsqueda" in es


@needs_mt
def test_translate_segments_keeps_timing(mt):
    segs = [{"start": 1.0, "end": 3.0, "text": "Oh no, I crashed!", "words": []}]
    out = mt.translate_segments(segs, "en", "es")
    assert (out[0]["start"], out[0]["end"]) == (1.0, 3.0)
    w = out[0]["words"]
    assert w[0]["start"] == 1.0 and abs(w[-1]["end"] - 3.0) < 0.01
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(w, w[1:]))


def test_distribute_words_empty():
    assert distribute_words("", 0, 1) == []


def _seg(text, start, end):
    ws = text.split()
    step = (end - start) / len(ws)
    return {"start": start, "end": end, "text": text,
            "words": [{"start": start + i * step, "end": start + (i + 1) * step - 0.02, "word": w}
                      for i, w in enumerate(ws)]}


LONG = [_seg("Okay chat I'm taking the Zentorno straight through the LSPD roadblock. "
             "Oh no, I just crashed into the gas station! That was insane, did you see that?", 0.0, 9.0),
        _seg("Yes", 9.2, 9.3)]


def test_build_cues_constraints():
    cues = subs.build_cues(LONG)
    assert cues
    for i, c in enumerate(cues):
        lines = c["text"].split("\n")
        assert len(lines) <= 2 and all(len(l) <= 32 for l in lines), c
        assert c["end"] - c["start"] <= 3.5 + 1e-6
        if i + 1 < len(cues):
            assert c["end"] <= cues[i + 1]["start"], "overlap"
    assert cues[0]["start"] >= 0 and cues[-1]["end"] <= 9.3 + 0.9
    # text preserved
    joined = " ".join(c["text"].replace("\n", " ") for c in cues)
    assert joined.split() == " ".join(s["text"] for s in LONG).split()


def test_build_cues_min_duration_and_no_function_word_end():
    cues = subs.build_cues([_seg("Wow", 1.0, 1.2), _seg("a very long sentence about the stuff in the game that we like", 5.0, 12.0)])
    assert cues[0]["end"] - cues[0]["start"] >= 0.8 - 1e-6
    for c in cues[1:]:
        assert c["text"].split()[-1].lower() not in ("the", "a", "of", "in", "that")


def test_balanced_lines():
    c = subs.build_cues([_seg("this is a fairly long caption line that wraps", 0, 3)])[0]
    a, b = c["text"].split("\n")
    assert abs(len(a) - len(b)) <= 12


def test_srt_vtt_roundtrip_and_edit():
    cues = subs.build_cues(LONG)
    srt, vtt = subs.to_srt(cues), subs.to_vtt(cues)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> ")
    for parsed in (subs.parse_srt(srt), subs.parse_vtt(vtt)):
        assert len(parsed) == len(cues)
        for a, b in zip(parsed, cues):
            assert abs(a["start"] - b["start"]) < 0.002 and a["text"] == b["text"]
    edited = subs.parse_srt(srt)
    edited[0]["text"] = "EDITED"
    assert "EDITED" in subs.to_vtt(edited)


def test_ass_structure_and_pixel_sizes():
    ass = subs.to_ass(subs.build_cues(LONG), play_res=(720, 1280))
    assert "PlayResX: 720" in ass and "PlayResY: 1280" in ass
    style = [l for l in ass.splitlines() if l.startswith("Style:")][0].split(",")
    assert style[1] == "Inter" and int(style[2]) >= 40 and style[7] == "-1"   # big + bold
    assert re.search(r"Dialogue: 0,0:00:00\.00,", ass) and "\\N" in ass
    big = subs.to_ass([{"start": 0, "end": 1, "text": "x"}], {"size": 99, "margin_v": 10}, (1080, 1920))
    assert "PlayResX: 1080" in big and ",99," in big
