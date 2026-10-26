// The graphics layer: scorebug, momentum, lower thirds, cards and tags, driven by cues.
// Shared by the fan app and the broadcast overlay. Every element is rebuilt from data; nothing
// here knows a player's name until a cue or the match meta supplies it.

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (v) => `${Math.round(v * 100)}%`;
// A momentum cue describes the last few minutes as of its match time; past this age it is stale and hides.
const MOMENTUM_HOLD_S = 90;

export class Graphics {
  constructor(root, meta, opts = {}) {
    this.root = root;
    this.meta = meta;
    this.opts = opts;               // { persona, lang, player, team, metric, showSources }
    this.root.innerHTML = `
      <div class="scorebug"><div class="chip" style="background:${meta.home.kit.primary}"></div>
        <div class="team">${esc(meta.home.short)}</div><div class="score num">0-0</div>
        <div class="team">${esc(meta.away.short)}</div><div class="chip" style="background:${meta.away.kit.primary}"></div>
        <div class="clock num">0:00</div></div>
      <div class="momentum" hidden><div class="label"><span>Chaos</span><span>Control</span></div>
        <div class="state">Balanced</div><div class="meter"><i style="left:50%"></i></div><div class="why"></div></div>
      <div class="cards"></div>
      <div class="lower"></div>`;
    this.$ = (s) => this.root.querySelector(s);
    this.timers = new Set();
  }

  set(opts) { Object.assign(this.opts, opts); }

  clear() {
    this.$(".cards").innerHTML = "";
    this.$(".lower").innerHTML = "";
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
  }

  clock(text, score) {
    this.$(".clock").textContent = text;
    if (score) this.$(".score").textContent = `${score[0]}-${score[1]}`;
  }

  later(el, ms) {
    const t = setTimeout(() => { el.remove(); this.timers.delete(t); }, ms);
    this.timers.add(t);
  }

  // Does this cue belong on this viewer's screen?
  wants(cue) {
    if (cue.verified === false) return false;      // nothing the Verifier did not check goes on screen
    const o = this.opts, a = cue.audience || {};
    const personas = a.personas || ["all"];
    if (cue.kind === "player_tag") {
      if (o.player) return a.player === o.player;
      if (o.persona === "casual") return a.metric === "speed";
      return true;
    }
    if (cue.kind === "stat") return o.persona !== "casual";
    if (cue.kind === "pass_card") return o.persona === "analyst" || (o.player && a.player === o.player) || o.metric === "pass_difficulty";
    if (cue.kind === "insight") return o.persona !== "casual";
    if (personas.includes("all")) return true;
    return personas.includes(o.persona);
  }

  text(cue) {
    const tr = cue.payload.translations || {};
    return (this.opts.lang && this.opts.lang !== "en" && tr[this.opts.lang]) || cue.payload.text;
  }

  show(cue, wallScale = 1) {
    if (!this.wants(cue)) return;
    const ttl = (cue.ttl_s || 6) * 1000 * wallScale;
    const p = cue.payload;
    switch (cue.kind) {
      case "scorebug": {
        this.$(".score").textContent = `${p.home}-${p.away}`;
        const sb = this.$(".scorebug");
        sb.classList.add("flash");
        setTimeout(() => sb.classList.remove("flash"), 2500);
        break;
      }
      case "momentum": this.momentum(p, cue.match_s); break;
      case "line": this.line(cue, ttl); break;
      case "shot_card": this.card("shot", cue, ttl); break;
      case "pass_card": this.card("pass", cue, ttl); break;
      case "insight": this.card("insight", cue, ttl); break;
      case "recap": this.card("recap", cue, ttl); break;
      case "milestone": if (this.opts.persona !== "studio") this.card("milestone", cue, ttl); break;
      case "stat": this.card("stat", cue, ttl); break;
      default: break;
    }
  }

  // Hide the momentum panel once its cue is older than the hold, or at once with no cue to show.
  age(T, none = false) {
    if (none || (this.momAt != null && T - this.momAt > MOMENTUM_HOLD_S)) {
      this.$(".momentum").hidden = true;
      this.momAt = null;
    }
  }

  momentum(p, at = null) {
    const el = this.$(".momentum");
    el.hidden = false;
    this.momAt = at;
    const label = p.label.startsWith("control:") ? `${this.club(p.label.split(":")[1])} in control` : p.label === "chaos" ? "Chaos" : "Balanced";
    el.querySelector(".state").textContent = label;
    el.querySelector(".state").style.color = p.label === "chaos" ? "var(--chaos)" : p.label.startsWith("control") ? "var(--control)" : "var(--chalk)";
    el.querySelector(".meter i").style.left = `${Math.max(2, Math.min(98, p.index))}%`;
    el.querySelector(".why").textContent = (p.reasons || []).slice(0, 2).join(" · ");
  }

  club(side) { return side === "home" ? this.meta.home.name : side === "away" ? this.meta.away.name : ""; }
  short(side) { return side === "home" ? this.meta.home.short : this.meta.away.short; }
  kit(side) { return side === "home" ? this.meta.home.kit.primary : this.meta.away.kit.primary; }

