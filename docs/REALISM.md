# Realism calibration

The match engine is tuned by an agent. The agent never edits the engine. It proposes dial vectors, a measurement plays them, and a gate decides. This page records how the defaults in `engine/touchline_engine/sim.py` were reached, what the gate refused, and what is still open.

## The target

Sixteen metrics, each with a band from published league aggregates (`engine/touchline_engine/realism.py`). Event-level bands span the 2023-24 and 2024-25 Premier League seasons (760 matches, totals from football-data.co.uk). Passing and physical bands are literature ranges. No real match rows enter the engine.

## The dials

Sixteen league-level dials live at the top of `sim.py`. Each has a bounded range in `engine/tools/calibrate.py`, and the agent may only propose values inside it. Any dial can be overridden for one run:

```
TOUCHLINE_DIALS='{"SHOT_VALUE": 0.7}' python -m touchline_engine realism --n 4
```

An unknown dial name raises, so a typo never runs silently on defaults.

Three structural changes came with the dials, because no existing dial could reach ball in play:

- `RESTART_PACE` scales the dead time before throw-ins, goal kicks, corners and free kicks.
- `SUB_STOP_S` holds a restart while a substitution window happens. Substitutions now happen while the ball is dead, at the first moment of a stoppage.
- `LATE_STALL` adds dead time when the restarting side leads after the hour.

Card thresholds became independent dials: `YELLOW_SHARE` is the chance a foul by an unbooked player is booked, `RED_SECOND` the chance a booked player's foul is a second yellow, `RED_STRAIGHT` the chance of a straight red.

## The loop

`engine/tools/calibrate.py` runs rounds. Each round:

1. **Propose.** gpt-5-mini on Microsoft Foundry (keyless, `AzureCliCredential`, Agent Framework `Agent` with a structured `Plan` response) reads the realism table, each metric's distance and margin, the dial table with bounds and effects, and every earlier trial with its verdict. It returns a diagnosis and several dial vectors, each with a hypothesis.
2. **Measure.** Each vector plays the same 24 seeds on a Kaggle CPU kernel (`engine/tools/kaggle_eval.py`). Each kernel is one self-contained script: the engine source rides inside as a base64 zip, nothing mounts and nothing downloads. Four kernels run at once, one vector each, four cores each.
3. **Gate.** A proposal is accepted only if more metrics land in band, or as many land while the summed normalised distance to the bands falls, and no metric that was in band leaves it. With all sixteen in band, it is accepted only if the safety margin (standard errors inside the band, capped at 2 per metric) rises. The best accepted proposal of a round becomes the incumbent.

Every proposal, trial, refusal and promotion goes to `reports/calibration/trace.jsonl`. `reports/calibration/summary.json` holds the incumbent and the path to it. The trace is the state: run the command again and it resumes from the last incumbent.

```
python engine/tools/calibrate.py --rounds 6 --k 4 --per-kernel 1 --seeds 1-24
```
