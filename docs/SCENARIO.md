# Write the match

A fan or a producer describes a match in plain words: "an underdog parks the bus and nicks it late", "a wild
end-to-end draw with a red card". Touchline writes it. A Match Director turns the story into engine settings and
story beats that can be checked. The engine plays candidate matches. A validator checks every beat against the
event log. The best match goes on the list, ready to watch or to go live with the crew.

The engine stays random. The settings shape how the clubs play; the search keeps the match that tells the story.
When no candidate meets every beat, the closest one is offered, with the beats it missed marked as missed.

Code: `services/live/touchline_live/scenario.py`. Service: `server.py` (`/api/scenario`). Screen: `web/js/write.js`.
Tests: `services/live/tests/test_scenario.py`. Bench: `services/live/touchline_live/eval/scenario_bench.py`.

## How it works

```
story -> Match Director -> Gate -> (refusals) -> Director repairs once -> Gate
         gpt-5.4-mini      clamps settings,                                 |
         structured spec   refuses beats that                               v
                           cannot hold together        candidates in a process pool
                                                       (engine Match, retuned clubs, one seed each)
                                                                            |  a goal that breaks a beat
                                                                            |  for good stops a candidate
                                                                            v
                                  data dir <- publish <- validator: every beat from the event log
```

1. **Match Director** (gpt-5.4-mini through the keyless client in `crew/llm.py`, reasoning effort none). One
   structured call returns a `ScenarioSpec`: strength, five style dials and an optional formation for each side,
   and up to six beats from a fixed vocabulary. Each beat carries the words of the story it checks.
2. **Gate** (deterministic). It clamps every setting into the engine's own ranges, fixes malformed beats
   (swapped bounds, a minute past 89, a side on a beat that takes none), merges duplicates, and refuses beats
   that cannot all hold together. Satisfiability is checked over every goal order up to ten goals. When the
   gate refuses a beat, the Director gets one repair round with the gate's reasons, and the repair passes the
   gate again. A failed repair keeps the gated first spec; a failed first call falls back to a keyword Director.
   The job status shows every round.
3. **Candidates.** Each candidate is `touchline_engine.sim.Match(seed, home, away)` with the clubs from
   `make_fixture(seed)` retuned to the spec: style dials and formation replaced, and every player's skills and
   pace moved with the club's strength the way `squads._player` builds them. Candidates run in a process pool
   sized to the host: one worker in a container, up to four on a laptop (`TOUCHLINE_SCENARIO_WORKERS`
   overrides). The engine's public interface is unchanged. The watcher subclasses `Match` and reads each event
   as it is emitted; it draws nothing from the engine's random stream, so a candidate that runs to the end is
   the match `Match.play` would play (a test checks this).
4. **Pruning.** A candidate is stopped at the goal that rules a beat out for good: a clean sheet conceded, a goal
   cap passed, the wrong side scoring first. Its budget goes to the next seed. The budget is three full matches
   of play by default. A pruned candidate costs only the minutes it played. Candidates already playing when the
   budget runs out finish, so a search can play up to about one match more than its budget.
5. **Early stop.** The search ends as soon as a finished candidate meets every beat. It never ends empty handed:
   if every candidate was pruned, one more plays to the end.
6. **Validator** (deterministic). Every beat is checked from `events.json`, with a plain detail and the event
   ids it rests on. Candidates rank by finished, beats met, then partial credit (a 1-1 is closer to a comeback
   than a 0-2).
7. **Publish.** The best candidate moves into the data dir in one rename, its `meta.json` already carrying a
   `scenario` note: the story, the headline, the spec, the trace of who did what, the beat report and the
   search summary. The server keeps the twelve newest written matches (`TOUCHLINE_SCENARIO_KEEP`); the showcase
   matches below TL-900000 are never touched.

### The beats

