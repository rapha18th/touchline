// The Crew room: how every crew line reached air. One card per crew moment: the handoffs with ms per role,
// every Verifier verdict (refusals with reasons, the repair, any fallback), the MCP calls, what aired, and
// the cited event ids, which open the event's facts straight from the match MCP server.

const ROLE = {
  producer: "Producer", analyst: "Analyst", narrator: "Narrator", recap: "Recap writer", verifier: "Verifier",
  fallback: "Fallback", localiser: "Localiser", director: "Director",
};
const KIND = {
  goal: "Goal", big_chance: "Big chance", big_save: "Big save", hard_pass: "Hard pass", milestone: "Milestone",
  momentum: "Momentum swing", red_card: "Red card", half_time: "Half time", full_time: "Full time",
};
const PERSONA = { analyst: "Analyst", casual: "Casual fan", studio: "Studio" };
const OUTCOME = { first: "First draft", repaired: "Repaired", fallback: "Fallback" };
const PIDS = ["recipient", "on", "off", "past", "won_by"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const secs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
const label = (k) => KIND[k] || String(k || "moment").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/** A failed typed claim, said plainly: what was claimed, what the record holds, and why it failed. */
function reason(p) {
  const m = /^claim failed: (\{.*\})$/.exec(p);
  if (!m) return p;
  try {
    const c = JSON.parse(m[1]);
    const what = [c.kind, c.team, c.player, c.stat || c.field, c.id != null ? `#${c.id}` : null].filter(Boolean).join(" ");
    const said = c.value !== undefined ? ` claimed ${c.value}` : c.home !== undefined ? ` claimed ${c.home}-${c.away}` : "";
    const actual = c.actual !== undefined && c.actual !== null ? `, record says ${typeof c.actual === "object" ? JSON.stringify(c.actual) : c.actual}` : "";
    return `claim failed: ${what}${said}${actual}${c.why ? ` (${c.why})` : ""}`;
  } catch { return p; }
}

let rpc = 0;
/** One stateless JSON-RPC POST to the web service's /mcp: the same read-only tools the crew uses. */
export async function mcpCall(name, args) {
  const t0 = performance.now();
  const r = await fetch("/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpc, method: "tools/call", params: { name, arguments: args } }),
  });
  if (!r.ok) throw new Error(`MCP ${r.status}`);
  const msg = await r.json();
  if (msg.error) throw new Error(msg.error.message);
  if (msg.result.isError) throw new Error(msg.result.content?.[0]?.text || "tool error");
  const sc = msg.result.structuredContent;
  const out = sc ? (sc.result ?? sc) : JSON.parse(msg.result.content[0].text);
  return { out, ms: performance.now() - t0 };
}

/** Group crew cues into moments: every cue one crew run produced carries the same crew record. */
function moments(list) {
  const by = new Map();
  for (const c of list) {
    if (c.author !== "crew" || !c.crew?.trace) continue;
    const tr = c.crew.trace;
    const key = c.crew.job || `${c.match_s}|${tr.length}|${tr[0]?.ms}`;
    let m = by.get(key);
    if (!m) {
      m = { key, match_s: c.match_s, crew: c.crew, cues: [], sources: new Set(), kind: null };
      by.set(key, m);
    }
    m.cues.push(c);
    (c.sources || []).forEach((s) => m.sources.add(s));
    m.kind ||= c.payload?.moment || (c.kind === "recap" ? (c.match_s > 2700 ? "full_time" : "half_time") : null);
  }
  for (const m of by.values()) {
    m.kind ||= (m.crew.job || "").split("-").slice(2, -1).join("-") || "insight";
    const tr = m.crew.trace;
    m.total = tr.reduce((a, t) => a + (t.ms || 0), 0);
    m.outcome = tr.some((t) => t.role === "fallback") ? "fallback" : m.crew.attempts ? "repaired" : "first";
    m.claims = (m.crew.verdicts || []).reduce((a, v) => a + (v.claims_checked || 0), 0);
    m.mcp = tr.flatMap((t) => (t.mcp || []).map((x) => ({ ...x, role: t.role })));
    m.mcpCount = tr.reduce((a, t) => a + (t.mcp ? t.mcp.length : t.mcp_calls || 0), 0);
  }
  return [...by.values()].sort((a, b) => b.match_s - a.match_s);
}

