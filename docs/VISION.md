# Vision: passes and shots read from Touchline's rendered feed

> These numbers were measured on matches from the engine before its realism calibration (main before c8e4d28). The calibration changed every generated match, so the same match ids now hold different matches. To reproduce exactly, step 0 below generates the six matches with the engine at commit 735c630, and every other step runs from this repository.

The challenge asks for pass and speed metrics generated automatically from an incoming video feed. Touchline
does it on its own rendered broadcast. We film generated matches through the fan app's 3D pitch, hand the
pipeline the frames and each side's shirt, second and keeper colours, and it writes every pass and shot with
its distance and speed. The engine's events, its tracking and the renderer's camera matrix grade it.

There is no trained model. The camera comes from the painted lines and the flights from least squares, so
there is nothing to label or license. Nothing in this pipeline runs on Azure.

Every number below comes from `reports/vision/summary.json`. The commands that rebuild it are at the end.

## What we found

On 20 test clips from two matches held apart until the code was set:

- **It finds 80% of passes and is right about 93% of the passes it calls** (40 of 50 found, 40 of 43
  right). With perfect detection the same event code finds 84%, so most of the shortfall is in the event
  logic, and seeing costs 4 points.
- **It finds 8 of the 10 shots it was shown.** Shot clips are cut around goals, saves and misses. Blocked
  shots are not sampled: they were 11 of the 59 shots in the two test matches, and the feed missed all three
  it met in dev. Over every shot, recall would be nearer 65% (80% of the 81% that are not blocked). It also
  calls 5 other kicks shots: two passes played along the grass toward goal from the edge of the box, a third
  such pass timed late, and two real shots timed more than 4 frames late. The oracle calls about as many (5
  of 14).
- **Mean errors: pass length 1.5 m (median 0.75 m), ball speed 3.3 km/h (median 1.2), shot speed 1.9 km/h**,
  over 32, 40 and 8 measured kicks. Pass length reads short: the mean signed error is -1.19 m (oracle -0.37 m).
- **The pitch is found from its lines alone to 0.08 px** (median clip; worst clip 0.72 px), about a
  centimetre on the grass. The camera position comes out to 6 cm (median).
- **The 3D flight model earns its place.** Measure every flight as if it rolled and the ball speed error grows
  from 3.3 to 17.3 km/h and the shot speed error from 1.9 to 21.9 km/h. The engine flies the ball on exactly
  the curve the aerial fit assumes, so this gain is an upper bound for real footage.
- **The ball tracker sometimes holds the wrong object.** On test, 9.7% of the frames that report a ball sit
  more than 10 px from the true ball (0.6% on dev, 2.2% on held out). The ball is on the true ball, within
  10 px, in 61% of test frames.
- **Timing hidden kicks earns its place.** When the ball leaves a player's feet out of sight, running the
  flight back to where it sat lifts pass recall from 72% to 80%.
- **White kits are the hard case.** A white kit beside the white ball is the main source of lost tracks. In
  the test and held-out sets the match with a white side finds the fewest passes (75% and 77%, against 85% and
  89%). In the dev set, where the detector was tuned, the white-kit match scored higher.

## Results

Three clip sets of 20, each from two matches, 1280x720 at 25 fps. Clips are cut around shots (8.5 s) and the
busiest open-play passing (8 s), and each has its own fixed camera.

- **Dev** (TL-000007, TL-000012): every threshold was tuned here.
- **Held out** (TL-000010, TL-000013): filmed and scored once with the code frozen (43ddaf6). That first
  look found three pipeline faults and one grading fault: a failed calibration, white kits read as the ball,
  one-metre "kicks" at players' feet, and detections in the first frames of a clip, before the truth window
  opens (those are now dropped on both sides). We fixed all four, so these clips are now a second dev set.
  The first look stays in the report as `heldout_first` and in its own column below.
- **Test** (TL-000009, TL-000011): filmed after the last change (commit c6f743a) and scored once. This is
  the clean number. A 30-frame capture smoke test used TL-000009 on 24 Oct, before the freeze; no detection
  or scoring ran on it.

