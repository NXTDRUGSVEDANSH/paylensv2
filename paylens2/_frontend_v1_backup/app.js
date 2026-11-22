/* PayLens v2 — one page, no build step.

   Conversations live in this browser, not on the server: v2's pipeline is
   stateless by design (a thread can be replayed exactly for debugging), so the
   caller holds the history and sends it with each question. Five threads are
   kept, matching the classic pipeline's limit. */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const MAX_CONVS = 5;
const KEY = "paylens2.convs";
const EXAMPLES = [
  "why did our overall success rate drop last week",
  "how much money did we lose to failures last week",
  "total sales by region last month",
  "which issuer bank has the worst success rate last week",
  "is there any outage in the terminal estate",
  "show me chargebacks",
];

/* ── state ──────────────────────────────────────────────────────────── */
let convs = load();
let active = convs[0]?.id || null;
let me = null;

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; }
}
function save() {
  // Rows are trimmed before storing: a 5,000-row result would blow the quota
  // and the table is re-fetchable by asking again. The answer itself is what
  // must survive a reload.
  try {
    localStorage.setItem(KEY, JSON.stringify(convs.slice(0, MAX_CONVS)));
  } catch {
    convs = convs.slice(0, 2);
    try { localStorage.setItem(KEY, JSON.stringify(convs)); } catch { }
  }
}
const conv = () => convs.find(c => c.id === active) || null;

function newConv(reason = "") {
  const c = { id: "c" + Date.now().toString(36), title: "", at: Date.now(),
              role: me?.role || "anonymous", turns: [] };
  convs.unshift(c);
  convs = convs.slice(0, MAX_CONVS);
  active = c.id;
  save(); renderConvs(); renderThread();
  if (reason) note(reason);
  return c;
}

/* ── theme ──────────────────────────────────────────────────────────── */
try {
  const t = localStorage.getItem("paylens2.theme");
  if (t) document.documentElement.dataset.theme = t;
} catch { }
$("#theme").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark";
  const now = dark ? "light" : "dark";
  document.documentElement.dataset.theme = now;
  try { localStorage.setItem("paylens2.theme", now); } catch { }
};

/* ── health ─────────────────────────────────────────────────────────── */
async function health() {
  try {
    const h = await (await fetch("/v2/health")).json();
    $("#health").innerHTML = [
      h.views_built ? "views ✓" : "<span class='bad'>views NOT built — run python -m paylens2.schema.build</span>",
      h.llm_available ? "model ✓" : "<span class='bad'>no LLM key — v2 cannot author a query</span>",
      "author: " + esc(h.author_model),
      "synth: " + esc(h.synth_model),
      esc((h.allowed_tables || []).join(" · ")),
    ].join("<i>|</i>");
  } catch { $("#health").innerHTML = "<span class='bad'>API unreachable</span>"; }
}

/* ── account (optional sign-in) ─────────────────────────────────────── */
async function whoami() {
  // /auth/me answers in two shapes: unauthenticated carries the anonymous role
  // it would use, authenticated nests the user. Both matter here, because the
  // role is what decides the row filter even when nobody has signed in.
  let d = null;
  try { const r = await fetch("/auth/me"); d = r.ok ? await r.json() : null; }
  catch { d = null; }
  const signed = !!(d && d.authenticated);
  me = signed ? d.user : { username: "anonymous", role: (d && d.anon_role) || "cxo",
                           display_name: "", claims: {} };
  const detail = (d && d.role_detail) || null;

  $("#who").innerHTML = signed
    ? `<b>${esc(me.display_name || me.username)}</b> · ${esc(me.role)}`
    : `not signed in · ${esc(me.role)}`;

  const claims = Object.entries(me.claims || {})
    .map(([k, v]) => `${k}=${v}`).join(", ");
  $("#acctstate").innerHTML = signed
    ? `<p style="margin:0;font-size:13.5px"><b>${esc(me.display_name || me.username)}</b><br>
       <span style="color:var(--ink-3);font-size:12.5px">${esc(detail?.label || me.role)}${
         claims ? " · " + esc(claims) : ""}</span></p>
       ${detail && detail.row_scope
         ? `<p class="hint" style="margin-top:9px">Every query you run is filtered
            to your own rows, in SQL. Open the <b>SQL</b> tab of any answer to see
            the predicate the guard injected.</p>` : ""}
       <button class="ghost wide" id="logout">Sign out</button>`
    : `<p style="margin:0;font-size:13px;color:var(--ink-3)">Browsing without an
       account — you see the <b>${esc((d && d.anon_role_label) || me.role)}</b>
       scope. Signing in is optional; do it to watch the row filter change.</p>`;

  $("#signtoggle").hidden = signed;
  if (signed) $("#loginform").hidden = true;
  const lo = $("#logout");
  if (lo) lo.onclick = async () => {
    await fetch("/auth/logout", { method: "POST" });
    await whoami();
    newConv("Signed out — new thread started, because nothing carries across a change of role.");
  };
}

