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
2. **Gate** (deterministic). It clamps every setting into the engine's own ranges (a NaN or an infinity becomes
   the middle of the range, with a note), fixes malformed beats (swapped bounds, a minute past 89, a side on a
   beat that takes none), merges duplicates, refuses a second goals beat that contradicts the first, and refuses
   beats that cannot all hold together. Satisfiability is checked over every goal order up to ten goals. When the
   gate refuses a beat, the Director gets one repair round with the gate's reasons, and the repair passes the
   gate again. A failed repair keeps the gated first spec; a failed first call falls back to a keyword Director.
   A story with no checkable beat at all is refused before any play, and the job says so. The job status shows
   every round.
3. **Candidates.** Each candidate is `touchline_engine.sim.Match(seed, home, away)` with the clubs from
   `make_fixture(seed)` retuned to the spec: style dials and formation replaced, and every player's skills and
   pace moved with the club's strength the way `squads._player` builds them. Candidates run in a process pool
   sized to the host: one worker in a container, up to four on a laptop (`TOUCHLINE_SCENARIO_WORKERS`
   overrides, at most four). Workers run at low priority, so the live channels keep the CPU while a story
   plays. The engine's public interface is unchanged. The watcher subclasses `Match` and reads each event
   as it is emitted; it draws nothing from the engine's random stream, so a candidate that runs to the end is
   the match `Match.play` would play (a test checks this).
4. **Pruning.** A candidate is stopped at the goal that rules a beat out for good: a clean sheet conceded, a goal
   cap passed, the wrong side scoring first. Its budget goes to the next seed. The budget is three full matches
   of play by default. A pruned candidate costs only the minutes it played. A seed starts only when the play
   counted so far, plus a full match for every seed still playing, leaves room for it, so the search stays
   inside its budget.
5. **Early stop.** The search ends as soon as a finished candidate meets every beat. It never ends empty handed:
   if every candidate was pruned, one more plays to the end. That candidate is the only play past the budget.
   Seeds count in order, and a result counts only once every lower seed has counted, so one worker and three
   workers choose the same match; extra workers only play ahead (`ahead` in the note). A search that runs past
   its time (900 s, or 400 s a match with one worker; `TOUCHLINE_SCENARIO_WAIT_S`) offers the best finished
   candidate and fails only if none finished.
