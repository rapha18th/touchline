// Write the match: a story in, a Match Director, candidate matches, a validator, and a link into the match.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const EXAMPLES = [
  "An underdog parks the bus and nicks it late",
  "A wild end-to-end draw with a red card",
  "The home side fall behind and come back to win",
  "A tight, cagey game settled by a single goal",
];

const STATE = {
  queued: "Waiting for the engine",
  directing: "The Match Director is reading your story",
  playing: "The engine is playing candidates",
  done: "Written",
  failed: "The match could not be written",
};

export function mountWrite(root) {
  root.innerHTML = `
    <h2 id="write-h">Write the match</h2>
    <p class="write-lede">Describe the match you want. The Match Director turns your words into engine settings and story beats, the engine plays candidate matches, and a validator checks every beat against the event log.</p>
    <form class="write-form" autocomplete="off">
      <label class="sr" for="story">Your match, in a sentence</label>
      <textarea id="story" rows="2" maxlength="300" placeholder="${esc(EXAMPLES[0])}"></textarea>
      <button type="submit" class="write-go">Write it</button>
    </form>
    <div class="write-examples" role="group" aria-label="Example stories">
      ${EXAMPLES.map((e) => `<button type="button" data-story="${esc(e)}">${esc(e)}</button>`).join("")}
    </div>
    <div class="write-status" aria-live="polite" hidden></div>`;
  const form = root.querySelector("form");
  const story = root.querySelector("#story");
  const go = root.querySelector(".write-go");
  const status = root.querySelector(".write-status");
  root.querySelector(".write-examples").onclick = (e) => {
    const b = e.target.closest("button[data-story]");
    if (b) { story.value = b.dataset.story; story.focus(); }
  };
  story.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } };

  let timer = null;
  const show = (html) => { status.hidden = false; status.innerHTML = html; };
  const busy = (on) => { go.disabled = on; story.disabled = on; go.textContent = on ? "Writing" : "Write it"; };

  const forget = () => { try { sessionStorage.removeItem("tl-write-job"); } catch { /* private mode */ } };

  // A job runs for minutes: a dropped request or a 5xx is retried with backoff; a 404 means the job is gone.
  async function poll(id, misses = 0) {
    let job;
    try {
      const r = await fetch(`/api/scenario/${encodeURIComponent(id)}`);
      if (r.status === 404) {
        forget();
        busy(false);
        show(`<p class="write-err">This server no longer has the job. Write the match again.</p>`);
        return;
      }
      if (!r.ok) throw new Error(`status ${r.status}`);
      job = await r.json();
    } catch (err) {
      if (misses < 5) { timer = setTimeout(() => poll(id, misses + 1), 1500 * 2 ** misses); return; }
      busy(false);
      show(`<p class="write-err">Lost touch with the server (${esc(err.message)}). The match may still be written; reload to check.</p>`);
      return;
    }
    show(render(job));
    if (job.state === "done" || job.state === "failed") {
      busy(false);
      forget();
      return;
    }
    timer = setTimeout(() => poll(id), 1500);
  }

  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = story.value.trim();
    if (text.length < 8) { show(`<p class="write-err">Say a little more: a sentence about the match you want.</p>`); return; }
    clearTimeout(timer);
    busy(true);
    show(`<p class="write-state">Sending your story</p>`);
    try {
      const r = await fetch("/api/scenario", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ story: text }) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "the story was not accepted");
      try { sessionStorage.setItem("tl-write-job", body.id); } catch { /* private mode */ }
      poll(body.id);
    } catch (err) {
      busy(false);
      show(`<p class="write-err">${esc(err.message)}</p>`);
    }
  };

  // A reload while a match is being written picks the job up again.
  let pending = null;
  try { pending = sessionStorage.getItem("tl-write-job"); } catch { /* private mode */ }
  if (pending) { busy(true); poll(pending); }
}

export function render(job) {
  const parts = [`<p class="write-state">${esc(STATE[job.state] || job.state)}${job.state === "queued" && job.queued_behind ? `, ${job.queued_behind} ahead` : ""}${job.elapsed_s != null ? ` <span class="num dim">${Math.round(job.elapsed_s)} s</span>` : ""}</p>`];
  if (job.headline) {
    parts.push(`<p class="write-headline">${esc(job.headline)}</p>`);
  }
  if (job.beats?.length && job.state !== "done") {
    parts.push(`<ul class="write-beats">${job.beats.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`);
  }
  const list = (ns) => ns.map((n) => `${esc(n.what)} (${esc(n.why)})`).join("; ");
  const gate = job.gate || { first: [], final: [] };
  if (job.repaired && gate.first.length) {
    parts.push(`<p class="write-gate">The gate refused ${list(gate.first)}. The Director rewrote its spec.</p>`);
  }
  if (gate.final.length) {
    parts.push(`<p class="write-gate">The gate set aside ${list(gate.final)}.</p>`);
  }
  if (job.state === "playing" || (job.played?.length && job.state !== "done")) {
    const rows = (job.played || []).map((c, i) => `<li><span class="num">#${i + 1}</span> ${c.pruned_at ? `stopped at ${esc(c.pruned_at)}: a beat could no longer hold` : c.complete ? `finished ${c.score?.[0]}-${c.score?.[1]}, ${c.met} of ${c.total} beats` : "did not finish"}</li>`);
    (job.playing || []).forEach((m) => rows.push(`<li class="live"><span class="num">#${rows.length + 1}</span> playing${m != null ? `, minute ${Math.floor(m)}` : ""}</li>`));
    if (rows.length) parts.push(`<ol class="write-cands">${rows.join("")}</ol>`);
    if (job.budget) {
      const pct = Math.min(100, Math.round((100 * (job.spent || 0)) / job.budget));
      parts.push(`<div class="write-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="Search budget used"><i style="width:${pct}%"></i></div>`);
    }
  }
  if (job.state === "done" && job.report) {
    const r = job.report;
    parts.push(`<ul class="write-report">${r.beats.map((b) => `<li class="${b.ok ? "ok" : "miss"}"><b>${b.ok ? "Met" : "Missed"}</b> ${esc(b.beat)}<span>${esc(b.detail)}</span></li>`).join("")}</ul>`);
    parts.push(`<p class="write-summary">${r.met} of ${r.total} beats met${r.met < r.total ? ". This is the closest of the matches the engine played." : "."}</p>`);
    const id = encodeURIComponent(job.match_id);
    parts.push(`<p class="write-links"><a class="write-watch" href="/?match=${id}">Watch it</a><a href="/?match=${id}&live=1">Go live</a><span class="num dim">${esc(job.match_id)}</span></p>`);
  }
  if (job.state === "failed") parts.push(`<p class="write-err">${esc(job.error || "")}</p>`);
  return parts.join("");
}
