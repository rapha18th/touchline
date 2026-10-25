// The fan app's commentary voice, with a fake audio element and a fake clock.
//   node --test web/tests/voice.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { airTime } from "../js/match.js";
import { Commentary } from "../js/voice.js";

class FakeAudio {
  constructor() { this.src = ""; this.plays = []; this.paused = true; }
  play() { this.plays.push(this.src); this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  removeAttribute(k) { if (k === "src") this.src = ""; }
  load() {}
  end() { this.paused = true; this.onended?.(); }
}

function rig() {
  const clock = { t: 0 };
  let el;
  const c = new Commentary({ audio: () => (el = new FakeAudio()), now: () => clock.t });
  return { c, clock, el: () => el };
}

const line = (id, audio, extra = {}) => ({ id, kind: "line", ttl_s: 8, priority: 1, payload: { audio }, ...extra });
const A = { en: "/api/audio/a-en.mp3", es: "/api/audio/a-es.mp3", pt: "/api/audio/a-pt.mp3" };
const B = { en: "/api/audio/b-en.mp3", pt: "/api/audio/b-pt.mp3" };
const C = { en: "/api/audio/c-en.mp3" };

test("silent until switched on, and never before a gesture", () => {
  const { c, el } = rig();
  assert.equal(c.air(line("l1", A)), false);         // off, not armed
  c.set(true);
  assert.equal(c.air(line("l1", A)), false);         // on, but no gesture yet: nothing is created or played
  assert.equal(el(), undefined);
  c.arm();                                           // the click on "On"
  assert.ok(el().plays[0].startsWith("data:audio/wav;base64,"), "the gesture plays a moment of silence");
  assert.equal(c.air(line("l1", A)), true);
  assert.equal(el().src, A.en);
});

test("plays the chosen language and skips a line without a clip in it", () => {
  const { c, el } = rig();
  c.arm(); c.set(true); c.setLang("pt");
  c.air(line("l1", A));
  assert.equal(el().src, A.pt);
  el().end();
  assert.equal(c.air(line("l3", C)), false);         // no Portuguese clip: the line airs without a voice
  assert.equal(c.current, null);
  c.setLang("fr");
  assert.equal(c.air(line("l1", A)), false);         // no French clip on this line either
});

test("one line at a time, in order, and a line that waited too long is skipped", () => {
  const { c, el, clock } = rig();
  c.arm(); c.set(true);
  c.air(line("l1", A));
  clock.t = 500; c.air(line("l2", B));
  clock.t = 900; c.air(line("l3", C));
  assert.equal(el().src, A.en);                      // l2 and l3 wait their turn
  clock.t = 3000; el().end();                        // l2 waited 2.5 s: still fresh
  assert.equal(el().src, B.en);
  clock.t = 6000; el().end();                        // l3 waited 5.1 s: stale, skipped
  assert.equal(c.current, null);
  assert.deepEqual(c.log.filter((x) => x.what !== "play").map((x) => [x.what, x.id]), [["stale", "l3"]]);
  assert.deepEqual(c.log.filter((x) => x.what === "play").map((x) => x.id), ["l1", "l2"]);
});

test("a big moment clears the smaller lines still waiting", () => {
  const { c, el, clock } = rig();
  c.arm(); c.set(true);
  c.air(line("l1", A));
  c.air(line("l2", B));
  c.air(line("goal", C, { priority: 3 }));
  clock.t = 1000; el().end();
  assert.equal(el().src, C.en);
  assert.ok(c.log.some((x) => x.what === "superseded" && x.id === "l2"));
});

test("seeking or switching off stops the voice and drops the queue; pause and resume hold the clip", () => {
  const { c, el } = rig();
  c.arm(); c.set(true);
  c.air(line("l1", A)); c.air(line("l2", B));
  c.pause();
  assert.equal(el().paused, true);
  c.resume();
  assert.equal(el().paused, false);
  c.stop();
  assert.equal(c.current, null); assert.equal(c.queue.length, 0); assert.equal(el().src, "");
  c.air(line("l3", C));
  c.set(false);
  assert.equal(c.current, null);
  assert.equal(c.air(line("l4", A)), false);
});

test("a clip the browser refuses moves on to the next line", async () => {
  const { c, el } = rig();
  c.arm(); c.set(true);
  const audio = el();
  let refuse = true;
  const play = audio.play.bind(audio);
  audio.play = () => (refuse ? (refuse = false, Promise.reject(new Error("NotAllowedError"))) : play());
  c.air(line("l1", A)); c.air(line("l2", B));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(audio.src, B.en);
  assert.ok(c.log.some((x) => x.what === "blocked" && x.id === "l1"));
});

test("a big moment cuts a smaller line that is already speaking", () => {
  const { c, el } = rig();
  c.arm(); c.set(true);
  c.air(line("l1", A));
  assert.equal(el().src, A.en);
  c.air(line("goal", C, { priority: 3 }));
  assert.equal(el().src, C.en, "the goal speaks now, not after l1 ends");
  assert.deepEqual(c.log.filter((x) => x.what === "cut").map((x) => x.id), ["l1"]);
  c.air(line("goal2", B, { priority: 3 }));
  assert.equal(el().src, C.en, "a big moment does not cut another big moment");
});

test("at a faster replay a waiting line goes stale sooner, by its ttl over the speed", () => {
  const { c, el, clock } = rig();
  c.arm(); c.set(true); c.setSpeed(4);
  assert.equal(c.staleAfter({ ttl: 8000 }), 2000);
  c.setSpeed(1);
  assert.equal(c.staleAfter({ ttl: 8000 }), 3500, "never longer than maxWaitMs");
  c.setSpeed(4);
  c.air(line("l1", A));
  c.air(line("l2", B));
  clock.t = 2500; el().end();                        // l2 waited 2.5 s at 4x: 10 match seconds, past its 8 s ttl
  assert.equal(c.current, null);
  assert.ok(c.log.some((x) => x.what === "stale" && x.id === "l2"));
});

// The recorded broadcasts, replayed at match pace with each clip as long as its file, in every language.
// Each line airs when the app airs it: airTime from match.js, the crew's measured latency after its moment.
const ROOT = fileURLToPath(new URL("../../", import.meta.url));

for (const match of ["TL-000007", "TL-000011"]) {
  test(`${match} plays one line at a time in every language, from clips on disk`, (t) => {
    const cues = readFileSync(join(ROOT, "broadcasts", `${match}.cues.jsonl`), "utf8").split("\n").filter(Boolean)
      .map((l) => JSON.parse(l)).filter((q) => q.kind === "line" && q.payload?.audio)
      .sort((a, b) => airTime(a) - airTime(b));
    const file = (url) => join(ROOT, "broadcasts", "audio", url.slice("/api/audio/".length));
    for (const lang of ["en", "es", "fr", "pt"]) {
      for (const persona of ["analyst", "casual"]) {
        const { c, el, clock } = rig();
        c.arm(); c.set(true); c.setLang(lang);
        let seen = el().plays.length, endsAt = Infinity;      // the gesture's silence is already in plays
        const played = [];
        const tick = () => {
          if (el().plays.length > seen) {
            seen = el().plays.length;
            const url = el().plays[seen - 1];
            played.push(url);
            endsAt = clock.t + (statSync(file(url)).size * 8 / 32000) * 1000;   // 32 kbit/s mp3
          }
        };
        const mine = cues.filter((q) => q.payload.persona === persona);
        for (const q of mine) {
          while (endsAt <= airTime(q) * 1000) { clock.t = endsAt; endsAt = Infinity; el().end(); tick(); }
          clock.t = airTime(q) * 1000;
          assert.equal(c.air(q), true, `${q.id} has a ${lang} clip`);
          tick();
        }
        while (endsAt < Infinity) { clock.t = endsAt; endsAt = Infinity; el().end(); tick(); }
        const skipped = c.log.filter((x) => x.what === "stale" || x.what === "superseded").length;
        const cut = c.log.filter((x) => x.what === "cut").length;      // a cut line started: it is in played
        assert.ok(cut <= played.length);
        assert.equal(played.length + skipped, mine.length, `${lang} ${persona}: every line plays or is skipped`);
        assert.ok(played.length >= mine.length / 2, `${lang} ${persona}: ${played.length} of ${mine.length} played`);
        assert.ok(played.every((u) => mine.some((q) => q.payload.audio[lang] === u)));
        t.diagnostic(`${match} ${lang} ${persona}: ${mine.length} aired, ${played.length} spoken (${cut} cut by a big moment), ${skipped} skipped`);
      }
    }
  });
}
