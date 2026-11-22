/* PayLens v2 — one page, no build step.

   Threads live in this browser, not on the server. v2's pipeline is stateless by
   design (a thread can be replayed exactly when debugging), so the caller holds
   the history and sends it with each question. Five threads are kept, matching
   the classic pipeline's limit.

   On submit the new question is scrolled to the top of the viewport and the
   answer arrives below it, so the previous turn leaves the screen. That is why
   .thread carries a tall tail in the stylesheet: without room below the last
   turn, there is nothing to scroll into. */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const MAX_CONVS = 5;
const KEY = "paylens2.convs";
const EXAMPLES = [
  "why did our overall success rate drop last week",
  "how much money did we lose to failures last week",
  "success rate by payment method last week",
  "which issuer bank has the worst success rate last week",
  "what should we do about retry failures",
  "show me chargebacks",
];

let convs = load();
let active = convs[0]?.id || null;
let me = null;

/* ── storage ────────────────────────────────────────────────────────────── */
function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; }
}
function save() {
  // Rows are trimmed before storing: a 5,000-row result would blow the quota,
  // and the table can be re-fetched by asking again. The answer is what has to
  // survive a reload.
  try {
    localStorage.setItem(KEY, JSON.stringify(convs.slice(0, MAX_CONVS)));
  } catch {
    convs = convs.slice(0, 2);
    try { localStorage.setItem(KEY, JSON.stringify(convs)); } catch { /* full */ }
  }
}
const conv = () => convs.find(c => c.id === active) || null;

function newConv(reason = "") {
  const c = { id: "c" + Date.now().toString(36), title: "", at: Date.now(),
              role: me?.role || "anonymous", turns: [] };
  convs.unshift(c);
  convs = convs.slice(0, MAX_CONVS);
  active = c.id;
  save(); renderHistory(); renderThread();
  if (reason) note(reason);
  $("#q").focus();
  return c;
}

/* ── theme ──────────────────────────────────────────────────────────────── */
try {
  const t = localStorage.getItem("paylens2.theme");
  if (t) document.documentElement.dataset.theme = t;
} catch { /* private mode */ }
$("#theme").onclick = () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const now = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = now;
  try { localStorage.setItem("paylens2.theme", now); } catch { /* private mode */ }
};

/* ── the narrow-screen drawer ───────────────────────────────────────────── */
const rail = $("#rail"), scrim = $("#scrim");
const openRail = () => { rail.classList.add("open"); scrim.hidden = false; };
const closeRail = () => { rail.classList.remove("open"); scrim.hidden = true; };
$("#railopen").onclick = openRail;
$("#railclose").onclick = closeRail;
scrim.onclick = closeRail;
addEventListener("keydown", e => { if (e.key === "Escape") closeRail(); });

/* ── health ─────────────────────────────────────────────────────────────── */
async function health() {
  const el = $("#health");
  try {
    const h = await (await fetch("/v2/health")).json();
    const bits = [];
    bits.push(h.views_built
      ? `<s title="v_attempts and v_settlements exist">views</s>`
      : `<s class="bad" title="run: python -m paylens2.schema.build">views missing</s>`);
    bits.push(h.llm_available
      ? `<s title="author ${esc(h.author_model)} · synth ${esc(h.synth_model)}">model</s>`
      : `<s class="bad" title="set LLM_API_KEY">no model</s>`);
    el.innerHTML = bits.join("");
  } catch {
    el.innerHTML = `<s class="bad">api unreachable</s>`;
  }
}

/* ── account ────────────────────────────────────────────────────────────────
   Sign-in is optional. Unauthenticated, /auth/me still reports the anonymous
   role, which matters because the role is what decides the row filter even
   when nobody has signed in. */
const acctbtn = $("#acctbtn"), acctpanel = $("#acctpanel");
acctbtn.onclick = () => {
  const open = acctpanel.hidden;
  acctpanel.hidden = !open;
  acctbtn.setAttribute("aria-expanded", String(open));
  if (open && $("#loginform").hidden === false) { demoUsers(); $("#u").focus(); }
};

async function whoami() {
  let d = null;
  try { const r = await fetch("/auth/me"); d = r.ok ? await r.json() : null; }
  catch { d = null; }

  const signed = !!(d && d.authenticated);
  me = signed ? d.user
              : { username: "anonymous", role: (d && d.anon_role) || "cxo",
                  display_name: "", claims: {} };
  const detail = (d && d.role_detail) || null;
  const name = signed ? (me.display_name || me.username) : "Not signed in";

  $("#acctname").textContent = name;
  $("#acctrole").textContent = signed
    ? (detail?.label || me.role)
    : `reading as ${(d && d.anon_role_label) || me.role}`;
  $("#avatar").textContent = signed
    ? name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase()
    : "·";
  acctbtn.dataset.signed = signed ? "1" : "0";

  const claims = Object.entries(me.claims || {}).map(([k, v]) => `${k}=${v}`);
  $("#acctbody").innerHTML = signed
    ? `<div class="acctstate">
         <b>${esc(me.display_name || me.username)}</b> — ${esc(detail?.label || me.role)}
         ${claims.length ? `<span class="scopetag">${esc(claims.join(" · "))}</span>` : ""}
         ${detail && detail.row_scope
           ? `<p class="railnote">Every query you run is filtered to your own rows,
              in SQL. Open the SQL tab of any answer to see the predicate the
              guard injected.</p>` : ""}
       </div>
       <button class="signout" type="button" id="logout">Sign out</button>`
    : `<p class="railnote" style="margin-top:2px">Browsing without an account.
       You are reading at the <b>${esc((d && d.anon_role_label) || me.role)}</b>
       scope.</p>`;

  $("#loginform").hidden = signed;
  const lo = $("#logout");
  if (lo) lo.onclick = async () => {
    await fetch("/auth/logout", { method: "POST" });
    await whoami();
    newConv("Signed out — new thread started, because nothing carries across a change of role.");
  };
  if (!signed) demoUsers();
}

