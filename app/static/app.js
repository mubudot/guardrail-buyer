/* GuardRail Buyer dashboard: AG Grid Community, plain JS, talks to the FastAPI REST API. */
const $ = (id) => document.getElementById(id);
const tokenInput = $("token");
tokenInput.value = localStorage.getItem("approverToken") || "";
tokenInput.addEventListener("change", () => localStorage.setItem("approverToken", tokenInput.value));

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4500);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
const usd = (v) => (v == null ? "" : "$" + Number(v).toFixed(2));
const LABEL = {PAYER_ACTION_REQUIRED: "AWAITING PAYER", NEEDS_APPROVAL: "NEEDS APPROVAL", NEEDS_RECONCILIATION: "UNCONFIRMED"};
const statusCell = (p) => p.value ? `<span class="st ${p.value}">${LABEL[p.value] || p.value.replaceAll("_", " ")}</span>` : "";
const time = (p) => p.value ? new Date(p.value).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "";

/* ---------- approvals ---------- */
function approvalButtons(p) {
  const wrap = document.createElement("span");
  if (p.data.status !== "PENDING") return wrap;
  for (const [label, cls, action] of [["Approve", "ok", "approve"], ["Reject", "bad", "reject"]]) {
    const b = document.createElement("button");
    b.className = `btn grid-btn ${cls}`; b.textContent = label;
    b.onclick = () => decide(p.data.id, action, b);
    wrap.appendChild(b);
  }
  return wrap;
}
async function decide(id, action, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/approvals/${id}/${action}`, {method: "POST",
      headers: {"X-Approver-Token": tokenInput.value}});
    const o = r.order;
    if (o && o.approve_url) toast(`${id} approved → order ${o.id} awaits payer approval on PayPal`);
    else toast(`${id}: ${r.result}${o ? " · " + o.id : ""}`);
    if (o && o.approve_url) window.open(o.approve_url, "_blank");
  } catch (e) { toast(`${action} failed: ${e.message}`); }
  refresh();
}
const approvalsGrid = agGrid.createGrid($("approvals"), {
  columnDefs: [
    {field: "id", headerName: "Approval", width: 135, suppressSizeToFit: true},
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 160},
    {field: "quantity", headerName: "Qty", minWidth: 60, maxWidth: 65},
    {field: "amount", headerName: "Amount", minWidth: 90, maxWidth: 95, valueFormatter: (p) => usd(p.value)},
    {field: "justification", headerName: "Agent's justification", flex: 1.4, minWidth: 180, tooltipField: "justification"},
    {field: "reasons", headerName: "Why it needs you", flex: 1.4, minWidth: 180,
      valueFormatter: (p) => (p.value || []).join("; "), tooltipValueGetter: (p) => (p.value || []).join("\n")},
    {field: "status", minWidth: 95, maxWidth: 100, cellRenderer: statusCell},
    {headerName: "Decision", minWidth: 175, maxWidth: 180, cellRenderer: approvalButtons, sortable: false},
  ],
  rowData: [], overlayNoRowsTemplate: "Nothing waiting for you", domLayout: "autoHeight",
  getRowId: (p) => p.data.id,
  rowHeight: 44, tooltipShowDelay: 200, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true, sortable: true},
});

/* ---------- orders ---------- */
function payCell(p) {
  if (p.data.status === "PAYER_ACTION_REQUIRED" && p.data.approve_url) {
    const a = document.createElement("a");
    a.className = "btn grid-btn primary"; a.textContent = "Pay with PayPal";
    a.href = p.data.approve_url; a.target = "_blank"; a.rel = "noopener";
    return a;
  }
  return p.data.capture_id || "";
}
const ordersGrid = agGrid.createGrid($("orders"), {
  columnDefs: [
    {field: "created_at", headerName: "Time", width: 100, suppressSizeToFit: true, valueFormatter: time, sort: "desc"},
    {field: "id", headerName: "Order", width: 160, suppressSizeToFit: true},
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 150},
    {field: "quantity", headerName: "Qty", minWidth: 60, maxWidth: 65},
    {field: "amount", headerName: "Amount", minWidth: 90, maxWidth: 95, valueFormatter: (p) => usd(p.value)},
    {headerName: "Policy", minWidth: 110, maxWidth: 115, valueGetter: (p) => p.data.approval_id ? "APPROVED" : (p.data.decision || {}).verdict,
      cellRenderer: statusCell},
    {field: "status", headerName: "Status", minWidth: 140, maxWidth: 150, cellRenderer: statusCell},
    {field: "paypal_order_id", headerName: "PayPal order", width: 195, suppressSizeToFit: true},
    {headerName: "Payment / capture", minWidth: 160, cellRenderer: payCell, sortable: false},
  ],
  rowData: [], overlayNoRowsTemplate: "No orders yet: ask the agent above",
  getRowId: (p) => p.data.id,
  rowHeight: 44, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true, sortable: true},
});

/* ---------- spend vs caps ---------- */
const spendGrid = agGrid.createGrid($("spend"), {
  columnDefs: [
    {field: "rule", headerName: "Guardrail", flex: 1.3},
    {field: "limit", headerName: "Limit", flex: 0.7, valueFormatter: (p) => usd(p.value)},
    {field: "used", headerName: "Used today", flex: 0.7, valueFormatter: (p) => p.value == null ? "–" : usd(p.value)},
    {field: "remaining", headerName: "Headroom", flex: 0.7, valueFormatter: (p) => p.value == null ? "–" : usd(p.value)},
    {field: "effect", headerName: "Above the limit", flex: 1.2, cellRenderer: statusCell},
  ],
  rowData: [], domLayout: "autoHeight", rowHeight: 40, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true},
});


/* ---------- audit: every guardrail decision (AG Grid Community features only) ---------- */
const STAGE = {check: "policy check", order: "create order", approval_request: "approval request",
               approval_execute: "approval executed"};
const RISK_RULES = {
  "risk-DENY": (p) => p.data && p.data.risk === "DENY",
  "risk-NEEDS_APPROVAL": (p) => p.data && p.data.risk === "NEEDS_APPROVAL",
  "risk-NEEDS_RECONCILIATION": (p) => p.data && p.data.risk === "NEEDS_RECONCILIATION",
};
const auditGrid = agGrid.createGrid($("audit"), {
  columnDefs: [
    {field: "created_at", headerName: "Timestamp", width: 170, sort: "desc", filter: "agDateColumnFilter",
      valueFormatter: (p) => p.value ? new Date(p.value).toLocaleString() : "",
      filterParams: {comparator: (day, v) => { const d = new Date(v); d.setHours(0, 0, 0, 0);
        return d < day ? -1 : d > day ? 1 : 0; }}},
    {field: "verdict", headerName: "Decision", width: 140, filter: "agTextColumnFilter",
      valueFormatter: (p) => ({ALLOW: "allow", NEEDS_APPROVAL: "approval", DENY: "deny"})[p.value] || p.value,
      cellClassRules: {"risk-DENY": (p) => p.value === "DENY", "risk-NEEDS_APPROVAL": (p) => p.value === "NEEDS_APPROVAL"}},
    {field: "rule_hit", headerName: "Rule hit", width: 190, filter: "agTextColumnFilter", tooltipValueGetter:
      (p) => (p.data.reasons || []).join("\n")},
    {field: "amount", headerName: "Amount", width: 110, filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data ? Number(p.data.amount) : null, valueFormatter: (p) => usd(p.value)},
    {field: "merchant", headerName: "Merchant", width: 180, filter: "agTextColumnFilter"},
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 160, filter: "agTextColumnFilter",
      valueGetter: (p) => p.data ? `${p.data.quantity} × ${p.data.product_name}` : ""},
    {field: "stage", headerName: "Stage", width: 150, filter: "agTextColumnFilter",
      valueFormatter: (p) => STAGE[p.value] || p.value},
    {field: "outcome", headerName: "Outcome", width: 160, filter: "agTextColumnFilter", cellRenderer: statusCell},
    {field: "order_status", headerName: "Payment status", width: 165, filter: "agTextColumnFilter",
      cellRenderer: statusCell, cellClassRules: {"risk-NEEDS_RECONCILIATION": (p) => p.value === "NEEDS_RECONCILIATION"}},
    {field: "request_id", headerName: "Request id", width: 160, filter: "agTextColumnFilter"},
    {field: "id", headerName: "Decision id", width: 150, filter: "agTextColumnFilter", hide: false},
  ],
  rowData: [], getRowId: (p) => p.data.id, rowClassRules: RISK_RULES,
  defaultColDef: {resizable: true, sortable: true, floatingFilter: true},
  rowSelection: {mode: "singleRow", checkboxes: false, enableClickSelection: true},
  tooltipShowDelay: 200, rowHeight: 38, overlayNoRowsTemplate: "No guardrail decisions yet",
  isExternalFilterPresent: () => $("audit-hide-checks").checked,
  doesExternalFilterPass: (node) => node.data.stage !== "check",
  onRowClicked: (e) => showDecision(e.data.id),
});
$("audit-search").addEventListener("input", (e) => auditGrid.setGridOption("quickFilterText", e.target.value));
$("audit-hide-checks").addEventListener("change", () => auditGrid.onFilterChanged());
$("audit-csv").addEventListener("click", () => auditGrid.exportDataAsCsv({
  fileName: `guardrail-audit-${new Date().toISOString().slice(0, 10)}.csv`,
  processCellCallback: (p) => p.column.getColId() === "created_at" ? p.value : (p.formatValue ? p.formatValue(p.value) : p.value),
}));
$("detail-close").addEventListener("click", () => { $("audit-detail").hidden = true; auditGrid.deselectAll(); });

function el(tag, attrs = {}, ...kids) {  // DOM builder: data is always set as text, never as HTML
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k == null ? "" : String(k)));
  return e;
}
const kv = (rows) => el("table", {}, ...rows.filter((r) => r[1] != null && r[1] !== "")
  .map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, v))));

async function showDecision(id) {
  const box = $("audit-detail"), body = $("detail-body");
  box.hidden = false; body.textContent = "Loading…"; $("detail-title").textContent = id;
  let d;
  try { d = await api(`/decisions/${encodeURIComponent(id)}`); }
  catch (e) { body.textContent = "Could not load: " + e.message; return; }
  const dec = d.decision, q = d.quote;
  body.textContent = "";
  body.append(el("h4", {}, "Decision"), kv([
    ["Verdict", dec.verdict], ["Stage", STAGE[dec.stage] || dec.stage], ["Outcome", dec.outcome],
    ["Rule hit", dec.rule_hit], ["When", new Date(dec.created_at).toLocaleString()],
    ["Request id", dec.request_id], ["Spent before", usd(dec.spent_before)],
    ["Reasons", (dec.reasons || []).join("; ")]]));
  body.append(el("h4", {}, "Quote"), kv([
    ["Product", `${q.product_id} · ${dec.product_name}`], ["Quantity", q.quantity],
    ["Amount", `${usd(q.amount)} ${q.currency}`], ["Merchant", q.merchant], ["Category", q.category]]));
  body.append(el("h4", {}, "Rules evaluated"), el("table", {}, ...(d.rules || []).map((r) =>
    el("tr", {}, el("td", {}, r.rule), el("td", {}, el("span", {class: `rule-${r.outcome}`}, r.outcome), " ", el("span", {class: "note"}, r.detail))))));
  if (d.approval) body.append(el("h4", {}, "Approval"), kv([
    ["Approval", d.approval.id], ["Status", d.approval.status], ["Justification", d.approval.justification],
    ["Decided", d.approval.decided_at ? new Date(d.approval.decided_at).toLocaleString() : "pending"]]));
  body.append(el("h4", {}, "Payment timeline"));
  if (!d.order) body.append(el("p", {class: "note"}, "No order was created by this decision."));
  else {
    body.append(kv([["Order", d.order.id], ["Current status", d.order.status],
                    ["PayPal order", d.order.paypal_order_id], ["Capture", d.order.capture_id]]));
    body.append(el("ol", {}, ...d.timeline.map((t) => el("li", {},
      el("span", {class: `st ${t.to_status}`}, (t.from_status ? `${t.from_status} → ` : "") + t.to_status),
      el("div", {class: "note"}, `${t.at ? new Date(t.at).toLocaleString() : ""}${t.note ? " · " + t.note : ""}`)))));
  }
}

/* ---------- tabs ---------- */
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll(".tab")) {
      const on = t === tab; t.classList.toggle("active", on); t.setAttribute("aria-selected", on);
      $(t.dataset.view).hidden = !on;
    }
    if (tab.id === "tab-audit") refresh();
  });
}

function kpi(k, v, extra = "") { return `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div>${extra}</div>`; }

async function refresh() {
  try {
    const [orders, approvals, spend, decisions] = await Promise.all([api("/orders"), api("/approvals"), api("/spend"),
                                                                     api("/decisions")]);
    auditGrid.setGridOption("rowData", decisions);  // getRowId -> delta update, selection kept
    $("audit-count").textContent = `(${decisions.length})`;
    ordersGrid.setGridOption("rowData", orders);
    approvalsGrid.setGridOption("rowData", approvals.filter((a) => a.status === "PENDING"));
    spendGrid.setGridOption("rowData", [
      {rule: "Daily cap (captured + reserved)", limit: spend.daily_cap, used: spend.total, remaining: spend.remaining, effect: "NEEDS_APPROVAL"},
      {rule: "Per-order cap", limit: spend.per_order_cap, used: null, remaining: null, effect: "NEEDS_APPROVAL"},
      {rule: "Hard max per order", limit: spend.hard_max_per_order, used: null, remaining: null, effect: "DENY"},
      {rule: "Merchant / category allowlist", limit: null, used: null, remaining: null, effect: "DENY"},
    ]);
    $("spend-day").textContent = `Day ${spend.day} (policy timezone) · ${spend.currency}`;
    const cap = Number(spend.daily_cap), c = Number(spend.captured), r = Number(spend.reserved);
    const bar = `<div class="bar"><i style="width:${Math.min(100, c / cap * 100)}%;background:#138a4c"></i>` +
                `<i style="width:${Math.min(100, r / cap * 100)}%;background:#e0a33a"></i></div>`;
    const pending = approvals.filter((a) => a.status === "PENDING").length;
    $("kpis").innerHTML =
      kpi("Spent today (captured)", usd(spend.captured), bar) +
      kpi("Reserved, awaiting payer", usd(spend.reserved)) +
      kpi("Daily headroom", usd(spend.remaining) + `<span class="hint"> / ${usd(spend.daily_cap)}</span>`) +
      kpi("Pending approvals", pending);
  } catch (e) { toast("refresh failed: " + e.message); }
}

$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("ask").value.trim(); if (!text) return;
  const btn = $("ask-btn"); btn.disabled = true; btn.textContent = "Working…";
  const ans = $("answer"); ans.hidden = false; ans.textContent = "The agent is searching, checking policy and paying…";
  try {
    const r = await api("/agent/request", {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({request: text})});
    const steps = r.transcript.filter((s) => s.role === "tool").map((s) => {
      const o = s.output || {};
      const tag = o.result || o.verdict || (o.products ? `${o.products.length} products` : "");
      return `→ ${s.name}(${Object.values(s.input || {}).join(", ")}) ${tag}`;
    });
    ans.innerHTML = "";
    ans.appendChild(document.createTextNode(r.final_text));
    const st = document.createElement("div"); st.className = "steps"; st.textContent = steps.join("\n");
    ans.appendChild(st);
  } catch (e) { ans.textContent = "Agent error: " + e.message; }
  btn.disabled = false; btn.textContent = "Send";
  refresh();
});
$("ask").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("ask-form").requestSubmit(); });

(async () => {
  const h = await api("/health").catch(() => null);
  if (h) {
    $("mode").textContent = `PayPal: ${h.paypal} · ${h.agent_mode === "offline" ? "OFFLINE rule-based agent (no AI)" : h.model}${h.approver_auth ? " · approver token on" : (h.approver_mode === "disabled" ? " · approvals disabled (no APPROVER_TOKEN)" : " · approvals OPEN (DEMO_MODE)")}`;
    if (h.agent_mode === "offline") $("mode").classList.add("offline");
  }
  const q = new URLSearchParams(location.search);
  if (q.get("paid")) toast(q.get("status") === "PAYER_ACTION_REQUIRED"
    ? `${q.get("paid")}: PayPal says the buyer has not approved yet, nothing captured`
    : `Payment returned from PayPal: ${q.get("paid")} → ${q.get("status")}`);
  if (q.get("cancelled")) toast(`Buyer cancelled ${q.get("cancelled")}: reserved budget released`);
  if (q.get("paid") || q.get("cancelled")) history.replaceState(null, "", "/");
  refresh();
  setInterval(refresh, 5000);
})();
