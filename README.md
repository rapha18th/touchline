# Touchline

A studio crew of AI agents that watches a football match as it happens, explains why each moment matters, proves every sentence against the event log, and sends a different broadcast to every fan.

Every match is synthetic. Touchline builds its own: a match engine that plays full 90-minute games between fictional clubs and writes football-realistic events and 25 Hz tracking for all 22 players and the ball.

Built for Inside the Game, the Microsoft and Premier League developer hackathon.

## Status

Every number in this table comes from a file in [`reports/`](reports/), and the page it links names the
command that writes it.

| Stage | State |
|---|---|
| Match engine (events, 25 Hz tracking, metadata) | working |
| Realism against league base rates | 15 of 16 metrics in band over 96 matches no dial was tuned on ([`reports/realism.json`](reports/realism.json), [REALISM](docs/REALISM.md)) |
| Azure: Foundry project with gpt-5.4-mini and gpt-5-mini | in use by the crew, keyless |
| Feed, state service, cue feed (`schema/cue.schema.json`) | working |
| Pass difficulty from tracking alone | working, graded against the engine's truth on held-out matches |
| Agent crew on Microsoft Agent Framework, with the Verifier | measured: under attack the judge calls 48 of 360 first drafts wrong and 21 of the 360 lines the crew aired ([EVAL](docs/EVAL.md)) |
| Match MCP server, stdio and `/mcp` over HTTP | working: POST only, one worker, a token bucket per client ([MCP](docs/MCP.md)) |
| Crew room and Ask the record | working: Ask answered 9 of 10 fixed questions with a verified answer, and 1 ended in a 502 ([MCP](docs/MCP.md)) |
| Voiced commentary in four languages | 288 clips for two recorded broadcasts; live voice off unless `TOUCHLINE_VOICE=1` ([VOICE](docs/VOICE.md)) |
| Fan app: 3D pitch, personas, player focus, one metric, four languages, voice | working |
| Broadcast overlay page for a browser source | working |
| Azure Container Apps with a managed identity, one replica | scripted: `infra/deploy.sh`, CI and CD on demand ([DEPLOY](docs/DEPLOY.md)) |
| Write the match: a story in, a match out | measured on the current engine: 9 of 17 hand-written beats met, the same as a keyword baseline; the Director reads 16 of 17 ([SCENARIO](docs/SCENARIO.md)) |
| Auto-eventing from the rendered feed | measured on 20 test clips filmed after the code froze: 40 of 50 passes found and 40 of 43 right, ball speed off by 3.3 km/h on average ([VISION](docs/VISION.md)) |

## Documentation

| Page | What it covers |
|---|---|
| [docs/EVAL.md](docs/EVAL.md) | The crew evaluation: the crew against itself without the Verifier and a one-call writer, three attacks, two judges, a hand check and the failure gallery |
| [docs/MCP.md](docs/MCP.md) | The match MCP server, how Copilot, Claude Code and Agent Framework connect, the Crew room, Ask the record and tracing |
| [docs/VOICE.md](docs/VOICE.md) | Voiced commentary with Azure AI Speech: voices, keyless auth, price, the Voice step, recorded broadcasts and the fan app control |
| [docs/DEPLOY.md](docs/DEPLOY.md) | CI, the staged build context, local deploy, CD with OpenID Connect and teardown |
| [docs/SCENARIO.md](docs/SCENARIO.md) | Write the match: the Match Director, the gate, the pruned search, the validator, the bench and its limits |
| [docs/VISION.md](docs/VISION.md) | Passes and shots read from the rendered feed: camera from the lines, ball and players, flight fits, scores on held-out clips |
| [docs/REALISM.md](docs/REALISM.md) | The realism bands, the gated Calibrator agent and the trail of every holdout run |

## How a moment becomes a line

```
engine match --> feed (events at resolve time, tracking at 5 Hz)
             --> state service: stats, pass difficulty, control vs chaos, pressure, rhythm
                 --> cues: scorebug, shot and pass cards, player tags, momentum, stats
                 --> moments: goal, big chance, big save, hard pass, red card, half and full time
                     --> crew workflow (Agent Framework)
                         Producer   reads the record through the match MCP server
                         Analyst    why it matters: control, chaos, pressure, rhythm, game state
                         Narrator   one line per viewer: analyst, casual, studio caption
                         Verifier   every claim resolved through MCP; every number and name in the
                                    text must be backed; plain punctuation; repair once, else a
                                    template line built from verified facts
                         Localiser  Spanish, French and Portuguese; a translation must carry every
                                    number intact and really be translated, or it is refused
                         Voice      with TOUCHLINE_VOICE=1, Azure AI Speech clips of the verified lines
                         Director   timed cues with sources and the crew's trace
```

On a goal, a verified caption airs at once from the record; the crew's lines follow seconds later.

