# Touchline

A studio crew of AI agents that watches a football match as it happens, explains why each moment matters, proves every sentence against the event log, and sends a different broadcast to every fan.

Every match is synthetic. Touchline builds its own: a match engine that plays full 90-minute games between fictional clubs and writes football-realistic events and 25 Hz tracking for all 22 players and the ball.

Built for Inside the Game, the Microsoft and Premier League developer hackathon.

## Status

| Stage | State |
|---|---|
| Match engine (events, 25 Hz tracking, metadata) | working |
| Realism report against league base rates | 12 of 16 metrics in band |
| Azure: Foundry project with gpt-5.4-mini and gpt-5-mini | live |
| Feed, state service, cue feed (`schema/cue.schema.json`) | working |
| Pass difficulty from tracking alone | 0.95 correlation with the engine's truth on held-out matches |
| Match MCP server | working |
| Agent crew on Microsoft Agent Framework, with the Verifier | working |
| Pitch view, broadcast overlay, personas | next |
| Auto-eventing from rendered video | planned |

## How a moment becomes a line

```
engine match --> feed (events at resolve time, tracking at 5 Hz)
             --> state service: stats, pass difficulty, control vs chaos, pressure, rhythm
                 --> cues: scorebug, shot and pass cards, player tags, momentum, stats   (under 1 ms)
                 --> moments: goal, big chance, big save, hard pass, red card, half and full time
                     --> crew workflow (Agent Framework)
                         Producer   reads the record through the match MCP server
                         Analyst    why it matters: control, chaos, pressure, rhythm, game state
                         Narrator   one line per viewer: analyst, casual, studio caption
                         Verifier   every claim resolved through MCP; every number and name in the
                                    text must be backed; plain punctuation; repair once, else a
                                    template line built from verified facts
                         Localiser  translations that must carry every number intact
                         Director   timed cues with sources and the crew's trace
```

On a goal, a verified caption airs at once from the record; the crew's lines follow seconds later.

```bash
.venv/Scripts/python -m pip install -e services/live
.venv/Scripts/python -m touchline_live.live TL-000007 --speed 0 --langs es zu   # needs an Azure login
.venv/Scripts/python -m touchline_live.mcp_server --http                        # the match record over MCP
.venv/Scripts/python -m pytest services/live
```

The state service replays a full match in about 5 seconds, under 0.4 ms per item at the 99th percentile.
A full match sends 22 moments through the crew with 114 MCP calls. The Verifier runs in about 20 ms.

## Pass difficulty

At the release frame the state service reads where the passer, the intended target and every
opponent stand, and estimates the completion chance: length, height, a physics race for the lane
(can any defender reach a point of the lane before the ball), the space around the receiver and the
passer, progression, final third and box. Difficulty is one minus that chance. The engine's own
success probability never enters the model; it grades it. On two held-out matches (1,469 passes) the
estimate correlates 0.95 with the engine's truth, within 4.8 points on average, Brier 0.124 against
0.118 for the truth itself and 0.169 for the base rate.

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

A match takes about 70 seconds on one core and writes:

| File | Contents |
|---|---|
| `meta.json` | clubs, squads, attributes, formations, style dials, venue, referee, weather, periods, final score |
| `events.json` | about 3,300 events, schema in [`schema/event.schema.json`](schema/event.schema.json) |
| `tracking.npz` | about 154,000 frames, 22 players and the ball, centimetres as int16 |

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
