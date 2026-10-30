/* GuardRail Buyer dashboard: shared UI helpers (loaded before app.js and policy.js).
   DOM + fetch helpers, money / status formatting, the connection banner, the summary
   cards and the confirm dialog used by the approval flow. Plain JS, no build step. */
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...kids) {  // DOM builder: data is always set as text, never as HTML
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) e.setAttribute(k, v === true ? "" : v);
  for (const k of kids) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);

function toast(msg, kind = "") {
  const t = $("toast"); t.textContent = msg; t.className = `toast ${kind}`.trim(); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 5000);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

/* ---------- formatting ---------- */
const MONEY = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"});
// "" for a missing value; amounts arrive as decimal strings ("25.98") and are shown as $25.98
const usd = (v) => (v == null || v === "" || Number.isNaN(Number(v)) ? "" : MONEY.format(Number(v)));

const LABEL = {PAYER_ACTION_REQUIRED: "AWAITING PAYER", NEEDS_APPROVAL: "NEEDS APPROVAL",
               NEEDS_RECONCILIATION: "UNCONFIRMED", DENIED_BY_POLICY: "DENIED"};
const TONE = {  // pill colour per status / verdict
  ok: ["COMPLETED", "ALLOW", "APPROVED", "RESERVED", "ORDER_CREATED"],
  warn: ["PAYER_ACTION_REQUIRED", "PENDING", "NEEDS_APPROVAL", "APPROVAL_REQUESTED", "CREATING", "CAPTURING",
         "EXECUTING", "NEEDS_RECONCILIATION", "AWAITING_PAYER_APPROVAL"],
  bad: ["FAILED", "CANCELLED", "EXPIRED", "CAPTURE_FAILED", "REJECTED", "DENY", "DENIED", "DENIED_BY_POLICY",
        "QUOTE_CHANGED", "MOCK_RESET"],
};
const toneOf = (v) => Object.keys(TONE).find((k) => TONE[k].includes(v)) || "neutral";
const statusLabel = (v) => LABEL[v] || String(v).replaceAll("_", " ");
// AG Grid cell renderer (returns HTML): every value is escaped
const statusCell = (p) => p.value
  ? `<span class="st ${escapeHtml(p.value)} tone-${toneOf(p.value)}">${escapeHtml(statusLabel(p.value))}</span>` : "";
const statusPill = (v) => el("span", {class: `st ${v} tone-${toneOf(v)}`}, statusLabel(v));

// PayPal checkout links come from the server; only http(s) URLs are ever opened or linked
function safeUrl(u) {
  try { const x = new URL(String(u), location.href); return ["https:", "http:"].includes(x.protocol) ? x.href : null; }
  catch (e) { return null; }
}

/* ---------- connection banner (error state) ---------- */
// Failures are tracked per resource ("core" = orders/approvals/spend, "audit", "health"), so a
// good poll of one resource never hides the error of another.
const conn = {
  down: new Map(), loadedOnce: false,
  ok(key) { this.down.delete(key); if (key === "core") this.loadedOnce = true; this.render(); },
  fail(key, err) { this.down.set(key, err && err.message ? err.message : String(err || "")); this.render(); },
  render() {
    $("conn-banner").hidden = this.down.size === 0;
    $("conn-stale").textContent = this.loadedOnce
      ? "Showing the last data loaded; retrying every 5 seconds."
      : "Nothing has loaded yet; retrying every 5 seconds.";
    $("conn-detail").textContent = [...this.down].map(([k, m]) => `${k}: ${m}`).join(" · ");
  },
};