async function demoUsers() {
  const box = $("#demousers");
  if (box.dataset.done) return;
  try {
    const d = await (await fetch("/auth/demo-users")).json();
    const users = (d && d.users) || [];
    if (!users.length || d.enabled === false) return;
    box.innerHTML = `<div class="demo">
      <p>Demo accounts — password <code>${esc(d.password || "paylens-demo")}</code></p>
      ${users.map(u => `<button type="button" data-u="${esc(u.username)}">
        ${esc(u.display_name || u.username)}<span>${esc(u.note || u.role)}</span>
      </button>`).join("")}</div>`;
    box.dataset.done = "1";
    box.onclick = (e) => {
      const b = e.target.closest("[data-u]"); if (!b) return;
      $("#u").value = b.dataset.u;
      $("#pw").value = d.password || "paylens-demo";
      $("#loginbtn").focus();
    };
  } catch { /* seeding off */ }
}

$("#loginform").onsubmit = async (e) => {
  e.preventDefault();
  $("#loginerr").hidden = true;
  $("#loginbtn").disabled = true;
  try {
    const r = await fetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#u").value, password: $("#pw").value }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "sign-in failed");
    $("#pw").value = "";
    await whoami();
    acctpanel.hidden = true;
    acctbtn.setAttribute("aria-expanded", "false");
    newConv("Signed in — new thread started, because nothing carries across a change of role.");
  } catch (err) {
    $("#loginerr").textContent = err.message;
    $("#loginerr").hidden = false;
  } finally { $("#loginbtn").disabled = false; }
};

/* ── history ────────────────────────────────────────────────────────────── */
$("#newchat").onclick = () => { newConv(); closeRail(); };

function dayOf(ts) {
  const d = new Date(ts), now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (ts >= midnight) return "Today";
  if (ts >= midnight - 864e5) return "Yesterday";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderHistory() {
  const el = $("#history");
  if (!convs.length) {
    el.innerHTML = `<p class="histnone">No conversations yet. Ask something and
      it will appear here.</p>`;
    titlebar();
    return;
  }
  let html = "", day = null;
  for (const c of convs) {
    const d = dayOf(c.at);
    if (d !== day) { html += `<p class="daygroup">${esc(d)}</p>`; day = d; }
    const when = new Date(c.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    html += `<button class="convitem${c.id === active ? " on" : ""}" type="button"
      data-c="${c.id}" title="${esc(c.title || "New chat")}">
      ${esc(c.title || "New chat")}
      <i>${c.turns.length} turn${c.turns.length === 1 ? "" : "s"} · ${esc(when)}${
        c.role && c.role !== "anonymous" ? " · " + esc(c.role) : ""}</i></button>`;
  }
  el.innerHTML = html;
  el.onclick = (e) => {
    const b = e.target.closest(".convitem"); if (!b) return;
    active = b.dataset.c;
    renderHistory(); renderThread(); closeRail();
  };
  titlebar();
}

function titlebar() {
  const c = conv();
  $("#threadtitle").textContent = c && c.title ? c.title : "New chat";
}

/* ── the context line under the composer ────────────────────────────────── */
function note(text) {
  $("#ctx").textContent = text;
  setTimeout(() => { if ($("#ctx").textContent === text) ctxNote(); }, 6000);
}
function ctxNote() {
  const c = conv();
  $("#ctx").textContent = c && c.turns.length
    ? `Follow-up — the previous ${c.turns.length} turn${c.turns.length === 1 ? "" : "s"} go with this question.`
    : "New thread — this question stands alone.";
}

/* ── composer ───────────────────────────────────────────────────────────── */
const q = $("#q"), go = $("#go");
function grow() {
  q.style.height = "auto";
  q.style.height = Math.min(q.scrollHeight, 184) + "px";
  go.classList.toggle("ready", q.value.trim().length > 1);
}
q.addEventListener("input", grow);
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  const text = q.value.trim();
  if (text.length < 2) return;
  q.value = ""; grow();
  ask(text);
};

$("#chips").innerHTML = EXAMPLES
  .map(x => `<button class="chip" type="button">${esc(x)}</button>`).join("");
$("#chips").onclick = (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  ask(b.textContent.trim());
};

/* ── the live stage panel ────────────────────────────────────────────────
   Every row here came from an event the pipeline emitted. Nothing is on a
   timer. This replaced a hardcoded six-step animation that advanced every
   1100ms whether or not anything had happened — and which gave the guard
   checks (18ms on a measured run) the same screen time as the model call
   (7,668ms). The two model calls are ~96% of the wall clock, so those are the
   rows that get a running counter. */
const STAGE_LABEL = {
  prepare: "Resolving your role and the views",
  author: "Writing the SQL",
  validate: "Checking it against the guard",
  execute: "Running it, read-only",
  profile: "Measuring the result in Python",
  evidence: "Running the evidence queries",
  synthesise: "Composing the answer",
  learn: "Keeping what was learned",
};

function livePanel(host) {
  const box = document.createElement("div");
  box.className = "thinking";
  host.innerHTML = "";
  host.appendChild(box);

  const live = new Map();
  let ticker = null;
  const tick = () => live.forEach(r => {
    r.el.querySelector(".el").textContent =
      ((Date.now() - r.t0) / 1000).toFixed(1) + "s";
  });

  function add(cls, label, msg, right) {
    const el = document.createElement("div");
    el.className = cls;
    el.innerHTML = `<span class="pip"></span>
      <span class="txt">${esc(label)}${msg
        ? ` <i>${esc(msg)}</i>` : ""}</span>
      <span class="el">${right || ""}</span>`;
    box.appendChild(el);
    return el;
  }

  return {
    event(ev) {
      if (ev.kind === "recovery") {
        add("stage fix", ev.message, "", "repaired").title = ev.detail || "";
        return;
      }
      if (ev.kind !== "stage") return;
      const label = STAGE_LABEL[ev.stage] || ev.stage;

      if (ev.status === "start") {
        const el = add("stage on", label, ev.message, "0.0s");
        live.set(ev.stage, { el, t0: Date.now() });
        if (!ticker) ticker = setInterval(tick, 100);
        return;
      }
      // done or fail: close the open row if there is one, otherwise this is a
      // stage that reported only its result (prepare, profile, each evidence
      // query) and gets a finished row of its own.
      const open = live.get(ev.stage);
      const el = open ? open.el : add("stage", label, "", "");
      if (open) live.delete(ev.stage);
      el.className = "stage " + (ev.status === "fail" ? "bad" : "done");
      el.querySelector(".txt").innerHTML =
        `${esc(label)}${ev.message ? ` <i>${esc(ev.message)}</i>` : ""}`;
      el.querySelector(".el").textContent = ev.ms ? ev.ms + "ms" : "";
      if (ev.detail) el.title = ev.detail;
      if (!live.size && ticker) { clearInterval(ticker); ticker = null; }
    },
    stop() { if (ticker) { clearInterval(ticker); ticker = null; } },
  };
}

/* ── asking ───────────────────────────────────────────────────────────────
   /v2/ask/stream carries the pipeline's own events and then the answer. If it
   is not there — an older server, a proxy that will not pass an event stream —
   this falls back to the plain POST, which is unchanged. */
async function runStreamed(payload, panel) {
  let r;
  try {
    r = await fetch("/v2/ask/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch { return null; }
  if (r.status === 404 || r.status === 405) return null;
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || ("HTTP " + r.status));
  }
  if (!r.body || !(r.headers.get("content-type") || "").includes("event-stream"))
    return null;

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "", answer = null, failed = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (!frame || frame.startsWith(":")) continue;      // comment / keepalive
      const m = /^event: ([^\n]+)\ndata: ([\s\S]*)$/.exec(frame);
      if (!m) continue;
      let data;
      try { data = JSON.parse(m[2]); } catch { continue; }
      if (m[1] === "event") panel.event(data);
      else if (m[1] === "answer") answer = data;
      else if (m[1] === "error") failed = data.detail || "the request failed";
    }
  }
  if (failed) throw new Error(failed);
  return answer;
}

