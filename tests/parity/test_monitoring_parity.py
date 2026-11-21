"""Monitoring parity: the public modules must produce the reference's state transitions, timings, backoffs,
decisions and outputs for the same synthetic sequences (data/monitoring_ref.json, from the reference harness).

Only these documented differences are excluded (docs/DEVIATIONS.md, monitoring):
  chat.kick_without_public_url.action   user-facing fix text points at the public app's Sources page.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

REF = json.loads((Path(__file__).parent / "data" / "monitoring_ref.json").read_text(encoding="utf-8"))
EXCLUDED = {("chat", "kick_without_public_url", "action")}


@pytest.fixture(scope="module")
def public(tmp_path_factory):
    return json.loads(json.dumps(harness.run_all(tmp_path_factory.mktemp("parity"))))


def _diff(ref, pub, path=()):
    if path in EXCLUDED:
        return []
    if isinstance(ref, dict) and isinstance(pub, dict):
        out = [f"{'.'.join(path + (k,))}: missing" for k in ref if k not in pub]
        return out + [d for k in ref if k in pub for d in _diff(ref[k], pub[k], path + (k,))]
    if ref != pub:
        return [f"{'.'.join(path)}: reference {ref!r} != public {pub!r}"]
    return []


@pytest.mark.parametrize("section", sorted(REF))
def test_section_matches_reference(public, section):
    assert _diff(REF[section], public[section], (section,)) == []


def test_documented_exclusion_still_reports_unavailable(public):
    assert public["chat"]["kick_without_public_url"]["state"] == "unavailable"
    assert "public HTTPS URL" in public["chat"]["kick_without_public_url"]["action"]
