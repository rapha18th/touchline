# Realism calibration

The match engine is tuned by an agent. The agent never edits the engine. It proposes dial vectors, a measurement plays them, and a gate decides. This page records how the defaults in `engine/touchline_engine/sim.py` were reached, what the gate refused, and what is still open.

## The target

Sixteen metrics, each with a band from published league aggregates (`engine/touchline_engine/realism.py`). Event-level bands span the 2023-24 and 2024-25 Premier League seasons (760 matches, totals from football-data.co.uk). Passing and physical bands are literature ranges. No real match rows enter the engine.

## The dials

Twenty league-level dials live at the top of `sim.py`. Each has a bounded range in `engine/tools/calibrate.py`, and the agent may only propose values inside it. Any dial can be overridden for one run:

```
TOUCHLINE_DIALS='{"SHOT_VALUE": 0.7}' python -m touchline_engine realism --n 4
```

An unknown dial name raises, so a typo never runs silently on defaults.

Three structural changes came with the dials, because no existing dial could reach ball in play:

- `RESTART_PACE` scales the dead time before throw-ins, goal kicks, corners and free kicks.
- `SUB_STOP_S` holds a restart while a substitution window happens. Substitutions now happen while the ball is dead, at the first moment of a stoppage.
- `LATE_STALL` adds dead time when the restarting side leads after the hour.

Four more dials came after the first rounds, when the trace showed the agent had no lever for them. `FINISH_LOGIT` shifts every chance's xG logit, the league's finishing level. `BLOCK_SCALE` scales the chance that bodies in the lane block a shot. `FOUL_RATE` scales the chance a challenge is a foul. `CORNER_DEFLECT` scales the chance a saved, blocked or missed shot goes behind for a corner. Each one's neutral value reproduces the engine as it was, so older trials read them at neutral.

Card thresholds became independent dials: `YELLOW_SHARE` is the chance a foul by an unbooked player is booked, `RED_SECOND` the chance a booked player's foul is a second yellow, `RED_STRAIGHT` the chance of a straight red.

## The loop

`engine/tools/calibrate.py` runs rounds. Each round:

1. **Propose.** gpt-5-mini on Microsoft Foundry (keyless, `AzureCliCredential`, Agent Framework `Agent` with a structured `Plan` response) reads the realism table, each metric's distance and margin, the dial table with bounds and effects, and every earlier trial with its verdict. It returns a diagnosis and several dial vectors, each with a hypothesis.
2. **Measure.** Each vector plays the same 24 seeds on a Kaggle CPU kernel (`engine/tools/kaggle_eval.py`). Each kernel is one self-contained script: the engine source rides inside as a base64 zip, nothing mounts and nothing downloads. Four kernels run at once, one vector each, four cores each.
3. **Gate.** A proposal is accepted only if more metrics land in band, or as many land while the summed normalised distance to the bands falls, and no metric that was in band leaves it. With all sixteen in band, it is accepted only if the safety margin (standard errors inside the band, capped at 2 per metric) rises. The best accepted proposal of a round becomes the incumbent.

### Noise, and the sensitivity screen

Over 24 matches, shots per team carry a standard error of about 0.64 and goals per match about 0.36. Any change to any dial reshuffles every random draw in every match, so two vectors a hair apart still differ by about one standard error per metric. Rounds 5 to 7 showed the cost. Trial 17 differs from trial 7 only by `FOUL_RATE` 1.0 to 1.03 and `RESTART_PACE` 1.08 to 1.09, yet shots moved by half a shot and goals by 0.4. The gate accepted it on ball in play. The proposer's small steps after that were all refused, mostly for corners or ball in play crossing an edge by noise.

Two changes answer that. The brief now gives each metric's standard error and band middle, and tells the proposer that a step too small to move a metric by two standard errors proves nothing. And `--probe` runs a sensitivity screen once per trace: one bold step per dial from the incumbent (`PROBES` in `calibrate.py`), measured on the same seeds and gated like any trial. The brief then carries each probe's effect on every metric with the noise of that effect, so the proposer can add effects and aim at band middles instead of guessing.

Every proposal, trial, refusal and promotion goes to `reports/calibration/trace.jsonl`. `reports/calibration/summary.json` holds the incumbent and the path to it. The trace is the state: run the command again and it resumes from the last incumbent.

```
python engine/tools/calibrate.py --rounds 16 --k 4 --per-kernel 1 --seeds 1-24 --probe
```

## Holdout: what the tuning seeds hid

The agent's best trial, trial 32, put all sixteen metrics in band on the 24 tuning seeds. That result did not survive unseen seeds. Shots per team carry a standard error of about 0.64 over 24 matches, so a gate that picks the best of 41 trials on one seed set partly picks luck. Every check after that used seeds the gate had never seen, and grew the sample to 96 matches where it mattered.

| Step | Dials changed from the step before | Seeds | Matches | In band | What moved |
| --- | --- | --- | --- | --- | --- |
| Trial 32 on tuning seeds | | 1-24 | 24 | 16 | |
| Trial 32 on unseen seeds | | 101-148 | 48 | 13 | goals 3.54, shots 15.2, on target 5.7 high |
| Round 11, best of four | `SHOT_VALUE` 0.76, `SHOT_RANGE` 28 | 201-248 | 48 | 14 | yellows 3.15 low, xG per shot 0.131 |
| Round 12, best of three | `YELLOW_SHARE` 0.21 | 301-348 | 48 | 16 | |
| Its confirmation | | 401-448 | 48 | 12 | goals 3.58, conversion 0.133 high |
| Centre finishing and saves | `FINISH_LOGIT` -0.22, `SAVED_SHARE` 0.37 | 501-596 | 96 | 14 | conversion 0.131, ball in play 61.5 |
| Longer restarts | `SHOT_RANGE` 30, `RESTART_PACE` 1.2 | 601-696 | 96 | 14 | xG per shot 0.133, conversion 0.142 |
| Lower finishing | `FINISH_LOGIT` -0.35, `SHOT_VALUE` 0.8 | 701-796 | 96 | 13 | goals 2.21, shots 11.2, on target 3.9 low |
| Screen of three pairs | `FINISH_LOGIT` and `SHOT_VALUE` together | 801-848 | 48 | 16, 15, 15 | |
| Final defaults | `FINISH_LOGIT` -0.25, `SHOT_VALUE` 0.9, `LOSS` 0.045 | 901-996 | 96 | 15 | conversion 0.137 |

Every row has its report in `reports/calibration/holdout/`. Steps after round 10 were chosen by hand from these holdout reports, without the agent; each was measured once on fresh seeds and is shown here whether it helped or not.

The published report, `reports/realism.json`, is the final row: 96 matches on seeds 901-996 that no step was tuned on. Fifteen of sixteen metrics are in band: 3.10 goals a match, 11.7 shots and 4.3 on target per team, 0.124 xG per shot, 396 passes per team at 79.1%, 59.2 minutes of ball in play. The one miss is conversion, 0.137 goals per shot against a band that ends at 0.13. The engine draws every goal from the shot's own xG, so conversion follows xG per shot (0.124) in expectation. Over 96 matches it carries a standard error of about 0.007, and across the five larger runs it sat 0.004 above xG on average.

## After merge

Every generated match changes with the engine. A seed played on the new defaults is a different match from the same seed played before. The showcase matches in `data/matches` and the recorded broadcasts in `broadcasts/` were made on the old engine, so they must be regenerated after this branch merges, and any number quoted from them must be measured again.
