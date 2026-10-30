/* Policy & spend tab: editable policy grids (AG Grid Community cell editing) and a spend-over-time
   chart (AG Charts Community, loaded lazily on first open). Uses $, api, toast, usd, tokenInput and
   refresh from app.js. Every edit is validated again by the server (PUT /policy, owner token). */
(() => {
  const CAPS = [
    {key: "per_order_cap", label: "Per-order cap", effect: "above → NEEDS_APPROVAL"},
    {key: "daily_cap", label: "Daily cap (captured + reserved)", effect: "above → NEEDS_APPROVAL"},
    {key: "hard_max_per_order", label: "Hard max per order", effect: "above → DENY (never escalated)"},
  ];
  const LISTS = ["allowed_categories", "allowed_merchants", "blocked_merchants"];
  let server = null, draft = null, revision = null, catalog = [], chart = null, chartLoading = null, loadedOnce = false;
  const ownerInput = $("owner-token");
  ownerInput.value = sessionStorage.getItem("ownerToken") || "";
  ownerInput.addEventListener("change", () => sessionStorage.setItem("ownerToken", ownerInput.value));
  const visible = () => !$("view-policy").hidden && !document.hidden;
  const clone = (p) => JSON.parse(JSON.stringify(p));
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const lc = (s) => String(s).trim().toLowerCase();

  function changedFields() {
    const out = {};
    for (const c of CAPS) if (Number(draft[c.key]).toFixed(2) !== Number(server[c.key]).toFixed(2)) out[c.key] = draft[c.key];
    for (const k of LISTS) if (!same([...draft[k]].sort(), [...server[k]].sort())) out[k] = draft[k];
    return out;
  }
  function renderDirty() {
    const n = Object.keys(changedFields()).length;
    $("policy-dirty").textContent = n ? `${n} unsaved change${n > 1 ? "s" : ""}` : "";
    $("policy-save").disabled = $("policy-discard").disabled = n === 0;
    for (const g of [capsGrid, merchantsGrid, categoriesGrid]) g.refreshCells({force: true});
  }

  /* ---------- caps ---------- */
  const capsGrid = agGrid.createGrid($("policy-caps"), {
    columnDefs: [
      {field: "label", headerName: "Rule", flex: 1.3, wrapText: true, autoHeight: true},
      {colId: "value", headerName: "Limit (USD)", flex: 1, editable: true, cellDataType: false,
       cellEditor: "agTextCellEditor", cellEditorParams: {maxLength: 10},
       valueGetter: (p) => draft ? draft[p.data.key] : null,
       valueSetter: (p) => {
         const s = String(p.newValue ?? "").trim().replace(/^\$/, "");
         if (!/^[0-9]{1,7}(\.[0-9]{1,2})?$/.test(s) || Number(s) <= 0) {
           toast("a cap must be a positive amount with at most 2 decimals, e.g. 60 or 49.99"); return false;
         }
         draft[p.data.key] = Number(s).toFixed(2); renderDirty(); return true;
       },
       valueFormatter: (p) => usd(p.value),
       cellClassRules: {"cell-dirty": (p) => server && Number(draft[p.data.key]).toFixed(2) !== Number(server[p.data.key]).toFixed(2)}},
      {field: "effect", headerName: "Effect", flex: 1.5, wrapText: true, autoHeight: true},
    ],
    rowData: CAPS, domLayout: "autoHeight", getRowId: (p) => p.data.key,
    singleClickEdit: false, stopEditingWhenCellsLoseFocus: true,
    defaultColDef: {resizable: true, sortable: false},
  });

  /* ---------- merchants / categories (select editors) ---------- */
  function listGrid(el, kind) {
    const values = kind === "merchant" ? ["allowed", "blocked", "not listed"] : ["allowed", "not allowed"];
    return agGrid.createGrid(el, {
      columnDefs: [
        {field: "name", headerName: kind === "merchant" ? "Merchant" : "Category", flex: 1.6, filter: true},
        {field: "products", headerName: "Items", flex: 0.6, minWidth: 82, type: "numericColumn"},
        {headerName: "Status", colId: "status", flex: 1, editable: true, singleClickEdit: true,
         cellEditor: "agSelectCellEditor", cellEditorParams: {values},
         valueGetter: (p) => statusOf(kind, p.data.name),
         valueSetter: (p) => { setStatus(kind, p.data.name, p.newValue); renderDirty(); return true; },
         cellRenderer: (p) => `<span class="st ${p.value === "allowed" ? "ALLOW" : p.value === "blocked" || p.value === "not allowed" ? "DENY" : "PENDING"}">${p.value}</span>`,
         cellClassRules: {"cell-dirty": (p) => server && statusOf(kind, p.data.name, server) !== statusOf(kind, p.data.name)}},
      ],
      getRowId: (p) => p.data.name, stopEditingWhenCellsLoseFocus: true,
      defaultColDef: {resizable: true, sortable: true},
      overlayNoRowsTemplate: "<span class='hint'>loading…</span>",
    });
  }
  function statusOf(kind, name, p = draft) {
    if (!p) return "";
    const n = lc(name);
    if (kind === "category") return p.allowed_categories.includes(n) ? "allowed" : "not allowed";
    if (p.blocked_merchants.includes(n)) return "blocked";
    return p.allowed_merchants.includes(n) ? "allowed" : "not listed";
  }
  function setStatus(kind, name, status) {
    const n = lc(name), drop = (k) => { draft[k] = draft[k].filter((x) => x !== n); };
    if (kind === "category") {
      drop("allowed_categories");
      if (status === "allowed") draft.allowed_categories.push(n);
      return;
    }
    drop("allowed_merchants"); drop("blocked_merchants");
    if (status === "allowed") draft.allowed_merchants.push(n);
    if (status === "blocked") draft.blocked_merchants.push(n);
  }
  const merchantsGrid = listGrid($("policy-merchants"), "merchant");
  const categoriesGrid = listGrid($("policy-categories"), "category");

  function listRows(field, policyKeys) {
    const rows = new Map();
    for (const p of catalog) {
      const k = lc(p[field]);
      const r = rows.get(k) || {name: p[field], products: 0};
      r.products += 1; rows.set(k, r);
    }
    for (const key of policyKeys) for (const n of server[key]) if (!rows.has(n)) rows.set(n, {name: n, products: 0});
    return [...rows.values()];
  }

  /* ---------- history ---------- */
  const historyGrid = agGrid.createGrid($("policy-history"), {
    columnDefs: [
      {field: "at", headerName: "When", width: 190, valueFormatter: (p) => p.value ? new Date(p.value).toLocaleString() : ""},
      {field: "actor", headerName: "By", width: 90},
      {headerName: "Changes", flex: 1, wrapText: true, autoHeight: true,
       valueGetter: (p) => Object.entries(p.data.changes || {}).map(([k, v]) =>
         `${k}: ${JSON.stringify(v.from)} → ${JSON.stringify(v.to)}`).join(" · ")},
      {field: "id", headerName: "Id", width: 130},
    ],
    domLayout: "autoHeight", getRowId: (p) => p.data.id,
    defaultColDef: {resizable: true, sortable: true},
    overlayNoRowsTemplate: "<span class='hint'>No owner edits yet: the policy is the one from policy.yaml</span>",
  });

  const editing = () => [capsGrid, merchantsGrid, categoriesGrid].some((g) => g.getEditingCells().length > 0);
  const dirty = () => draft && Object.keys(changedFields()).length > 0;
  async function loadPolicy(force = false) {
    // never clobber unsaved edits or an open cell editor; a poll only redraws on a new revision
    if (!force && (dirty() || editing())) return;
    const [p, cat, hist] = await Promise.all([api("/policy"), catalog.length ? catalog : api("/catalog"), api("/policy/history")]);
    if (!force && (dirty() || editing())) return;  // the user started editing while we were loading
    if (!force && p.revision === revision) return;
    catalog = cat;
    server = {}; for (const k of [...CAPS.map((c) => c.key), ...LISTS]) server[k] = p[k];
    draft = clone(server); revision = p.revision;
    merchantsGrid.setGridOption("rowData", listRows("merchant", ["allowed_merchants", "blocked_merchants"]));
    categoriesGrid.setGridOption("rowData", listRows("category", ["allowed_categories"]));
    historyGrid.setGridOption("rowData", hist);
    renderDirty();
  }

  $("policy-discard").addEventListener("click", () => { draft = clone(server); renderDirty(); });
  $("policy-save").addEventListener("click", async () => {
    const body = changedFields(); if (!Object.keys(body).length) return;
    const btn = $("policy-save"); btn.disabled = true;
    try {
      const r = await api("/policy", {method: "PUT", body: JSON.stringify(body), headers: {
        "content-type": "application/json", "X-Owner-Token": ownerInput.value || tokenInput.value,
        "If-Match": revision}});
      toast(r.result === "UPDATED" ? `Policy saved (${Object.keys(r.change.changes).join(", ")})` : "Nothing changed");
      await loadPolicy(true);
      refresh(); loadChart();
    } catch (e) {
      if (/changed by someone else/.test(e.message)) {  // 409: another owner saved first
        toast("Policy not saved: it was changed meanwhile. Reloaded the current policy; apply your edit again.");
        await loadPolicy(true).catch(() => {});
      } else { toast("Policy not saved: " + e.message); renderDirty(); }
    }
  });

  /* ---------- spend over time (AG Charts Community) ---------- */
  function loadChartLib() {
    if (window.agCharts) return Promise.resolve();
    chartLoading = chartLoading || new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/ag-charts-community-10.3.3/ag-charts-community.min.js";
      s.onload = ok; s.onerror = () => { chartLoading = null; fail(new Error("chart library failed to load")); };
      document.head.appendChild(s);
    });
    return chartLoading;
  }
  async function loadChart() {
    const h = await api(`/spend/history?days=${$("chart-days").value}`);
    await loadChartLib();
    const data = h.days.map((d) => ({day: d.day.slice(5), captured: Number(d.captured), reserved: Number(d.reserved),
                                    cap: Number(h.daily_cap), orders: d.orders}));
    const money = {renderer: ({datum, yKey, yName}) => ({title: datum.day, content: `${yName}: ${usd(datum[yKey])} · ${datum.orders} order(s)`})};
    const options = {
      container: $("spend-chart"), data,
      series: [
        {type: "bar", xKey: "day", yKey: "captured", yName: "Captured", stacked: true, fill: "#138a4c", tooltip: money},
        {type: "bar", xKey: "day", yKey: "reserved", yName: "Reserved (awaiting payer / unconfirmed)", stacked: true, fill: "#e0a33a", tooltip: money},
        {type: "line", xKey: "day", yKey: "cap", yName: `Daily cap (now ${usd(h.daily_cap)})`, stroke: "#c0392b",
         marker: {enabled: false}, lineDash: [6, 4]},
      ],
      axes: [{type: "category", position: "bottom"},
             {type: "number", position: "left", label: {formatter: ({value}) => "$" + value}}],
      legend: {position: "bottom"},
    };
    if (chart) chart.update(options); else chart = agCharts.AgCharts.create(options);
  }
  $("chart-days").addEventListener("change", () => loadChart().catch((e) => toast("chart: " + e.message)));

  async function refreshPolicyTab(force = false) {
    try { await Promise.all([loadPolicy(force), loadChart()]); }
    catch (e) { toast("policy tab: " + e.message); }
  }
  $("tab-policy").addEventListener("click", () => { refreshPolicyTab(!loadedOnce); loadedOnce = true; });
  setInterval(() => { if (visible()) refreshPolicyTab(); }, 5000);
})();
