// Fix round 7 regressions for the Audit tab (docs/CODEX_REVIEW_4.md N3-N5), run with the
// vendored AG Grid in jsdom against a running server seeded by tests/test_ui_jsdom.py:
//   N3 a slow detail response for an earlier selection never overwrites the current one,
//      Close drops in-flight responses, and the open detail refreshes on the Audit poll;
//   N4 CSV export neutralises formula prefixes;
//   N5 "showing newest N of TOTAL", Load older (before_id cursor), audit polled only when visible.
//
//   node scripts/ui_review4_jsdom.cjs http://127.0.0.1:PORT APPROVER_TOKEN '{"A": "<decision id>", ...}'
const {JSDOM, VirtualConsole} = require("jsdom");

const base = process.argv[2];
const token = process.argv[3];
const ids = JSON.parse(process.argv[4]);  // {pending: approval_request decision, deny: formula deny decision, approval: approval id}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const until = async (cond, ms = 4000) => { for (let t = 0; t < ms && !cond(); t += 50) await sleep(50); return cond(); };

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e.stack || e)));
  vc.on("error", (e) => errors.push(String(e)));
  const html = await (await fetch(base + "/")).text();
  const calls = [];
  const delay = {};
  const dom = new JSDOM(html, {
    url: base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o) => {
        const url = String(u);
        calls.push(url);
        const r = await fetch(new URL(url, base), o);
        for (const [k, ms] of Object.entries(delay)) if (url.includes(k)) await sleep(ms);
        return r;
      };
      w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.URL.createObjectURL = (blob) => { w.__csvBlob = blob; return "blob:csv"; };
      w.URL.revokeObjectURL = () => {};
      w.document.addEventListener("click", (e) => { if (e.target && e.target.download) e.preventDefault(); }, true);
    },
  });
  const w = dom.window, d = w.document, $ = (id) => d.getElementById(id);
  await until(() => w.agGrid && d.querySelector("#audit .ag-root"), 8000);
  if (!w.agGrid) fail("AG Grid bundle did not load");
  await until(() => calls.some((c) => c.startsWith("/spend")));
  await w.eval("refresh()");
  await w.eval("refresh()");
  const auditCalls = () => calls.filter((c) => c.startsWith("/decisions?")).length;
  // N5: hidden Audit tab -> no audit polling at all
  if (auditCalls() !== 0) fail(`audit polled while hidden: ${auditCalls()}`);
  console.log("N5 hidden tab: /decisions list requests =", auditCalls());

  $("tab-audit").click();
  await until(() => $("audit-window").textContent.includes("of"));
  const grid = w.eval("auditGrid");
  const count = () => { let n = 0; grid.forEachNode(() => n++); return n; };
  console.log("N5 window:", JSON.stringify($("audit-window").textContent), "| rows", count(),
              "| older button hidden:", $("audit-older").hidden);
  if (!/^Showing newest 1000 of 100\d decisions/.test($("audit-window").textContent)) fail("window label");
  if ($("audit-older").hidden || count() !== 1000) fail("older button / first page");
  const total = Number($("audit-window").textContent.match(/of (\d+)/)[1]);
  $("audit-older").click();
  await until(() => $("audit-window").textContent.startsWith("Showing all"));
  console.log("N5 after Load older:", JSON.stringify($("audit-window").textContent), "| rows", count(),
              "| cursor request:", calls.filter((c) => c.includes("before_id=")).length);
  if (count() !== total || !$("audit-older").hidden) fail("load older did not reach the full history");
  await w.eval("refresh()");  // a poll keeps the older rows
  await w.eval("auditRefreshing || Promise.resolve()");
  if (count() !== total) fail("poll dropped loaded older rows");

  // N4: formula neutralisation in CSV
  $("audit-csv").click();
  await until(() => w.__csvBlob);
  const csv = await new Promise((res) => { const r = new w.FileReader(); r.onload = () => res(r.result); r.readAsText(w.__csvBlob); });
  const formulaCells = csv.split(/\r?\n/).flatMap((l) => l.split('","')).map((c) => c.replace(/^"|"$/g, ""))
    .filter((c) => /^[=+\-@\t\r]/.test(c));
  console.log("N4 CSV lines", csv.split("\n").length, "| merchant cell:", (csv.match(/"'?=1\+1"/) || [""])[0],
              "| raw formula cells:", formulaCells.length);
  if (!csv.includes(`"'=1+1"`) || formulaCells.length) fail("CSV formula not neutralised");

  // N3a: slow response for the earlier selection must not overwrite the later one
  delay[`/decisions/${ids.pending}`] = 700;
  w.eval(`showDecision(${JSON.stringify(ids.pending)})`);
  await sleep(30);
  w.eval(`showDecision(${JSON.stringify(ids.deny)})`);
  await sleep(1100);
  const body = () => $("detail-body").textContent;
  console.log("N3 race: title", $("detail-title").textContent, "| has deny merchant", body().includes("=1+1"),
              "| has pending justification", body().includes("JUSTIFY-PENDING"));
  if ($("detail-title").textContent !== ids.deny || !body().includes("=1+1") || body().includes("JUSTIFY-PENDING"))
    fail("stale detail response overwrote the current selection");

  // N3b: Close drops an in-flight response
  w.eval(`showDecision(${JSON.stringify(ids.pending)})`);
  await sleep(30);
  $("detail-close").click();
  await sleep(1000);
  if (!$("audit-detail").hidden || body().includes("JUSTIFY-PENDING")) fail("response after Close re-rendered the panel");
  delete delay[`/decisions/${ids.pending}`];
  console.log("N3 close: panel hidden, late response ignored");

  // N3c: the open detail refreshes on the Audit poll and is labelled with its snapshot time
  w.eval(`showDecision(${JSON.stringify(ids.pending)})`);
  await until(() => body().includes("JUSTIFY-PENDING"));
  if (!body().includes("PENDING")) fail("pending approval detail");
  const before = $("detail-asof").textContent;
  const r = await fetch(`${base}/approvals/${ids.approval}/reject`, {method: "POST", headers: {"X-Approver-Token": token}});
  if (!r.ok) fail("reject via API " + r.status);
  await w.eval("refresh()");
  await until(() => body().includes("REJECTED"));
  console.log("N3 freshness: approval status now", body().includes("REJECTED") ? "REJECTED" : "stale",
              "| label:", JSON.stringify($("detail-asof").textContent), "(before:", JSON.stringify(before) + ")");
  if (!body().includes("REJECTED") || !$("detail-asof").textContent.startsWith("Snapshot")) fail("detail not refreshed");

  // switching back to another tab stops audit polling again
  d.querySelector('.tab:not(#tab-audit)').click();
  const n0 = auditCalls();
  await w.eval("refresh()");
  if (auditCalls() !== n0) fail("audit polled after leaving the tab");
  if (errors.length) fail("page errors:\n" + errors.join("\n"));
  console.log("UI REVIEW4 OK");
  w.close();
  process.exit(0);
})().catch((e) => fail(e.stack || e));