/* ---------- summary cards ---------- */
const pct = (part, whole) => (whole > 0 ? Math.max(0, Math.min(100, (part / whole) * 100)) : 0);
// The cards are built once and then updated in place on every poll, so keyboard focus on a card
// survives the 5-second refresh.
const KPI_DEFS = [
  {key: "spend", label: "Spend today vs daily cap", wide: true},
  {key: "pending", label: "Pending approvals", action: {hint: "Show the approvals",
    run: () => { $("tab-ops").click(); $("approvals-card").scrollIntoView({behavior: "smooth", block: "start"}); }}},
  {key: "blocked", label: "Blocked attempts today", action: {hint: "Open the audit log", run: () => $("tab-audit").click()}},
  {key: "auto", label: "Auto-approved today"},
];
function ensureKpis() {
  const root = $("kpis");
  if (root.querySelector("[data-kpi]")) return;
  root.replaceChildren(...KPI_DEFS.map((d) => {
    const card = d.action ? el("button", {type: "button", class: "kpi", "data-kpi": d.key}) : el("div", {class: "kpi", "data-kpi": d.key});
    if (d.wide) card.classList.add("wide");
    card.append(el("div", {class: "k"}, d.label), el("div", {class: "v"}), el("div", {class: "x"}), el("div", {class: "s"}));
    if (d.action) card.addEventListener("click", d.action.run);
    return card;
  }));
}
function setKpi(key, {value, sub, tone = "", extra = null}) {
  const card = $("kpis").querySelector(`[data-kpi="${key}"]`), def = KPI_DEFS.find((d) => d.key === key);
  card.className = ["kpi", def.wide ? "wide" : "", ...tone.split(" ")].filter(Boolean).join(" ");
  card.querySelector(".v").replaceChildren(value);
  card.querySelector(".x").replaceChildren(...(extra ? [extra] : []));
  card.querySelector(".s").replaceChildren(sub);
  if (def.action) card.setAttribute("aria-label", `${def.label}: ${card.querySelector(".v").textContent}. ${def.action.hint}`);
}
function spendBar(captured, reserved, cap) {
  const bar = el("div", {class: "bar", role: "img",
    "aria-label": `${usd(captured)} captured and ${usd(reserved)} reserved of a ${usd(cap)} daily cap`});
  bar.append(el("i", {class: "captured", style: `width:${pct(captured, cap)}%`}),
             el("i", {class: "reserved", style: `width:${Math.min(pct(reserved, cap), 100 - pct(captured, cap))}%`}));
  return bar;
}
// spend = GET /spend, pending = number of PENDING approvals
function renderKpis(spend, pending) {
  ensureKpis();
  const cap = Number(spend.daily_cap), c = Number(spend.captured), r = Number(spend.reserved);
  const used = c + r, share = pct(used, cap), unconfirmed = Number(spend.needs_reconciliation || 0);
  setKpi("spend", {
    value: el("span", {}, usd(used), el("small", {}, ` / ${usd(cap)}`)),
    tone: unconfirmed || share >= 100 ? "bad" : share >= 80 ? "warn" : "",
    extra: spendBar(c, r, cap),
    sub: el("span", {class: "legend-line"},
      el("span", {class: "li"}, el("i", {class: "dot captured"}), ` captured ${usd(c)}`),
      // reserved = orders awaiting the buyer on PayPal, or payments the server could not confirm yet
      el("span", {class: "li"}, el("i", {class: "dot reserved"}), ` reserved ${usd(r)} (awaiting payer or confirmation)`),
      unconfirmed ? el("b", {class: "unconfirmed"}, `${unconfirmed} payment${unconfirmed > 1 ? "s" : ""} unconfirmed: check the audit log`) : null,
      el("b", {class: "headroom"}, `${usd(spend.remaining)} headroom left today`))});
  setKpi("pending", {value: String(pending), tone: pending ? "warn attention" : "",
    sub: pending ? "Over a cap: waiting for a human" : "Nothing waiting for you"});
  const blocked = spend.blocked_today ?? 0, auto = spend.allowed_today ?? 0;
  setKpi("blocked", {value: String(blocked), tone: blocked ? "bad" : "", sub: "Deny rules: never escalated, never paid"});
  setKpi("auto", {value: String(auto), tone: auto ? "ok" : "",
    sub: `PayPal orders within the ${usd(spend.per_order_cap)} cap · hard max ${usd(spend.hard_max_per_order)}`});
}

