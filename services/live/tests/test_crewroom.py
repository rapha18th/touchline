"""What the Crew room reads: live pushes and recorded cues keep the crew's whole story."""

import asyncio
import json

from touchline_live import server


def test_live_pushes_keep_verdicts_drafts_and_mcp_calls():
    crew = {"job": "TL-000023-goal-6000", "attempts": 1, "scratch": "dropped",
            "trace": [{"role": "producer", "ms": 300, "mcp_calls": 2, "mcp": [{"tool": "state_at", "match_s": 600.0, "ms": 40.0}]},
                      {"role": "verifier", "ms": 15, "ok": False, "problems": 1}],
            "verdicts": [{"ok": False, "problems": ["casual: number 54.3 is not in the facts"], "claims_checked": 4,
                          "drafts": [{"persona": "casual", "text": "A 54.3 metre run."}]},
                         {"ok": True, "problems": [], "claims_checked": 5}]}

    async def go():
        ch = server.Channel("TL-000023", 1.0, 0.0)
        ch.push({"kind": "line", "match_s": 600.0, "payload": {"text": "Goal."}, "crew": crew})
        await asyncio.sleep(0)
        return ch.recent[-1]

    slim = asyncio.run(go())
    assert set(slim["crew"]) == {"job", "trace", "attempts", "verdicts"}
    assert slim["crew"]["verdicts"][0]["drafts"][0]["text"] == "A 54.3 metre run."
    assert slim["crew"]["trace"][0]["mcp"][0]["tool"] == "state_at"


def test_recorded_broadcasts_carry_the_crew_on_every_crew_cue():
    for p in sorted(server.BROADCASTS.glob("*.cues.jsonl")):
        cues = server.broadcast(p.name.split(".")[0]).body
        crew = [c for c in json.loads(cues) if c.get("author") == "crew"]
        assert crew, p.name
        assert all({"trace", "attempts", "verdicts"} <= set(c["crew"]) for c in crew if c["kind"] in ("line", "insight", "recap"))