```bash
.venv/Scripts/python -m pip install -e services/live
.venv/Scripts/python -m touchline_live.live TL-000007 --speed 0 --langs es fr pt   # needs an Azure login
.venv/Scripts/python -m touchline_live.mcp_server --http                        # the match record over MCP
.venv/Scripts/python -m pytest services/live
```

Every crew cue carries its trace: ms per role, each Verifier verdict with its reasons, and the MCP calls.
[`reports/crew_trace_TL-000008.txt`](reports/crew_trace_TL-000008.txt) is one real crew moment, exported to the
console as a tree of 33 spans. The Crew room in the fan app draws the same story for every moment ([MCP](docs/MCP.md)).

The crew evaluation ([EVAL](docs/EVAL.md), [`reports/crew_eval.json`](reports/crew_eval.json)) puts 40 moments
through the crew, the crew without its Verifier and a one-call writer, then attacks it three ways. With a false
fact planted, 23 of 120 first drafts repeated it and the crew aired none. Under attack, the judge calls 48 of 360
first drafts wrong and 21 of the 360 lines the crew aired. On clean moments the one-call writer is as accurate in
meaning; the crew's gain is in numbers and claims. The page says what the gate misses as plainly.

## One match, a broadcast for every fan

The fan app plays a match on a floodlit 3D pitch and builds the graphics from the cue stream.

- **Watch as** an analyst (tactical camera, xG, pass cards that say why a pass was hard, the control index, pressing), a casual fan (broadcast camera, plain words, the name of whoever has the ball), or the studio (clean captions).
- **Your club** narrows the player list and tints the story.
- **Follow a player**: the camera rides with them, a ring marks them, and a live line builds from the event log and the tracking: passes and accuracy, shots and xG, distance and top speed.
- **One number all match**: an xG race, the difficulty of the hardest completed passes, or your player's speed.
- **Language**: English, Spanish, French and Portuguese, translated by the crew with every number carried intact.
- **Commentary voice**: the analyst and casual lines spoken as they air, in the viewer's language, one at a time. A big moment cuts a smaller line; a line that waits too long is skipped ([VOICE](docs/VOICE.md)).
- **Crew room**: every crew moment with its handoffs, verdicts, refusals, repairs and MCP calls, and **Ask the record**, where a viewer's question is answered over MCP and shown only if the Verifier passes it ([MCP](docs/MCP.md)).
- **Recorded or live**: recorded broadcasts replay every cue a live crew run put on air, at the latency it measured; Go live starts a fresh run on the server with the crew working as the match plays.

`/overlay?match=TL-000007&persona=studio&preview=0` serves the same graphics on a transparent page for a vision mixer or a browser source.

```bash
.venv/Scripts/python -m uvicorn touchline_live.server:app --app-dir services/live --port 8080
bash infra/deploy.sh        # Azure Container Apps, image built in ACR, keyless Foundry access
```

Deploy, CI and CD are in [docs/DEPLOY.md](docs/DEPLOY.md).

## Write the match

A fan types a story: "an underdog parks the bus and nicks it late". A Match Director (gpt-5.4-mini) turns it
into engine settings and typed beats. A deterministic gate clamps the settings and refuses beats that cannot
hold together, and the Director repairs once. Candidate matches play in a process pool; a goal that rules a beat
out stops a candidate, so the same budget tries more seeds. A validator checks every beat against the event log
and cites the events. The best match joins the list, ready to watch or to go live with the crew.

- On the current engine, over eight stories, the product met 9 of 17 hand-written beats and told 3 stories
  completely; a keyword Director with the same search met 9 and told 2 (`reports/scenario_bench_v3.json`).
- On the engine before the realism calibration, the search met 9 of 17 gold beats while single matches with
  the same settings met 17 of 68, and pruning tried 55 candidates in 2,574 match minutes against 24 in 2,376
  (`reports/scenario_bench.json`).
- The Director read the app's four examples in Spanish, French and Portuguese with no fallback in 60 calls
  (`reports/scenario_director.json`).
- The style dials shape how a match looks and not its goals: over 32 matches a setting, high dials against
  low gave 4.3 more shots a side (standard error 0.6) and 0.16 more goals (standard error 0.33)
  (`reports/scenario_dials_v2.json`). The search does the work.

```bash
.venv/Scripts/python -m touchline_live.scenario write "a tight, cagey game settled by a single goal"
.venv/Scripts/python -m touchline_live.scenario replay reports/scenario_TL-923149.json --out data/replay
```

`POST /api/scenario` starts a job and `GET /api/scenario/{id}` follows it. Details: [docs/SCENARIO.md](docs/SCENARIO.md).

## Reading the feed

A vision pipeline watches the fan app's rendered broadcast and writes every pass and shot with its distance and
speed. The camera comes from the painted lines alone, the ball from a shortest-path tracker, and each flight
from a rolling and an aerial fit through that camera. No model is trained. The engine's events, its tracking and
the renderer's camera grade it (`reports/vision/summary.json`, test set):

