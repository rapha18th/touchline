// Match data and the clock: tracking frames, events, substitutions and cue timelines.

export const HALF = 2700;

export async function loadMatch(id) {
  const [meta, events, buf] = await Promise.all([
    fetch(`/api/match/${id}`).then((r) => r.json()),
    fetch(`/api/match/${id}/events`).then((r) => r.json()),
    fetch(`/api/match/${id}/tracking`).then((r) => r.arrayBuffer()),
  ]);
  return new Match(meta, events, new Int16Array(buf));
}

export async function loadBroadcast(id) {
  const r = await fetch(`/api/broadcast/${id}`);
  return r.ok ? r.json() : [];
}

export class Match {
  constructor(meta, events, frames) {
    this.meta = meta;
    this.events = events;
    this.frames = frames;                         // (F, 24, 2) int16 cm, 12.5 Hz
    this.hz = meta.stream.hz;
    this.nFrames = meta.stream.frames;            // per period, at 12.5 Hz
    this.len = meta.periods.map((p) => p.length_s);
    this.players = {};
    for (const side of ["home", "away"]) for (const p of meta[side].players) this.players[p.id] = { ...p, team: side };
    // Slot -> player over time, from the starting XI and substitution events.
    this.slots = [];
    meta.home.starters.forEach((pid, i) => (this.slots[i] = [[0, pid]]));
    meta.away.starters.forEach((pid, i) => (this.slots[11 + i] = [[0, pid]]));
    for (const e of events) {
      e.ms = matchSeconds(e.period, e.t_end ?? e.t);
      if (e.type === "substitution") this.slots[e.q.slot].push([e.ms, e.q.on]);
    }
    this.goals = events.filter((e) => e.type === "goal");
    this.end = HALF + this.len[1];
  }

  playerAt(slot, ms) {
    let pid = this.slots[slot][0][1];
    for (const [t, p] of this.slots[slot]) if (t <= ms) pid = p;
    return pid;
  }

  name(pid) { return this.players[pid]?.name ?? ""; }
  short(pid) { return this.players[pid]?.short ?? ""; }

  // Fractional frame index for a match time, or null during the interval.
  frameAt(ms) {
    if (ms <= this.len[0]) return Math.max(0, ms * this.hz);
    if (ms < HALF) return null;
    return this.nFrames[0] + Math.min(ms - HALF, this.len[1]) * this.hz;
  }

  // Interpolated positions: 24 rows of [x, y] in metres; row 23 col 0 is ball height.
  sample(ms, out) {
    const f = this.frameAt(ms);
    if (f === null) return null;
    const total = this.nFrames[0] + this.nFrames[1];
    const i0 = Math.min(Math.floor(f), total - 1), i1 = Math.min(i0 + 1, total - 1), a = f - i0;
    const stride = 48, b0 = i0 * stride, b1 = i1 * stride, fr = this.frames;
    for (let k = 0; k < 48; k++) out[k] = (fr[b0 + k] * (1 - a) + fr[b1 + k] * a) / 100;
    return out;
  }

  clock(ms) {
    const period = ms < HALF ? 1 : 2;
    const t = period === 1 ? ms : ms - HALF;
    const base = period === 1 ? 0 : 45;
    const m = Math.floor(t / 60), s = Math.floor(t % 60);
    if (m >= 45) return `${base + 45}+${m - 44}'`;
    return `${base + m}:${String(s).padStart(2, "0")}`;
  }

  scoreAt(ms) {
    let s = [0, 0];
    for (const g of this.goals) if (g.ms <= ms) s = g.score;
    return s;
  }

  // A player's line from the event log, as of a match time.
  line(pid, ms) {
    const L = { passes: 0, complete: 0, shots: 0, goals: 0, xg: 0, tackles: 0, touches: 0, carries: 0 };
    for (const e of this.events) {
      if (e.ms > ms) break;
      if (e.player !== pid) continue;
      if (e.type === "pass") { L.passes++; if (e.outcome === "complete") L.complete++; }
      if (e.type === "shot") { L.shots++; L.xg += e.q.xg || 0; }
      if (e.type === "goal") L.goals++;
      if (e.type === "tackle" && e.outcome === "won") L.tackles++;
      if (["pass", "carry", "shot", "dribble"].includes(e.type)) L.touches++;
    }
    return L;
  }
}

export function matchSeconds(period, t) { return period === 1 ? t : HALF + t; }

// When a recorded cue airs: state cues at their moment; crew cues after the crew's measured latency.
export function airTime(cue) {
  if (cue.author === "crew" && cue.crew?.trace) return cue.match_s + Math.min(cue.crew.trace.reduce((a, t) => a + t.ms, 0) / 1000, 20);
  return cue.match_s;
}