| | Test | Test oracle | Held out, after fixes (second dev set) | Held out, first look (43ddaf6) | Dev |
|---|---|---|---|---|---|
| Passes found (recall) | 40/50 = 80% (67 to 89) | 42/50 = 84% (72 to 92) | 44/53 = 83% (71 to 91) | 43/53 = 81% (69 to 89) | 41/46 = 89% (77 to 95) |
| Passes right (precision) | 40/43 = 93% (81 to 98) | 42/44 = 96% (85 to 99) | 44/49 = 90% (78 to 96) | 43/62 = 69% (57 to 79) | 41/43 = 95% (84 to 99) |
| Shots found (recall) | 8/10 = 80% (49 to 94) | 9/10 = 90% (60 to 98) | 7/10 = 70% (40 to 89) | 7/10 = 70% (40 to 89) | 9/13 = 69% (42 to 87) |
| Shots right (precision) | 8/13 = 62% (36 to 82) | 9/14 = 64% (39 to 84) | 7/10 = 70% (40 to 89) | 7/9 = 78% (45 to 94) | 9/9 = 100% (70 to 100) |
| Pass length error | 1.49 m (median 0.75, n 32) | 0.72 m (median 0.39, n 34) | 1.48 m (median 0.78, n 33) | 2.86 m (median 0.89, n 34) | 1.05 m (median 0.81, n 35) |
| Pass length bias (signed) | -1.19 m | -0.37 m | -1.20 m | -2.14 m | -0.37 m |
| Ball speed error | 3.27 km/h (median 1.15, n 40) | 1.50 km/h (median 0.60, n 42) | 6.45 km/h (median 2.30, n 44) | 8.42 km/h (median 2.30, n 43) | 2.09 km/h (median 1.50, n 41) |
| Shot speed error | 1.86 km/h (median 1.45, n 8) | 1.08 km/h (median 0.90, n 9) | 2.80 km/h (median 2.00, n 7) | | 5.21 km/h (median 1.30, n 9) |
| Kick time error | 1.18 frames | 0.44 frames | 0.98 frames | | 0.94 frames |
| Worst calibration | 0.72 px | | 0.50 px | 228 px | 1.07 px |

The first look's pass length is scored with the old flag for a pass landing in the clip (see Honest limits);
its run cache has since been overwritten, so it cannot be scored again.

Pass length reads short. The measured flight ends where the receiver's touch is first seen, while
`q.length_m` runs from the passer to the target. On test, 24 of 32 matched complete passes read short; the
worst are m000009-p09 (-8.1 m) and m000011-s03 (-7.0 m).

| Seeing (every frame of the set pooled) | Test | Held out | Dev |
|---|---|---|---|
| Pitch reprojection, median of clip means | 0.08 px, 1.1 cm | 0.08 px, 1.0 cm | 0.09 px, 1.0 cm |
| Pitch reprojection, worst clip | 0.72 px, 6.6 cm | 0.50 px, 7.9 cm | 1.07 px, 11.2 cm |
| Camera position error, median | 0.06 m | 0.04 m | 0.06 m |
| Frames with a ball reported | 67.3% | 65.3% | 68.4% |
| Reported ball on the true ball (within 10 px) | 90.3% | 97.8% | 99.4% |
| Frames with the ball seen on the true ball | 60.8% | 63.9% | 68.0% |
| Ball pixel error, median | 0.83 px | 0.85 px | 0.88 px |
| Ball pixel error, 95th percentile | 242 px | 1.2 px | 1.3 px |
| Rolling ball on the grass, median error | 15 cm | 18 cm | 15 cm |
| Unseen ball within 1 m of a player | 94% | 90% | 92% |
| Players found | 88% | 90% | 82% |
| Player position error, median | 18 cm | 19 cm | 20 cm |
| Player team right | 97.9% | 99.5% | 97.6% |

The ball is out of sight in about a third of the frames, and in 90% or more of those it is within a metre of
a player: at someone's feet, inside or behind the player model. The pooled 95th percentile shows the wrong
locks the median hides: on test, 8 of 20 clips have a clip 95th percentile between 116 and 659 px.
`tracks_clip_median` in the summary keeps the older median-of-clips figures.

Recall is the share of the engine's passes (or shots) the feed found and called right. Precision is the share
of the feed's passes (or shots) that match one. A detection matches a true kick within 4 frames (0.16 s).
Pass length is scored on complete passes whose ball lands inside the clip; the engine's `q.length_m` is the
distance from the passer to the target. Ball speed is the engine's `q.ball_speed_kmh`: the release speed of a
ground pass, and the mean speed over the ground of a lofted pass or cross. Shot speed is `q.shot_speed_kmh`,
the shot's mean speed over the ground. The intervals are 95% Wilson intervals.

