// Dashboard polish regressions, run with the vendored AG Grid in jsdom against a running server
// seeded by tests/test_ui_dashboard.py (one pending approval, one denied attempt, one allowed order):
//   - the four summary cards render from GET /spend (spend vs cap, pending, blocked, auto-approved);
//   - Approve / Reject open a confirmation dialog first: nothing is POSTed until it is confirmed,
//     Escape and Cancel close it, and a missing approver token is asked for inside the dialog;
//   - a confirmed Reject POSTs with the token and the approval leaves the grid;
//   - a second confirmDialog() settles the first one (no promise left pending);
//   - a double Ctrl+Enter sends one agent request, not two;
//   - a failing poll shows the connection banner, the next good poll hides it, and a failure of
//     one resource is not hidden by another resource's success;
//   - a failed first load shows "Could not load" and the next good load restores the empty state
//     (orders and audit); an audit that never loaded is not shown as "No guardrail decisions yet";
//   - the outcome list keeps two finished purchases of the same product apart, collapses
//     intermediate calls of one attempt, and lists a policy dry-run that was denied (gift card);
//   - a checkout is opened only for PAYER_ACTION_REQUIRED and a vetted PayPal / mock URL;
//   - Shift+Tab into a cell with buttons visits them (last first) before leaving the cell.
//   - uncertain orders expose authenticated rechecks, retain the action on error/unresolved status,
//     preserve its disabled state during polling, and render the recovered payment state.
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
  let breakOrders = false, breakAudit = false, emptyAudit = false;
  let recovery = null, recheckMode = "error", releaseRecheck = null;
  const dom = new JSDOM(html, {
    url: base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o = {}) => {
        const url = String(u);
        if (o.method === "POST") posts.push({url, headers: o.headers || {}});
        // Controlled provider outcomes: API/service recovery is covered in test_review_fixes.py.
        if (recovery && url === "/orders") return Response.json([recovery]);
        if (recovery && url === `/orders/${recovery.id}/reconcile`) {
          if (recheckMode === "error") return Response.json({detail: "invalid approver token"}, {status: 401});
          if (recheckMode === "wait") await new Promise((resolve) => { releaseRecheck = resolve; });
          if (recheckMode === "completed") recovery = {...recovery, status: "COMPLETED", capture_id: "MOCKCAP-RECOVERED"};
          if (recheckMode === "payable") recovery = {...recovery, status: "PAYER_ACTION_REQUIRED",
            approve_url: base + "/paypal/mock-checkout?token=MOCK-RECOVERED"};
          return Response.json(recovery);
        }
        if (breakOrders && url.startsWith("/orders")) throw new TypeError("Failed to fetch");
        if (breakAudit && url.startsWith("/decisions")) throw new TypeError("Failed to fetch");
        if (emptyAudit && url.startsWith("/decisions?"))
          return new Response(JSON.stringify({items: [], total: 0, has_more: false, next_before_id: null}), {headers: {"content-type": "application/json"}});
        return fetch(new URL(url, base), o);
      };
      w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.open = (u) => { (w.__opened = w.__opened || []).push(u); return null; };
      w.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  const w = dom.window, d = w.document, $ = (id) => d.getElementById(id);
  await until(() => w.agGrid && d.querySelectorAll("#kpis [data-kpi]").length === 4, 60000);

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

  // dialog re-entry: the first promise is settled (false) when a second dialog replaces it
  const first = w.eval("confirmDialog({title: 'one'})");
  const second = w.eval("confirmDialog({title: 'two'})");
  const firstResult = await Promise.race([first, sleep(500).then(() => "pending")]);
  if (firstResult !== false) fail("first dialog promise should settle false, got " + firstResult);
  $("modal-confirm").click();
  if ((await second) !== true || !$("modal").hidden) fail("second dialog should confirm and close");
  if ([...d.body.children].some((n) => n.inert)) fail("page must not stay inert after the dialog closes");

  // double Ctrl+Enter -> one agent request
  $("ask").value = "Buy 1 USB-C cable";
  const ctrlEnter = () => $("ask").dispatchEvent(new w.KeyboardEvent("keydown", {key: "Enter", ctrlKey: true, bubbles: true}));
  ctrlEnter(); ctrlEnter();
  await until(() => posts.some((x) => x.url.startsWith("/agent/request")) && !$("ask-btn").disabled, 30000);
  await sleep(300);
  const agentPosts = posts.filter((x) => x.url.startsWith("/agent/request")).length;
  if (agentPosts !== 1) fail(`double Ctrl+Enter sent ${agentPosts} agent requests`);
  if (!d.querySelector("#answer .outcomes li") || !d.querySelector("#answer details.trace")) fail("answer: outcomes + collapsed trace expected");

  // connection banner. refresh() returns a poll already in flight (started before the flag
  // changed) and queues one more, so await twice to be sure a refresh ran with the new flag.
  const fresh = async () => { await w.eval("refresh()"); await w.eval("refresh()"); };
  breakOrders = true;
  await fresh();
  if ($("conn-banner").hidden) fail("a failing poll should show the connection banner");
  breakOrders = false;
  await fresh();
  if (!$("conn-banner").hidden) fail("a good poll should hide the banner");
  w.eval("conn.fail('audit', new Error('boom'))");
  await fresh();  // core is fine, audit is still down
  if ($("conn-banner").hidden || !$("conn-detail").textContent.includes("audit")) fail("core success must not hide an audit failure");
  w.eval("conn.ok('audit')");
  if (!$("conn-banner").hidden) fail("banner should clear once every resource recovered");


  // error overlays: a failed first load says "Could not load"; the next good load restores the empty
  // state. Checked on the overlay text actually rendered (approvals is empty here after the reject).
  const shown = (id) => { const o = d.querySelector(`#${id} .ag-overlay`); return o ? o.textContent.replace(/\s+/g, " ").trim() : ""; };
  breakOrders = true; await w.eval("refresh()");  // let any poll that started before the break finish
  w.eval("conn.loadedOnce = false"); await fresh(); await sleep(100);
  if (!/Could not load/.test(shown("approvals"))) fail("failed first load should render 'Could not load': " + shown("approvals"));
  breakOrders = false; await fresh(); await sleep(100);
  if (/Could not load/.test(shown("approvals")) || !/Nothing waiting for you/.test(shown("approvals"))) fail("recovery should render the approvals empty state: " + shown("approvals"));
  // audit: never loaded + failing -> "Could not load" (not "No guardrail decisions yet"); then an empty success
  w.eval("overlays.auditLoaded = false; audit.rows.clear(); audit.oldestCursor = null; auditGrid.setGridOption('rowData', [])");
  breakAudit = true; await w.eval("refreshAudit()"); await sleep(100);
  if (!/Could not load/.test(shown("audit")) || /No guardrail decisions yet/.test(shown("audit"))) fail("an audit that never loaded must render 'Could not load': " + shown("audit"));
  breakAudit = false; emptyAudit = true; await w.eval("refreshAudit()"); await sleep(100);
  if (!/No guardrail decisions yet/.test(shown("audit"))) fail("audit recovery should render its empty state: " + shown("audit"));
  emptyAudit = false; await w.eval("refreshAudit()");
  if (!$("conn-banner").hidden) fail("banner should clear after audit recovery");

  // outcome list: distinct attempts, intermediate calls collapsed, denied dry-run listed
  const tr = [
    {role: "user", text: "x"},
    {role: "tool", name: "search_catalog", input: {query: "cable"}, output: {products: [{id: "P-1001", name: "USB-C Cable"}, {id: "P-5001", name: "Gift Card"}]}},
    {role: "tool", name: "check_policy", input: {product_id: "P-5001", quantity: 1}, output: {verdict: "DENY", reasons: ["merchant blocked"], amount: "100.00"}},
    {role: "tool", name: "create_order", input: {product_id: "P-1001", quantity: 2}, output: {result: "ORDER_PLACED", order: {amount: "25.98"}}},
    {role: "tool", name: "create_order", input: {product_id: "P-1001", quantity: 4}, output: {result: "NEEDS_APPROVAL", verdict: "NEEDS_APPROVAL", reasons: ["over cap"]}},
    {role: "tool", name: "request_approval", input: {product_id: "P-1001", quantity: 4, justification: "j"}, output: {result: "APPROVAL_REQUESTED", reasons: ["over cap"]}},
  ];
  w.__tr = tr;
  const box = d.createElement("div");
  box.append(...w.eval("renderAgentAnswer({final_text: 'done', transcript: window.__tr})"));
  const lis = [...box.querySelectorAll(".outcomes li")].map((li) => li.textContent);
  console.log("outcomes:", JSON.stringify(lis));
  if (lis.length !== 3) fail("expected 3 outcome lines (gift card deny, 2 cables, 4 cables), got " + lis.length);
  if (!/DENIED/.test(lis[0]) || !/Gift Card/.test(lis[0]) || !/refused at the policy check/.test(lis[0])) fail("dry-run deny line: " + lis[0]);
  if (!/2 × USB-C Cable/.test(lis[1]) || !/4 × USB-C Cable/.test(lis[2]) || !/APPROVAL REQUESTED/.test(lis[2])) fail("same-product attempts must stay apart: " + lis.join(" | "));

  // checkout opening: only PAYER_ACTION_REQUIRED + a vetted URL
  w.__opened = [];
  const realApi = w.eval("api");
  const tryDecide = async (order) => {
    w.__order = order;
    w.eval("api = async () => ({result: 'X', order: window.__order})");
    await w.eval("decide('APR-T', 'approve', document.createElement('button'))");
  };
  await tryDecide({id: "O1", status: "COMPLETED", approve_url: null});
  await tryDecide({id: "O2", status: "PAYER_ACTION_REQUIRED", approve_url: null});
  await tryDecide({id: "O3", status: "PAYER_ACTION_REQUIRED", approve_url: "https://evil.example/checkout"});
  await tryDecide({id: "O4", status: "COMPLETED", approve_url: "https://www.sandbox.paypal.com/checkoutnow?token=A"});
  await tryDecide({id: "O5", status: "PAYER_ACTION_REQUIRED", approve_url: "https://www.sandbox.paypal.com/checkoutnow?token=B"});
  w.__realApi = realApi; w.eval("api = window.__realApi");
  if (JSON.stringify(w.__opened) !== JSON.stringify(["https://www.sandbox.paypal.com/checkoutnow?token=B"])) fail("opened checkouts: " + JSON.stringify(w.__opened));

  // Recovery row: missing token prompts before POST; failures keep the retry available.
  // jsdom has no layout width; render the payment column even when it is outside that viewport.
  w.eval("ordersGrid.setGridOption('suppressColumnVirtualisation', true)");
  recovery = {id: "ORD-RECHECK", product_name: "USB-C Charging Cable 2m", quantity: 1, amount: "12.99",
    status: "NEEDS_RECONCILIATION", created_at: new Date().toISOString()};
  await fresh();
  const recheck = () => d.querySelector('#orders button[data-action="reconcile"]');
  await until(() => recheck());
  if (!recheck() || recheck().textContent !== "Recheck status") fail("uncertain row lacks recovery action");
  if (!/captured \+ reserved/.test($("spend-day").textContent) || !/approve past the daily threshold/.test($("spend-day").textContent)) fail("daily threshold semantics missing");
  if (!/daily approval threshold/.test(d.querySelector('[data-kpi="spend"] .k').textContent)) fail("daily figure label");
  $("token").value = "";
  const countRechecks = () => posts.filter((p) => p.url.endsWith("/reconcile")).length;
  recheck().click();
  await until(() => !$("modal").hidden);
  if ($("modal-token-row").hidden || countRechecks()) fail("recheck must ask for missing token before POST");
  $("modal-cancel").click();
  await until(() => recheck() && !recheck().disabled);
  if (countRechecks()) fail("cancelled recovery dialog must not POST");
  recheck().click();
  await until(() => !$("modal").hidden);
  $("modal-token").value = "wrong"; $("modal-confirm").click();
  await until(() => countRechecks() === 1 && recheck() && !recheck().disabled);
  if (!/Recheck failed/.test($("toast").textContent) || !recheck()) fail("recheck error should be visible and retryable");
  $("token").value = token;
  recheckMode = "wait";
  const pendingButton = recheck(); pendingButton.focus(); pendingButton.click();
  await until(() => releaseRecheck);
  await fresh();
  if (recheck() !== pendingButton || !pendingButton.disabled) fail("poll must retain the pending recovery control");
  pendingButton.click();
  if (countRechecks() !== 2) fail("pending recheck must not POST twice");
  recheckMode = "unknown"; releaseRecheck();
  await until(() => recheck() && !recheck().disabled);
  if (!$("toast").textContent.includes("NEEDS_RECONCILIATION")) fail("unresolved result must remain visible");
  if (posts.filter((p) => p.url.endsWith("/reconcile"))[1].headers["X-Approver-Token"] !== token) fail("recheck needs approver token header");
  recheckMode = "completed"; recheck().click();
  await until(() => !recheck() && $("orders").textContent.includes("MOCKCAP-RECOVERED"));
  if (recheck() || !$("orders").textContent.includes("MOCKCAP-RECOVERED") || !$("toast").textContent.includes("COMPLETED")) fail("completed recovery must update row and show capture id");
  // An uncertain create can instead recover to awaiting payer approval, showing its checkout link.
  recovery = {...recovery, status: "NEEDS_RECONCILIATION", capture_id: null};
  await fresh(); await until(() => recheck());
  recheckMode = "payable"; recheck().click();
  await until(() => d.querySelector("#orders a.paypal"));
  if (recheck() || !d.querySelector("#orders a.paypal")) fail("recovered create must show Pay with PayPal");
  recovery = null; await fresh();

  // Shift+Tab into a cell: last control, then the first, then out
  const cell = d.createElement("div"); cell.className = "ag-cell"; cell.tabIndex = -1;
  const b1 = d.createElement("button"), b2 = d.createElement("button"); cell.append(b1, b2); d.body.append(cell);

  const press = (target) => { const ev = new w.KeyboardEvent("keydown", {key: "Tab", shiftKey: true}); Object.defineProperty(ev, "target", {value: target}); return w.eval("tabThroughControls")({event: ev}); };

  cell.focus();
  if (!press(cell) || d.activeElement !== b2) fail("Shift+Tab into a cell should focus its last button");
  if (!press(b2) || d.activeElement !== b1) fail("Shift+Tab should move to the previous button");
  if (press(b1)) fail("Shift+Tab from the first button should leave the cell");
  cell.remove();

  const bad = errors.filter((e) => !/zero width|Could not parse CSS/.test(e));
  if (bad.length) fail("errors: " + bad.join("\n"));
  console.log("UI DASHBOARD OK");
  process.exit(0);
})().catch((e) => fail(e.stack || e));