async function runPlain(payload) {
  const r = await fetch("/v2/ask", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
  return d;
}

async function ask(question) {
  const c = conv() || newConv();      // newConv() re-renders, so hide after it
  $("#empty").hidden = true;
  if (!c.title) { c.title = question; save(); }
  renderHistory();

  const wrap = document.createElement("div");
  wrap.className = "turn";
  wrap.innerHTML = `<div class="ask"><p>${esc(question)}</p></div>
    <div class="answer"></div>`;
  $("#thread").appendChild(wrap);

  // The GPT move: pin the new question to the top of the viewport. Everything
  // asked before it scrolls off, and the answer arrives in the space below.
  wrap.scrollIntoView({ behavior: "smooth", block: "start" });

  const panel = livePanel(wrap.querySelector(".answer"));
  go.disabled = true;
  $("#ctx").textContent = "Working…";

  // Only the question and the SQL go back as context: the pipeline needs to
  // know what was asked and what ran, not what was then said about it.
  const payload = {
    question,
    conversation: c.turns.filter(t => t.sql)
                         .map(t => ({ question: t.question, sql: t.sql })),
  };

  try {
    let d = await runStreamed(payload, panel);
    if (d === null) d = await runPlain(payload);   // streaming unavailable
    panel.stop();

    const t = trim(question, d);
    c.turns.push(t);
    c.at = Date.now();
    c.role = me?.role || c.role;
    save(); renderHistory();
    paint(wrap, t);
  } catch (err) {
    panel.stop();
    const host = wrap.querySelector(".answer");
    host.className = "answer is-sysfail";
    host.innerHTML = `<p class="kicker">The request did not complete</p>
      <p class="headline">${esc(err.message)}</p>
      <p class="caveat" style="margin-top:14px">Nothing was measured, so there is
        nothing to show. The server may be restarting — ask again.</p>`;
  } finally {
    go.disabled = false;
    ctxNote();
  }
}

/** Keep what a reload needs; drop what would blow the storage quota. */
function trim(question, d) {
  return {
    question, at: Date.now(),
    ok: d.ok, refused: d.refused, refusal_reason: d.refusal_reason,
    failure_kind: d.failure_kind, failure_detail: d.failure_detail,
    suggestions: d.suggestions, sql_repairs: d.sql_repairs,
    needs_clarification: d.needs_clarification, clarification: d.clarification,
    headline: d.headline, observed: d.observed, hypotheses: d.hypotheses,
    caveats: d.caveats, assumptions: d.assumptions,
    sql: d.sql_authored, sql_executed: d.sql_executed,
    advice: d.advice, playbooks: d.playbooks, explanation: d.explanation,
    data_quality: d.data_quality, decomposition: d.decomposition,
    cache_hit: d.cache_hit, cache_state: d.cache_state,
    // Evidence rows are trimmed for storage but the chart needs them, so each
    // query keeps a small slice rather than the whole result.
    evidence_queries: (d.evidence_queries || []).map(e => ({
      ...e, data: (e.data || []).slice(0, 60) })),
    rows: (d.rows || []).slice(0, 60), row_count: d.row_count, chart: d.chart,
    truncated: d.truncated, facts: d.facts, profile_text: d.profile_text,
    grounding: d.grounding, checks_passed: d.checks_passed,
    injected_predicates: d.injected_predicates, llm_calls: d.llm_calls,
    attempts: d.attempts, timings_ms: d.timings_ms, total_ms: d.total_ms,
    composed_by: d.composed_by, recoveries: d.recoveries,
  };
}

/* ── rendering ──────────────────────────────────────────────────────────── */
function renderThread() {
  const c = conv();
  const el = $("#thread");
  el.innerHTML = "";
  $("#empty").hidden = !!(c && c.turns.length);
  if (c) c.turns.forEach(t => el.appendChild(turnEl(t)));
  // Not scrollHeight: .thread carries a tall tail so a new question can be
  // pinned to the top, and scrolling to the bottom would land in that empty
  // space. The last turn is where the eye expects to be.
  const last = el.lastElementChild;
  if (last) last.scrollIntoView({ block: "start" });
  else $("#scroller").scrollTop = 0;
  ctxNote();
  titlebar();
}

function turnEl(t) {
  const wrap = document.createElement("div");
  wrap.className = "turn";
  wrap.innerHTML = `<div class="ask"><p>${esc(t.question)}</p></div>
    <div class="answer"></div>`;
  paint(wrap, t);
  return wrap;
}

/** Fill the answer half of a turn, and wire up everything inside it. */
function paint(wrap, t) {
  const host = wrap.querySelector(".answer");
  host.className = "answer " + kindClass(t);
  host.innerHTML = body(t);

  const tabs = host.querySelector(".tabs");
  if (tabs) tabs.onclick = (e) => {
    const b = e.target.closest(".tab"); if (!b) return;
    host.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === b));
    host.querySelectorAll(".pane").forEach(p =>
      p.classList.toggle("on", p.dataset.p === b.dataset.t));
  };
  host.querySelectorAll(".opt").forEach(b => {
    b.onclick = () => ask(b.dataset.q);
  });
  drawCharts(host, t);
}