The oracle runs the same event code on the true ball and players, projected through the true camera. It is
the ceiling the event logic allows with perfect detection and calibration. The gap from the feed to the
oracle is what seeing costs.

## What each part buys

Two switches in `touchline_vision.events.detect_events`, scored on the same detections.

| Ablation | Set | Passes found | Passes right | Shots found | Pass length | Ball speed | Shot speed |
|---|---|---|---|---|---|---|---|
| Full pipeline | test | 80% | 93% | 80% | 1.49 m | 3.27 km/h | 1.86 km/h |
| Every flight on the grass | test | 80% | 91% | 70% | 1.83 m | 17.28 km/h | 21.93 km/h |
| Kick timed where first seen moving | test | 72% | 86% | 80% | 1.84 m | 3.65 km/h | 1.86 km/h |
| Full pipeline | heldout | 83% | 90% | 70% | 1.48 m | 6.45 km/h | 2.80 km/h |
| Every flight on the grass | heldout | 83% | 88% | 60% | 2.14 m | 12.22 km/h | 5.08 km/h |
| Kick timed where first seen moving | heldout | 79% | 89% | 80% | 1.98 m | 6.22 km/h | 3.14 km/h |
| Full pipeline | dev | 89% | 95% | 69% | 1.05 m | 2.09 km/h | 5.21 km/h |
| Every flight on the grass | dev | 89% | 93% | 62% | 1.99 m | 18.42 km/h | 28.91 km/h |
| Kick timed where first seen moving | dev | 80% | 86% | 69% | 1.30 m | 2.39 km/h | 5.24 km/h |

Every flight on the grass reads a lofted ball by where it appears over the turf, so a cross or a shot reads
far too fast. Timing kicks where the ball is first seen moving starts many passes late, past the 4-frame match,
and measures them short.

## How it works

**Capture.** `web/capture.html` is the fan app's own pitch (`web/js/pitch.js`) with the graphics stripped. A
window hook steps it one frame at a time, so every frame is exact. Playwright drives it in headless Chrome
against the local service (crew off) and saves JPEG frames at quality 0.92 to `%TEMP%`. Each clip gets a fixed
camera on the near side, 40 to 56 m back from the touchline and 22 to 34 m up, framed on the play and widened
until the ball stays in shot. The camera sits on the near stand's gantry, so the capture page hides that
stand's tier. The renderer's camera matrix goes to `truth.json` for grading; the pipeline reads frames and
`clip.json` (size, rate, kit colours).

**The pitch from its lines.** The per-pixel median of 31 frames is the empty pitch, since the camera holds
still. A white top-hat on it finds the painted lines. A search over 30,000 broadcast placements scores each by
how much of the projected pitch model lands on line pixels and how many line pixels it explains. Nelder-Mead
then refines the best eight on a symmetric truncated chamfer, first with a 40 px reach and then 10 px. The
camera is parametrised as an orbit of its look point (distance, elevation, azimuth, image scale, roll): fixing
the image scale keeps the search out of the long valley where moving back and zooming in look alike. Basin
hopping restarts a poor fit, and ICP polishes the full eight-parameter homography. The focal length and the
whole camera follow from the homography (square pixels, centred principal point). The fit grades itself: a
chamfer cost above 6 means the wrong pitch, and the clip is searched again from fresh seeds.

**Ball and players.** Foreground is the difference from the empty pitch, less shadows (a shadow darkens all
three channels by one factor; a kit changes the hue). The ball is a small round white blob, scored on
roundness and on its size against the ball's expected size at that spot. White inside the upper body of a
player's blob counts against it. Players are upright blobs; each takes the nearest of the four kit colours
(two outfield, two keepers) in Lab, then the majority label of its track. Feet go to the pitch through the
homography. The ball's path is a shortest path through each frame's candidates: a candidate earns its score,
a skipped frame costs a little, motion is capped at 40 px a frame, and a jump anywhere costs more (the ball
is reset after a goal). A second pass removes zig-zags onto a white kit beside the ball.

**Kicks.** A kick starts where the ball's ground speed holds 10 m/s for five frames in a straight line (or 16
m/s for three, for a short shot). When the ball sat hidden at the kicker's feet, the early flight is run back
in time to where the ball was last seen at rest. The flight ends when a player takes the ball and it moves
with them, when it stops, or when its image path breaks. A ball that vanishes behind a player and comes back on
its line keeps its flight.

