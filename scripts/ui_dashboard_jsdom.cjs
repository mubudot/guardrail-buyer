// Dashboard polish regressions, run with the vendored AG Grid in jsdom against a running server
// seeded by tests/test_ui_dashboard.py (one pending approval, one denied attempt, one allowed order):
//   - the four summary cards render from GET /spend (spend vs cap, pending, blocked, auto-approved);
//   - Approve / Reject open a confirmation dialog first: nothing is POSTed until it is confirmed,
//     Escape and Cancel close it, and a missing approver token is asked for inside the dialog;
//   - a confirmed Reject POSTs with the token and the approval leaves the grid;
//   - a failing poll shows the connection banner, the next good poll hides it.
//
//   node scripts/ui_dashboard_jsdom.cjs http://127.0.0.1:PORT APPROVER_TOKEN APPROVAL_ID
const {JSDOM, VirtualConsole} = require("jsdom");

const [base, token, approvalId] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const until = async (cond, ms = 8000) => { for (let t = 0; t < ms; t += 50) { try { if (cond()) return true; } catch (e) {} await sleep(50); } return false; };

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e.stack || e)));
  vc.on("error", (e) => errors.push(String(e)));
  const html = await (await fetch(base + "/")).text();
  const posts = [];
  let breakOrders = false;
  const dom = new JSDOM(html, {
    url: base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o = {}) => {
        const url = String(u);
        if (o.method === "POST") posts.push({url, headers: o.headers || {}});
        if (breakOrders && url.startsWith("/orders")) throw new TypeError("Failed to fetch");
        return fetch(new URL(url, base), o);
      };
      w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.open = () => null;
      w.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  const w = dom.window, d = w.document, $ = (id) => d.getElementById(id);
  await until(() => w.agGrid && d.querySelectorAll("#kpis .kpi:not(.skeleton)").length === 4, 60000);

  // summary cards
  const cards = [...d.querySelectorAll("#kpis .kpi")];
  if (cards.length !== 4) fail(`expected 4 summary cards, got ${cards.length}`);
  const card = (k) => d.querySelector(`#kpis [data-kpi=${k}] .v`).textContent;
  console.log("cards:", cards.map((c) => c.dataset.kpi + "=" + c.querySelector(".v").textContent).join(" | "));
  if (card("pending") !== "1") fail("pending card should show 1");
  if (card("blocked") !== "1") fail("blocked card should show 1");
  if (card("auto") !== "1") fail("auto-approved card should show 1");
  if (!/\$25\.98/.test(card("spend")) || !/\$150\.00/.test(card("spend"))) fail("spend card should show $25.98 / $150.00");
  if (!$("approvals-count").textContent.includes("1") || $("approvals-count").hidden) fail("approvals badge");

  // approval flow: dialog first, nothing POSTed until confirmed
  await until(() => d.querySelector("#approvals button.ok"));
  d.querySelector("#approvals button.ok").click();
  await until(() => !$("modal").hidden);
  if ($("modal").hidden) fail("approve did not open the confirmation dialog");
  if (!/Approve/.test($("modal-title").textContent) || !$("modal-rows").textContent.includes("Mechanical Keyboard")) fail("dialog content");
  if ($("modal-token-row").hidden) fail("dialog should ask for the approver token when none is entered " + JSON.stringify({tok: $("token").value, h: w.eval("JSON.stringify(health)")}));
  if (d.activeElement !== $("modal-token")) fail("focus should move into the dialog");
  if (posts.length) fail("POST sent before confirmation");
  $("modal").dispatchEvent(new w.KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
  await until(() => $("modal").hidden);
  if (!$("modal").hidden || posts.length) fail("Escape must close the dialog without acting");

  d.querySelector("#approvals button.bad").click();
  await until(() => !$("modal").hidden);
  $("modal-confirm").click();  // no token typed yet: stays open
  await sleep(100);
  if ($("modal").hidden || posts.length || $("modal-token").getAttribute("aria-invalid") !== "true") fail("empty token must keep the dialog open");
  $("modal-token").value = token;
  $("modal-confirm").click();
  await until(() => posts.length === 1);
  const p = posts[0];
  if (!p || !p.url.endsWith(`/approvals/${approvalId}/reject`) || p.headers["X-Approver-Token"] !== token) fail("reject POST " + JSON.stringify(p));
  if ($("token").value !== token) fail("the token typed in the dialog should fill the header box");
  await until(() => d.querySelectorAll("#approvals .ag-center-cols-container .ag-row").length === 0 && card("pending") === "0");
  if (card("pending") !== "0") fail("pending card should drop to 0 after the reject");

  // connection banner
  breakOrders = true;
  await w.eval("refresh()");
  if ($("conn-banner").hidden) fail("a failing poll should show the connection banner");
  breakOrders = false;
  await w.eval("refresh()");
  if (!$("conn-banner").hidden) fail("a good poll should hide the banner");

  const bad = errors.filter((e) => !/zero width|Could not parse CSS/.test(e));
  if (bad.length) fail("errors: " + bad.join("\n"));
  console.log("UI DASHBOARD OK");
  process.exit(0);
})().catch((e) => fail(e.stack || e));
