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
| Stream, state service, cue feed | next |
| Agent crew on Microsoft Agent Framework | next |
| Pitch view, broadcast overlay, personas | next |
| Auto-eventing from rendered video | planned |

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
