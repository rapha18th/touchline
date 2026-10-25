# The match record over MCP, the Crew room, and tracing

Touchline's crew works from one source of truth: the match MCP server. Every fact a line rests on comes from it.
Every number a line states is checked against it before air. This page shows how to reach that server yourself,
how the Crew room makes the crew's work visible, and how each crew moment becomes one trace in Application Insights.

## 1. The match MCP server

The server is `services/live/touchline_live/mcp_server.py`, built on FastMCP. Every tool answers as of a match
time, by replaying the event log up to that second. A client sees only what was known when a line went to air.
Every tool is read-only and says so in its annotations (`readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true`, `openWorldHint: false`).

| Tool | What it returns |
| --- | --- |
| `list_matches()` | Every generated match: clubs and final score. |
| `match_info(match_id)` | Clubs, formations, style dials, squads, venue, referee. |
| `state_at(match_id, match_s)` | Score, team stats and the control and chaos read at that time. |
| `events_between(match_id, from_s, to_s, types?, limit?)` | Events in a time window, names resolved. |
| `get_events(match_id, ids)` | The events with these ids. Use it to check any cited id. |
| `player_line(match_id, player, match_s)` | One player's numbers up to that time. |
| `leaders(match_id, stat, match_s, team?, limit?)` | Who leads a stat, best first. |
| `verify_numbers(match_id, match_s, claims)` | Checks typed claims against the record. |

`verify_numbers` takes claims of six kinds: `score`, `team_stat`, `player_stat`, `event`, `explain` and `leader`.
A `leader` claim passes only when the named player tops the stat (ties count) among everyone it ranks.
Numbers pass within 2 percent or 0.05, whichever is larger, or when they are the true value honestly rounded.

### Three ways in

| Transport | Command or URL | Use |
| --- | --- | --- |
| stdio | `python -m touchline_live.mcp_server` | The crew's default: one child process per run. |
| Streamable HTTP, standalone | `python -m touchline_live.mcp_server --http` on port 8765 | A local client. |
| Streamable HTTP, in the web service | `POST /mcp` on the Touchline app | Any client, the Crew room, Ask the record. |

The `/mcp` route lives inside the FastAPI app (`server.py`). It is stateless and replies with plain JSON, so any
replica can answer any request and a browser can call a tool with one POST. Tools run on a worker thread with a
capacity of two, so a long replay never stalls the live channels. Data comes from `TOUCHLINE_DATA`.
Host checks against DNS rebinding are off: the server is public, read-only, and serves synthetic data only.

The live URL serves `/mcp` once this branch is deployed:
`https://touchline.mangohill-b9c35cb7.swedencentral.azurecontainerapps.io/mcp`.
Until then, run the app locally and use `http://127.0.0.1:8080/mcp`.

```bash
python -m uvicorn touchline_live.server:app --app-dir services/live --port 8080
```

### GitHub Copilot

VS Code, in `.vscode/mcp.json`:

```json
{
  "servers": {
    "touchline": {
      "type": "http",
      "url": "https://touchline.mangohill-b9c35cb7.swedencentral.azurecontainerapps.io/mcp"
    }
  }
}
```

Copilot CLI, in `~/.copilot/mcp-config.json`, and the Copilot coding agent, in the repository's MCP settings:

```json
{
  "mcpServers": {
    "touchline": {
      "type": "http",
      "url": "https://touchline.mangohill-b9c35cb7.swedencentral.azurecontainerapps.io/mcp",
      "tools": ["*"]
    }
  }
}
```

Then ask in agent mode: "Using touchline, who leads xG in TL-000008 at 32:40?"

### Claude Code

```bash
claude mcp add --transport http touchline https://touchline.mangohill-b9c35cb7.swedencentral.azurecontainerapps.io/mcp
```

Or commit it for the team in `.mcp.json`:

```json
{
  "mcpServers": {
    "touchline": { "type": "http", "url": "https://touchline.mangohill-b9c35cb7.swedencentral.azurecontainerapps.io/mcp" }
  }
}
```

### Microsoft Agent Framework

`MCPStreamableHTTPTool` turns the server into tools for any agent. This is how Ask the record works.

