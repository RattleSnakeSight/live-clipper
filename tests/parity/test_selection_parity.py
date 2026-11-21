"""Selection parity: the public code reproduces the reference decisions, scores and windows on the synthetic
inputs.

data/selection_inputs.json are the synthetic inputs (no real channels); data/selection_ref.json is what the
original
implementation produced for them. With the built-in GTA VI format loaded, every covered section must be
identical.
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import selection_runner as runner  # noqa: E402


@pytest.fixture(scope="module")
def results():
    inputs = json.loads((HERE / "data" / "selection_inputs.json").read_text(encoding="utf-8"))
    ref = json.loads((HERE / "data" / "selection_ref.json").read_text(encoding="utf-8"))
    return runner.expected_view(ref), runner.run(inputs)


@pytest.mark.parametrize("section", runner.COVERED)
def test_section_identical(results, section):
    expected, got = results
    problems = runner.diff(expected[section], got[section], section)
    assert not problems, "\n".join(problems[:40])
    dump = {"ensure_ascii": False, "sort_keys": True}
    assert json.dumps(expected[section], **dump) == json.dumps(got[section], **dump)


def test_covers_every_case(results):
    expected, got = results
    assert set(got["detector_cases"]) == set(expected["detector_cases"]) and len(got["detector_cases"]) == 14
    assert len(got["pipeline_cases"]) == 15