export class CrewRoom {
  constructor(root, M, now = () => 0) {
    this.root = root;
    this.M = M;
    this.now = now;
    this.list = [];
    this.errors = [];
    this.count = -1;
    this.open = new Set();
    this.follow = true;           // the newest moment opens itself until the viewer picks one
    this.facts = new Map();
    root.innerHTML = `
      <header class="cr-top">
        <div><h2 class="cr-title">Crew room</h2><p class="cr-sub">How each line reached air: who did what, what the Verifier refused, which facts it rests on.</p></div>
        <button class="cr-x" aria-label="Close the crew room">✕</button>
      </header>
      <div class="cr-tally" aria-live="polite"></div>
      <div class="cr-legend">${["producer", "analyst", "narrator", "verifier", "localiser", "director"]
        .map((r) => `<span><i class="r-${r}"></i>${ROLE[r]}</span>`).join("")}</div>
      <section class="cr-ask">
        <h3>Ask the record</h3>
        <form><input name="q" maxlength="200" autocomplete="off" placeholder="Who scored? Who has run furthest?" aria-label="Ask about the match so far"><button>Ask</button></form>
        <div class="cr-answer" aria-live="polite"></div>
      </section>
      <ol class="cr-list"></ol>
      <p class="cr-empty">The crew's first moment appears here as soon as it airs.</p>`;
    this.$list = root.querySelector(".cr-list");
    this.$tally = root.querySelector(".cr-tally");
    this.$empty = root.querySelector(".cr-empty");
    root.querySelector(".cr-x").onclick = () => this.toggle(false);
    this.$answer = root.querySelector(".cr-answer");
    root.querySelector(".cr-ask form").onsubmit = (e) => { e.preventDefault(); this.ask(e.target.q.value.trim(), e.target); };
    this.$answer.addEventListener("click", async (e) => {
      const b = e.target.closest(".cr-cite");
      if (!b) return;
      const box = this.$answer.querySelector(".cr-fact"), was = b.getAttribute("aria-expanded") === "true";
      this.$answer.querySelectorAll(".cr-cite").forEach((x) => x.setAttribute("aria-expanded", "false"));
      box.hidden = was;
      if (was) return;
      b.setAttribute("aria-expanded", "true");
      box.innerHTML = await this.fact(Number(b.dataset.id));
    });
    this.$list.addEventListener("click", (e) => this.click(e));
  }

  get visible() { return !document.body.classList.contains("crew-closed"); }

  toggle(on = !this.visible) {
    document.body.classList.toggle("crew-closed", !on);
    if (on && innerWidth <= 820) document.body.classList.add("rail-closed");     // one drawer at a time on a phone
    document.querySelector("#toggle-crew")?.setAttribute("aria-pressed", String(on));
    if (on) this.render();
  }

  /** Recorded mode: the moments whose cues have aired by now. */
  sync(cues, upTo) {
    if (upTo === this.count) return;
    this.count = upTo;
    this.list = cues.slice(0, upTo).filter((c) => c.crew);
    this.dirty = true;
    if (this.visible) this.render();
  }

  /** Live mode: cues pushed by the server, verdicts and all. */
  push(cue) {
    if (!cue.crew) return;
    this.list.push(cue);
    this.count = -1;
    this.dirty = true;
    if (this.visible) this.render();
  }

  /** Live mode: a crew run that failed outright (the model could not be reached, say). Nothing aired for it. */
  failed(msg) {
    this.errors.push({ key: `err-${msg.kind}-${msg.match_s}`, match_s: msg.match_s ?? 0, kind: msg.kind, error: msg.error });
    this.dirty = true;
    if (this.visible) this.render();
  }

  reset() { this.list = []; this.errors = []; this.count = -1; this.dirty = true; if (this.visible) this.render(); }