function kindClass(t) {
  if (t.decomposition && t.decomposition.dimension) return "is-decomp";
  if (t.explanation && t.explanation.definition) return "is-explain";
  if (t.needs_clarification) return "is-clarify";
  if (t.refused || !t.ok) return (t.failure_kind || "data") === "data"
    ? "is-refusal" : "is-sysfail";
  if (t.advice && t.advice.situation) return "is-advice";
  return "";
}

function body(t) {
  if (t.decomposition && t.decomposition.dimension) return decomp(t, t.decomposition);
  if (t.explanation && t.explanation.definition) return explain(t, t.explanation);
  if (t.needs_clarification) return clarify(t);
  if (t.refused || !t.ok) return refusal(t);
  if (t.advice && t.advice.situation) return advice(t, t.advice);
  return measured(t);
}

/* ── one of four shapes ─────────────────────────────────────────────────── */
function clarify(t) {
  const c = t.clarification || {};
  return `<p class="kicker">One thing first</p>
    <p class="headline">${esc(c.question)}</p>
    <h3 class="sec">Pick the one you meant</h3>
    <div class="opts">${(c.options || []).map(o =>
      `<button class="opt" type="button" data-q="${esc(o.question)}">
         <b>${esc(o.label)}</b><span>${esc(o.question)}</span></button>`).join("")}</div>
    <p class="caveat" style="margin-top:16px">Asked because the answer genuinely
      changes with it — not picked from a fixed list of metrics.</p>`;
}

/* Why a rate moved. The split is the answer, so it leads — and the two
   effects are kept visually apart because confusing them is the whole error
   this analysis exists to prevent. */