$("#signtoggle").onclick = () => {
  const f = $("#loginform");
  f.hidden = !f.hidden;
  if (!f.hidden) { demoUsers(); $("#u").focus(); }
};

async function demoUsers() {
  const box = $("#demousers");
  if (box.dataset.done) return;
  try {
    const d = await (await fetch("/auth/demo-users")).json();
    const users = (d && d.users) || [];
    if (d && d.enabled === false) return;
    if (!users.length) return;
    box.innerHTML = `<div class="demo"><b>Demo accounts</b> — password
      <code>${esc(d.password || "paylens-demo")}</code><br>` +
      users.map(u => `<button type="button" class="ghost" data-u="${esc(u.username)}"
        >${esc(u.username)}</button>${esc(u.note || u.role)}<br>`).join("") + `</div>`;
    box.dataset.done = "1";
    box.onclick = (e) => {
      const b = e.target.closest("[data-u]"); if (!b) return;
      $("#u").value = b.dataset.u;
      $("#pw").value = d.password || "paylens-demo";
    };
  } catch { }
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
    newConv("Signed in — a new thread was started, because nothing carries across a change of role.");
  } catch (err) {
    $("#loginerr").textContent = err.message;
    $("#loginerr").hidden = false;
  } finally { $("#loginbtn").disabled = false; }
};

/* ── conversations ──────────────────────────────────────────────────── */
$("#newconv").onclick = () => newConv();

function renderConvs() {
  const el = $("#convlist");
  if (!convs.length) {
    el.innerHTML = `<p class="none">No threads yet. Ask something.</p>`;
    return;
  }
  el.innerHTML = convs.map(c => `
    <button class="conv${c.id === active ? " on" : ""}" data-c="${c.id}" type="button">
      <b>${esc(c.title || "New conversation")}</b>
      <span>${c.turns.length} turn${c.turns.length === 1 ? "" : "s"} ·
        ${new Date(c.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        ${c.role && c.role !== "anonymous" ? " · " + esc(c.role) : ""}</span>
    </button>`).join("");
  el.onclick = (e) => {
    const b = e.target.closest(".conv"); if (!b) return;
    active = b.dataset.c;
    renderConvs(); renderThread();
  };
}

function note(text) {
  $("#ctx").textContent = text;
  setTimeout(() => { if ($("#ctx").textContent === text) ctxNote(); }, 6000);
}
function ctxNote() {
  const c = conv();
  $("#ctx").textContent = c && c.turns.length
    ? `Follow-up in this thread — the previous ${c.turns.length} turn${
        c.turns.length === 1 ? "" : "s"} go with the question.`
    : "New thread — this question stands alone.";
}

/* ── chips ──────────────────────────────────────────────────────────── */
$("#chips").innerHTML = EXAMPLES
  .map(q => `<button class="chip" type="button">${esc(q)}</button>`).join("");
$("#chips").onclick = (e) => {
  if (!e.target.classList.contains("chip")) return;
  $("#q").value = e.target.textContent.trim();
  ask($("#q").value);
};

$("#go").onclick = () => { const q = $("#q").value.trim(); if (q) ask(q); };
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#go").click();
});

