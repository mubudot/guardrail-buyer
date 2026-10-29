// Headless DOM smoke test of the dashboard (no real browser): loads the served page, the
// vendored AG Grid 32.3.3 bundle and app.js in jsdom against a running server, then
// exercises the Audit tab (rows, cellClassRules, quick filter, CSV export, row click ->
// detail panel). jsdom has no layout engine, so this checks behaviour, not visuals.
//
//   npm i jsdom@24            (in any directory; pass NODE_PATH if needed)
//   node scripts/ui_smoke_jsdom.cjs http://127.0.0.1:8000
const {JSDOM, VirtualConsole} = require("jsdom");

const base = process.argv[2] || "http://127.0.0.1:8000";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e.stack || e)));
  vc.on("error", (e) => errors.push(String(e)));
  // jsdom has no layout engine: AG Grid's "zero width" sizing warning is expected here
  vc.on("warn", (e) => { if (String(e).includes("AG Grid") && !String(e).includes("zero width")) errors.push("warn: " + e); });
  const html = await (await fetch(base + "/")).text();
  const dom = new JSDOM(html, {
    url: base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, base), o);
      w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.URL.createObjectURL = (blob) => { w.__csvBlob = blob; return "blob:csv"; };
      w.URL.revokeObjectURL = () => {};
      w.document.addEventListener("click", (e) => {   // CSV download link: record it, do not navigate
        if (e.target && e.target.download) { w.__download = e.target.download; e.preventDefault(); }
      }, true);
    },
  });
  const w = dom.window, d = w.document;
  for (let i = 0; i < 60 && !(w.agGrid && d.querySelector("#audit .ag-root")); i++) await sleep(100);
  if (!w.agGrid) fail("AG Grid bundle did not load");
  await sleep(1500); // first refresh()
  const auditRows = () => [...d.querySelectorAll("#audit .ag-center-cols-container .ag-row")];
  d.getElementById("tab-audit").click();
  await sleep(800);
  if (d.getElementById("view-audit").hidden) fail("audit tab did not open");
  let rows = auditRows();
  console.log("audit rows rendered:", rows.length, "| count label:", d.getElementById("audit-count").textContent);
  if (rows.length < 3) fail("expected audit rows");
  const cls = (sel) => d.querySelectorAll(`#audit ${sel}`).length;
  console.log("risk classes: DENY", cls(".ag-row.risk-DENY"), "NEEDS_APPROVAL", cls(".ag-row.risk-NEEDS_APPROVAL"),
              "NEEDS_RECONCILIATION", cls(".ag-row.risk-NEEDS_RECONCILIATION"), "cells DENY", cls(".ag-cell.risk-DENY"));
  if (!cls(".ag-row.risk-DENY") || !cls(".ag-row.risk-NEEDS_APPROVAL")) fail("risk highlighting missing");
  if (!d.querySelectorAll("#audit .ag-floating-filter").length) fail("floating column filters missing");

  const search = d.getElementById("audit-search");
  search.value = "Shady"; search.dispatchEvent(new w.Event("input"));
  await sleep(400);
  // jsdom has no layout, so column virtualisation hides most cells: read the filtered rows from the grid API
  const grid = w.eval("auditGrid"), kept = [];
  grid.forEachNodeAfterFilter((n) => kept.push(n.data.merchant));
  console.log("quick filter 'Shady':", kept.length, "rows,", auditRows().length, "rendered, merchants:", [...new Set(kept)]);
  if (!kept.length || !kept.every((m) => m.includes("Shady")) || auditRows().length !== kept.length) fail("quick filter");
  search.value = ""; search.dispatchEvent(new w.Event("input"));
  await sleep(300);

  d.getElementById("audit-csv").click();
  await sleep(300);
  if (!w.__csvBlob) fail("CSV export produced nothing");
  const csv = await new Promise((res) => { const r = new w.FileReader(); r.onload = () => res(r.result);
                                          r.readAsText(w.__csvBlob); });
  console.log("CSV:", w.__download, csv.split("\n").length - 1, "data lines; header:", csv.split("\n")[0].slice(0, 120));
  if (!csv.includes("Rule hit") || !csv.includes("Decision id")) fail("CSV header");
  if (!/^guardrail-audit-\d{4}-\d{2}-\d{2}\.csv$/.test(w.__download || "")) fail("CSV file name " + w.__download);

  const first = auditRows().find((r) => r.classList.contains("risk-NEEDS_APPROVAL")) || auditRows()[0];
  first.querySelector(".ag-cell").dispatchEvent(new w.MouseEvent("click", {bubbles: true}));
  for (let i = 0; i < 30 && !d.getElementById("detail-body").textContent.includes("Rules evaluated"); i++) await sleep(100);
  const body = d.getElementById("detail-body").textContent;
  console.log("detail panel:", d.getElementById("audit-detail").hidden ? "hidden" : "open", "|", body.slice(0, 160).replace(/\s+/g, " "));
  for (const k of ["Quote", "Rules evaluated", "Payment timeline", "per_order_cap"]) if (!body.includes(k)) fail("detail missing " + k);
  const opsRows = d.querySelectorAll("#orders .ag-center-cols-container .ag-row").length;
  const apprRows = d.querySelectorAll("#approvals .ag-center-cols-container .ag-row").length;
  console.log("existing grids still populated: orders", opsRows, "approvals", apprRows);
  if (!opsRows) fail("orders grid empty");
  if (errors.length) fail("page errors:\n" + errors.join("\n"));
  console.log("UI SMOKE OK");
  w.close();
  process.exit(0);
})().catch((e) => fail(e.stack || e));
