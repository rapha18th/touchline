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
| Realism against league base rates | 12 of 16 metrics in band ([`reports/realism.json`](reports/realism.json)) |
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
| Auto-eventing from rendered video | planned |

## Documentation

| Page | What it covers |
|---|---|
| [docs/EVAL.md](docs/EVAL.md) | The crew evaluation: the crew against itself without the Verifier and a one-call writer, three attacks, two judges, a hand check and the failure gallery |
| [docs/MCP.md](docs/MCP.md) | The match MCP server, how Copilot, Claude Code and Agent Framework connect, the Crew room, Ask the record and tracing |
| [docs/VOICE.md](docs/VOICE.md) | Voiced commentary with Azure AI Speech: voices, keyless auth, price, the Voice step, recorded broadcasts and the fan app control |
| [docs/DEPLOY.md](docs/DEPLOY.md) | CI, the staged build context, local deploy, CD with OpenID Connect and teardown |

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

Sixteen generated matches against aggregate base rates. The event-level bands span the 2023-24 and 2024-25 Premier League seasons (760 matches, totals from football-data.co.uk). Passing and physical bands are literature ranges. No real match rows enter the engine or this repository; only league averages set the targets.

| Metric | Engine | Band | |
|---|---|---|---|
| xG per shot | 0.116 | 0.09-0.13 | in |
| goals per shot | 0.117 | 0.095-0.13 | in |
| shots on target share | 0.375 | 0.32-0.39 | in |
| fouls per team | 11.3 | 9.5-12.5 | in |
| corners per team | 5.1 | 4.4-6.2 | in |
| yellow cards per match | 4.3 | 3.3-4.9 | in |
| red cards per match | 0.25 | 0.05-0.3 | in |
| passes per team | 417 | 380-600 | in |
| pass completion | 0.774 | 0.76-0.87 | in |
| match length, minutes | 100.6 | 95-103 | in |
| distance per outfield player, km | 11.3 | 9-12 | in |
| top speed, km/h | 30.7 | 30-37 | in |
| shots per team | 16.8 | 11.5-14.5 | high |
| shots on target per team | 6.3 | 4.0-5.4 | high |
| goals per match | 3.9 | 2.6-3.5 | high |
| ball in play, minutes | 61.8 | 53-61 | high |

Full output: [`reports/realism.json`](reports/realism.json).

## Rights

All clubs, players, venues and officials are generated from syllable banks. No league, club or player marks appear anywhere in the product.