**Flights in 3D.** Each flight is fitted twice through the camera, both as linear least squares on the
projection equations. Rolling: the ball's centre at its radius above the grass, a quadratic path. In the air:
a straight path over the ground and a parabola in height, on the grass at the kick. A single camera leaves the
scale of an aerial flight open; the kick on the grass fixes it. The model with clearly smaller pixel residual
wins. Release speed comes from a quadratic through the first eight ground positions; aerial speed is the
fitted speed over the ground.

**Pass or shot.** A shot heads at a goal from within 40 m, crosses the goal line within 11 m of the centre, and
is fast (14 m/s, or 9 m/s from inside 18 m for headers). It ends anywhere but with a teammate out in the field
or with the kicker's own keeper. A ball lofted above 4.5 m into the box is a cross.

## Gallery

Small frames in `reports/vision/gallery/`, drawn from the test set: the fitted pitch lines over a frame, the
clearest pass and the clearest shot with their measured and true numbers, and a kick the feed missed.

- `01_calibration.png`: the pitch model drawn through the estimated camera over the empty pitch.
- `02_pass.png`: a lofted pass rebuilt in the air: 37.9 m and 51.6 km/h measured, 38.2 m and 50.7 km/h true.
- `03_shot.png`: a shot off target: 92.9 km/h measured, 93.9 km/h true. It comes from m000009-s01, a clip
  the calibration check flagged as untrusted, though its camera was right to 0.08 px.
- `04_failure.png`: an incomplete pass among black and white kits that the feed missed.

The pass and the shot are the clearest of their kind in the test set, picked by hand. The failure is the
first miss in it.

## Honest limits

- **The feed is clean.** It is a rendered pitch with crisp lines, even light, one fixed camera per clip and a
  ball that is the only small white sphere on the grass. Players are uniform capsules, and each clip's camera
  is placed from the true ball path, so the ball never leaves the frame. Broadcast video pans and zooms all
  the time, blurs a fast ball, lays graphics over play and cuts to close-ups and replays. The fixed-camera
  background, the once-per-clip calibration and the white-blob detector each rest on the clean feed. These
  numbers measure the method on an easy feed; real video will score lower, and that drop is not measured.
- **The engine's ball is simple, and the 3D model matches its shape.** Engine flights run straight over the
  ground with a parabola in height, the same family the aerial fit assumes. A real ball swerves, drags and
  bounces. Ground passes follow one easing curve, which a quadratic follows closely.
- **Engine shortcuts shape the hard cases.** At the kick the ball jumps from the dribble back to the kicker's
  feet. A blocked shot flies three or four frames and then jumps to the blocker: the feed missed all three
  blocked shots in the dev set (the oracle found two). Shot clips are cut around goals, saves and misses, so
  the held-out and test sets hold no blocked shot by design, though about one shot in five in these matches
  was blocked (`match_shots` in the summary). A carrier standing still holds the ball inside the player
  model, out of sight.
- **The tracker can lock onto the wrong object.** With the ball hidden at a player's feet, it can hold a
  static fragment of a painted line beside a standing player as the ball. Wrong locks rose from 0.6% of
  reported frames on dev to 9.7% on test, and four test clips report the wrong object in 23% to 56% of their
  ball frames (m000009-p09 is the worst; `tracks.ball_wrong` in clips.json).
  Headers leave from the grass in the engine; a real header leaves at head height, which the aerial anchor
  would need to know.
- **Incomplete passes are the most missed.** Of the ten test passes the feed missed, five were incomplete
  (short flights into a crowd), two were timed late, one was never seen to start, and two were called shots.
- **Two known faults wait for the next clip set,** since the code is frozen for this test set. A kick timed
  back from where the ball sat can start inside the previous flight: in held-out m000013-s03 it placed a
  kick at frame 132, inside the previous pass, and the real saved shot at frame 150 was scored as a miss and
  a false shot. And the rule that lifts a
  rising shot into the air never re-applies the cross rule, so a ball lofted to between 4.5 and 6 m stays a
  shot.
- **A lofted ball cut short reads far too fast.** Five of the six lofted test passes read within 1 km/h. The
  sixth lost the end of its flight and reads 81.8 km/h for 43.4 (m000011-s03). When the cut leaves the rolling
  model the better fit, a cross reads 154 km/h for 44 (held out, m000013-s00).
