# Independent review 2 — after `e302d94`

Reviewed 2026-10-29 UTC. **The original budget and duplicate-approval races are fixed; payment recovery still permits lost accounting.** Read `CODEX_REVIEW_1.md`, `FIX_ROUND_4.md`, application code and all seven test modules. Only this report was added; no source edits, commits, pushes or publishing.

## Verification

- Fresh `git archive HEAD` copy and Python **3.12.15** venv: `/tmp/guardrail-review2-zl6yMU/checkout/.venv`; pinned requirements installed successfully; `pip check` clean. Full suite: **110 passed**, one Starlette/AnyIO deprecation warning, 1.75s.
- README offline block: suite **110 passed** with its exports, 1.65s. Verbatim launch found port 8000 occupied; the unchecked curl reached the existing mock server and submitted the mixed demo request. That server was not stopped. Repeated using free port **55255** plus matching `PUBLIC_BASE_URL`, fresh DB and a deliberately conflicting sample `.env`: **110 passed**, 1.59s; server/CLI worked offline. Extra HTTP checks: mock checkout/capture, approval 401 without token, idempotent approval with token, vendored JS 200, negation refusal. Spend: captured **25.98**, reserved **89.00**, total **114.98**. Only the review's server was stopped.
- Independent barrier/event probes covered threads **and actual separate processes**, capture/cancel/expiry, provider failures, quote changes and restart. Scripts/logs: `/tmp/guardrail-review2-zl6yMU/{probes.py,probes.log,offline.sh,offline-isolated.sh,http_check.py,offline-isolated.log,install.log}`. Re-run probes from the copied checkout with `PYTHONPATH=. .venv/bin/python /tmp/guardrail-review2-zl6yMU/probes.py`.
- Payment failures and interleavings use local fake providers, not live Sandbox evidence. No live Claude/PayPal calls; dashboard assets/HTTP checked, browser rendering not tested.

## B1–B9 verdicts

| Bug | Verdict | Independent evidence / remaining repro |
|---|---|---|
| **B1** budget race | **Fixed** | Four simultaneous $49 purchases: three orders, one `NEEDS_APPROVAL`, **$147** reserved, both on one threaded service and across four processes sharing SQLite. Check/reserve precedes provider I/O in one transaction (`service.py:533`). |
| **B2** approval reuse | **Fixed** | Four concurrent approvals produce one order and **$89** reserved, in threads and separate processes. Retries return that order, sometimes still `CREATING`; claim/link/reservation are atomic. |
| **B3** cancel/expiry race | **Partially fixed** | Paused capture survives public GET cancel and expiry; repeat capture returns `CAPTURE_IN_PROGRESS`; final captured **$49**. But callback authentication remains absent: public `/orders` supplies the token; POST cancel without credentials returns 401, GET `/paypal/cancel?token=…&format=json` cancels the same awaiting order. Recovery also reopens release races (N1/N3 below). |
| **B4** uncertain payment | **Partially fixed** | Capture-then-500 reconciles to `COMPLETED`, retaining **$49**; `PENDING`/missing capture status retains reservations. Suite also covers transport normalization and partial agent results. New reconciliation/replay paths still lose accounting (N1/N2). |
| **B5** quote substitution | **Fixed** | Changing $89→$499, merchant, currency or category returns `QUOTE_CHANGED`, with no order/spend; suite additionally checks removed products. |
| **B6** auth/ownership/limits | **Not fixed** | Anonymous agent execution, listings, capture/reconcile remain available; no ownership or rate limiting. Empty approver token still fails open; shared secret remains in localStorage. README now discloses these limitations. |
| **B7** recipient binding | **Not fixed** | `SandboxPayPal.create_order` still omits payee mapping and ignores `items`. Merchant controls protect catalog labels; all payments use the configured receiver. Correctly disclosed now. |
| **B8** offline intent | **Partially fixed** | Original “Don't buy…” and zero quantities buy nothing. **“Avoid buying USB-C cables”**, **“Buy USB-C cables only when approved”**, and **“Buy -1 USB-C cables”** each create a **$12.99** order. Denylisting words does not enforce the advertised positive-only grammar. |
| **B9** mock restart | **Partially fixed** | Ordinary restart deliberately marks open orders `MOCK_RESET`, releases reservations and preserves completed history, as documented. A second live worker is mistaken for a restart and can erase an in-flight capture (N3). |

