"""Ask the record: a viewer's question, answered by an Agent Framework agent over the match MCP server,
and shown only when the crew's own Verifier passes it.

The agent sees the match through MCPStreamableHTTPTool pointed at the web service's /mcp, the same read-only
tools the crew uses. Its answer comes back with typed claims and cited event ids. The Verifier then checks the
claims against the record, every number and name in the text, the house style, and that no cited event lies
after the viewer's match time. One repair is allowed. If the second answer fails too, the viewer is told the
record could not back an answer, and is shown why.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
from agent_framework import Agent, MCPStreamableHTTPTool
from pydantic import BaseModel, Field

from .. import telemetry
from . import llm, prompts
from .models import Claim
from .verify import MatchRecord, verify_text

TOOLS = ("match_info", "state_at", "events_between", "get_events", "player_line", "leaders")
# Words that rank players. The Verifier checks numbers, so a ranking needs its own checkable claim.
RANKING = re.compile(r"\b(most|least|highest|lowest|fewest|furthest|farthest|fastest|top)\b", re.I)
MAX_WORDS = 45

INSTRUCTIONS = f"""You answer a fan's question about one synthetic football match, as of a match time the fan is
watching. Look facts up with the match tools, always with the given match_id. Never look past match_s: the fan
has not seen those events yet.
Which tool:
- state_at(match_s): the score, team totals (goals, shots, sot, xg, passes, complete, corners) and the control read.
- events_between(from_s=0, to_s=match_s, types=[...]): who did what and when. Types include goal, shot, save,
  card, substitution, corner, foul, offside. Each event has an id, a clock and the player's name.
- player_line(player, match_s): one player's numbers. match_info: squads, venue, referee.
- leaders(stat, match_s, team): who leads a stat, best first. Use it for every "who has the most" question, and
  back the answer with {{"kind": "leader", "stat": "<stat>", "team": "home" | "away" | null, "player": "<id>"}}.
- get_events(ids): only for ids you already saw in a tool result.
Answer in one or two short sentences, {MAX_WORDS - 5} words at most, plain and friendly. Give match times
exactly as the event clock gives them, like 22:48, and cite that event.
`sources` holds event ids from tool results (their `id` field), never times. Leave it empty when the answer
rests on state_at alone.
Never rank players (most, highest, furthest, top) without a leader claim from the leaders tool.
Claims are for numbers and rankings only; names and words need none.
If the tools cannot answer the question, say so in one sentence and make no claims.

{prompts.RULES}"""


class Answer(BaseModel):
    answer: str = Field(description="one or two sentences")
    claims: list[Claim] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)


def _tool_traffic(messages) -> tuple[list[dict], list]:
    """The tool calls the agent made and the results it read, from the run's messages."""
    calls, results = [], []
    for m in messages or []:
        for c in getattr(m, "contents", None) or []:
            kind = getattr(c, "type", "")
            if kind == "function_call":
                args = c.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                calls.append({"tool": c.name, **{k: v for k, v in (args or {}).items() if k in ("match_s", "from_s", "to_s", "ids", "player")}})
            elif kind == "function_result":
                r = c.result
                if isinstance(r, str):
                    try:
                        r = json.loads(r)
                    except ValueError:
                        pass
                results.append(r if not isinstance(r, list) else [getattr(x, "text", x) for x in r])
    return calls, results


async def _run(agent: Agent, prompt: str, tries: int = 3):
    """One agent run. A dropped connection to the model is retried after a short pause; nothing else is."""
    for i in range(tries):
        try:
            return await agent.run(prompt, options={"response_format": Answer, "reasoning": {"effort": "none"}})
        except Exception as exc:
            if i == tries - 1 or "connection" not in str(exc).lower():
                raise
            await asyncio.sleep(1.5 * (i + 1))


async def ask(question: str, match_id: str, match_s: float, mcp_url: str, *, http_client: httpx.AsyncClient | None = None) -> dict:
    """Answer one question. Returns the answer (or None), its claims and sources, and the whole trace."""
    question = " ".join(question.split())[:300]
    trace, verdicts = [], []
    with telemetry.span("crew.ask", **{"touchline.match_id": match_id, "touchline.match_s": match_s}) as span:
        async with MCPStreamableHTTPTool(name="match", url=mcp_url, allowed_tools=TOOLS, load_prompts=False,
                                         http_client=http_client, request_timeout=30) as tool, \
                MatchRecord("", url=mcp_url, http_client=http_client) as record:
            info = await record.call("match_info", match_id=match_id)
            roster = [p["name"] for s in ("home", "away") for p in info["squads"][s]]
            clubs = [info["home"], info["away"]]
            # The referee and the ground are named in the record too.
            roster += [n for n in (info.get("referee"), info.get("venue")) if n] + [info.get("referee", "").replace(".", "")]
            agent = Agent(client=llm.client(llm.FAST), name="record", instructions=INSTRUCTIONS, tools=[tool])
            prompt = json.dumps({"match_id": match_id, "match_s": round(match_s, 1), "home": clubs[0], "away": clubs[1],
                                 "question": question}, ensure_ascii=False)
            ans = None
            for attempt in range(2):
                t0 = time.perf_counter()
                run = await _run(agent, prompt)
                ans = run.value
                calls, results = _tool_traffic(run.messages)
                trace.append({"role": "record", "ms": round((time.perf_counter() - t0) * 1000), "attempt": attempt, "mcp": calls})
                t0 = time.perf_counter()
                v = await verify_text(record, match_id, match_s, ans.answer, ans.claims, {"tool_results": results},
                                      roster, clubs, max_words=MAX_WORDS)
                problems = list(v["problems"])
                if RANKING.search(ans.answer) and not any(c.kind == "leader" for c in ans.claims):
                    problems.append("a ranking needs a leader claim from the leaders tool")
                if ans.sources:
                    cited = await record.call("get_events", match_id=match_id, ids=ans.sources[:20])
                    problems += [f"event #{e['id']} is after the viewer's match time" for e in cited or [] if e["match_s"] > match_s + 1]
                ok = not problems
                verdicts.append({"ok": ok, "problems": problems, "claims_checked": v["claims_checked"]})
                trace.append({"role": "verifier", "ms": round((time.perf_counter() - t0) * 1000), "ok": ok, "problems": len(problems)})
                if ok:
                    break
                prompt = prompt + "\n\nYOUR LAST ANSWER FAILED THE VERIFIER. Problems: " + json.dumps(problems, ensure_ascii=False) + \
                    "\nAnswer again, fixing every problem. Drop any number you cannot back with a claim."
        ok = verdicts[-1]["ok"]
        span.set_attribute("touchline.outcome", "first_pass" if ok and len(verdicts) == 1 else "repaired" if ok else "refused")
        return {"ok": ok, "answer": ans.answer if ok else None,
                "claims": [c.model_dump(exclude_none=True) for c in ans.claims] if ok else [],
                "sources": sorted(set(ans.sources)) if ok else [], "verdicts": verdicts, "trace": trace}