function decomp(t, d) {
  const g = t.grounding || {};
  const share = Math.abs(d.mix_share_pct);
  const pp = (v) => (v >= 0 ? "+" : "") + v.toFixed(2) + "pp";
  return `<p class="kicker">Why it moved</p>
    <p class="headline">${esc(t.headline)}</p>
    <div class="badges">
      ${groundBadge(g, "arithmetic shown")}
      <span class="badge hl">${(t.checks_passed || []).length} checks passed</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      ${(t.injected_predicates || []).length
        ? `<span class="badge ok">row filter injected</span>` : ""}
      ${extraBadges(t)}
    </div>

    <div class="split">
      <div class="split-tot"><b>${pp(d.delta_pp)}</b>
        <span>${d.baseline_value.toFixed(2)}% &rarr; ${d.target_value.toFixed(2)}%</span></div>
      <div class="split-bar">
        <i class="mix" style="width:${Math.min(100, share).toFixed(1)}%"></i>
        <i class="rate" style="width:${Math.max(0, 100 - share).toFixed(1)}%"></i>
      </div>
      <div class="split-legend">
        <span><i class="mix"></i> mix ${pp(d.mix_pp)} — traffic moved between segments</span>
        <span><i class="rate"></i> rate ${pp(d.rate_pp)} — segments changed their own rate</span>
      </div>
      <p class="caveat" style="border:0;padding:0;margin-top:10px">
        ${share.toFixed(0)}% of the move is <b>${esc(d.dominant)}</b>.
        ${d.dominant === "mix"
          ? "Nothing got worse at processing — the traffic went somewhere weaker, so the fix belongs to whoever changed the routing."
          : d.dominant === "rate"
            ? "Processing genuinely degraded; the segments themselves got worse."
            : "Both effects are material, so neither explanation stands alone."}</p>
    </div>

    <h3 class="sec">Segment by segment</h3>
    <div class="scrollx"><table><thead><tr>
      <th>${esc(d.dimension)}</th><th>share before</th><th>share after</th>
      <th>its own rate</th><th>mix</th><th>rate</th></tr></thead>
      <tbody>${(d.segments || []).map(s => `<tr>
        <td>${esc(s.key)}</td>
        <td class="num">${(s.weight_base * 100).toFixed(1)}%</td>
        <td class="num">${(s.weight_target * 100).toFixed(1)}%</td>
        <td class="num">${(s.value_base * 100).toFixed(2)}% &rarr; ${(s.value_target * 100).toFixed(2)}%</td>
        <td class="num">${pp(s.mix_pp)}</td>
        <td class="num">${pp(s.rate_pp)}</td></tr>`).join("")}</tbody></table></div>

    ${(t.hypotheses || []).length ? `<h3 class="sec">Hypotheses — not proven</h3>
      ${t.hypotheses.map(h => `<div class="hyp">${esc(h.text)}
        <div class="meta">confidence ${esc(h.confidence)}${
          h.testable_with ? " · test with: " + esc(h.testable_with) : ""}</div>
      </div>`).join("")}` : ""}
    ${caveats(t)}

    <div class="evidence">
      <div class="tabs">${tab("facts", "Facts", true)}${tab("sql", "SQL")}
        ${(t.data_quality || []).length ? tab("dq", "Data quality") : ""}
        ${tab("trace", "Trace")}</div>
      <div class="pane on" data-p="facts">${facts(t)}</div>
      <div class="pane" data-p="sql">
        <p class="lbl">Built from the semantic model, not written by the model —
          the arithmetic needs one exact shape, so the query is generated and
          then validated like any other.</p>
        <pre>${esc(t.sql)}</pre>
        <p class="lbl">As it executed</p><pre>${esc(t.sql_executed)}</pre></div>
      ${(t.data_quality || []).length
        ? `<div class="pane" data-p="dq">${dqPane(t)}</div>` : ""}
      <div class="pane" data-p="trace">${trace(t)}</div>
    </div>`;
}

/* A definition, not a measurement. There is no chart, no result table and no
   fact pack here — nothing was executed. What stands in for the grounding
   check is the formula check: the expression quoted in the prose has to be the
   one the guard enforces in generated SQL. */
