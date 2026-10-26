"""The Analyst's insight airs only after it passes the Verifier's gates; a failed insight is dropped, never the lines.

Before this, insight cards aired unchecked while every crew cue carried verified=True. No model calls, no match data.
"""

import asyncio

from agent_framework import WorkflowBuilder

from touchline_live.crew.models import Insight, Job, PersonaLine
from touchline_live.crew.workflow import Director, Verifier

HOME, AWAY = "Fallowmere Albion", "Elmstead United"
FACTS = {"scorer": "Sipho Lunwell", "team": AWAY, "score": f"{HOME} 2-3 {AWAY}", "xg": 0.21, "distance_m": 8.3}


class NoClaims:
    async def call(self, tool, **args):
        return []


def run(insight):
    j = Job(match_id="TL-000099", kind="goal", match_s=4260.0, frame=0, period=2, priority=3, facts=FACTS, sources=[7],
            context={"home": HOME, "away": AWAY, "state": {"score": [2, 3]}, "events": []},
            personas=["casual"], langs=["en"], roster=["Sipho Lunwell"], clubs=[HOME, AWAY], insight=insight,
            lines=[PersonaLine(persona="casual", text="Sipho Lunwell scores from 8.3 metres.", sources=[7])])
    cues = []
    verifier, director = Verifier(NoClaims()), Director(cues.append)
    asyncio.run(WorkflowBuilder(name="insight-test", start_executor=verifier).add_edge(verifier, director).build().run(j))
    return j, cues


def test_an_insight_with_an_unbacked_number_is_dropped_and_the_lines_still_air():
    j, cues = run(Insight(headline="Away side turn it", pattern="game_state", sources=[7],
                          why_it_matters="Elmstead United have won 14 duels in a row and now lead."))
    assert j.insight_ok is False
    assert [c["kind"] for c in cues] == ["line"]
    v = j.verdicts[-1]
    assert v["ok"] and not v["insight"]["ok"]
    assert any("14" in p for p in v["insight"]["problems"])


def test_an_insight_that_passes_airs_and_every_aired_cue_is_verified():
    j, cues = run(Insight(headline="Lunwell finds the gap", pattern="individual", sources=[7],
                          why_it_matters="Sipho Lunwell finished from 8.3 metres to put Elmstead United ahead."))
    assert j.insight_ok is True
    assert sorted(c["kind"] for c in cues) == ["insight", "line"]
    assert all(c["verified"] for c in cues)