/* ── asking ─────────────────────────────────────────────────────────── */
async function ask(question) {
  $("#empty").hidden = true;
  let c = conv() || newConv();
  if (!c.title) { c.title = question; save(); renderConvs(); }

  $("#go").disabled = true;
  const pending = document.createElement("div");
  pending.className = "turn";
  pending.innerHTML = `
    <div class="qline"><span class="dot"></span><p>${esc(question)}</p></div>
    <div class="card"><div class="steps">
      <div><span class="spin"></span>authoring the query…</div>
      <div style="opacity:.55;margin-top:6px">validate · execute · profile · synthesise</div>
    </div></div>`;
  $("#thread").appendChild(pending);
  pending.scrollIntoView({ behavior: "smooth", block: "end" });

  try {
    const r = await fetch("/v2/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // Only the question and the SQL go back as context: the pipeline needs to
      // know what was asked and what it ran, not what it then said about it.
      body: JSON.stringify({
        question,
        conversation: c.turns.filter(t => t.sql).map(t => ({ question: t.question, sql: t.sql })),
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
    c.turns.push(trim(question, d));
    c.at = Date.now();
    save(); renderConvs(); renderThread();
  } catch (err) {
    pending.querySelector(".card").outerHTML =
      `<div class="card refuse"><p class="err"><b>Request failed.</b>
       ${esc(err.message)}</p></div>`;
  } finally { $("#go").disabled = false; ctxNote(); }
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
    advice: d.advice, playbooks: d.playbooks,
    // Evidence rows are trimmed for storage but the chart needs them, so each
    // query keeps a small slice rather than the whole result.
    evidence_queries: (d.evidence_queries || []).map(e => ({
      ...e, data: (e.data || []).slice(0, 60)})),
    rows: (d.rows || []).slice(0, 60), row_count: d.row_count, chart: d.chart,
    truncated: d.truncated, facts: d.facts, profile_text: d.profile_text,
    grounding: d.grounding, checks_passed: d.checks_passed,
    injected_predicates: d.injected_predicates, llm_calls: d.llm_calls,
    attempts: d.attempts, timings_ms: d.timings_ms, total_ms: d.total_ms,
    composed_by: d.composed_by,
  };
}

/* ── rendering ──────────────────────────────────────────────────────── */
function renderThread() {
  const c = conv();
  const el = $("#thread");
  el.innerHTML = "";
  if (!c || !c.turns.length) { $("#empty").hidden = false; ctxNote(); return; }
  $("#empty").hidden = true;
  c.turns.forEach((t, i) => el.appendChild(turnEl(t, i + 1)));
  ctxNote();
}

function turnEl(t, n) {
  const wrap = document.createElement("div");
  wrap.className = "turn";
  wrap.innerHTML =
    `<div class="qline"><span class="dot"></span><p>${esc(t.question)}</p>
       <span class="n">#${n}</span></div>` + body(t);
  const tabs = wrap.querySelector(".tabs");
  if (tabs) tabs.onclick = (e) => {
    const b = e.target.closest(".tab"); if (!b) return;
    wrap.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === b));
    wrap.querySelectorAll(".pane").forEach(p =>
      p.classList.toggle("on", p.dataset.p === b.dataset.t));
  };
  wrap.querySelectorAll(".opt").forEach(b => {
    b.onclick = () => { $("#q").value = b.dataset.q; ask(b.dataset.q); };
  });
  drawCharts(wrap, t);
  return wrap;
}

