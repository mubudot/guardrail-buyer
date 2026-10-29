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
const LABEL = {PAYER_ACTION_REQUIRED: "AWAITING PAYER", NEEDS_APPROVAL: "NEEDS APPROVAL"};
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
    {field: "created_at", headerName: "Time", width: 90, suppressSizeToFit: true, valueFormatter: time, sort: "desc"},
    {field: "id", headerName: "Order", width: 145, suppressSizeToFit: true},
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 170},
    {field: "quantity", headerName: "Qty", minWidth: 60, maxWidth: 65},
    {field: "amount", headerName: "Amount", minWidth: 90, maxWidth: 95, valueFormatter: (p) => usd(p.value)},
    {headerName: "Policy", minWidth: 110, maxWidth: 115, valueGetter: (p) => p.data.approval_id ? "APPROVED" : (p.data.decision || {}).verdict,
      cellRenderer: statusCell},
    {field: "status", headerName: "Status", minWidth: 140, maxWidth: 150, cellRenderer: statusCell},
    {field: "paypal_order_id", headerName: "PayPal order", minWidth: 160},
    {headerName: "Payment / capture", minWidth: 160, cellRenderer: payCell, sortable: false},
  ],
  rowData: [], overlayNoRowsTemplate: "No orders yet: ask the agent above",
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

function kpi(k, v, extra = "") { return `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div>${extra}</div>`; }

async function refresh() {
  try {
    const [orders, approvals, spend] = await Promise.all([api("/orders"), api("/approvals"), api("/spend")]);
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
  if (h) $("mode").textContent = `PayPal: ${h.paypal} · ${h.model}${h.approver_auth ? " · approver token on" : ""}`;
  const q = new URLSearchParams(location.search);
  if (q.get("paid")) toast(`Payment returned from PayPal: ${q.get("paid")} → ${q.get("status")}`);
  if (q.get("cancelled")) toast(`Buyer cancelled ${q.get("cancelled")}: reserved budget released`);
  if (q.get("paid") || q.get("cancelled")) history.replaceState(null, "", "/");
  refresh();
  setInterval(refresh, 5000);
})();