/* ---------- confirm dialog (approval flow) ---------- */
// confirmDialog({title, intro, rows: [[label, value]], notes: [..], confirmLabel, tone, needsToken, returnFocus})
// resolves true on confirm, false on cancel / Escape / backdrop / a newer dialog replacing it.
// While open the rest of the page is inert; on close focus returns to returnFocus() (a fresh lookup,
// since a poll may have re-rendered the opener) or to the element that opened it.
const dialog = {settle: null};
function confirmDialog({title, intro = "", rows = [], notes = [], confirmLabel = "Confirm", tone = "primary",
                        needsToken = false, returnFocus = null}) {
  if (dialog.settle) dialog.settle(false);  // never leave an earlier promise pending
  const root = $("modal"), box = $("modal-box");
  $("modal-title").textContent = title;
  $("modal-intro").textContent = intro;
  $("modal-rows").replaceChildren(...rows.filter((r) => r[1] != null && r[1] !== "")
    .map(([k, v]) => el("div", {class: "mrow"}, el("dt", {}, k), el("dd", {}, v))));
  $("modal-notes").replaceChildren(...notes.map((n) => el("li", {}, n)));
  $("modal-notes").hidden = !notes.length;
  const tokenRow = $("modal-token-row"), tokenBox = $("modal-token");
  tokenRow.hidden = !needsToken; tokenBox.value = ""; tokenBox.removeAttribute("aria-invalid");
  const ok = $("modal-confirm"), cancel = $("modal-cancel");
  ok.textContent = confirmLabel; ok.className = `btn ${tone}`;
  const opener = document.activeElement;
  const background = [...document.body.children].filter((n) => n !== root && n.tagName !== "SCRIPT");
  root.hidden = false; document.body.classList.add("modal-open");
  for (const n of background) n.inert = true;
  (needsToken ? tokenBox : ok).focus();
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return; done = true;
      dialog.settle = null;
      root.hidden = true; document.body.classList.remove("modal-open");
      for (const n of background) n.inert = false;
      root.removeEventListener("keydown", onKey); root.removeEventListener("click", onBackdrop);
      ok.removeEventListener("click", onOk); cancel.removeEventListener("click", onCancel);
      try {
        const target = (returnFocus && returnFocus()) || (opener && document.contains(opener) ? opener : null);
        if (target && target.focus) target.focus();
      } finally { resolve(v); }  // a focus problem must never leave the caller waiting
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); finish(false); return; }
      if (e.key !== "Tab") return;
      const f = [...box.querySelectorAll("button, input")].filter((x) => !x.disabled && !x.closest("[hidden]"));
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    const onBackdrop = (e) => { if (e.target === root) finish(false); };
    const onCancel = () => finish(false);
    const onOk = () => {
      if (needsToken) {
        if (!tokenBox.value) { tokenBox.setAttribute("aria-invalid", "true"); tokenBox.focus(); return; }
        const main = $("token"); main.value = tokenBox.value;
        main.dispatchEvent(new Event("input")); main.dispatchEvent(new Event("change"));
      }
      finish(true);
    };
    dialog.settle = finish;
    root.addEventListener("keydown", onKey); root.addEventListener("click", onBackdrop);
    ok.addEventListener("click", onOk); cancel.addEventListener("click", onCancel);
  });
}

/* ---------- agent answer ---------- */
const ORDER_TOOLS = ["create_order", "request_approval"];
// One line per purchase outcome (the last order/approval call per product), then the full tool
// trace in a collapsed disclosure, so the answer does not push the dashboard out of view.
function renderAgentAnswer(r) {
  const tools = r.transcript.filter((x) => x.role === "tool");
  const last = new Map();
  for (const s of tools) if (ORDER_TOOLS.includes(s.name) && s.input && s.input.product_id) last.set(s.input.product_id, s);
  const names = new Map();  // product id -> name, from catalog searches and policy checks
  for (const s of tools) {
    const o = s.output || {};
    for (const p of o.products || []) if (p.id && p.name) names.set(p.id, p.name);
    if (o.product_id && o.product_name) names.set(o.product_id, o.product_name);
  }
  const outcomes = el("ul", {class: "outcomes"});
  for (const s of last.values()) {
    const o = s.output || {}, ord = o.order || {};
    const name = ord.product_name || o.product_name || names.get(s.input.product_id) || s.input.product_id;
    const qty = s.input.quantity || ord.quantity || 1;
    const amount = ord.amount || o.amount;
    outcomes.append(el("li", {}, statusPill(o.result || o.verdict || "UNKNOWN"),
      el("span", {class: "what"}, `${qty} × ${name}`), amount ? el("span", {class: "amt"}, usd(amount)) : null,
      (o.reasons || []).length && o.result !== "AWAITING_PAYER_APPROVAL" && o.result !== "ORDER_PLACED"
        ? el("span", {class: "why"}, o.reasons.join("; ")) : null));
  }
  const trace = el("details", {class: "trace"}, el("summary", {}, `View all ${tools.length} tool calls`), renderAgentSteps(r.transcript));
  const kids = [el("div", {class: "answer-h"}, "Agent's answer"), el("div", {class: "final"}, r.final_text)];
  if (last.size) kids.push(el("div", {class: "answer-h"}, "Outcomes (every order went through the policy gate)"), outcomes);
  kids.push(trace);
  return kids;
}
// One line per tool call: tool name, its arguments, and the verdict / result as a pill.
function renderAgentSteps(transcript) {
  const list = el("ol", {class: "steps"});
  for (const s of transcript.filter((x) => x.role === "tool")) {
    const o = s.output || {};
    const tag = o.result || o.verdict || "";
    const args = Object.values(s.input || {}).join(", ");
    const short = args.length > 72 ? args.slice(0, 70) + "…" : args;  // full text in the tooltip
    const n = o.products ? o.products.length : 0;
    list.append(el("li", {},
      el("code", {class: "tool"}, s.name), el("span", {class: "args", title: args.length > 72 ? args : null}, `(${short})`),
      o.products ? el("span", {class: "st tone-neutral"}, `${n} product${n === 1 ? "" : "s"}`) : tag ? statusPill(tag) : null));
  }
  return list;
}