function body(t) {
  if (t.needs_clarification) {
    const c = t.clarification || {};
    return `<div class="card clarify">
      <h4 class="sec">One thing first</h4>
      <p class="headline" style="font-size:16.5px">${esc(c.question)}</p>
      <div class="opts">${(c.options || []).map(o =>
        `<button class="opt" type="button" data-q="${esc(o.question)}">
           <b>${esc(o.label)}</b><span>${esc(o.question)}</span></button>`).join("")}</div>
      <p class="caveat" style="margin-top:15px">Asked because the answer genuinely
        changes with it — not picked from a fixed list of metrics.</p></div>`;
  }
  if (t.refused || !t.ok) {
    // Three different things used to print the same red box. A query that
    // failed to build is OUR fault and says so; only kind === "data" is
    // entitled to claim the dataset does not hold something.
    const kind = t.failure_kind || "data";
    const head = kind === "data" ? "Not in this dataset"
      : kind === "config" ? "Not configured" : "I could not answer that";
    const foot = kind === "data"
      ? `A refusal naming something the schema does hold is treated as an error
         and retried, so this one means the field genuinely is not here.`
      : kind === "config"
        ? `This is a setup problem, not a limit of your data.`
        : `This is a failure on my side — the query I wrote would not validate.
           Your data may well contain the answer.`;
    const sug = (t.suggestions || []).length
      ? `<h4 class="sec">Try one of these</h4><div class="opts">${
          t.suggestions.map(q => `<button class="opt" type="button"
            data-q="${esc(q)}"><b>${esc(q)}</b></button>`).join("")}</div>`
      : "";
    return `<div class="card ${kind === "data" ? "refuse" : "sysfail"}">
      <h4 class="sec">${head}</h4>
      <p class="headline" style="font-size:16px">${esc(t.refusal_reason)}</p>
      ${sug}
      <p class="caveat" style="margin-top:13px">${foot}</p>
      ${t.failure_detail ? `<details style="margin-top:10px">
        <summary class="lbl" style="cursor:pointer">Technical detail</summary>
        <pre style="margin-top:7px">${esc(t.failure_detail)}</pre></details>` : ""}
    </div>`;
  }

  const ad = t.advice || null;
  if (ad && ad.situation) return advice(t, ad);

  const g = t.grounding || {};
  const gb = g.verdict === "grounded"
    ? `<span class="badge ok">grounded${g.repaired ? " · repaired" : ""}</span>`
    : g.verdict === "rejected"
      ? `<span class="badge no">ungrounded — measured answer shown</span>` : "";

  return `<div class="card">
    <p class="headline">${esc(t.headline)}</p>
    <div class="badges">${gb}
      <span class="badge hl">${(t.checks_passed || []).length} checks passed</span>
      <span class="badge">${(t.row_count || 0).toLocaleString()} rows</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      <span class="badge">${esc(t.composed_by)}</span>
      ${(t.injected_predicates || []).length
        ? `<span class="badge ok">row filter injected</span>` : ""}
      ${t.truncated ? `<span class="badge no">truncated</span>` : ""}</div>

    ${(t.observed || []).length ? `<h4 class="sec">What the data shows</h4>
      <ul class="obs">${t.observed.map(o => `<li>${esc(o)}</li>`).join("")}</ul>` : ""}
    ${(t.hypotheses || []).length ? `<h4 class="sec">Hypotheses — not proven</h4>
      ${t.hypotheses.map(h => `<div class="hyp">${esc(h.text)}<div class="meta">
        confidence ${esc(h.confidence)}${h.testable_with
          ? " · test with: " + esc(h.testable_with) : ""}</div></div>`).join("")}` : ""}
    ${(t.caveats || []).length ? `<h4 class="sec">Caveats</h4>
      ${t.caveats.map(c => `<div class="caveat">${esc(c)}</div>`).join("")}` : ""}
    ${(t.assumptions || []).length ? `<h4 class="sec">Assumptions made</h4>
      ${t.assumptions.map(a => `<div class="caveat">${esc(a)}</div>`).join("")}` : ""}

    <div class="tabs">${hasChart(t) ? tab("chart", "Chart", true) : ""}${
      tab("result", "Result", !hasChart(t))}${tab("sql", "SQL")}
      ${tab("facts", "Facts")}${tab("profile", "Profile")}${tab("trace", "Trace")}</div>
    ${hasChart(t) ? `<div class="pane on" data-p="chart">
       <div data-chart="main"></div></div>` : ""}
    <div class="pane${hasChart(t) ? "" : " on"}" data-p="result">${
      table(t.rows, t.row_count)}</div>
    <div class="pane" data-p="sql">
      <p class="lbl">As the model wrote it</p><pre>${esc(t.sql)}</pre>
      <p class="lbl">As it executed — access filter and row cap added by the guard</p>
      <pre>${esc(t.sql_executed)}</pre>
      ${(t.injected_predicates || []).length ? `<p class="lbl">Injected predicates</p>
        <pre>${esc(t.injected_predicates.join("\n"))}</pre>` : ""}</div>
    <div class="pane" data-p="facts">
      <p class="lbl">Computed in Python. The prose may state these and nothing else.</p>
      <table class="facts">${(t.facts || []).map(f => `<tr><td>${esc(f.id)}</td>
        <td>${esc(f.label)}${f.detail
          ? ` <span style="color:var(--ink-3)">— ${esc(f.detail)}</span>` : ""}</td>
        <td>${esc(f.value)}</td></tr>`).join("")}</table>
      ${g.note ? `<p class="lbl" style="margin-top:13px">Grounding</p>
        <div class="caveat">${esc(g.note)}</div>` : ""}</div>
    <div class="pane" data-p="profile"><pre>${esc(t.profile_text)}</pre></div>
    <div class="pane" data-p="trace">
      <p class="lbl">Checks passed</p><pre>${esc((t.checks_passed || []).join("\n"))}</pre>
      <p class="lbl">LLM calls</p><pre>${(t.llm_calls || []).map(c =>
        `${c.purpose.padEnd(8)} ${c.ok ? "ok  " : "FAIL"} ${String(c.ms).padStart(6)}ms  ${esc(c.model)}`
      ).join("\n") || "none"}</pre>
      ${(t.sql_repairs || []).length ? `<p class="lbl">SQL repaired before validation</p>
        <pre>${esc(t.sql_repairs.join("\n"))}</pre>` : ""}
      <p class="lbl">Validation attempts</p><pre>${(t.attempts || []).map(a =>
        `${a.ok ? "pass" : "FAIL"}  ${esc(a.stage)}${a.reason ? "  " + esc(a.reason) : ""}`
      ).join("\n")}</pre>
      <p class="lbl">Timings</p><pre>${Object.entries(t.timings_ms || {})
        .map(([k, v]) => `${k.padEnd(12)} ${String(v).padStart(6)}ms`).join("\n")}</pre></div>
  </div>`;
}

