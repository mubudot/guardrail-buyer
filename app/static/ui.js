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

/* ---------- connection banner (error state) ---------- */
const conn = {
  failures: 0,
  ok() { this.failures = 0; $("conn-banner").hidden = true; },
  fail(err) {
    this.failures += 1;
    const b = $("conn-banner");
    $("conn-detail").textContent = err && err.message ? err.message : String(err || "");
    b.hidden = false;
  },
};

/* ---------- summary cards ---------- */
const pct = (part, whole) => (whole > 0 ? Math.max(0, Math.min(100, (part / whole) * 100)) : 0);
function kpiCard({key, label, value, sub, tone = "", extra = null, action = null}) {
  const attrs = {class: `kpi ${tone}`.trim(), "data-kpi": key};
  const card = action ? el("button", {...attrs, type: "button", "aria-label": `${label}: ${value}. ${action.hint}`}) : el("div", attrs);
  card.append(el("div", {class: "k"}, label), el("div", {class: "v"}, value));
  if (extra) card.append(extra);
  if (sub) card.append(el("div", {class: "s"}, sub));
  if (action) card.addEventListener("click", action.run);
  return card;
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
  const cap = Number(spend.daily_cap), c = Number(spend.captured), r = Number(spend.reserved);
  const used = c + r, share = pct(used, cap);
  const spendCard = kpiCard({key: "spend", label: "Spend today vs daily cap",
    value: el("span", {}, usd(used), el("small", {}, ` / ${usd(cap)}`)),
    tone: share >= 100 ? "bad" : share >= 80 ? "warn" : "",
    extra: spendBar(c, r, cap),
    sub: el("span", {class: "legend-line"},
      el("i", {class: "dot captured"}), `captured ${usd(c)}`, el("i", {class: "dot reserved"}), `reserved for unpaid orders ${usd(r)}`,
      el("b", {class: "headroom"}, `${usd(spend.remaining)} headroom left today`))});
  spendCard.classList.add("wide");
  const blocked = spend.blocked_today ?? 0, auto = spend.allowed_today ?? 0;
  $("kpis").replaceChildren(
    spendCard,
    kpiCard({key: "pending", label: "Pending approvals", value: String(pending),
      tone: pending ? "warn attention" : "",
      sub: pending ? "Over a cap: waiting for a human" : "Nothing waiting for you",
      action: {hint: "Show the approvals", run: () => { $("tab-ops").click(); $("approvals-card").scrollIntoView({behavior: "smooth", block: "start"}); }}}),
    kpiCard({key: "blocked", label: "Blocked attempts today", value: String(blocked), tone: blocked ? "bad" : "",
      sub: "Deny rules: never escalated, never paid",
      action: {hint: "Open the audit log", run: () => $("tab-audit").click()}}),
    kpiCard({key: "auto", label: "Auto-approved today", value: String(auto), tone: auto ? "ok" : "",
      sub: `Within the ${usd(spend.per_order_cap)} per-order cap · hard max ${usd(spend.hard_max_per_order)}`}),
  );
}

/* ---------- confirm dialog (approval flow) ---------- */
// confirmDialog({title, intro, rows: [[label, value]], notes: [..], confirmLabel, tone, needsToken})
// resolves true on confirm, false on cancel / Escape / backdrop. Focus is trapped while open
// and returned to the element that opened it.
function confirmDialog({title, intro = "", rows = [], notes = [], confirmLabel = "Confirm", tone = "primary", needsToken = false}) {
  const root = $("modal"), box = $("modal-box");
  $("modal-title").textContent = title;
  $("modal-intro").textContent = intro;
  $("modal-rows").replaceChildren(...rows.filter((r) => r[1] != null && r[1] !== "")
    .map(([k, v]) => el("div", {class: "mrow"}, el("dt", {}, k), el("dd", {}, v))));
  $("modal-notes").replaceChildren(...notes.map((n) => el("li", {}, n)));
  $("modal-notes").hidden = !notes.length;
  const tokenRow = $("modal-token-row"), tokenBox = $("modal-token");
  tokenRow.hidden = !needsToken; tokenBox.value = "";
  const ok = $("modal-confirm");
  ok.textContent = confirmLabel; ok.className = `btn ${tone}`;
  const opener = document.activeElement;
  root.hidden = false; document.body.classList.add("modal-open");
  (needsToken ? tokenBox : ok).focus();
  return new Promise((resolve) => {
    const done = (v) => {
      root.hidden = true; document.body.classList.remove("modal-open");
      root.removeEventListener("keydown", onKey); root.removeEventListener("click", onBackdrop);
      ok.onclick = null; $("modal-cancel").onclick = null;
      if (opener && opener.focus && document.contains(opener)) opener.focus();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done(false); return; }
      if (e.key !== "Tab") return;
      const f = [...box.querySelectorAll("button, input")].filter((x) => !x.disabled && !x.closest("[hidden]"));
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    const onBackdrop = (e) => { if (e.target === root) done(false); };
    root.addEventListener("keydown", onKey); root.addEventListener("click", onBackdrop);
    ok.onclick = () => {
      if (needsToken) {
        if (!tokenBox.value) { tokenBox.focus(); tokenBox.setAttribute("aria-invalid", "true"); return; }
        tokenBox.removeAttribute("aria-invalid");
        const main = $("token"); main.value = tokenBox.value;
        main.dispatchEvent(new Event("input")); main.dispatchEvent(new Event("change"));
      }
      done(true);
    };
    $("modal-cancel").onclick = () => done(false);
  });
}

/* ---------- agent answer ---------- */
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