  render() {
    if (!this.dirty && this.$list.childElementCount) return;
    this.dirty = false;
    const ms = moments(this.list);
    this.ms = new Map(ms.map((m) => [m.key, m]));
    if (this.follow && ms.length) this.open = new Set([ms[0].key]);
    const n = (o) => ms.filter((m) => m.outcome === o).length;
    const mcp = ms.reduce((a, m) => a + m.mcpCount, 0), claims = ms.reduce((a, m) => a + m.claims, 0);
    this.$tally.innerHTML = ms.length ? [
      [ms.length, "moments"], [n("first"), "first draft"], [n("repaired"), "repaired"], [n("fallback"), "fallback"],
      [claims, "claims checked"], [mcp, "MCP calls"],
    ].map(([v, k]) => `<div><b class="num">${v}</b><span>${k}</span></div>`).join("") : "";
    const rows = [...ms, ...this.errors].sort((a, b) => b.match_s - a.match_s);
    this.$empty.hidden = !!rows.length;
    this.$list.innerHTML = rows.slice(0, 80).map((m) => (m.error ? this.errorCard(m) : this.card(m))).join("");
  }

  errorCard(e) {
    return `<li class="cr-moment"><div class="cr-head"><span class="cr-clock num">${this.M.clock(e.match_s)}</span><span class="cr-kind">${esc(label(e.kind))}</span>
      <span class="cr-out fallback">Crew error</span></div><p class="cr-note fallback cr-err">${esc(e.error)}. The run stopped before the Verifier, so nothing aired for this moment.</p></li>`;
  }

  card(m) {
    const M = this.M, tr = m.crew.trace, open = this.open.has(m.key);
    const bar = tr.filter((t) => t.ms > 0).map((t) =>
      `<i class="r-${t.role}${t.role === "verifier" && t.ok === false ? " refused" : ""}" style="flex-grow:${Math.max(t.ms, m.total / 120)}" title="${ROLE[t.role] || t.role} ${secs(t.ms)}"></i>`).join("");
    const head = `<button class="cr-head" aria-expanded="${open}" data-key="${esc(m.key)}">
        <span class="cr-clock num">${M.clock(m.match_s)}</span><span class="cr-kind">${esc(label(m.kind))}</span>
        <span class="cr-out ${m.outcome}">${OUTCOME[m.outcome]}</span><span class="cr-ms num">${secs(m.total)}</span></button>
      <div class="cr-bar" aria-hidden="true">${bar}</div>`;
    if (!open) return `<li class="cr-moment">${head}</li>`;
    return `<li class="cr-moment open">${head}<div class="cr-body">
      <h3>Handoffs</h3><ol class="cr-steps">${this.steps(m)}</ol>
      ${this.verdicts(m)}
      ${this.mcp(m)}
      <h3>On air</h3>${this.aired(m)}
      <h3>Cited events</h3>${this.cites(m)}
    </div></li>`;
  }

