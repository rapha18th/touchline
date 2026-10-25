# Vision: passes and shots read from the broadcast feed

The challenge names a hard problem: "running a model over an incoming video feed to generate the pass and
speed metrics automatically". Touchline does it on its own broadcast. We film generated matches through the
fan app's 3D pitch, hand the pipeline the frames and the two kit colours, and it writes every pass and shot
with its distance and speed. The engine's events, its tracking and the renderer's camera matrix grade it.

Every number below comes from `reports/vision/summary.json`. The commands that rebuild it are at the end.

## What we found

On 20 test clips from two matches held apart until the code was set:

- **It finds 80% of passes and is right about 93% of the passes it calls** (40 of 50 found, 40 of 43
  right). With perfect detection the same event code finds 84%, so most of the shortfall is in the event
  logic, and seeing costs 4 points.
- **It finds 8 of 10 shots.** It also calls 5 other kicks shots: two passes played along the grass toward goal
  from the edge of the box, a third such pass timed late, and two real shots timed more than 4 frames late.
  The oracle calls about as many (5 of 14).
- **Mean errors: pass length 1.5 m (median 0.75 m), ball speed 3.3 km/h (median 1.2), shot speed 1.9 km/h**,
  over 32, 40 and 8 measured kicks.
- **The pitch is found from its lines alone to 0.08 px** (median clip; worst clip 0.72 px), about a
  centimetre on the grass. The camera position comes out to 6 cm (median).
- **The 3D flight model earns its place.** Measure every flight as if it rolled and the ball speed error grows
  from 3.3 to 17.3 km/h and the shot speed error from 1.9 to 21.9 km/h.
- **Timing hidden kicks earns its place.** When the ball leaves a player's feet out of sight, running the
  flight back to where it sat lifts pass recall from 72% to 80%.
- **White kits are the hard case.** A white kit beside the white ball is the main source of lost tracks. In
  the test and held-out sets the match with a white side finds the fewest passes (75% and 77%, against 85% and
  89%). In the dev set, where the detector was tuned, the white-kit match scored higher.

## Results

Three clip sets of 20, each from two matches, 1280x720 at 25 fps. Clips are cut around shots (8.5 s) and the
busiest open-play passing (8 s), and each has its own fixed camera.

- **Dev** (TL-000007, TL-000012): every threshold was tuned here.
- **Held out** (TL-000010, TL-000013): filmed and scored once with the code frozen. That first look found a
  failed calibration, white kits read as the ball, one-metre "kicks" at players' feet and kicks counted in the
  first frames of a clip, before the truth window opens. We fixed all four, so these clips are now a second
  dev set. The first look stays in the report as `heldout_first`.
- **Test** (TL-000009, TL-000011): filmed after the last change (commit c6f743a) and scored once. This is
  the clean number.

| | Test | Test oracle | Held out | Dev |
|---|---|---|---|---|
| Passes found (recall) | 40/50 = 80% (67 to 89) | 42/50 = 84% (72 to 92) | 44/53 = 83% (71 to 91) | 41/46 = 89% (77 to 95) |
| Passes right (precision) | 40/43 = 93% (81 to 98) | 42/44 = 96% (85 to 99) | 44/49 = 90% (78 to 96) | 41/43 = 95% (84 to 99) |
| Shots found (recall) | 8/10 = 80% (49 to 94) | 9/10 = 90% (60 to 98) | 7/10 = 70% (40 to 89) | 9/13 = 69% (42 to 87) |
| Shots right (precision) | 8/13 = 62% (36 to 82) | 9/14 = 64% (39 to 84) | 7/10 = 70% (40 to 89) | 9/9 = 100% (70 to 100) |
| Pass length error | 1.49 m (median 0.75, n 32) | 0.72 m (median 0.39, n 34) | 2.04 m (median 0.82, n 36) | 1.05 m (median 0.81, n 35) |
| Ball speed error | 3.27 km/h (median 1.15, n 40) | 1.50 km/h (median 0.60, n 42) | 6.45 km/h (median 2.30, n 44) | 2.09 km/h (median 1.50, n 41) |
| Shot speed error | 1.86 km/h (median 1.45, n 8) | 1.08 km/h (median 0.90, n 9) | 2.80 km/h (median 2.00, n 7) | 5.21 km/h (median 1.30, n 9) |
| Kick time error | 1.18 frames | 0.44 frames | 0.98 frames | 0.94 frames |