function explain(t, x) {
  const g = t.grounding || {};
  const auth = (x.retrieved || []).filter(r => r.authoritative).length;
  return `<p class="kicker">Definition</p>
    <p class="headline">${esc(x.definition)}</p>
    <div class="badges">
      ${g.verdict === "grounded"
        ? `<span class="badge ok">formula checked against the semantic model</span>`
        : g.verdict === "rejected"
          ? `<span class="badge no">stated formula rejected — showing the model's own wording</span>`
          : `<span class="badge">read from the semantic model</span>`}
      <span class="badge hl">${auth} authoritative source${auth === 1 ? "" : "s"}</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      <span class="badge">${esc(t.composed_by)}</span>
      ${(t.recoveries || []).length
        ? `<span class="badge fix">${t.recoveries.length} self-repair${
            t.recoveries.length === 1 ? "" : "s"}</span>` : ""}
    </div>

    ${x.formula ? `<h3 class="sec">The expression PayLens runs</h3>
      <pre>${esc(x.formula)}</pre>
      ${x.formula_plain ? `<p class="caveat">${esc(x.formula_plain)}</p>` : ""}` : ""}
    ${list(x.notes, "Worth knowing")}
    ${(x.industry || []).length ? `<h3 class="sec">How the industry words it</h3>
      ${x.industry.map(i => `<div class="caveat">${esc(i)}</div>`).join("")}` : ""}
    ${(t.suggestions || []).length ? `<h3 class="sec">Measure it</h3>
      <div class="opts">${t.suggestions.map(q =>
        `<button class="opt" type="button" data-q="${esc(q)}">
           <b>${esc(q)}</b></button>`).join("")}</div>` : ""}

    <div class="evidence">
      <div class="tabs">${tab("src", "Where this came from", true)}${
        tab("trace", "Trace")}</div>
      <div class="pane on" data-p="src">
        <p class="lbl">Retrieved from the knowledge base, authoritative first.
          The metric tier is generated from the semantic model, so it cannot
          drift from the SQL the guard enforces.</p>
        <table><thead><tr><th>tier</th><th>term</th><th>match</th></tr></thead>
          <tbody>${(x.retrieved || []).map(r => `<tr>
            <td>${r.authoritative
              ? `<b>${esc(r.tier)}</b>` : esc(r.tier)}</td>
            <td>${esc(r.term)}</td>
            <td class="num">${(r.similarity * 100).toFixed(0)}%</td></tr>`).join("")}
          </tbody></table>
        ${(x.sources || []).length ? `<p class="lbl" style="margin-top:14px">
          Industry sources</p>${x.sources.map(u =>
          `<div class="caveat"><a href="${esc(u)}" target="_blank"
             rel="noopener noreferrer">${esc(u)}</a></div>`).join("")}` : ""}
        ${g.note ? `<p class="lbl" style="margin-top:14px">Formula check</p>
          <div class="caveat">${esc(g.note)}</div>` : ""}</div>
      <div class="pane" data-p="trace">${trace(t)}</div>
    </div>`;
}

function refusal(t) {
  // Three different things used to print the same red box. A query that failed
  // to build is our fault and says so; only kind === "data" is entitled to
  // claim the dataset does not hold something.
  const kind = t.failure_kind || "data";
  const head = kind === "data" ? "Not in this dataset"
    : kind === "config" ? "Not configured yet" : "I could not answer that";
  const foot = kind === "data"
    ? `A refusal naming something the schema does hold is treated as an error and
       retried, so this one means the field genuinely is not here.`
    : kind === "config"
      ? `This is a setup problem, not a limit of your data.`
      : `This is a failure on my side — the query I wrote would not validate. Your
         data may well contain the answer.`;
  return `<p class="kicker">${esc(head)}</p>
    <p class="headline">${esc(t.refusal_reason)}</p>
    ${(t.suggestions || []).length ? `<h3 class="sec">These do work</h3>
      <div class="opts">${t.suggestions.map(s =>
        `<button class="opt" type="button" data-q="${esc(s)}">
           <b>${esc(s)}</b></button>`).join("")}</div>` : ""}
    <p class="caveat" style="margin-top:16px">${foot}</p>
    ${t.failure_detail ? `<details>
      <summary>Technical detail</summary>
      <pre style="margin-top:8px">${esc(t.failure_detail)}</pre></details>` : ""}`;
}

function measured(t) {
  const g = t.grounding || {};
  return `<p class="headline">${esc(t.headline)}</p>
    <div class="badges">
      ${groundBadge(g, "measured answer shown")}
      <span class="badge hl">${(t.checks_passed || []).length} checks passed</span>
      <span class="badge">${(t.row_count || 0).toLocaleString()} rows</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      <span class="badge">${esc(t.composed_by)}</span>
      ${(t.injected_predicates || []).length
        ? `<span class="badge ok">row filter injected</span>` : ""}
      ${extraBadges(t)}
      ${t.truncated ? `<span class="badge no">truncated</span>` : ""}
    </div>

    ${list(t.observed, "What the data shows")}
    ${(t.hypotheses || []).length ? `<h3 class="sec">Hypotheses — not proven</h3>
      ${t.hypotheses.map(h => `<div class="hyp">${esc(h.text)}
        <div class="meta">confidence ${esc(h.confidence)}${
          h.testable_with ? " · test with: " + esc(h.testable_with) : ""}</div>
      </div>`).join("")}` : ""}
    ${caveats(t)}

    <div class="evidence">
      <div class="tabs">
        ${hasChart(t) ? tab("chart", "Chart", true) : ""}
        ${tab("result", "Result", !hasChart(t))}
        ${tab("sql", "SQL")}${tab("facts", "Facts")}
        ${(t.data_quality || []).length ? tab("dq", "Data quality") : ""}
        ${tab("profile", "Profile")}${tab("trace", "Trace")}
      </div>
      ${hasChart(t) ? `<div class="pane on" data-p="chart">
        <div data-chart="main"></div></div>` : ""}
      <div class="pane${hasChart(t) ? "" : " on"}" data-p="result">
        ${table(t.rows, t.row_count)}</div>
      <div class="pane" data-p="sql">
        <p class="lbl">As the model wrote it</p><pre>${esc(t.sql)}</pre>
        <p class="lbl">As it executed — access filter and row cap added by the guard</p>
        <pre>${esc(t.sql_executed)}</pre>
        ${(t.injected_predicates || []).length
          ? `<p class="lbl">Injected predicates</p>
             <pre>${esc(t.injected_predicates.join("\n"))}</pre>` : ""}</div>
      <div class="pane" data-p="facts">${facts(t)}</div>
      ${(t.data_quality || []).length
        ? `<div class="pane" data-p="dq">${dqPane(t)}</div>` : ""}
      <div class="pane" data-p="profile"><pre>${esc(t.profile_text)}</pre></div>
      <div class="pane" data-p="trace">${trace(t)}</div>
    </div>`;
}

function advice(t, ad) {
  const g = t.grounding || {};
  const q = t.evidence_queries || [];
  const charted = q.some(e => e.chart && e.chart.chart_type
                              && e.chart.chart_type !== "table");
  return `<p class="kicker">A briefing, not a measurement</p>
    <p class="headline">${esc(ad.situation)}</p>
    <div class="badges">
      ${groundBadge(g, "catalogue briefing shown")}
      <span class="badge hl">${q.filter(x => !x.error).length}/${q.length} evidence queries</span>
      <span class="badge">${(t.facts || []).length} measured facts</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      ${(t.playbooks || []).map(p => `<span class="badge">${esc(p)}</span>`).join("")}
      ${(g.invented_levers || []).length
        ? `<span class="badge no">${g.invented_levers.length} invented lever(s) dropped</span>` : ""}
      ${(t.recoveries || []).length
        ? `<span class="badge fix">${t.recoveries.length} self-repair${
            t.recoveries.length === 1 ? "" : "s"}</span>` : ""}
    </div>

    ${list(ad.whats_happening, "What's happening")}
    ${(ad.what_to_do || []).length ? `<h3 class="sec">What to do</h3>
      ${ad.what_to_do.map(d => `<div class="lever">
        <b>${esc(d.lever)}</b><span class="owner">${esc(d.owner)}</span>
        ${d.why_here ? `<p>${esc(d.why_here)}</p>` : ""}
        ${d.check_with ? `<button class="opt check" type="button"
          data-q="${esc(d.check_with)}">check it: ${esc(d.check_with)}</button>` : ""}
      </div>`).join("")}` : ""}
    ${(ad.cannot_tell || []).length ? `<h3 class="sec">What this can't tell you</h3>
      ${ad.cannot_tell.map(c => `<div class="caveat">${esc(c)}</div>`).join("")}` : ""}
    ${caveats(t)}

    <div class="evidence">
      <div class="tabs">
        ${charted ? tab("chart", "Charts", true) : ""}
        ${tab("facts", "Facts", !charted)}
        ${tab("sql", "Evidence queries")}${tab("profile", "Profile")}${tab("trace", "Trace")}
      </div>
      ${charted ? `<div class="pane on" data-p="chart">${q.filter(e => !e.error)
        .map(e => `<div class="evchart"><p class="lbl">${esc(e.purpose || e.id)}</p>
          <div data-chart="${esc(e.id)}"></div></div>`).join("")}</div>` : ""}
      <div class="pane${charted ? "" : " on"}" data-p="facts">${facts(t)}</div>
      <div class="pane" data-p="sql">${q.map(e => `
        <p class="lbl">${esc(e.id)} — ${esc(e.purpose || "evidence")}${
          e.error ? ` · not measured` : ""}</p>
        <pre>${esc(e.error || e.sql_executed || e.sql)}</pre>`).join("")}</div>
      <div class="pane" data-p="profile"><pre>${esc(t.profile_text)}</pre></div>
      <div class="pane" data-p="trace">${trace(t)}</div>
    </div>`;
}

/* ── shared pieces ──────────────────────────────────────────────────────── */
const tab = (id, label, on) =>
  `<button class="tab${on ? " on" : ""}" data-t="${id}" type="button">${label}</button>`;

/* Badges every answer shape shares: self-repairs, cache provenance and the
   data-quality verdict. A reader should not have to open a tab to learn that
   the query was reused or that something about the data is off. */
function extraBadges(t) {
  const out = [];
  if ((t.recoveries || []).length)
    out.push(`<span class="badge fix">${t.recoveries.length} self-repair${
      t.recoveries.length === 1 ? "" : "s"}</span>`);
  if (t.cache_hit)
    out.push(`<span class="badge cache" title="The query was reused, so the
      authoring call was skipped. It was still re-validated and re-run, and the
      numbers are measured fresh.">query reused (${esc(t.cache_hit)})</span>`);
  const dq = t.data_quality || [];
  const bad = dq.filter(c => c.status !== "pass");
  if (dq.length)
    out.push(bad.length
      ? `<span class="badge warn">${bad.length} data-quality finding${
          bad.length === 1 ? "" : "s"}</span>`
      : `<span class="badge ok">${dq.length} data-quality checks clear</span>`);
  return out.join("");
}

