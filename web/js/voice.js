// Commentary voice: the line on air, spoken in the viewer's language, one line at a time.
// It stays silent until the viewer switches it on and the page has had a gesture (a click or a key),
// and a line that waited too long behind another is skipped: the voice never runs behind the pictures.

// A tenth of a second of silence, played inside the gesture so the browser lets this element speak later.
function silentWav(samples = 800, rate = 8000) {
  const b = new Uint8Array(44 + samples);
  const v = new DataView(b.buffer);
  const tag = (o, t) => { for (let i = 0; i < t.length; i++) b[o + i] = t.charCodeAt(i); };
  tag(0, "RIFF"); v.setUint32(4, 36 + samples, true); tag(8, "WAVE"); tag(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true);
  tag(36, "data"); v.setUint32(40, samples, true); b.fill(128, 44);
  let s = "";
  for (const x of b) s += String.fromCharCode(x);
  return `data:audio/wav;base64,${btoa(s)}`;
}
const SILENCE = silentWav();

export class Commentary {
  constructor({ audio = () => new Audio(), now = () => performance.now(), maxWaitMs = 3500 } = {}) {
    this.make = audio;
    this.now = now;
    this.maxWaitMs = maxWaitMs;     // a line still waiting after this long is stale
    this.on = false;
    this.lang = "en";
    this.armed = false;             // true once a gesture on the page has unlocked sound
    this.el = null;
    this.current = null;
    this.queue = [];
    this.log = [];                  // what played and what was skipped
    this.onchange = null;
  }

  // Call from inside a click or key handler: the gesture is what lets the browser play sound.
  arm() {
    if (this.armed) return;
    this.armed = true;
    this.el = this.make();
    this.el.preload = "auto";
    this.el.onended = () => this.next();
    this.el.onerror = () => { if (this.current) { this.note("error", this.current); this.next(); } };
    this.el.src = SILENCE;
    this.el.play()?.catch?.(() => {});
  }

  set(on) {
    this.on = on;
    if (!on) this.stop();
  }

  setLang(lang) { this.lang = lang; }

  // A line has aired on this viewer's screen. Returns true when its clip was queued.
  air(cue) {
    if (!this.on || !this.armed || cue.kind !== "line") return false;
    const url = cue.payload?.audio?.[this.lang];
    if (!url) return false;
    const item = { id: cue.id, url, at: this.now(), ttl: (cue.ttl_s || 8) * 1000, priority: cue.priority || 0 };
    if (item.priority >= 3) {
      // A big moment goes next: the smaller lines still waiting would only talk over it.
      for (const it of this.queue) this.note("superseded", it);
      this.queue = [];
    }
    this.queue.push(item);
    if (!this.current) this.next();
    return true;
  }

  next() {
    this.current = null;
    while (this.queue.length) {
      const it = this.queue.shift();
      if (this.now() - it.at > Math.min(this.maxWaitMs, it.ttl)) { this.note("stale", it); continue; }
      this.current = it;
      this.el.src = it.url;
      this.note("play", it);
      this.el.play()?.catch?.((e) => {
        if (this.current !== it || e?.name === "AbortError") return;     // a newer clip took the element
        this.note("blocked", it);
        this.next();
      });
      return;
    }
  }

  // Seeking, switching off, or changing source: whatever was speaking or waiting no longer fits the screen.
  stop() {
    this.queue = [];
    this.current = null;
    if (this.el) { this.el.pause(); this.el.removeAttribute("src"); this.el.load?.(); }
  }

  pause() { if (this.el && this.current) this.el.pause(); }

  resume() { if (this.el && this.current && this.on) this.el.play()?.catch?.(() => {}); }

  note(what, it) {
    this.log.push({ what, id: it?.id, t: Math.round(this.now()) });
    if (this.log.length > 200) this.log.shift();
    this.onchange?.(what, it);
  }
}