| kind | fields | holds when |
|---|---|---|
| winner | side: home, away or draw | the final score says so |
| total_goals | at_least, at_most | the goals in the match fall inside the bounds |
| late_goal | minute (1 to 89), side or either | a goal (by that side) comes once `minute` minutes are on the match clock; first-half stoppage counts as minute 45 |
| red_card | at_least (1 to 3), side or either | that many red cards are shown |
| comeback | side or either | the side was behind at some point and won |
| first_goal | side | that side scored first |
| clean_sheet | side | that side conceded nothing |
| shots_diff | side, at_least | that side had at least that many more shots |

Minutes follow the match clock: "from minute 75" means 75:00 played or later, which a broadcast labels 76'.

### Settings and their ranges

| setting | range | source |
|---|---|---|
| strength | 0.3 to 0.7 | `squads.make_team` draws every club's strength from this range |
| press, directness, width, tempo, risk | 0.15 to 0.9 | `squads.make_team` draws every style dial from this range |
| formation | 4-3-3, 4-2-3-1, 3-5-2 | `squads.FORMATIONS` |

A null keeps the club's own value. Built into the engine already: a side trailing after the hour presses and goes
direct; a side leading late sits deeper.

## What reaches the screen

The story is free text from anyone. Nothing in it reaches the engine or another viewer except through typed
fields. The headline on the match list is built from the beats' types and numbers, never from the story's words.
`/api/match/{id}` returns the headline and the beat report and leaves the story out; the match MCP server never
reads it. The person who wrote the story sees it again in their own job status. Every name on screen still comes
from the generated clubs. The Director is told never to name real clubs, players or competitions, and nothing it
writes is shown except the typed beats.

A public page spends real compute and model credit, so the server takes at most 12 stories an hour
(`TOUCHLINE_SCENARIO_PER_HOUR`), runs one job at a time and queues at most three.

## The API

```
POST /api/scenario        {"story": "a wild end-to-end draw with a red card"}       8 to 300 characters
  202 {"id": "7ac6e45b99fc", "state": "queued", ...}

GET  /api/scenario/{id}
  {"state": "playing", "headline": "A draw, a red card, at least 3 goals", "beats": [...],
   "gate": {"first": [...], "final": [...]}, "repaired": true, "director": "gpt-5.4-mini",
   "played": [{"seed": 9..., "complete": true, "met": 2, "total": 3, "score": [3, 4]}],
   "playing": [52.4], "budget": 3, "spent": 1.0, "workers": 1, "elapsed_s": 84.2}
  ... then {"state": "done", "match_id": "TL-9.....", "report": {"met": 3, "total": 3, "beats": [...]}}
```

States run queued, directing, playing, then done or failed. The new match is in `/api/matches` with a `written`
field (headline, beats met). `/?match=TL-9.....&live=1` opens it and goes live with the crew.

## Command line

```
python -m touchline_live.scenario direct "an underdog parks the bus and nicks it late"   the spec and the trace
python -m touchline_live.scenario write  "an underdog parks the bus and nicks it late"   a match in the data dir
python -m touchline_live.scenario write  "..." --rules                                    no model: the keyword Director
python -m touchline_live.scenario replay TL-9..... --out <dir>                            play it again from its meta
python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json --workers 3
```

`replay` rebuilds the clubs from the seed and the saved spec, plays the match with the plain engine and compares
the event logs byte for byte.

## What we measured

Every number here comes from `reports/scenario_bench.json`, `reports/scenario_bench_v2.json` and
`reports/scenario_director.json`. `python -m touchline_live.eval.scenario_bench --tables` prints the tables
from the first two (add `--out reports/scenario_bench_v2.json` for the second). The first run used the Director's
first prompt; the calibration section shows the prompt the code ships now.

**Headline.** With the shipped prompt, the product met 11 of 17 hand written beats and told 3 of 8 stories
completely, against 6 of 17 and none for a keyword baseline on the same seeds and budget. Settings alone move
the odds a little at best; the search and the validator do most of the work.