const dqPane = (t) => {
  const dq = t.data_quality || [];
  if (!dq.length) return "";
  return `<p class="lbl">Run against the window this answer actually used.
    Every probe goes through the guard with your own access scope.</p>
    <table class="dq"><tbody>${dq.map(c => `<tr class="dq-${esc(c.status)}">
      <td class="dq-s">${c.status === "pass" ? "pass"
        : esc(c.severity)}</td>
      <td>${esc(c.message)}</td></tr>`).join("")}</tbody></table>`;
};

const groundBadge = (g, fallback) =>
  g.verdict === "grounded"
    ? `<span class="badge ok">grounded${g.repaired ? " · repaired" : ""}</span>`
    : g.verdict === "rejected"
      ? `<span class="badge no">ungrounded — ${esc(fallback)}</span>` : "";

const list = (items, heading) => (items || []).length
  ? `<h3 class="sec">${esc(heading)}</h3><ul class="obs">${
      items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";

function caveats(t) {
  const out = [];
  if ((t.caveats || []).length) out.push(`<h3 class="sec">Caveats</h3>` +
    t.caveats.map(c => `<div class="caveat">${esc(c)}</div>`).join(""));
  if ((t.assumptions || []).length) out.push(`<h3 class="sec">Assumptions made</h3>` +
    t.assumptions.map(a => `<div class="caveat">${esc(a)}</div>`).join(""));
  return out.join("");
}

function facts(t) {
  const g = t.grounding || {};
  return `<p class="lbl">Computed in Python. The prose may state these and nothing else.</p>
    <table class="facts">${(t.facts || []).map(f => `<tr>
      <td>${esc(f.id)}</td>
      <td>${esc(f.label)}${f.detail
        ? ` <span style="color:var(--ink-3)">— ${esc(f.detail)}</span>` : ""}</td>
      <td>${esc(f.value)}</td></tr>`).join("")}</table>
    ${g.note ? `<p class="lbl" style="margin-top:14px">Grounding</p>
      <div class="caveat">${esc(g.note)}</div>` : ""}`;
}

function trace(t) {
  const pad = (s, n) => String(s).padEnd(n);
  return `${(t.recoveries || []).length ? `<p class="lbl">Repaired while running —
      things that went wrong and were fixed without failing the request</p>
    ${t.recoveries.map(r => `<div class="caveat">${esc(r.message)}${
      r.detail ? `<br><span style="color:var(--ink-3);font-family:var(--mono);font-size:11.5px">${
        esc(r.detail)}</span>` : ""}</div>`).join("")}` : ""}
    <p class="lbl">Checks passed</p>
    <pre>${esc((t.checks_passed || []).join("\n") || "none")}</pre>
    ${(t.sql_repairs || []).length ? `<p class="lbl">SQL repaired before validation</p>
      <pre>${esc(t.sql_repairs.join("\n"))}</pre>` : ""}
    <p class="lbl">Model calls</p>
    <pre>${esc((t.llm_calls || []).map(c =>
      `${pad(c.purpose, 9)}${c.ok ? "ok  " : "FAIL"} ${String(c.ms).padStart(6)}ms  ${c.model}`
    ).join("\n") || "none")}</pre>
    <p class="lbl">Validation attempts</p>
    <pre>${esc((t.attempts || []).map(a =>
      `${a.ok ? "pass" : "FAIL"}  ${a.stage}${a.reason ? "  " + a.reason : ""}`
    ).join("\n") || "none")}</pre>
    <p class="lbl">Timings</p>
    <pre>${esc(Object.entries(t.timings_ms || {})
      .map(([k, v]) => `${pad(k, 13)}${String(v).padStart(6)}ms`).join("\n") || "none")}</pre>`;
}

function table(rows, total) {
  if (!rows || !rows.length) return `<p class="caveat">No rows. That is a finding,
    not an error — nothing matched this window and these filters.</p>`;
  const keys = Object.keys(rows[0]);
  const fmt = (v) => v === null || v === undefined ? "—"
    : typeof v === "number"
      ? (Number.isInteger(v) ? v.toLocaleString()
        : Math.abs(v) < 1 ? v.toFixed(4)
          : v.toLocaleString(undefined, { maximumFractionDigits: 2 }))
      : String(v);
  return `<div class="scrollx"><table>
    <thead><tr>${keys.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${keys.map(k =>
      `<td class="${typeof r[k] === "number" ? "num" : ""}">${esc(fmt(r[k]))}</td>`
    ).join("")}</tr>`).join("")}</tbody></table></div>
    ${total > rows.length ? `<p class="caveat" style="margin-top:10px">Showing
      ${rows.length} of ${total.toLocaleString()} rows — the rest are not kept in
      this browser.</p>` : ""}`;
}