- **The calibration check is cautious.** Two test clips failed the chamfer check after three searches and
  were flagged untrusted, yet both cameras were right (0.40 px and 0.08 px). The flag is in each clip's
  `calib_fit`; the scores include those clips.
- **The camera search assumes a broadcast placement:** a main camera on the near side, above the pitch, with
  square pixels and the principal point at the centre. The capture page hides the near stand's tier, since a
  gantry camera looks over it.
- **The samples are small.** Ten shots a set gives intervals 40 points wide. The dev set was tuned on. The
  first held-out look found four faults, which we then fixed, so that set is a second dev set; its first
  scores are in `heldout_first` (passes 81% found, 69% right). Only the test set is clean.
- **A grading fault was fixed after the test.** The flag for a pass that lands inside the clip compared a
  time from the period start with an absolute frame, so every second-half pass counted as landing. Fixed, it
  moves only the held-out pass length (2.04 m to 1.48 m, oracle 1.41 m to 0.88 m); dev and test come out the
  same. The fault is still in `heldout_first`.
- **It runs offline.** Calibration takes about 40 s a clip on one core (`calib_fit.seconds` in each clip) and
  detection about 50 ms a frame (timed by hand; not stored in the reports). A live feed needs the
  calibration once per camera position and a faster detector.

## Reproduce

Git Bash, from the repository root. The clips were filmed on matches from the engine before its realism
calibration, so step 0 generates them with that engine, from a worktree at 735c630, into a folder of their own.

```
# 0. The six matches, from the engine at 735c630 (data/ is not in git).
git worktree add ../tl-v1 735c630
for s in 7 9 10 11 12 13; do PYTHONPATH=../tl-v1/engine .venv/Scripts/python -m touchline_engine generate --seed $s --out data/matches_v1; done

# 1. Film the clips (the service starts on a free local port with the crew off and stops after).
export PYTHONPATH=vision TOUCHLINE_DATA=$PWD/data/matches_v1 V=$TEMP/touchline_vision
.venv/Scripts/python -m touchline_vision.capture --matches TL-000007 TL-000012 --shots 5 --passes 5
.venv/Scripts/python -m touchline_vision.capture --matches TL-000010 TL-000013 --shots 5 --passes 5 --out $V/heldout/clips
.venv/Scripts/python -m touchline_vision.capture --matches TL-000009 TL-000011 --shots 5 --passes 5 --out $V/test/clips

# 2. Camera, detections, tracks and kicks for each set.
.venv/Scripts/python -m touchline_vision.pipeline
.venv/Scripts/python -m touchline_vision.pipeline --clips $V/heldout/clips --work $V/heldout/run
.venv/Scripts/python -m touchline_vision.pipeline --clips $V/test/clips --work $V/test/run

# 3. Grade against the engine and draw the gallery.
.venv/Scripts/python -m touchline_vision.score --name dev
.venv/Scripts/python -m touchline_vision.score --name heldout --clips $V/heldout/clips --work $V/heldout/run
.venv/Scripts/python -m touchline_vision.score --name test --clips $V/test/clips --work $V/test/run
.venv/Scripts/python -m touchline_vision.gallery --report reports/vision/test --clips $V/test/clips --work $V/test/run

# The first held-out look: the code at 43ddaf6, then score it under its own name.
git checkout 43ddaf6 -- vision && .venv/Scripts/python -m touchline_vision.pipeline --clips $V/heldout/clips --work $V/heldout/run_first --force
.venv/Scripts/python -m touchline_vision.score --name heldout_first --clips $V/heldout/clips --work $V/heldout/run_first
git checkout HEAD -- vision

# Unit tests for the geometry, the fits and the scoring.
.venv/Scripts/python -m pytest vision/tests
```

The dev and held-out cameras in the published run were cached from code before the freeze (their
`calib.json` has no `tries` or `trusted` field). A fresh run recalibrates with the final code and can move the
dev and held-out numbers a little. The test cameras come from the final code. Scoring exits non-zero and
names any clip the pipeline did not finish (`failed_clips`), so a crashed clip never drops out of the
denominators unseen.

The package needs numpy and `opencv-python-headless==4.14.0.94`; capture also needs `playwright==1.63.0`
and Chrome. Calibration takes about 40 s a clip on one CPU core, detection about 50 ms a frame, the events
under a second. Per-clip detail sits beside each summary in `clips.json`.