- 40 of 50 passes found and 40 of 43 right; 8 of the 10 shots shown found.
- Pass length off by 1.49 m on average (it reads short), ball speed by 3.27 km/h, shot speed by 1.86 km/h.
- The pitch lands within 0.08 px of the truth on the median clip.
- The reported ball sits on the wrong object in 9.7% of the test frames that report one.

The feed is clean: rendered lines, even light, one fixed camera a clip. Real video will score lower.

```bash
PYTHONPATH=vision .venv/Scripts/python -m touchline_vision.score --name test --clips $TEMP/touchline_vision/test/clips --work $TEMP/touchline_vision/test/run
.venv/Scripts/python -m pytest vision/tests
```

Details: [docs/VISION.md](docs/VISION.md).

## Pass difficulty

At the release frame the state service reads where the passer, the intended target and every
opponent stand, and estimates the completion chance: length, height, a physics race for the lane
(can any defender reach a point of the lane before the ball), the space around the receiver and the
passer, progression, final third and box. Difficulty is one minus that chance. The engine's own
success probability never enters the model; it grades it. The fitted weights and their scores on two
held-out matches are in `services/live/touchline_live/pass_difficulty.json`. No command in the repository
refits it yet, so this page quotes no number from it.

## The match engine

`engine/touchline_engine` plays a match frame by frame at 25 Hz.

- **Off the ball**, each side holds a formation shape that slides with the ball. Out of possession the block drops to a depth set by the team's pressing style, the nearest players press, and defenders mark runners goal-side near their box. In possession the shape stretches, and attackers make runs in behind.
- **On the ball**, the carrier weighs every pass, a carry, a shot, a cross and a clearance by expected possession value, then picks one through a softmax shaped by the team's style dials (press, directness, width, tempo, risk).
- **Outcomes come from physical races.** A pass succeeds or fails on who reaches the lane first, who contests the reception and the passer's skill. A carry is stopped by the defender on its path. A shot's xG folds in distance, angle, pressure, body part and the bodies in the shooting lane.
- **The match has a life of its own.** Set pieces, offsides, fouls, cards, sending-offs, penalties, VAR checks, treatment stoppages, fatigue, substitution windows, momentum swings and score effects (a trailing side presses and goes direct after the hour).

Every event carries a `truth` block with the engine's own probabilities, such as a pass's success chance and a shot's xG. Downstream services estimate these from tracking alone, and the truth block grades them.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e "engine[dev]"
.venv/Scripts/python -m touchline_engine generate --seed 7 --out data/matches
.venv/Scripts/python -m touchline_engine realism --n 16 --out reports/realism.json
.venv/Scripts/python -m pytest engine
```

A match writes:

| File | Contents |
|---|---|
| `meta.json` | clubs, squads, attributes, formations, style dials, venue, referee, weather, periods, final score |
| `events.json` | every event, schema in [`schema/event.schema.json`](schema/event.schema.json) |
| `tracking.npz` | every frame at 25 Hz, 22 players and the ball, centimetres as int16 |

## Realism

96 generated matches against aggregate base rates, on seeds no dial was tuned on. 15 of 16 metrics
land in band; the miss is goals per shot. The event-level bands come from the 2023-24 and 2024-25 Premier League
seasons and the passing and physical bands from published ranges; each metric's source is in the report. No real
match rows enter the engine or this repository; only league averages set the targets. A gated Calibrator agent
set the engine's dials: [docs/REALISM.md](docs/REALISM.md) has the trail.

| Metric | Engine | Band | |
|---|---|---|---|
| xG per shot | 0.124 | 0.09-0.13 | in |
| shots on target share | 0.370 | 0.32-0.39 | in |
| fouls per team | 10.9 | 9.5-12.5 | in |
| corners per team | 5.0 | 4.4-6.2 | in |
| yellow cards per match | 4.3 | 3.3-4.9 | in |
| red cards per match | 0.15 | 0.05-0.3 | in |
| passes per team | 396 | 380-600 | in |
| pass completion | 0.791 | 0.76-0.87 | in |
| match length, minutes | 99.6 | 95-103 | in |
| distance per outfield player, km | 10.9 | 9-12 | in |
| top speed, km/h | 30.5 | 30-37 | in |
| shots per team | 11.7 | 11.5-14.5 | in |
| shots on target per team | 4.3 | 4-5.4 | in |
| goals per match | 3.1 | 2.6-3.5 | in |
| ball in play, minutes | 59.2 | 53-61 | in |
| goals per shot | 0.137 | 0.095-0.13 | high |

Full output: [`reports/realism.json`](reports/realism.json).

## Rights

All clubs, players, venues and officials are generated from syllable banks. No league, club or player marks appear anywhere in the product.
