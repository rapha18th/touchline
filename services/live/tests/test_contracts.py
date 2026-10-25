"""Contracts: every event and every cue validates against the published JSON schemas.

A freshly generated match plays two short periods, so both halves, the half-time whistle and the
period 2 clock are covered. Its events go through schema/event.schema.json and every cue the state
service emits goes through schema/cue.schema.json. The recorded broadcasts in broadcasts/ hold the
crew's own cues (lines, insights, recaps, translations), so they are checked too. Nothing here needs
an Azure login.
"""

import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from touchline_engine import export
from touchline_engine.sim import Match as EngineMatch
from touchline_live.match_io import load
from touchline_live.pipeline import run_fast

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "schema"
BROADCASTS = sorted((ROOT / "broadcasts").glob("*.cues.jsonl"))
DATA = Path(os.environ.get("TOUCHLINE_DATA", ROOT / "data" / "matches"))
STORED = sorted(p for p in DATA.glob("TL-*") if (p / "events.json").exists()) if DATA.is_dir() else []


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def problems(v: Draft202012Validator, docs: list[dict], label: str) -> list[str]:
    out = []
    for i, d in enumerate(docs):
        for err in v.iter_errors(d):
            path = "/".join(str(p) for p in err.absolute_path) or "(root)"
            out.append(f"{label}[{i}] {d.get('kind') or d.get('type')} {path}: {err.message}")
    return out


@pytest.fixture(scope="module")
def match(tmp_path_factory):
    m = EngineMatch(17)
    m.play_period(1, 420, 0)
    m.play_period(2, 420, 1)
    out = tmp_path_factory.mktemp("matches") / export.match_id(17)
    export.write(m, out)
    return load(out)


@pytest.fixture(scope="module")
def cues(match):
    got: list[dict] = []
    run_fast(match, sinks=[got.append])
    return got


def test_schemas_are_valid_draft_2020_12():
    validator("event")
    validator("cue")


def test_generated_events_follow_the_event_schema(match):
    assert {e["period"] for e in match.events} == {1, 2}
    bad = problems(validator("event"), match.events, "event")
    assert not bad, f"{len(bad)} schema errors, first: " + "; ".join(bad[:5])


def test_generated_cues_follow_the_cue_schema(cues):
    assert cues
    assert "scorebug" in {c["kind"] for c in cues}
    bad = problems(validator("cue"), cues, "cue")
    assert not bad, f"{len(bad)} schema errors, first: " + "; ".join(bad[:5])


def test_cue_sources_name_real_events(match, cues):
    ids = {e["id"] for e in match.events}
    for c in cues:
        assert set(c["sources"]) <= ids, c


@pytest.mark.parametrize("path", BROADCASTS, ids=[p.name for p in BROADCASTS])
def test_recorded_broadcasts_follow_the_cue_schema(path):
    docs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert docs
    bad = problems(validator("cue"), docs, path.name)
    assert not bad, f"{len(bad)} schema errors, first: " + "; ".join(bad[:5])


@pytest.mark.skipif(not STORED, reason="no generated matches on this machine (data/ is not in git)")
@pytest.mark.parametrize("path", STORED, ids=[p.name for p in STORED])
def test_stored_matches_follow_the_event_schema(path):
    events = json.loads((path / "events.json").read_text(encoding="utf-8"))
    bad = problems(validator("event"), events, path.name)
    assert not bad, f"{len(bad)} schema errors, first: " + "; ".join(bad[:5])


def test_the_validator_refuses_broken_documents(match, cues):
    ev, cue = validator("event"), validator("cue")
    e = next(e for e in match.events if e["type"] == "pass")
    assert not list(ev.iter_errors(e))
    assert list(ev.iter_errors({**e, "type": "header"}))
    assert list(ev.iter_errors({**e, "period": 3}))
    assert list(ev.iter_errors({k: v for k, v in e.items() if k != "score"}))
    c = cues[0]
    assert not list(cue.iter_errors(c))
    assert list(cue.iter_errors({**c, "priority": 4}))
    assert list(cue.iter_errors({**c, "schema": "touchline.cue/0"}))
    assert list(cue.iter_errors({k: v for k, v in c.items() if k != "sources"}))
