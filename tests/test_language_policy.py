"""Per-channel subtitle language policy (pure decisions, ported from the reference suite). The
publisher/engine
integration tests of the reference belong to the publish and edit subsystems."""
from __future__ import annotations

import pytest

from live_clipper.language_policy import auto_ineligible, channel_pref, decide, original_language_check

PASS = {"status": "pass", "reason": "test windows agree"}
EN = {"speech_status": "speech", "language": "en", "speech_note": "en speech, global probability 0.85",
      "original_check": PASS}
ES = {"speech_status": "speech", "language": "es", "speech_note": "es speech, global probability 0.80",
      "original_check": PASS}


def W(*spec):
    return [{"start": i * 10, "end": i * 10 + 10, "lang": lang, "prob": p} for i, (lang,
                                                                                   p) in enumerate(spec)]


@pytest.mark.parametrize("asr,pref,status,lang,why", [
    (EN, "auto", "ok", "en", "original language English"), (ES, "auto", "ok", "es", "Spanish"),
    (EN, "en", "ok", "en", "channel setting"), (ES, "es", "ok", "es", "channel setting"),
    (ES, "en", "hold", None, "set to English but the audio is Spanish"),
    (EN, "es", "hold", None, "set to Spanish but the audio is English"),
    ({"speech_status": "no_speech"}, "auto", "hold", None, "no coherent speech"),
    ({"speech_status": "uncertain", "language": "en", "speech_note": "mixed languages (en 0-10s, es 10-20s)"},
     "en", "hold", None, "mixed languages"),
    (None, "auto", "hold", None, "not analysed"),
    ({**EN, "original_check": None, "check_pending": True}, "auto", "hold", None, "still running"),
    ({k: v for k, v in EN.items() if k != "original_check"}, "auto", "hold", None,
     "no original-language evidence"),
    ({**EN, "original_check": {"status": "fail",
                               "reason": "window languages contradict the overall en: es 10-20s (0.80)"}},
     "auto", "hold", None, "contradict"),
])
def test_decisions(asr, pref, status, lang, why):
    d = decide(asr, pref)
    assert (d["status"], d["lang"]) == (status, lang) and why in d["reason"]


def test_mismatch_does_not_assert_speaker_identity():
    r = decide(ES, "en")["reason"]
    assert "not verified" in r and "streamer" not in r


def test_only_original_language_is_auto_eligible():
    assert auto_ineligible(EN, "auto", "en") is None
    assert "translation" in auto_ineligible(EN, "auto", "es")
    assert "held for review" in auto_ineligible(ES, "en", "es")


def test_older_channels_default_to_auto_and_invalid_setting_reads_auto():
    assert channel_pref({}) == channel_pref(None) == channel_pref({"subtitle_language": "fr"}) == "auto"
    assert channel_pref({"subtitle_language": "es"}) == "es"


def test_original_language_check_rules():
    assert original_language_check("es", W(("es", 0.75), ("es", 0.49), ("pt", 0.44)))["status"] == "pass"
    assert original_language_check("en", W(("en", 0.89), ("en", 0.44), ("en", 0.93)))["status"] == "pass"
    assert original_language_check("en", None)["status"] == original_language_check("en",
                                                                                    [])["status"] == "fail"
    assert "contradict" in original_language_check("en", W(("en", 0.9), ("es", 0.8)))["reason"]