```python
from agent_framework import Agent, MCPStreamableHTTPTool
from touchline_live.crew import llm

async with MCPStreamableHTTPTool(
    name="match",
    url="http://127.0.0.1:8080/mcp",
    allowed_tools=["state_at", "events_between", "get_events", "player_line", "leaders", "match_info"],
    load_prompts=False,
) as record:
    agent = Agent(client=llm.client(llm.FAST), name="record",
                  instructions="Answer from the match tools only, as of the match time given.", tools=[record])
    reply = await agent.run('{"match_id": "TL-000008", "match_s": 1960, "question": "Who scored?"}')
```

The crew's own `MatchRecord` (`crew/verify.py`) speaks to the same server over stdio by default, or over HTTP
with `MatchRecord(data_dir, url="http://127.0.0.1:8080/mcp")`.

### One plain POST

```bash
curl -s http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_events","arguments":{"match_id":"TL-000008","ids":[947]}}}'
```

### Tests

`services/live/tests/test_mcp_http.py` drives `/mcp` with the official MCP Python client: it lists the tools and
checks every one is read-only, calls `state_at` and `get_events`, checks a bad match id is an error, checks
`leaders` and `leader` claims, and sends the single plain POST the browser sends.

## 2. The Crew room

Press **Crew room** in the transport bar, or the C key, or open the app with `?crew=1`. A drawer opens on the
left of the pitch. It fills as the match plays, one card per crew moment, newest first.

Each card shows:

- **The handoffs.** Producer, Analyst, Narrator, Verifier, Localiser and Director (Recap writer and Fallback when
  they run), in order, with the milliseconds each role took. A bar on the card draws the same timeline to scale.
- **The Verifier's verdicts.** Every check, with the number of claims it tested. A refusal lists its reasons in
  plain words, such as "claim failed: explain index claimed 2, record says 2.7", with the refused draft struck
  through. A repaired line says so. A line that failed twice says the crew aired a template built only from
  verified facts.
- **The MCP calls.** Which role called which tool, with which ids or how many claims, as of which match time,
  and how long the record took to answer.
- **What aired.** Each persona's line, the Analyst's insight, or the recap.
- **The cited events.** Each id is a button. It opens the event's facts, fetched live from `/mcp` with
  `get_events`, and says so ("Event #1236, from MCP get_events, 244 ms"). Without `/mcp` it falls back to the
  event log the page already loaded, and says that instead.

At the top, a tally counts moments, first drafts, repairs, fallbacks, claims checked and MCP calls.

**Recorded mode** reads `crew.trace`, `crew.attempts` and `crew.verdicts` from the cues in `broadcasts/`.
The three recordings in the repo predate per-call logging: they show how many MCP calls the Producer made,
without each call's tool and timing, and refusals without the refused draft. A new recording carries both.

**Live mode** shows the same story as it happens. `server.py` keeps `crew.job`, `crew.trace`, `crew.attempts`
and `crew.verdicts` in every live push. A crew run that fails outright, for example when the model cannot be
reached, appears as a "Crew error" card that says nothing aired for that moment.

### Ask the record

The box at the top of the Crew room takes a viewer's question about the match so far. `POST /api/ask/{match_id}`
with `{"question": ..., "match_s": ...}` runs `crew/ask.py`:

1. An Agent Framework agent with `MCPStreamableHTTPTool` on `/mcp` looks the answer up and replies with one or
   two sentences, typed claims and cited event ids.
2. The crew's Verifier (`verify_text`) checks the claims through `verify_numbers`, every number and name in the
   text against the tool results, and the house style. Ask adds two gates: no cited event may lie after the
   viewer's match time, and any ranking word (most, highest, furthest, top) needs a `leader` claim.
3. One repair is allowed. If the second answer fails too, the viewer sees the reasons and no answer.

The answer shows with its claim count, the MCP tools the agent called, and its cited events as buttons.
The endpoint allows two questions at a time and 40 per ten minutes (`TOUCHLINE_ASK_PER_10MIN`), since every
question spends model credit.

Measured on TL-000008 at 32:40, ten fixed questions, gpt-5.4-mini, one run
(`python scripts/ask_eval.py http://127.0.0.1:8080 TL-000008 1960 reports/ask_record_TL-000008.json`):