  line(cue, ttl) {
    const lower = this.$(".lower");
    const persona = cue.payload.persona || "studio";
    const bar = document.createElement("div");
    bar.className = `bar ${persona}${cue.payload.instant ? " instant" : ""}`;
    if (cue.audience?.team) bar.style.borderLeftColor = this.kit(cue.audience.team);
    const src = (cue.sources || []).length && this.opts.showSources !== false
      ? `<span class="src"><b>verified</b> · events ${cue.sources.slice(0, 4).join(", ")}${cue.payload.fallback ? " · template" : ""}${cue.crew ? ` · crew ${Math.round(cue.crew.trace.reduce((a, t) => a + t.ms, 0) / 100) / 10}s` : ""}</span>` : "";
    bar.innerHTML = `${esc(this.text(cue))}${src}`;
    while (lower.children.length >= 2) lower.firstChild.remove();
    lower.appendChild(bar);
    this.later(bar, ttl);
  }

  card(kind, cue, ttl) {
    const p = cue.payload, side = cue.audience?.team;
    const el = document.createElement("div");
    el.className = `card ${kind}`;
    if (side && kind !== "insight") el.style.borderLeft = `3px solid ${this.kit(side)}`;
    const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;
    if (kind === "shot") {
      const out = { goal: "Goal", saved: "Saved", blocked: "Blocked", off_target: "Off target" }[p.outcome] || p.outcome;
      el.innerHTML = `<div class="k">Shot · ${esc(this.short(side))}</div><div class="h">${esc(p.player)} · ${out}</div>
        ${this.opts.persona === "casual" ? row("Chance of scoring", pct(p.xg)) : row("xG", p.xg.toFixed(2))}
        ${row("Distance", `${p.distance_m} m`)}${row("Shot speed", `${Math.round(p.shot_speed_kmh)} km/h`)}
        ${this.opts.persona === "casual" ? "" : row("Team xG", p.team_xg.toFixed(2))}
        <div class="gauge"><i style="width:${Math.min(100, p.xg * 100)}%"></i></div>`;
    } else if (kind === "pass") {
      const reasons = (p.why_hard || []).map((r) => ({ lane_race: "a defender could reach the lane", length_10m: "length", length_sq: "length", final_third: "final third", box: "into the box", high: "in the air", log_receiver_space: "tight on the receiver", log_lane_gap: "a narrow lane", lane_opp: "bodies in the lane", forward_10m: "progression", log_passer_space: "pressure on the passer" }[r] || r));
      el.innerHTML = `<div class="k">Pass · difficulty ${Math.round(p.difficulty * 100)}</div><div class="h">${esc(p.player)} to ${esc(p.recipient || "")}</div>
        ${row("Distance", `${p.distance_m} m`)}${row("Ball speed", `${Math.round(p.ball_speed_kmh || 0)} km/h`)}
        ${row("Accuracy tonight", `${p.accuracy_pct}% of ${p.passes}`)}
        <div class="gauge"><i style="width:${Math.round(p.difficulty * 100)}%"></i></div>
        <div class="why">Hard because: ${esc([...new Set(reasons)].join(", "))}</div>`;
    } else if (kind === "insight") {
      el.innerHTML = `<div class="k">Why it matters · ${esc(p.pattern)}</div><div class="h">${esc(p.headline)}</div><div class="why">${esc(p.why)}</div>`;
    } else if (kind === "recap") {
      const story = (this.opts.lang !== "en" && p.translations?.[this.opts.lang]) || p.story;
      el.innerHTML = `<div class="k">${(cue.period ?? (cue.match_s > 2800 ? 2 : 1)) === 1 ? "Half time" : "Full time"} · verified recap</div><div class="h">${esc(p.title)}</div>
        <div class="why" style="font-size:14px;color:var(--chalk)">${esc(story)}</div>
        <div class="why">${(p.key_moments || []).map(esc).join(" · ")}</div>`;
    } else if (kind === "milestone") {
      el.innerHTML = `<div class="k">${esc((p.type || "").replace("_", " "))}</div><div class="h">${esc(p.text)}</div>`;
    } else if (kind === "stat") {
      el.innerHTML = `<div class="k">Match stats</div>
        ${row("Possession", `${p.possession_pct.home}% · ${p.possession_pct.away}%`)}
        ${row("xG", `${p.xg.home.toFixed(2)} · ${p.xg.away.toFixed(2)}`)}
        ${row("Shots", `${p.shots.home} · ${p.shots.away}`)}
        ${row("Pass completion", `${p.pass_completion_pct.home}% · ${p.pass_completion_pct.away}%`)}
        ${row("Passes per defensive action", `${p.ppda.home ?? "n/a"} · ${p.ppda.away ?? "n/a"}`)}`;
    }
    const cards = this.$(".cards");
    while (cards.children.length >= 3) cards.firstChild.remove();
    cards.appendChild(el);
    this.later(el, ttl);
  }
}