**The bench.** Eight generic stories, each with beats written by hand before any run (the gold beats). Each story
goes through four arms on the same seeds and the same budget of three full matches:

- **director**: the Match Director's spec, pruned search. This is the product.
- **rules**: a keyword Director (no model) reads the story; pruned search. The baseline.
- **untouched**: the Director's beats, the clubs left as drawn (no settings); pruned search.
- **no_prune**: the Director's spec; every candidate plays to the end.

Each arm offers its best match by its own beats. The bench then checks that match against the gold beats.

| arm | gold beats met | stories with every gold beat | own beats met | candidates (pruned) | match minutes played | wall s |
|---|---|---|---|---|---|---|
| director | 9 of 17 (53%) | 2 of 8 | 12 of 24 | 55 (42) | 2574 | 1173 |
| rules | 6 of 17 (35%) | 0 of 8 | 9 of 19 | 41 (23) | 2534 | 999 |
| untouched | 9 of 17 (53%) | 1 of 8 | 12 of 24 | 56 (41) | 2755 | 1335 |
| no_prune | 7 of 17 (41%) | 1 of 8 | 10 of 24 | 24 (0) | 2376 | 842 |

| Director | gold beats read | extra beats |
|---|---|---|
| director | 17 of 17 | 7 |
| rules | 13 of 17 | 6 |

| story | gold | director | rules | untouched | no_prune |
|---|---|---|---|---|---|
| an underdog parks the bus and nicks it late | away win, a goal for away from minute 75 | 2/2 | 0/2 | 2/2 | 2/2 |
| a wild end-to-end draw with a red card | a draw, at least 4 goals, a red card | 1/3 | 2/3 | 1/3 | 1/3 |
| the home side fall behind and come back to win | a home comeback | 0/1 | 0/1 | 0/1 | 0/1 |
| a tight, cagey game settled by a single goal | 1 goal | 1/1 | 0/1 | 0/1 | 0/1 |
| the favourites dominate at home and win with a clean sheet | home win, a home clean sheet, home outshoot away by 6 | 1/3 | 2/3 | 2/3 | 2/3 |
| a goalless draw where both defences win every battle | no goals | 0/1 | 0/1 | 0/1 | 0/1 |
| the visitors score first, go down to ten men and hang on to win | away score first, a red card for away, away win | 2/3 | 1/3 | 2/3 | 1/3 |
| a late winner for the home side in a game of few chances | home win, a goal for home from minute 75, at most 3 goals | 2/3 | 1/3 | 2/3 | 1/3 |

Wall seconds are on a shared laptop with three workers. Own beats are each arm's own reading of the story, so
their totals differ.

**What it says.**

- **The Director reads stories better than keywords.** Its beats cover all 17 gold beats; the keyword Director
  covers 13. It also adds beats nobody asked for: 7 over eight stories, such as a clean sheet for an underdog that
  "nicks it". An extra beat narrows the match the search looks for. Sometimes it also helps: that clean sheet let
  the search stop 7 of 8 candidates at the first home goal.
- **The Director arm meets the most.** 9 of 17 gold beats and every gold beat in 2 of 8 stories, against 6 of 17
  and none for the keyword baseline.
- **Pruning buys more tries for the same play.** With pruning the Director's spec tried 55 candidates in 2,574
  match minutes; without it, 24 candidates in 2,376. It met 9 gold beats against 7, and one more whole story (the
  single goal story, where every second goal stopped a candidate).
- **Settings, in the search, did not show a gain.** The Director's beats on clubs as drawn met 9 of 17 too. With
  eight stories and three matches of budget the bench cannot separate the settings from luck. The tilt check
  below asks that question directly.
- **Rare stories miss.** No arm wrote the comeback or the goalless draw. The engine averages 3 to 4 goals a match,
  and three matches of budget are few tries for a 0-0.
- The sample is small. A difference of one or two beats here is within what a new seed range would change.