const hasChart = (t) => !!(t.chart && t.chart.chart_type
                           && t.chart.chart_type !== "table"
                           && (t.rows || []).length);


function advice(t, ad) {
  const g = t.grounding || {};
  const gb = g.verdict === "grounded"
    ? `<span class="badge ok">grounded${g.repaired ? " · repaired" : ""}</span>`
    : `<span class="badge no">ungrounded — catalogue briefing shown</span>`;
  const q = t.evidence_queries || [];
  return `<div class="card advice">
    <p class="headline">${esc(ad.situation)}</p>
    <div class="badges">${gb}
      <span class="badge hl">${q.filter(x => !x.error).length}/${q.length} evidence queries</span>
      <span class="badge">${(t.facts || []).length} measured facts</span>
      <span class="badge">${((t.total_ms || 0) / 1000).toFixed(1)}s</span>
      ${(t.playbooks || []).map(p => `<span class="badge">${esc(p)}</span>`).join("")}
      ${(g.invented_levers || []).length
        ? `<span class="badge no">${g.invented_levers.length} invented lever(s) dropped</span>` : ""}
    </div>

    ${(ad.whats_happening || []).length ? `<h4 class="sec">What's happening</h4>
      <ul class="obs">${ad.whats_happening.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}

    ${(ad.what_to_do || []).length ? `<h4 class="sec">What to do</h4>
      ${ad.what_to_do.map(d => `<div class="lever">
         <b>${esc(d.lever)}</b>
         <span class="owner">${esc(d.owner)}</span>
         ${d.why_here ? `<p>${esc(d.why_here)}</p>` : ""}
         ${d.check_with ? `<button class="opt check" type="button"
            data-q="${esc(d.check_with)}">check it: ${esc(d.check_with)}</button>` : ""}
       </div>`).join("")}` : ""}

    ${(ad.cannot_tell || []).length ? `<h4 class="sec">What this can't tell you</h4>
      ${ad.cannot_tell.map(c => `<div class="caveat">${esc(c)}</div>`).join("")}` : ""}
    ${(t.caveats || []).length ? ad.cannot_tell && ad.cannot_tell.length ? "" :
      `<h4 class="sec">Caveats</h4>` : ""}
    ${(t.caveats || []).map(c => `<div class="caveat">${esc(c)}</div>`).join("")}
    ${(t.assumptions || []).length ? `<h4 class="sec">Assumptions made</h4>
      ${t.assumptions.map(a => `<div class="caveat">${esc(a)}</div>`).join("")}` : ""}

    <div class="tabs">
      ${q.some(e => e.chart && e.chart.chart_type && e.chart.chart_type !== "table")
        ? tab("chart", "Charts", true) : ""}
      ${tab("facts", "Facts", !q.some(e => e.chart && e.chart.chart_type
                                        && e.chart.chart_type !== "table"))}
      ${tab("sql", "Evidence queries")}${tab("profile", "Profile")}${tab("trace", "Trace")}</div>
    ${q.some(e => e.chart && e.chart.chart_type && e.chart.chart_type !== "table")
      ? `<div class="pane on" data-p="chart">${q.filter(e => !e.error).map(e =>
          `<div class="evchart"><p class="lbl">${esc(e.purpose || e.id)}</p>
             <div data-chart="${esc(e.id)}"></div></div>`).join("")}</div>` : ""}
    <div class="pane${q.some(e => e.chart && e.chart.chart_type
                                 && e.chart.chart_type !== "table") ? "" : " on"}"
         data-p="facts">
      <p class="lbl">Computed in Python. The briefing may state these and nothing else.</p>
      <table class="facts">${(t.facts || []).map(f => `<tr><td>${esc(f.id)}</td>
        <td>${esc(f.label)}${f.detail
          ? ` <span style="color:var(--ink-3)">— ${esc(f.detail)}</span>` : ""}</td>
        <td>${esc(f.value)}</td></tr>`).join("")}</table>
      ${g.note ? `<div class="caveat" style="margin-top:12px">${esc(g.note)}</div>` : ""}</div>
    <div class="pane" data-p="sql">${q.map(e => `
      <p class="lbl">${esc(e.id)} — ${esc(e.purpose || "evidence")}${
        e.error ? ` · <span style="color:var(--critical)">not measured</span>` : ""}</p>
      <pre>${esc(e.error || e.sql_executed || e.sql)}</pre>`).join("")}</div>
    <div class="pane" data-p="profile"><pre>${esc(t.profile_text)}</pre></div>
    <div class="pane" data-p="trace">
      <p class="lbl">Checks passed</p><pre>${esc((t.checks_passed || []).join("\n"))}</pre>
      ${(t.sql_repairs || []).length ? `<p class="lbl">SQL repaired</p>
        <pre>${esc(t.sql_repairs.join("\n"))}</pre>` : ""}
      <p class="lbl">LLM calls</p><pre>${(t.llm_calls || []).map(c =>
        `${c.purpose.padEnd(8)} ${c.ok ? "ok  " : "FAIL"} ${String(c.ms).padStart(6)}ms  ${esc(c.model)}`
      ).join("\n") || "none"}</pre>
      <p class="lbl">Timings</p><pre>${Object.entries(t.timings_ms || {})
        .map(([k, v]) => `${k.padEnd(12)} ${String(v).padStart(6)}ms`).join("\n")}</pre></div>
  </div>`;
}

/* ── charts ──────────────────────────────────────────────────────────────
   PLCharts comes from /shared/charts.js — the classic UI's renderer, served
   rather than copied. It reads --s1..--s7 off :root and writes into #tip,
   both of which this page now provides. */
function chartNode(spec, rows) {
  if (!window.PLCharts || !spec || !rows || !rows.length) return null;
  const t = spec.chart_type;
  if (t === "table" || !t) return null;
  // The renderer formats by kind, and a ratio drawn as a plain number is
  // unreadable. Column names carry the kind here, as they do server-side.
  const kindOf = (c) => /rate|share|pct/.test(c || "") ? "percent"
    : /gmv|amount|value|revenue|sales|fee|cost/.test(c || "") ? "currency" : "number";
  try {
    if (t === "kpi") {
      const v = rows[0][spec.y];
      return PLCharts.kpi(v, kindOf(spec.y), spec.title || "");
    }
    if (t === "line") {
      return PLCharts.line(rows, { x: spec.x, y: spec.y, series: spec.series,
                                   kind: kindOf(spec.y) });
    }
    if (t === "stacked_area_100" && spec.series) {
      return PLCharts.shareArea(rows, { x: spec.x, series: spec.series });
    }
    if (t === "pie") {
      return PLCharts.donut(rows, { x: spec.x, y: spec.y });
    }
    if (t === "bar" || t === "horizontal_bar" || t === "stacked_bar") {
      return PLCharts.bar(rows, { x: spec.x, y: spec.y, kind: kindOf(spec.y),
                                  horizontal: t === "horizontal_bar" });
    }
  } catch (err) {
    // A chart that throws must not take the answer down with it.
    console.warn("chart failed", err);
  }
  return null;
}

/** Draw into `host`; returns false when there was nothing to draw. */
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

/** Fill every <div data-chart> left by the markup, after it is in the DOM. */
function drawCharts(root, turn) {
  root.querySelectorAll("[data-chart]").forEach(host => {
    const key = host.dataset.chart;
    const spec = key === "main" ? turn.chart
      : (turn.evidence_queries || []).find(e => e.id === key)?.chart;
    const rows = key === "main" ? turn.rows
      : (turn.evidence_queries || []).find(e => e.id === key)?.data;
    if (!mountChart(host, spec, rows)) host.remove();
  });
}

const tab = (id, label, on) =>
  `<button class="tab${on ? " on" : ""}" data-t="${id}" type="button">${label}</button>`;

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
  return `<table><thead><tr>${keys.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${keys.map(k =>
      `<td class="${typeof r[k] === "number" ? "num" : ""}">${esc(fmt(r[k]))}</td>`
    ).join("")}</tr>`).join("")}</tbody></table>
    ${total > rows.length ? `<p class="caveat">Showing ${rows.length} of
      ${total.toLocaleString()} rows — the rest are not kept in this browser.</p>` : ""}`;
}

/* ── boot ───────────────────────────────────────────────────────────── */
(async function () {
  await whoami();
  health();
  if (!convs.length) newConv();
  renderConvs();
  renderThread();
})();