6. **Validator** (deterministic). Every beat is checked from `events.json`, with a plain detail and the event
   ids it rests on (a shots gap cites the side's shots, up to 30). Candidates rank by finished, beats met, then
   partial credit (a 1-1 is closer to a comeback than a 0-2).
7. **Publish.** The best candidate moves into the data dir in one rename, its `meta.json` already carrying a
   `scenario` note: the story, the headline, the spec, the trace of who did what, the beat report, the
   search summary, the sha256 of `events.json` and of the Director's prompt, and the commit. The server keeps the twelve newest written matches (`TOUCHLINE_SCENARIO_KEEP`); the showcase
   matches below TL-900000 are never touched.

### The beats

| kind | fields | holds when |
|---|---|---|
| winner | side: home, away or draw | the final score says so |
| total_goals | at_least, at_most | the goals in the match fall inside the bounds |
| late_goal | minute (1 to 89), side or either | a goal (by that side) at or after that minute as the screen shows it; first-half stoppage counts as minute 45 |
| red_card | at_least (1 to 3), side or either | that many red cards are shown |
| comeback | side or either | the side was behind at some point and won |
| first_goal | side | that side scored first |
| clean_sheet | side | that side conceded nothing |
| shots_diff | side, at_least | that side had at least that many more shots |

Minutes are the ones on the screen: a goal labelled 75' meets "from minute 75". Before the review the
validator read 74:30 played as minute 74.5 while the screen showed 75', so a goal labelled 89' could miss a
minute-89 beat. The bench reports below were made with that older reading, which asked for up to a minute more.

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
fields. The headline on the match list is built only from the beats' types and numbers.
`/api/match/{id}` returns the headline and the beat report and leaves the story out; the match MCP server never
reads it. The person who wrote the story sees it again in their own job status. Every name on screen still comes
from the generated clubs. The Director is told never to name real clubs, players or competitions, and nothing it
writes is shown except the typed beats.

A public page spends real compute and model credit, so the server takes at most 12 stories an hour
(`TOUCHLINE_SCENARIO_PER_HOUR`) and 3 from any one viewer (`TOUCHLINE_SCENARIO_PER_CLIENT`). It runs one job
at a time and holds at most three, the one playing included (`TOUCHLINE_SCENARIO_QUEUE`). A story refused for
having nothing to check, or a job that fails on our side, gives its slot back.

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
python -m touchline_live.scenario replay reports/scenario_TL-9......json --out <dir>      or from a committed report
python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json --workers 3
```

`replay` rebuilds the clubs from the seed and the saved spec and plays the match with the plain engine. With the
original `events.json` at hand it compares the event logs; from a report it compares the sha256 of the new
`events.json` with the one the note recorded.

## What we measured

Every number here comes from a file in `reports/`, named where it is used.
`python -m touchline_live.eval.scenario_bench --tables --out <report>` prints the tables of any bench report.
Reports made from now on record the sha256 of the Director's prompt and the commit (`prompt_sha256`,
`git_sha`); the three bench reports predate that.

The bench reports were made before the review fixes, and three of those fixes touch them. The search then
counted candidates in the order they finished, so with three workers a rerun of the same command could play other
seeds; now one worker and three choose the same match. The validator read minutes half a minute or so behind the
screen (see The beats). And the keyword Director ignored a named side for "score first": "the visitors score
first" became "home score first". That bug decided the keyword arm's 0 of 3 on the visitors story in every run
below; the fixed reader gives "away score first".

The engine changed under this work. The first runs (`scenario_bench.json`, `scenario_bench_v2.json`) played on the
engine before its realism calibration (`docs/REALISM.md`, merged here at 19d909f). The current engine plays about
3.1 goals a match (`reports/realism.json`). The dial probe and `scenario_bench_v3.json` play on it.

**Headline, current engine.** Over eight hand-written stories on the same seeds and budget, the product (Match
Director, gate, pruned search, validator) met 9 of 17 hand-written beats and told 3 stories completely. A keyword
Director with the same search also met 9 of 17 and told 2. The Director read 16 of the 17 beats by kind and side
(13 exactly, bounds and minute too); the keywords read 13 (10 exactly). No effect of the style dials on goals was
detected over eight matches a setting (a difference of 0.6 goals, standard error 0.7).

**Search against one match.** On the current engine only the director and keyword arms have run, so the
closed loop has no current-engine baseline yet. On the engine before the realism calibration it had one: the
Director's search met 9 of 17 gold beats (53%), and single matches with the same settings met 17 of 68 (25%).
Pruning tried 55 candidates in 2,574 match minutes (2.1 per 100 minutes) against 24 in 2,376 without it (1.0 per
100).

### On the current engine

**The dial probe** (`reports/scenario_dials.json`, `--dial-probe 8`). Eight full matches on the same seeds with
both sides' dials high (tempo and risk 0.85, press and directness 0.75), low (tempo 0.3, risk 0.2) and as drawn:

| dials | matches | goals a match | matches with a red card |
|---|---|---|---|
| high | 8 | 3.38 | 2 |
| low | 8 | 2.75 | 0 |
| as drawn | 8 | 3.25 | 0 |

High against low is 0.62 goals a match with a standard error of 0.72 (the matches gave 7, 3, 2, 2, 4, 4, 2, 3
and 5, 1, 2, 2, 3, 3, 3, 3 goals). Eight matches cannot tell "no effect" from an effect of 1.5 goals, so this
says only that no effect on goals was detected. The probe counted goals and red cards; it did not measure how
the dials change the look of a match. The Director's prompt quotes these numbers, says no effect on goals has
been shown, and tells it to set the dials for the style the story describes and let the beats carry the result.

**The bench** (`reports/scenario_bench_v3.json`, `--out reports/scenario_bench_v3.json --arms director,rules`).
The same eight stories, gold beats and seeds as the earlier runs, described below. Two arms: the product with the
shipped prompt, and the keyword baseline.

| arm | gold beats met | stories with every gold beat | own beats met | candidates (pruned) | match minutes played | wall s |
|---|---|---|---|---|---|---|
| director | 9 of 17 (53%) | 3 of 8 | 12 of 22 | 35 (19) | 2264 | 909 |
| rules | 9 of 17 (53%) | 2 of 8 | 12 of 19 | 34 (17) | 2323 | 828 |

| Director | gold beats read (kind and side) | read exactly (bounds and minute too) | extra beats |
|---|---|---|---|
| director | 16 of 17 | 13 of 17 | 6 |
| rules | 13 of 17 | 10 of 17 | 6 |

"Read" matches a gold beat by kind and side only, so "at least 3 goals" reads gold "at least 4 goals". "Read
exactly" also needs the same bounds and minute. Extra beats are kinds and sides the gold never asked for.

| story | gold | director | rules |
|---|---|---|---|
| an underdog parks the bus and nicks it late | away win, a goal for away from minute 75 | 0/2 | 1/2 |
| a wild end-to-end draw with a red card | a draw, at least 4 goals, a red card | 1/3 | 2/3 |
| the home side fall behind and come back to win | a home comeback | 0/1 | 1/1 |
| a tight, cagey game settled by a single goal | 1 goal | 1/1 | 0/1 |
| the favourites dominate at home and win with a clean sheet | home win, a home clean sheet, home outshoot away by 6 | 3/3 | 2/3 |
| a goalless draw where both defences win every battle | no goals | 1/1 | 0/1 |
| the visitors score first, go down to ten men and hang on to win | away score first, a red card for away, away win | 2/3 | 0/3 |
| a late winner for the home side in a game of few chances | home win, a goal for home from minute 75, at most 3 goals | 1/3 | 3/3 |

**What it says.** The two arms tie on beats. The visitors story is no evidence for the Director: the keyword
arm's 0 of 3 there came from the reader bug above. The goalless draw went to the Director by luck as much as
anything: both arms stopped every candidate at its first goal. The keywords won stories a single seed decided,
such as the comeback. Three cautions. The keyword list and the bench stories were written by the same hand, so
the keywords know these phrasings; by construction they read English only. Eight stories cannot rank close arms.
And each run of the Director is a new set of model calls.

### On the engine before its realism calibration

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

| Director | gold beats read (kind and side) | read exactly (bounds and minute too) | extra beats |
|---|---|---|---|
| director | 17 of 17 | 13 of 17 | 7 |
| rules | 13 of 17 | 10 of 17 | 6 |

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

- **The Director reads stories better than keywords.** Its beats cover all 17 gold beats by kind and side (13
  exactly); the keyword Director covers 13 (10 exactly). It also adds beats nobody asked for: 7 over eight stories, such as a clean sheet for an underdog that
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
- **Rare stories miss.** No arm wrote the comeback or the goalless draw. That engine averaged about 4 goals a
  match, and three matches of budget are few tries for a 0-0.
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

**The measured line in the prompt.** By hand, from the tilt check, the Director's prompt gained one measured
line: mild dials barely move the result; both sides at tempo and risk 0.85 (press and directness 0.75)
gave about 7 goals a match; tempo 0.3 and risk 0.2 gave about 2.5. The low-end number rests on one story's four
matches. Then the Director wrote all eight specs again and its arm ran again on the same seeds, in the search and
in the tilt check (`reports/scenario_bench_v2.json`, same command with `--out reports/scenario_bench_v2.json
--arms director --tilt-arms director`).

| | search: gold beats met | search: stories with every gold beat | single matches: gold beats met | single matches with every gold beat |
|---|---|---|---|---|
| Director, measured line | 11 of 17 | 3 of 8 | 22 of 68 (32%) | 3 of 32 |
| Director, first prompt | 9 of 17 | 2 of 8 | 17 of 68 (25%) | 2 of 32 |
| Director's beats, no settings | 9 of 17 | 1 of 8 | 16 of 68 (24%) | 0 of 32 |
| keyword Director | 6 of 17 | 0 of 8 | 16 of 68 (24%) | 1 of 32 |

With the measured line the Director met the most in both checks, and its goals moved the way the stories asked:
the wild draw went from 3.5 goals a match to 5.0, "a game of few chances" from 5.0 to 2.8. Its search wrote the
comeback, which no arm had written before. Two cautions. The gain in the single matches, 6 beats of 68 over no
settings, is about one standard deviation of a count that size: it points the right way and needs more matches
to stand as a result. And the
second run is a new set of Director calls, so the change mixes the prompt with the model's own variation (it
also read one gold beat fewer: 16 of 17). Then the realism calibration landed. On the new engine the dial probe
above detected no effect on goals, though its eight matches a setting are too few to compare with the 7 and 2.5
goals of the old tilt cells. The prompt quotes the probe now, and these two runs describe the engine as it was.

**The Director on the app's examples** (`reports/scenario_director.json`, `--director-check 5`). Five calls
on each of the four stories the app offers, with the prompt that ships (its sha256 is in the file):

| example story | calls | first spec refused by the gate | repaired | fell back to rules | beats, most common |
|---|---|---|---|---|---|
| An underdog parks the bus and nicks it late | 5 | 0 | 0 | 0 | away win, a goal for away from minute 75, an away clean sheet (3 of 5) |
| A wild end-to-end draw with a red card | 5 | 0 | 0 | 0 | a draw, a red card, at least 3 goals (3 of 5) |
| The home side fall behind and come back to win | 5 | 0 | 0 | 0 | a home comeback, home win (4 of 5) |
| A tight, cagey game settled by a single goal | 5 | 1 | 1 | 0 | 1 goal (3 of 5) |

No call fell back to the keyword reader, and no spec for the wild draw carried a shots gap. The gate refused
one first spec of twenty, and the repair passed. The Director still adds beats of its own, such as a clean sheet
for the underdog. Calls took 3.6 to 7.8 s.

**Matches written end to end.** Two notes from `python -m touchline_live.scenario write` on this laptop with
three workers, on the current engine and the shipped prompt, each with a replay from the committed report alone.

- `reports/scenario_TL-923149.json`, "A tight, cagey game settled by a single goal". The Director read one beat,
  "1 goal", in 12.1 s, and the gate passed it. Seed 923148 was stopped at 54' when a second goal broke the beat;
  seed 923149 finished 0-1 and met it. A third seed, played ahead by a spare worker, was dropped. 78 s from
  the spec to the match. The replay from the report gives the recorded sha256 (3,359 events).
- `reports/scenario_TL-978699.json`, "An underdog parks the bus and nicks it late". The Director read four
  beats: away win, an away goal from minute 75, an away clean sheet, away score first. Five candidates were
  stopped early (the best had met 2 of 4 when the home side's goal at 72' broke the clean sheet), the budget
  ran out, and one more played to the end: a 3-3 draw that met none of the four. The job offered it with every
  beat marked missed. 174 s from the spec to the match; the replay gives the recorded sha256 (3,320 events).
  The extra beats the Director added, the clean sheet and the first goal, are what pruned the near misses.

`reports/scenario_TL-942704.json` is an earlier note, from the engine before the realism calibration and an
older prompt. Its first spec had a shots gap with no side, the gate refused it, and the repair added a home
shots edge the story never asked for; the repair prompt now says to drop a refused beat the story never asked
for. Its replay needs the original match files, which are gone, and it recorded no hash, so it cannot be
checked again.

**Languages.** The product speaks English, Spanish, French and Portuguese. The same four examples in Spanish,
French and Portuguese went through the same five calls each (`languages` in `reports/scenario_director.json`):

| example story | language | calls | fell back to rules | same beats as the English, most common (any order) | beats, most common |
|---|---|---|---|---|---|
| Un equipo modesto aparca el autobús y gana al final | es | 5 | 0 | 3 of 5 | away win, a goal for away from minute 75, an away clean sheet (3 of 5) |
| Un empate loco de ida y vuelta con una tarjeta roja | es | 5 | 0 | 1 of 5 | a draw, a red card (4 of 5) |
| Los locales van perdiendo y remontan para ganar | es | 5 | 0 | 5 of 5 | home win, a home comeback (3 of 5) |
| Un partido cerrado y táctico que se decide por un solo gol | es | 5 | 0 | 0 of 5 | 1 goal, home win (4 of 5) |
| Un petit poucet gare le bus et l'emporte sur le tard | fr | 5 | 0 | 3 of 5 | away win, a goal for away from minute 75, an away clean sheet (3 of 5) |
| Un match nul fou, d'un but à l'autre, avec un carton rouge | fr | 5 | 0 | 0 of 5 | a draw, at least 2 goals, a red card (2 of 5) |
| L'équipe à domicile est menée puis renverse le match pour gagner | fr | 5 | 0 | 2 of 5 | home win, a home comeback, away score first (2 of 5) |
| Un match serré et prudent, décidé par un seul but | fr | 5 | 0 | 0 of 5 | 1 goal, home win (5 of 5) |
| Um azarão estaciona o ônibus e vence no fim | pt | 5 | 0 | 0 of 5 | away win, a goal for away from minute 80, an away clean sheet (3 of 5) |
| Um empate maluco, lá e cá, com um cartão vermelho | pt | 5 | 0 | 3 of 5 | a draw, a goal from minute 75, a red card (2 of 5) |
| O time da casa sai perdendo e vira para vencer | pt | 5 | 0 | 4 of 5 | a home comeback, home win (4 of 5) |
| Um jogo truncado e cauteloso decidido por um único gol | pt | 5 | 0 | 1 of 5 | 1 goal, home win, a home clean sheet, home score first (1 of 5) |

None of the 60 calls fell back. The beat sets match the English call's most common set in 22 of 60 calls; most
differences are one beat added, dropped or moved (a home win beside "1 goal", minute 80 for minute 75, no goals
beat for the wild draw). Five calls a story is a small sample, and the English calls vary too: their own most
common set covers 3 or 4 of 5. The keyword fallback reads English only, so a story in another language finds no
beat when the model is out of reach, and the job says so.

**Fixed along the way.** The first bench run showed one Director call failing with "Event loop is closed": each
job runs its own event loop, and the cached model client still held connections from a closed one. That story
had quietly fallen back to the keyword Director. The Director now builds a fresh client per job and closes it in
its own loop; the bench retries once and records any fallback. The affected story was run again.

## Limits

- **The engine decides.** No effect of the settings on goals has been shown on the current engine, and nothing
  can script a goal. Rare stories come true on luck: over the seven search runs in the bench reports the
  comeback came true twice and the goalless draw once. When a story misses, the job offers the closest match and
  says which beats it missed.
- **Waiting.** A full match on the current engine takes 126 to 150 s on this laptop (median 139 s over 96
  matches; the `secs` of each row in `reports/realism.json`). The container runs one worker on one vCPU, so a
  story that needs every candidate takes several minutes there; the container was not timed. The job reports
  each candidate as it plays.
- **The bench is small.** Eight stories, one seed range each, four matches a cell in the tilt check. It shows
  where the product stands; it cannot rank close arms.
- **One replica, local disk.** Jobs live in the memory of the replica that took them, and written matches live
  on its disk. `infra/deploy.sh` runs at most one replica, so every poll reaches the job. It also scales to zero,
  and a cold start clears the written matches. A shared file share for the data dir would keep them.
- **The vocabulary is small.** "Parks the bus" becomes settings only: there is no possession or shape beat. "A late winner" is checked as a win plus a late goal by the winner, which also holds for a late third goal
  in a 3-0.
- **The Director varies.** The same story can produce different beats on different calls. The bench saves every
  spec it used so its tables can be checked.
- **Beats the story never asked for are hard.** An extra beat prunes candidates like any other. In the current
  bench the Director's own additions ("1 to 2 goals", an away clean sheet) sit on two of the stories it lost:
  the underdog and the late winner.