**The tilt check.** The same question without a search: single full matches on paired seeds (the same clubs), with
the Director's settings, the keyword settings and none, each checked against the gold beats. Four matches per
story and setting, 96 matches.

| settings | single matches | gold beats met | matches with every gold beat | goals a match |
|---|---|---|---|---|
| director | 32 | 17 of 68 (25%) | 2 of 32 | 4.12 |
| rules | 32 | 16 of 68 (24%) | 1 of 32 | 3.84 |
| untouched | 32 | 16 of 68 (24%) | 0 of 32 | 3.94 |

| story (single matches) | director: gold met, goals a match | rules: gold met, goals a match | untouched: gold met, goals a match |
|---|---|---|---|
| an underdog parks the bus and nicks it late | 2 of 8, 3.5 | 0 of 8, 3.8 | 3 of 8, 4.2 |
| a wild end-to-end draw with a red card | 3 of 12, 3.5 | 7 of 12, 6.8 | 4 of 12, 4.5 |
| the home side fall behind and come back to win | 1 of 4, 4.0 | 0 of 4, 3.8 | 0 of 4, 3.8 |
| a tight, cagey game settled by a single goal | 0 of 4, 2.5 | 0 of 4, 2.5 | 0 of 4, 5.0 |
| the favourites dominate at home and win with a clean sheet | 3 of 12, 6.0 | 3 of 12, 3.0 | 3 of 12, 3.0 |
| a goalless draw where both defences win every battle | 0 of 4, 4.0 | 0 of 4, 3.8 | 0 of 4, 3.8 |
| the visitors score first, go down to ten men and hang on to win | 4 of 12, 4.5 | 4 of 12, 3.5 | 4 of 12, 3.5 |
| a late winner for the home side in a game of few chances | 4 of 12, 5.0 | 2 of 12, 3.8 | 2 of 12, 3.8 |

**What it says.** On their own, the Director's settings hardly change the odds: 17 gold beats of 68 against 16
with no settings at all. The dials can move the engine when they are pushed hard. The keyword Director sets both
sides to tempo and risk 0.85 for a wild game, and those four matches averaged 6.8 goals against 4.5 on the same
clubs as drawn. Low tempo and risk gave 2.5 goals against 5.0 on one story's clubs, and 4.0 against 3.8 on
another's, so the low end is less clear. The Director's dials stayed near the middle (most between 0.35 and
0.75). Twice the matches went against the story: a home clean sheet wanted, and the away side scored in all four;
"a game of few chances", and 5.0 goals against 3.8 with no settings. Four matches a cell is a small sample. Where
a story comes true, the search does the work: it keeps the match where the beats hold.

**Calibration: the engine's measured response goes into the prompt.** By hand, from the tilt check, the
Director's prompt gained one measured line: mild dials barely move the result; both sides at tempo and risk 0.85 (press and directness 0.75)
gave about 7 goals a match; tempo 0.3 and risk 0.2 gave about 2.5. The low-end number rests on one story's four
matches. Then the Director wrote all eight specs again and its arm ran again on the same seeds, in the search and
in the tilt check (`reports/scenario_bench_v2.json`, same command with `--out reports/scenario_bench_v2.json
--arms director --tilt-arms director`).

| | search: gold beats met | search: stories with every gold beat | single matches: gold beats met | single matches with every gold beat |
|---|---|---|---|---|
| Director, calibrated | 11 of 17 | 3 of 8 | 22 of 68 (32%) | 3 of 32 |
| Director, first prompt | 9 of 17 | 2 of 8 | 17 of 68 (25%) | 2 of 32 |
| Director's beats, no settings | 9 of 17 | 1 of 8 | 16 of 68 (24%) | 0 of 32 |
| keyword Director | 6 of 17 | 0 of 8 | 16 of 68 (24%) | 1 of 32 |