  /** Ask the record: an agent answers over MCP, and the answer shows only if the Verifier passes it. */
  async ask(question, form) {
    if (question.length < 3) return;
    const at = this.now(), btn = form.querySelector("button");
    btn.disabled = true;
    this.$answer.className = "cr-answer";
    this.$answer.innerHTML = `<p class="cr-k">The record agent is looking it up, as of ${this.M.clock(at)}</p>`;
    try {
      const r = await fetch(`/api/ask/${this.M.meta.match_id}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, match_s: at }),
      });
      const res = await r.json();
      if (!r.ok) throw new Error(typeof res.detail === "string" ? res.detail : `the server said ${r.status}`);
      const tools = res.trace.flatMap((t) => (t.mcp || []).map((c) => c.tool));
      const counts = Object.entries(tools.reduce((a, t) => ({ ...a, [t]: (a[t] || 0) + 1 }), {}))
        .map(([t, n]) => `<code>${esc(t)}</code>${n > 1 ? ` ×${n}` : ""}`).join(" ");
      const last = res.verdicts.at(-1), tries = res.verdicts.length;
      if (res.ok) {
        this.$answer.innerHTML = `<p>${esc(res.answer)}</p>
          <p class="cr-via"><span class="cr-pill ok">Verified</span> ${last.claims_checked} claims checked${tries > 1 ? ", after one repair" : ""} · as of ${this.M.clock(at)}</p>
          ${counts ? `<p class="cr-via">MCP ${counts}</p>` : ""}
          ${res.sources.length ? `<div class="cr-cites">${res.sources.map((id) => `<button class="cr-cite" data-id="${id}" aria-expanded="false">#${id}</button>`).join("")}</div><div class="cr-fact" hidden></div>` : ""}`;
      } else {
        this.$answer.className = "cr-answer refused";
        this.$answer.innerHTML = `<p>The Verifier did not pass an answer, so none is shown.</p>
          <ul class="cr-probs">${last.problems.map((p) => `<li>${esc(reason(p))}</li>`).join("")}</ul>
          ${counts ? `<p class="cr-via">MCP ${counts}</p>` : ""}`;
      }
    } catch (err) {
      this.$answer.className = "cr-answer refused";
      this.$answer.innerHTML = `<p>No answer: ${esc(err.message)}.</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  steps(m) {
    const verdicts = m.crew.verdicts || [];
    let v = 0, drafts = 0;
    return m.crew.trace.map((t) => {
      let what = "";
      if (t.role === "producer") what = t.mcp ? t.mcp.map((c) => `${c.tool}${c.ids ? ` #${c.ids.join(" #")}` : ""}`).join(", ") : `${t.mcp_calls ?? 2} MCP calls to the record`;
      else if (t.role === "analyst") what = "reads the moment, picks the pattern";
      else if (t.role === "narrator" || t.role === "recap") what = (drafts++ ? "repair: a new draft from the reasons" : "first draft, one line per persona");
      else if (t.role === "verifier") {
        const vd = verdicts[v++] || {};
        what = t.ok ? `passed, ${vd.claims_checked ?? "?"} claims checked` : `refused, ${t.problems} problem${t.problems === 1 ? "" : "s"}`;
      } else if (t.role === "fallback") what = "template line from verified facts only";
      else if (t.role === "localiser") what = `${t.langs} language${t.langs === 1 ? "" : "s"}`;
      else if (t.role === "director") what = `${t.cues} cue${t.cues === 1 ? "" : "s"} to air`;
      const bad = t.role === "verifier" && t.ok === false;
      return `<li class="${bad ? "refused" : ""}"><i class="r-${t.role}"></i><b>${ROLE[t.role] || esc(t.role)}</b><span>${esc(what)}</span><em class="num">${secs(t.ms)}</em></li>`;
    }).join("");
  }

  verdicts(m) {
    const vs = m.crew.verdicts || [];
    if (!vs.length) return "";
    const rows = vs.map((v, i) => {
      const head = `<div class="cr-vhead"><span class="cr-pill ${v.ok ? "ok" : "no"}">${v.ok ? "Passed" : "Refused"}</span>Check ${i + 1} · ${v.claims_checked} claims</div>`;
      const probs = (v.problems || []).map((p) => {
        const [who, ...rest] = String(p).split(": ");
        const persona = PERSONA[who];
        return `<li>${persona ? `<span class="cr-who">${persona}</span>${esc(reason(rest.join(": ")))}` : esc(reason(p))}</li>`;
      }).join("");
      const drafts = (v.drafts || []).map((d) => `<li><span class="cr-who">${PERSONA[d.persona] || esc(d.persona)}</span><s>${esc(d.text)}</s></li>`).join("");
      return `<div class="cr-verdict ${v.ok ? "ok" : "no"}">${head}${probs ? `<ul class="cr-probs">${probs}</ul>` : ""}${drafts ? `<p class="cr-k">Refused draft</p><ul class="cr-drafts">${drafts}</ul>` : ""}</div>`;
    }).join("");
    const end = m.outcome === "fallback" ? `<p class="cr-note fallback">Two refusals. The crew aired a template line built only from verified facts.</p>`
      : m.outcome === "repaired" ? `<p class="cr-note repaired">The Narrator rewrote the line from the Verifier's reasons, and the rewrite passed.</p>` : "";
    return `<h3>Verifier</h3>${rows}${end}`;
  }

  mcp(m) {
    if (!m.mcp.length) return "";
    return `<h3>MCP calls</h3><ul class="cr-mcp">${m.mcp.map((c) => `<li><code>${esc(c.tool)}</code><span>${esc(ROLE[c.role] || c.role)}${
      c.ids ? ` · ids ${c.ids.join(", ")}` : ""}${c.claims ? ` · ${c.claims} claims` : ""}${c.match_s != null ? ` · as of ${this.M.clock(c.match_s)}` : ""}</span><em class="num">${secs(c.ms)}</em></li>`).join("")}</ul>`;
  }

  aired(m) {
    return `<ul class="cr-aired">${m.cues.map((c) => {
      const p = c.payload || {};
      if (c.kind === "insight") return `<li><span class="cr-who">Insight</span><b>${esc(p.headline)}</b> ${esc(p.why)}</li>`;
      if (c.kind === "recap") return `<li><span class="cr-who">Recap</span><b>${esc(p.title)}</b> ${esc(p.story)}</li>`;
      return `<li><span class="cr-who">${PERSONA[p.persona] || esc(p.persona)}</span>${esc(p.text)}${p.fallback ? ' <span class="cr-pill no">template</span>' : ""}</li>`;
    }).join("")}</ul>`;
  }

  cites(m) {
    const ids = [...m.sources].sort((a, b) => a - b);
    if (!ids.length) return `<p class="cr-k">No event ids cited.</p>`;
    // A fact the viewer opened stays open while new moments arrive and the list redraws.
    const pick = this.cite?.key === m.key ? this.cite.id : null, cached = pick != null && this.facts.get(pick);
    return `<div class="cr-cites">${ids.map((id) => `<button class="cr-cite" data-id="${id}" aria-expanded="${id === pick && !!cached}">#${id}</button>`).join("")}</div>
      <div class="cr-fact"${cached ? "" : " hidden"}>${cached || ""}</div>`;
  }

  async click(e) {
    const head = e.target.closest(".cr-head");
    if (head) {
      this.follow = false;
      const k = head.dataset.key;
      this.open.has(k) ? this.open.delete(k) : this.open.add(k);
      this.dirty = true;
      this.render();
      return;
    }
    const cite = e.target.closest(".cr-cite");
    if (!cite) return;
    this.follow = false;
    const box = cite.closest(".cr-body").querySelector(".cr-fact");
    const was = cite.getAttribute("aria-expanded") === "true";
    cite.parentElement.querySelectorAll(".cr-cite").forEach((b) => b.setAttribute("aria-expanded", "false"));
    const key = cite.closest(".cr-moment").querySelector(".cr-head").dataset.key, id = Number(cite.dataset.id);
    if (was) { box.hidden = true; this.cite = null; return; }
    this.cite = { key, id };
    cite.setAttribute("aria-expanded", "true");
    box.hidden = false;
    box.innerHTML = `<p class="cr-k">Asking the match record for event #${cite.dataset.id}</p>`;
    box.innerHTML = await this.fact(id);
  }

  async fact(id) {
    if (this.facts.has(id)) return this.facts.get(id);
    let e, via;
    try {
      const { out, ms } = await mcpCall("get_events", { match_id: this.M.meta.match_id, ids: [id] });
      e = out[0];
      via = `MCP <code>get_events</code> · ${secs(ms)}`;
    } catch {
      e = this.M.events.find((x) => x.id === id);
      via = "the event log loaded with the match";
    }
    if (!e) return `<p class="cr-k">No event #${id} in the record.</p>`;
    const M = this.M, q = e.q || {};
    const who = e.player_name || (e.player && M.name(e.player));
    const team = e.team_name || (e.team && M.meta[e.team]?.name);
    const rows = [["Clock", e.clock || M.clock(e.period === 1 ? e.t : 2700 + e.t)], ["Event", `${e.type}${e.outcome ? ` · ${e.outcome}` : ""}`],
      who && ["Player", `${who}${team ? ` · ${team}` : ""}`], e.score && ["Score", Array.isArray(e.score) ? e.score.join("-") : `${e.score.home ?? ""}-${e.score.away ?? ""}`]];
    for (const [k, v] of Object.entries(q)) {
      if (v == null || typeof v === "object" || k.endsWith("_name") || k.startsWith("target_")) continue;
      const val = PIDS.includes(k) ? q[`${k}_name`] || M.name(v) || v
        : typeof v === "number" ? Math.round(v * 100) / 100 : v === true ? "yes" : v === false ? "no" : String(v).replace(/_/g, " ");
      rows.push([k.replace(/_/g, " "), val]);
    }
    const html = `<dl>${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl><p class="cr-via">Event #${id}, from ${via}</p>`;
    this.facts.set(id, html);
    return html;
  }
}