/* ── charts ───────────────────────────────────────────────────────────────
   PLCharts comes from /shared/charts.js — the classic UI's renderer, served
   rather than copied, so a fix lands in both front ends and the palette cannot
   drift. It reads --s1..--s7 off :root and writes into #tip, both of which this
   page provides. */
const hasChart = (t) => !!(t.chart && t.chart.chart_type
                           && t.chart.chart_type !== "table"
                           && (t.rows || []).length);

function chartNode(spec, rows) {
  if (!window.PLCharts || !spec || !rows || !rows.length) return null;
  const type = spec.chart_type;
  if (!type || type === "table") return null;
  // The renderer formats by kind, and a ratio drawn as a plain number is
  // unreadable. Column names carry the kind here, as they do server-side.
  const kindOf = (c) => /rate|share|pct/.test(c || "") ? "percent"
    : /gmv|amount|value|revenue|sales|fee|cost/.test(c || "") ? "currency" : "number";
  try {
    if (type === "kpi")
      return PLCharts.kpi(rows[0][spec.y], kindOf(spec.y), spec.title || "");
    if (type === "line")
      return PLCharts.line(rows, { x: spec.x, y: spec.y, series: spec.series,
                                   kind: kindOf(spec.y) });
    if (type === "stacked_area_100" && spec.series)
      return PLCharts.shareArea(rows, { x: spec.x, series: spec.series });
    if (type === "pie")
      return PLCharts.donut(rows, { x: spec.x, y: spec.y });
    if (type === "bar" || type === "horizontal_bar" || type === "stacked_bar")
      return PLCharts.bar(rows, { x: spec.x, y: spec.y, kind: kindOf(spec.y),
                                  horizontal: type === "horizontal_bar" });
  } catch (err) {
    // A chart that throws must not take the answer down with it.
    console.warn("chart failed", err);
  }
  return null;
}

function mountChart(host, spec, rows) {
  const node = chartNode(spec, rows);
  if (!node) return false;
  const box = document.createElement("div");
  box.className = "chartbox";
  box.appendChild(node);
  host.appendChild(box);
  if ((spec.overrides || []).length) {
    const p = document.createElement("p");
    p.className = "overrides";
    p.innerHTML = "<b>Adjusted:</b> " + spec.overrides.map(esc).join(" · ");
    host.appendChild(p);
  }
  return true;
}

/** Fill every <div data-chart> left by the markup, once it is in the DOM. */
function drawCharts(root, t) {
  root.querySelectorAll("[data-chart]").forEach(host => {
    const key = host.dataset.chart;
    const ev = key === "main" ? null
      : (t.evidence_queries || []).find(e => e.id === key);
    const spec = key === "main" ? t.chart : ev?.chart;
    const rows = key === "main" ? t.rows : ev?.data;
    if (!mountChart(host, spec, rows)) host.remove();
  });
}

/* ── boot ───────────────────────────────────────────────────────────────── */
(async function () {
  await whoami();
  health();
  if (!convs.length) newConv();
  renderHistory();
  renderThread();
  grow();
  q.focus();
})();