The calibrated Director met the most in both checks, and its goals moved the way the stories asked: the wild draw
went from 3.5 goals a match to 5.0, "a game of few chances" from 5.0 to 2.8. The calibrated search wrote the
comeback, which no arm had written before. Two cautions. The gain in the single matches, 6 beats of 68 over no
settings, is about one standard deviation of a count that size: it points the right way and needs more matches
to stand as a result. And the
second run is a new set of Director calls, so the change mixes the prompt with the model's own variation (it
also read one gold beat fewer: 16 of 17).

**The Director on the app's examples.** Five calls on each of the four stories the app offers, after the repair
fix below and before the calibration:

| example story | calls | first spec refused by the gate | repaired | fell back to rules | beats, most common |
|---|---|---|---|---|---|
| An underdog parks the bus and nicks it late | 5 | 0 | 0 | 0 | away win, a goal for away from minute 75, an away clean sheet (4 of 5) |
| A wild end-to-end draw with a red card | 5 | 1 | 1 | 0 | a draw, a red card, 3 to 6 goals (3 of 5) |
| The home side fall behind and come back to win | 5 | 0 | 0 | 0 | a home comeback, home win (4 of 5) |
| A tight, cagey game settled by a single goal | 5 | 1 | 1 | 0 | home win, 1 goal (4 of 5) |

No call fell back, and no spec for the wild draw carried a shots gap after the fix. The Director still adds beats
of its own: a late goal in two of the wild draws, a side for the single goal.

**One match written in the app.** `reports/scenario_TL-942704.json` is the full note of a match written from the
picker on a laptop with one worker: "A wild end-to-end draw with a red card". The Director's first spec had a
shots gap with no side; the gate refused it; the Director repaired the spec in 1.9 s and added a home shots edge
the story never asked for. Three candidates played, 102 to 131 s each while the bench shared the CPU; the job
took 366 s from story to match. None met every beat; the 1-1 draw met 2 of 4 (the draw and the shots edge) and was offered with the
missed red card and goals marked as missed. `replay` played it again from its meta alone: 3,545 events,
identical byte for byte. That repair is why the repair prompt now says to drop a refused beat the story never
asked for, and why a shots gap is reserved for a side that dominates. The bench's specs were made before that
fix, and its wild draw carries the same invented edge (home outshoot away by 1). The Director check above was
made after the fix.

**Languages.** The Director read one story each in Spanish, French and Portuguese correctly in a spot check (a
draw with goals and a red card; the visitors scoring first and holding on; a home comeback). That is three
calls, not a measurement. The keyword fallback reads English only.

**Fixed along the way.** The first bench run showed one Director call failing with "Event loop is closed": each
job runs its own event loop, and the cached model client still held connections from a closed one. That story
had quietly fallen back to the keyword Director. The Director now builds a fresh client per job and closes it in
its own loop; the bench retries once and records any fallback. The affected story was run again.

## Limits

- **The engine decides.** The settings barely move the odds of a beat on their own, and nothing can script a
  goal. Rare stories (a comeback, a goalless draw) missed in every arm of the bench. The job then offers the
  closest match and says which beats it missed.
- **Waiting.** A full match took 102 to 131 s on this laptop while the bench shared it
  (`reports/scenario_TL-942704.json`). The container runs one worker on one vCPU, so a story that needs every
  candidate takes several minutes there; the container was not timed. The job reports each candidate as it plays.
- **The bench is small.** Eight stories, one seed range each, four matches a cell in the tilt check. It shows
  where the product stands; it cannot rank close arms.
- **One replica, local disk.** Jobs live in the memory of the replica that took them, and written matches live
  on its disk. `infra/deploy.sh` runs at most one replica, so every poll reaches the job. It also scales to zero,
  and a cold start clears the written matches. A shared file share for the data dir would keep them.
- **The vocabulary is small.** "Parks the bus" becomes settings, never a beat: there is no possession or shape
  beat. "A late winner" is checked as a win plus a late goal by the winner, which also holds for a late third goal
  in a 3-0.
- **The Director varies.** The same story can produce different beats on different calls. The bench saves every
  spec it used so its tables can be checked.