| Outcome | Questions |
| --- | --- |
| Passed first time | 8 |
| Passed after one repair | 1 |
| Refused | 0 |
| No answer: the model connection dropped three times | 1 |

Median wall time was 4.4 seconds. The ranking gate earned its place in this work. Before it existed, the agent
answered "who has the most passes for the away side" after checking two players, and its count was correct for
the player it named, so the Verifier passed a wrong ranking. With the `leaders` tool and a `leader` claim, the
same question returns the true leader, checked against the record.

Limits, plainly:

- Numbers written as words ("two yellow cards") are not checked. Only digits are.
- The ranking gate knows a fixed list of words. A ranking phrased another way can pass unchecked.
- The run above is one sample of ten questions. Expect answers to vary between runs.
- On this PC the network drops long connections to Azure, which is the one failure above.

## 3. Tracing

`services/live/touchline_live/telemetry.py` turns on Agent Framework's OpenTelemetry instrumentation
(`agent_framework.observability.configure_otel_providers`) and decides where spans go:

| Setting | Spans go to |
| --- | --- |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Azure Application Insights, through `azure-monitor-opentelemetry-exporter` 1.0.0b58 |
| `ENABLE_CONSOLE_EXPORTERS=true` | stdout, for local checks |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | any OTLP collector |
| none | nowhere: `setup()` does nothing and the crew runs as before |

Prompts and model replies stay out of spans unless `ENABLE_SENSITIVE_DATA=true`.

Agent Framework emits the workflow, executor, agent and model spans. Touchline adds three of its own:
`crew.moment <kind>` around each moment, with its outcome (first pass, repaired or fallback), attempts and claims
checked; `tools/call <tool>` for each MCP call, shaped per the OpenTelemetry MCP conventions; and `crew.ask` around
each Ask the record question. The Verifier's span carries a `touchline.verdict` event with the problems it found.

One real crew moment on Foundry, exported to the console and printed as a tree, is one trace of 33 spans
(`reports/crew_trace_TL-000008.txt`):

```
crew.moment hard_pass  11408 ms  {"touchline.outcome": "first_pass", "touchline.claims_checked": 9}
  workflow.run  11026 ms
    executor.process producer  282 ms
      tools/call get_events  237 ms
      tools/call state_at  44 ms
    executor.process analyst  5987 ms
      invoke_agent analyst  5987 ms
        chat gpt-5.4-mini  5984 ms  {"gen_ai.usage.input_tokens": 1475, "gen_ai.usage.output_tokens": 421}
    executor.process narrator  2864 ms
    executor.process verifier  17 ms  {"touchline.verdict.ok": true}
      tools/call verify_numbers  12 ms  (three calls, one per persona)
    executor.process localiser  1859 ms
    executor.process director  1 ms
```

Reproduce it:

```powershell
$env:ENABLE_CONSOLE_EXPORTERS = "true"
python -m touchline_live.live TL-000008 --speed 16 --until 60 --langs es --out runs/TL-000008.cues.jsonl > console.txt
python scripts/span_tree.py console.txt
```

`services/live/tests/test_telemetry.py` checks the same shape without a model: the moment span, the MCP span under
it, and Agent Framework's `workflow.run` and `executor.process` spans in the same trace.

### In Azure

Application Insights resource `touchline-insights` in `rg-touchline` (Sweden Central) is workspace-based. It writes
to `workspace-rgtouchlines3IO`, the Log Analytics workspace of the Container Apps environment `touchline-env`, so app
logs and traces sit side by side. The container app `touchline` carries `APPLICATIONINSIGHTS_CONNECTION_STRING` and
`OTEL_SERVICE_NAME=touchline-web` as environment variables. Spans flow once an image built from this branch is
deployed; the image running today predates the telemetry module.

Queries for the workspace:

```kusto
// Each crew moment and how it ended
AppDependencies
| where Name startswith "crew.moment"
| project TimeGenerated, Name, DurationMs, Outcome = tostring(Properties["touchline.outcome"])

// The match record's latency per tool
AppDependencies
| where Name startswith "tools/call"
| summarize calls = count(), p50 = percentile(DurationMs, 50), p95 = percentile(DurationMs, 95) by Name
```
