"""The Fallback's template lines read like broadcast text, and the Verifier fails raw machine output.

The crew evaluation found 21 of 42 aired template lines malformed: a raw score list ("Fallowmere Albion [2, 3]")
on milestones and xG to four decimals on big saves. These tests pin the fix. No model calls, no match data.
"""

import asyncio
import re

import pytest

from touchline_live.crew.models import Job
from touchline_live.crew.verify import verify_text
from touchline_live.crew.workflow import template

HOME, AWAY = "Fallowmere Albion", "Elmstead United"
CTX = {"home": HOME, "away": AWAY, "state": {"score": [2, 3], "clock": "71:00"}, "events": []}
RAW = re.compile(r"\[\s*\d+\s*,\s*\d+\s*\]|\d\.\d{3,}")       # the eval's own malformed-line test

FACTS = {
    "goal": {"scorer": "Sipho Lunwell", "team": AWAY, "minute": "71'", "score": f"{HOME} 2-3 {AWAY}", "xg": 0.2119,
             "distance_m": 8.3},
    "big_chance": {"shooter": "Kenji Halez", "team": HOME, "xg": 0.2016, "outcome": "blocked", "distance_m": 5.9},
    "big_save": {"keeper": "Luca Draova", "shooter": "Tariq Garo", "xg": 0.3583, "distance_m": 7.0},
    "hard_pass": {"passer": "Tendai Calova", "recipient": "Kenji Halez", "difficulty": 0.9047, "distance_m": 20.4},
    "red_card": {"player": "Yusuf Ndaenko", "team": HOME, "minute": "71'"},
    "milestone": {"text_hint": "brace", "player": "Kenji Halez", "goals": 2},
    "momentum_swing": {},
}
ROSTER = ["Sipho Lunwell", "Kenji Halez", "Luca Draova", "Tariq Garo", "Tendai Calova", "Yusuf Ndaenko"]


def job(kind, context=CTX):
    return Job(match_id="TL-000099", kind=kind, match_s=4260.0, frame=0, period=2, priority=2, facts=FACTS[kind],
               sources=[], context=context, personas=["analyst", "casual", "studio"], langs=["en"])


class NoClaims:
    async def call(self, tool, **args):
        return []


def verify(text, facts):
    return asyncio.run(verify_text(NoClaims(), "TL-000099", 4260.0, text, [], {"facts": facts, "context": CTX},
                                   ROSTER, [HOME, AWAY]))


@pytest.mark.parametrize("kind", list(FACTS))
def test_template_lines_have_no_raw_list_and_at_most_two_decimals(kind):
    for persona in ("analyst", "casual", "studio"):
        text = template(job(kind), persona)
        assert text and not RAW.search(text), (kind, persona, text)
        assert not re.search(r"\d\.\d{3,}", text)


def test_scores_read_home_score_away():
    assert template(job("milestone"), "casual") == f"Kenji Halez has 2 goals. {HOME} 2-3 {AWAY}."
    assert template(job("momentum_swing"), "casual") == f"{HOME} 2-3 {AWAY}."
    assert template(job("red_card"), "analyst") == f"Red card for Yusuf Ndaenko. {HOME} 2-3 {AWAY}."
    # A facts score given as a list is spoken the same way.
    j = job("red_card")
    j.facts = {**j.facts, "score": [1, 0]}
    assert template(j, "casual").endswith(f"{HOME} 1-0 {AWAY}.")


def test_xg_and_difficulty_air_with_two_decimals():
    assert template(job("big_save"), "casual") == "Luca Draova saves from Tariq Garo, a chance worth 0.36 xG."
    assert "xG 0.2," in template(job("big_chance"), "casual")
    assert "rated 0.9 difficulty" in template(job("hard_pass"), "casual")
    assert "xG 0.21." in template(job("goal"), "analyst")


@pytest.mark.parametrize("kind", list(FACTS))
def test_template_lines_pass_the_verifier(kind):
    for persona in ("analyst", "casual"):
        text = template(job(kind), persona)
        v = verify(text, FACTS[kind])
        assert v["ok"], (kind, persona, text, v["problems"])


def test_verifier_fails_a_raw_list_or_more_than_two_decimals():
    raw = verify(f"{HOME} [2, 3]", {"score": [2, 3]})
    assert not raw["ok"] and any(p.startswith("format: a raw list") for p in raw["problems"])
    long = verify("Luca Draova saves a chance worth 0.3583 xG.", FACTS["big_save"])
    assert not long["ok"] and "format: 0.3583 has more than two decimals" in long["problems"]
    assert verify("Luca Draova saves a chance worth 0.36 xG.", FACTS["big_save"])["ok"]
    assert verify("Kenji Halez scored at 22:48.", {"clock": "22:48"})["ok"]