## New bugs / regressions

**N1 — P1: reconciliation accepts an obsolete provider snapshot** (`app/service.py:357–395`). Start in `NEEDS_RECONCILIATION` after a 503. Reconciler A reads provider `PAYER_ACTION_REQUIRED`; hold its response. Buyer approves; reconciler B captures and receives `COMPLETED/PENDING`, returning local state to `NEEDS_RECONCILIATION`. Resume A: its status-only conditional update succeeds against that reused state, restoring `PAYER_ACTION_REQUIRED`. Cancel then releases **all $49**, although the provider has a pending capture. No stale provider behavior is needed: only delayed delivery of a once-current read. Use a persisted operation generation/claim, and reject results from superseded reads; protect unknown captures from cancellation/expiry.

**N2 — P1 recovery edge: completed create replay becomes cancellable** (`app/service.py:281–300`). Fake provider creates/captures, then loses the create response. Reconciliation replays the reference and receives that same order as `COMPLETED`; `_create_at_provider` unconditionally stores `PAYER_ACTION_REQUIRED` unless status is `APPROVED`. Cancel releases **$12.99** while provider remains `COMPLETED`. This is an injected completed-response case, not a demonstrated Sandbox sequence. Interpret completed replay responses through capture verification before exposing cancellation.

**N3 — P2, mock only: startup invalidation races active workers** (`app/main.py:49`, `app/service.py:479–492`). Pause worker A's $49 capture. Start app B on the same SQLite DB with its own mock; startup marks A's `CAPTURING` order `MOCK_RESET` and deletes its reservation. Resume A: provider completes, local completion transition fails; final local `MOCK_RESET`, spend **$0**. Persist/share mock state or enforce one mock process and exclusive DB ownership; document the restriction.

**N4 — P2: approval execution bypasses the new aggregate hard maximum** (`app/service.py:589`, `app/guardrails.py:169`). Queue two one-monitor approvals with the **same request_id before either executes**, then approve both. Each costs $329; **$658** is reserved despite the claimed $500 aggregate hard limit. A subsequent policy check correctly says `DENY`. `approve()` calls `hard_rules_only()` without related spend. Recheck aggregate hard rules inside the approval transaction, including pending approvals that became stale as other purchases executed. Human approval should waive soft caps only.

These schedules are absent from the passing regression suite. B8's remaining parser weaknesses predate this fix; the recovery paths, startup invalidation and aggregate-policy inconsistency concern newly added behavior.

## Remaining README / reproduction issues

- **Occupied-port failure is silent to later commands:** background launch + `sleep 3` does not establish server identity/readiness. The verbatim block demonstrated this. Check startup/PID and fresh spend before POSTing; use a selectable port and matching public URL.
- **Switching modes in the same shell is misleading:** offline exports (`AGENT_MODE`, `PAYPAL_MODE`, `GRB_NO_DOTENV`) survive into the Claude/Sandbox instructions, overriding or disabling `.env`. Require a fresh shell or explicit mode/reset exports.
- Vendoring and dependency pins resolve the former CDN/bootstrap ambiguity. Positive-only parser, universal split-hard-limit and safe multi-process claims need narrowing or fixes above. README's shared-token cancellation claim must explicitly exclude the public callback.
- Real Sandbox buyer capture and default live Claude model remain unverified here; README now acknowledges missing capture evidence. Devpost still has `<GITHUB_URL>`/`<YOUTUBE_URL>`; video script/scenes still say **68 tests**. These remain reproduction/submission gaps.

## Top 5 next fixes, ranked

1. **Make recovery monotonic and exclusive:** operation generations/claims, stale-response rejection, verified handling of completed create replays; add N1/N2 event-controlled regressions.
2. **Close B6/B3 shared-server access:** requester auth/ownership, cost/request limits, fail-closed approver configuration and authenticated/signed cancellation state.
3. **Enforce aggregate hard rules during approval**, transactionally; cover queued and concurrent approvals (N4).
4. **Persist mock provider state or enforce exclusive single-process mock use**, preventing startup from invalidating another worker's captures (N3).
5. **Validate offline grammar before execution**, rejecting unsupported negation, conditions and signed quantities or presenting a parsed cart for confirmation (B8).
