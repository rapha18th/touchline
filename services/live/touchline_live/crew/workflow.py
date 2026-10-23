"""The studio crew as an Agent Framework workflow.

    Producer --> Analyst --> Narrator --> Verifier --ok--> Localiser --> Director --> cues
        |                      ^             | repair (once)
        |                      +-------------+
        |                                    | still failing
        +--> Recap -------------> Verifier   +--> Fallback (template lines) --> Localiser

Shared state travels in the Job. The Producer and the Verifier read the match record through
the match MCP server, so every role works from the same facts as of the same match time.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Never

from agent_framework import Agent, Case, Default, Executor, WorkflowBuilder, WorkflowContext, handler

from . import llm, prompts
from .models import Insight, Job, Lines, PersonaLine, Recap, Translations
from .verify import NUM, MatchRecord, verify_text

RECAP_KINDS = {"half_time", "full_time"}


def _facts_blob(job: Job) -> str:
    return json.dumps({"moment": job.kind, "match_s": job.match_s, "facts": job.facts, "sources": job.sources,
                       "context": job.context}, ensure_ascii=False)


def _mark(job: Job, role: str, t0: float, **extra):
    job.trace.append({"role": role, "ms": round((time.perf_counter() - t0) * 1000), **extra})


class Producer(Executor):
    """Gathers the record for the moment through MCP, then routes it."""

    def __init__(self, record: MatchRecord):
        super().__init__(id="producer")
        self.record = record

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        events = await self.record.call("get_events", match_id=job.match_id, ids=job.sources[:6]) if job.sources else []
        state = await self.record.call("state_at", match_id=job.match_id, match_s=job.match_s)
        slim = lambda e: {k: e.get(k) for k in ("id", "clock", "type", "outcome", "player", "player_name", "team", "team_name", "q", "score")}
        job.context = {"events": [slim(e) for e in events or []],
                       "state": {"score": state["score"], "clock": state["clock"], "explain": state["explain"],
                                 "teams": {s: {k: state["teams"][s][k] for k in ("goals", "shots", "sot", "xg", "passes", "complete", "corners")}
                                           for s in ("home", "away")}},
                       "home": job.clubs[0], "away": job.clubs[1], **job.context}
        _mark(job, "producer", t0, mcp_calls=2)
        await ctx.send_message(job)


class Analyst(Executor):
    def __init__(self):
        super().__init__(id="analyst")
        self.agent = Agent(client=llm.client(llm.FAST), name="analyst", instructions=prompts.ANALYST)

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        r = await self.agent.run(_facts_blob(job), options={"response_format": Insight, "reasoning": {"effort": "none"}})
        job.insight = r.value
        _mark(job, "analyst", t0)
        await ctx.send_message(job)


class Narrator(Executor):
    def __init__(self):
        super().__init__(id="narrator")
        self.agent = Agent(client=llm.client(llm.FAST), name="narrator", instructions=prompts.NARRATOR)

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        ask = {"personas": job.personas, "insight": job.insight.model_dump() if job.insight else None,
               "viewer": job.context.get("viewer")}
        if job.verdicts:
            ask["previous_attempt"] = [{"persona": l.persona, "text": l.text} for l in job.lines]
            ask["problems"] = job.verdicts[-1]["problems"]
        r = await self.agent.run(_facts_blob(job) + "\n\nWRITE: " + json.dumps(ask, ensure_ascii=False),
                                 options={"response_format": Lines, "reasoning": {"effort": "none"}})
        job.lines = [l for l in r.value.lines if l.persona in job.personas]
        _mark(job, "narrator", t0, attempt=job.attempts)
        await ctx.send_message(job)


class RecapWriter(Executor):
    def __init__(self, record: MatchRecord):
        super().__init__(id="recap")
        self.record = record
        self.agent = Agent(client=llm.client(llm.DEEP), name="recap", instructions=prompts.RECAP)

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        ask = {}
        if job.verdicts:
            ask = {"previous": job.recap.model_dump() if job.recap else None, "problems": job.verdicts[-1]["problems"]}
        r = await self.agent.run(_facts_blob(job) + ("\n\nFIX: " + json.dumps(ask) if ask else ""),
                                 options={"response_format": Recap})
        job.recap = r.value
        _mark(job, "recap", t0, attempt=job.attempts)
        await ctx.send_message(job)


class Verifier(Executor):
    def __init__(self, record: MatchRecord):
        super().__init__(id="verifier")
        self.record = record

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        facts = {"facts": job.facts, "context": job.context}
        problems: list[str] = []
        if job.kind in RECAP_KINDS:
            items = [("recap", job.recap.title + ". " + job.recap.story + " " + " ".join(job.recap.key_moments),
                      job.recap.claims, 160)]
        else:
            limits = {"analyst": 36, "casual": 28, "studio": 12}
            items = [(l.persona, l.text, l.claims, limits.get(l.persona, 36)) for l in job.lines]
            missing = set(job.personas) - {l.persona for l in job.lines}
            problems += [f"missing a line for persona {p}" for p in missing]
        results = await asyncio.gather(*[
            verify_text(self.record, job.match_id, job.match_s, text, claims, facts, job.roster, job.clubs, max_words=mw)
            for _, text, claims, mw in items])
        for (persona, _, _, _), res in zip(items, results):
            problems += [f"{persona}: {p}" for p in res["problems"]]
        ok = not problems
        job.verdicts.append({"ok": ok, "problems": problems, "claims_checked": sum(r["claims_checked"] for r in results)})
        _mark(job, "verifier", t0, ok=ok, problems=len(problems))
        if not ok:
            job.attempts += 1
        await ctx.send_message(job)


def route(job: Job) -> str:
    v = job.verdicts[-1] if job.verdicts else {"ok": False}
    if v["ok"]:
        return "ok"
    return "repair" if job.attempts <= 1 else "fallback"


class Fallback(Executor):
    """When a line cannot be proven, air a template line built only from verified facts."""

    def __init__(self):
        super().__init__(id="fallback")

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        job.fallback = True
        if job.kind in RECAP_KINDS:
            f = job.facts
            job.recap = Recap(title=f["score"], story=f"{f['score']}. Shots {f['shots']['home']} to {f['shots']['away']}.",
                              key_moments=[f"{g['player']} {g['minute']}" for g in f.get("goals", [])])
        else:
            job.lines = [PersonaLine(persona=p, text=template(job, p)) for p in job.personas]
        job.trace.append({"role": "fallback", "ms": 0})
        await ctx.send_message(job)


def template(job: Job, persona: str) -> str:
    f = job.facts
    k = job.kind
    if k == "goal":
        return {"studio": f"GOAL {f['scorer'].upper()} {f['minute']}",
                "casual": f"{f['scorer']} scores for {f['team']}. {f['score']}.",
                "analyst": f"{f['scorer']} scores from {f['distance_m']} metres, xG {round(f['xg'], 2)}. {f['score']}."}[persona]
    if k == "big_chance":
        return f"Big chance for {f['shooter']}, xG {f['xg']}, {f['outcome'].replace('_', ' ')}."
    if k == "big_save":
        return f"{f['keeper']} saves from {f['shooter']}, a chance worth {f['xg']} xG."
    if k == "hard_pass":
        return f"{f['passer']} finds {f['recipient']} with a pass rated {f['difficulty']} difficulty."
    if k == "red_card":
        return f"Red card for {f['player']}."
    return f"{job.context.get('home', '')} {job.context.get('state', {}).get('score', '')}".strip()


class Localiser(Executor):
    def __init__(self):
        super().__init__(id="localiser")
        self.agent = Agent(client=llm.client(llm.FAST), name="localiser", instructions=prompts.TRANSLATOR)

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Job]) -> None:
        t0 = time.perf_counter()
        langs = [l for l in job.langs if l != "en"]
        if not langs:
            await ctx.send_message(job)
            return
        texts = {l.persona: l.text for l in job.lines} if job.kind not in RECAP_KINDS else {"recap": job.recap.story}

        async def one(persona: str, text: str):
            r = await self.agent.run(json.dumps({"text": text, "languages": langs}, ensure_ascii=False),
                                     options={"response_format": Translations, "reasoning": {"effort": "none"}})
            out = {}
            src_nums = sorted(NUM.findall(text))
            for it in r.value.items:
                # The Verifier's rule carries across languages: the numbers must survive translation intact.
                if sorted(NUM.findall(it.text)) == src_nums and "—" not in it.text:
                    out[it.lang] = it.text
            return persona, out

        for persona, out in await asyncio.gather(*[one(p, t) for p, t in texts.items() if p != "studio"]):
            job.translations[persona] = out
        _mark(job, "localiser", t0, langs=len(langs))
        await ctx.send_message(job)


class Director(Executor):
    """Turns the verified work into timed cues on the feed."""

    def __init__(self, sink):
        super().__init__(id="director")
        self.sink = sink
        self.n = 0

    @handler
    async def run(self, job: Job, ctx: WorkflowContext[Never, dict]) -> None:
        cues = []
        base = {"schema": "touchline.cue/1", "match_id": job.match_id, "period": job.period, "match_s": job.match_s,
                "frame": job.frame, "verified": not job.fallback or True, "anchor": None,
                "audience": {"team": job.team, "player": job.player, "metric": None}}
        if job.kind in RECAP_KINDS:
            self.n += 1
            cues.append({**base, "id": f"{job.match_id}-r{self.n:04d}", "kind": "recap", "priority": 3, "ttl_s": 40,
                         "audience": {**base["audience"], "personas": ["all"]}, "lang": "en",
                         "payload": {**job.recap.model_dump(exclude={"claims"}), "translations": job.translations.get("recap", {}),
                                     "fallback": job.fallback},
                         "sources": sorted(set(job.recap.sources) | set(job.sources)), "author": "crew"})
        else:
            if job.insight and not job.fallback:
                self.n += 1
                cues.append({**base, "id": f"{job.match_id}-i{self.n:04d}", "kind": "insight", "priority": job.priority,
                             "ttl_s": 10, "audience": {**base["audience"], "personas": ["analyst", "studio"]}, "lang": "en",
                             "payload": {"headline": job.insight.headline, "why": job.insight.why_it_matters,
                                         "pattern": job.insight.pattern},
                             "sources": sorted(set(job.insight.sources) | set(job.sources)), "author": "crew"})
            for l in job.lines:
                self.n += 1
                cues.append({**base, "id": f"{job.match_id}-l{self.n:04d}", "kind": "line", "priority": job.priority,
                             "ttl_s": 8, "audience": {**base["audience"], "personas": [l.persona]}, "lang": "en",
                             "payload": {"text": l.text, "persona": l.persona, "moment": job.kind,
                                         "translations": job.translations.get(l.persona, {}), "fallback": job.fallback},
                             "sources": sorted(set(l.sources) | set(job.sources)), "author": "crew"})
        for c in cues:
            c["verified"] = True
            c["crew"] = {"trace": job.trace, "attempts": job.attempts, "verdicts": job.verdicts}
            self.sink(c)
        await ctx.yield_output({"kind": job.kind, "cues": len(cues), "fallback": job.fallback, "trace": job.trace})


def build(record: MatchRecord, sink) -> "Workflow":
    producer, analyst, narrator = Producer(record), Analyst(), Narrator()
    recap, verifier, fallback = RecapWriter(record), Verifier(record), Fallback()
    localiser, director = Localiser(), Director(sink)
    return (
        WorkflowBuilder(name="touchline-crew", start_executor=producer, max_iterations=20)
        .add_switch_case_edge_group(producer, [Case(condition=lambda j: j.kind in RECAP_KINDS, target=recap),
                                               Default(target=analyst)])
        .add_edge(analyst, narrator)
        .add_edge(narrator, verifier)
        .add_edge(recap, verifier)
        .add_switch_case_edge_group(verifier, [
            Case(condition=lambda j: route(j) == "ok", target=localiser),
            Case(condition=lambda j: route(j) == "repair" and j.kind in RECAP_KINDS, target=recap),
            Case(condition=lambda j: route(j) == "repair", target=narrator),
            Default(target=fallback)])
        .add_edge(fallback, localiser)
        .add_edge(localiser, director)
        .build()
    )