| Seeing | Test | Held out | Dev |
|---|---|---|---|
| Pitch reprojection, median of clip means | 0.08 px, 1.1 cm | 0.08 px, 1.0 cm | 0.09 px, 1.0 cm |
| Pitch reprojection, worst clip | 0.72 px, 6.6 cm | 0.50 px, 7.9 cm | 1.07 px, 11.2 cm |
| Camera position error, median | 0.06 m | 0.04 m | 0.06 m |
| Frames with the ball seen | 66% | 61% | 68% |
| Ball pixel error, median | 0.84 px | 0.84 px | 0.86 px |
| Rolling ball on the grass, median error | 16 cm | 16 cm | 14 cm |
| Players found | 93% | 90% | 86% |
| Player position error, median | 20 cm | 20 cm | 19 cm |
| Player team right | 98% | 100% | 99% |

The ball is out of sight in a third of the frames. Most of those frames are the ball at a player's feet,
inside or behind the player model.

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
| Full pipeline | heldout | 83% | 90% | 70% | 2.04 m | 6.45 km/h | 2.80 km/h |
| Every flight on the grass | heldout | 83% | 88% | 60% | 2.65 m | 12.22 km/h | 5.08 km/h |
| Kick timed where first seen moving | heldout | 79% | 89% | 80% | 2.56 m | 6.22 km/h | 3.14 km/h |
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
- `03_shot.png`: a shot off target: 92.9 km/h measured, 93.9 km/h true.
- `04_failure.png`: an incomplete pass among black and white kits that the feed missed.

The pass and the shot are the clearest of their kind in the test set. The failure is the first miss in it.

## Honest limits

- **The feed is clean.** It is a rendered pitch with crisp lines, even light, one fixed camera per clip and a
  ball that is the only small white sphere on the grass. Broadcast video pans and zooms all the time, blurs a
  fast ball, lays graphics over play and cuts to close-ups and replays. The fixed-camera background, the
  once-per-clip calibration and the white-blob detector each rest on the clean feed. These numbers measure the
  method on an easy feed; real video will score lower.
- **The engine's ball is simple, and the 3D model matches its shape.** Engine flights run straight over the
  ground with a parabola in height, the same family the aerial fit assumes. A real ball swerves, drags and
  bounces. Ground passes follow one easing curve, which a quadratic follows closely.
- **Engine shortcuts shape the hard cases.** At the kick the ball jumps from the dribble back to the kicker's
  feet. A blocked shot flies three or four frames and then jumps to the blocker: the feed missed all three
  blocked shots in the dev set (the oracle found two). The held-out and test sets hold zero. A carrier
  standing still holds the ball inside the player model, out of sight.
  Headers leave from the grass in the engine; a real header leaves at head height, which the aerial anchor
  would need to know.
- **Incomplete passes are the most missed.** Of the ten test passes the feed missed, five were incomplete
  (short flights into a crowd), three started out of sight and were timed late, and two were called shots.
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
- **It runs offline.** Calibration takes about 40 s a clip and detection about 50 ms a frame on one core.
  A live feed needs the calibration once per camera position and a faster detector.

## Reproduce

```
# 1. Film the clips (the service starts on port 8091 with the crew off and stops after).
set PYTHONPATH=vision
set TOUCHLINE_DATA=<repo>/data/matches
python -m touchline_vision.capture --matches TL-000007 TL-000012 --shots 5 --passes 5
python -m touchline_vision.capture --matches TL-000010 TL-000013 --shots 5 --passes 5 --out %TEMP%/touchline_vision/heldout/clips
python -m touchline_vision.capture --matches TL-000009 TL-000011 --shots 5 --passes 5 --out %TEMP%/touchline_vision/test/clips

# 2. Camera, detections, tracks and kicks for each set.
python -m touchline_vision.pipeline
python -m touchline_vision.pipeline --clips %TEMP%/touchline_vision/heldout/clips --work %TEMP%/touchline_vision/heldout/run
python -m touchline_vision.pipeline --clips %TEMP%/touchline_vision/test/clips --work %TEMP%/touchline_vision/test/run

# 3. Grade against the engine and draw the gallery.
python -m touchline_vision.score --name dev
python -m touchline_vision.score --name heldout --clips %TEMP%/touchline_vision/heldout/clips --work %TEMP%/touchline_vision/heldout/run
python -m touchline_vision.score --name test --clips %TEMP%/touchline_vision/test/clips --work %TEMP%/touchline_vision/test/run
python -m touchline_vision.gallery --report reports/vision/test --clips %TEMP%/touchline_vision/test/clips --work %TEMP%/touchline_vision/test/run

# Unit tests for the geometry, the fits and the scoring.
python -m pytest vision/tests
```

The package needs numpy and `opencv-python-headless==4.14.0.94`; capture also needs `playwright==1.63.0`
and Chrome. Calibration takes about 40 s a clip on one CPU core, detection about 50 ms a frame, the events
under a second. Per-clip detail sits beside each summary in `clips.json`.
