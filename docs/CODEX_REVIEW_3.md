# Independent review 3 — `6ab0b35` / `caed3a3`

Reviewed 2026-10-29 UTC, HEAD `caed3a3`. Read review 2, fix round 5, application code and tests. **No P1 found in the exercised current-version local-demo flows.** The prior N1/N2 lost-accounting schedules are fixed. Upgrade recovery and offline parsing still need attention; this is not ready for final submission or public shared-server use. Only this report was written in the working tree; no source changes, commit, push or publication.

## Verification

- Fresh `git archive HEAD` in `/tmp/guardrail-review3-86iCdX`, new `.venv`, Python **3.12.15**, pinned install successful, `pip check` clean. Full suite: **148 passed**, 2.84s; one Starlette/AnyIO deprecation warning.
- Extracted README's first bash block without edits and sourced it in a fresh bash, including venv creation/install: **148 passed**, 3.08s; port **8000** was free, instance readiness succeeded, mixed request reserved **$25.98**, keyboard needed approval, gift card was denied, CLI worked. A separate run changing only `PORT` to **41379** also passed (148 tests, 2.96s) and used matching checkout URLs. Both servers were killed and waited for; process inspection found no uvicorn left running.
- Re-ran review 2's independent probes, adapting signed cancellation and assertions to the intended fixed behavior. Threads **and four separate processes** still enforce B1 ($147 reserved) and B2 (one $89 order). Gated capture/cancel/expiry, lost capture response, pending/missing capture status, quote substitution, restart, stale reconciliation, completed create replay and aggregate approvals passed.
- Artifacts: `offline-verbatim.sh/.log`, `offline-freeport.sh/.log`, `full-suite.log`, `probes.py/.log`, `edge_probes.py/.log` under that temp directory. Re-run probes there with `PYTHONPATH=. .venv/bin/python probes.py` and `PYTHONPATH=. .venv/bin/python edge_probes.py`. `old/` is an archive of `e302d94` used to produce a genuine legacy database.
- Provider interleavings use local fakes. No live Claude/Sandbox payment or browser rendering was tested; HTTP/assets and API behavior are covered by the suite. Do not treat these results as real buyer-capture evidence.

## Requested verdicts

| Item | Verdict and reproduction |
|---|---|
| **N1** stale recovery | **Fixed for new rows; migration gap below.** Hold A's awaiting-buyer snapshot, let B capture to pending, then resume A: `SUPERSEDED`, `NEEDS_RECONCILIATION`, cancel refused, **$49 retained**. Persisted generation guards and `capture_unknown` protect subsequent reads. |
| **N2** completed create replay | **Fixed.** Lose the create response after provider capture, then replay: verified `COMPLETED`, cancellation refused, **$12.99 counted**. Suite also verifies failed verification reads remain reserved, and approved replay captures. |
| **N3** second mock worker | **Fixed for app startup; CLI bypass below.** Start another app while A's capture is gated: lock refusal; A completes with **$49 captured**. Suite's actual second-process import fails while locked and succeeds after release; ordinary restart preserves completed history. |
| **N4** aggregate approval hard max | **Fixed.** Queue two $329 monitor approvals with one request ID; execute both: first orders, second `DENIED`, **$329 reserved**. Concurrent regression passes. Related spend and deny rules are checked inside the approval transaction. |
| **B3** cancel race/auth | **Fixed within stated scope.** Missing/forged/other-order state gets **403**; correct HMAC cancels an awaiting order. Signed cancel and expiry during gated capture cannot release spend; duplicate capture returns `CAPTURE_IN_PROGRESS`. Mock checkout exposes its cancel link by design; Sandbox state is a per-order bearer capability. |
| **B6** access/limits | **Partially fixed.** Empty token gives **503** on approve/reject/cancel; explicit demo mode opens them. Configured 2/60 limiter returns **429** on call three. Requester identity/ownership, anonymous listings/capture/reconcile, shared localStorage token and per-process agent-only limiting remain as disclosed. |
| **B8** offline intent | **Original repros fixed; parser still partial.** “Avoid buying…”, “only when approved”, signed/fractional quantities refuse before tools; zero quantities create nothing. The new validator still accepts inputs whose execution differs from its grammar; see below. |

## Newly found bugs / residual edges

1. **P2 — upgrade loses capture uncertainty** (`app/service.py:116–120,478–486`). With old `e302d94`, persist a capture 503 plus failed verification read: order is `NEEDS_RECONCILIATION`, $49 reserved. Open that DB with current code: migration defaults `capture_unknown=0`. An awaiting-buyer provider read restores `PAYER_ACTION_REQUIRED`; cancel releases all **$49**. The corresponding new-row test deliberately forbids this transition. Conservatively backfill uncertain legacy orders before recovery, with an upgrade regression. This proves a safety-invariant gap, **not a demonstrated captured-payment loss at live PayPal**.
2. **P2 — purpose text affects executable quantity** (`app/offline_agent.py:127,233`). `Buy a USB-C cable for two interns` passes validation and orders **2 / $25.98**; `…for zero downtime` orders nothing. `_quantity()` rescans the whole clause despite the claimed inert purpose tail. `Buy two three USB-C cables` also passes and silently chooses two. Parse once into structured verb/quantity/item/purpose; reject malformed quantities. The selector predates this round; the new validator leaves it reachable and overstates safety.
3. **P2 — CLI evades mock DB ownership** (`app/cli.py:26`, `app/service.py:612`). Hold the app's mock lock, then run a separate offline CLI with `CLI_DB_PATH` set to that DB and `PAYPAL_MOCK_APPROVAL=1`: exits **0**, inserts an awaiting $12.99 order, server checkout returns **404** because its provider does not know the order. Acquire ownership at every mock entry point, or reject/document shared-DB CLI use in mock mode. Default in-memory CLI is fine.
4. **P3 — invalid limiter configuration crashes requests** (`app/main.py:47–76`). `AGENT_RATE_LIMIT=0/60` is accepted, then the first check raises `IndexError` on an empty deque. Validate positive finite limits/windows at startup; documented `off` works.

## README and submission readiness

The offline block now works verbatim; readiness, selectable port and mode-switch instructions address review 2's main reproduction issues. Remaining README corrections: remove the shared-DB CLI advice for mock mode until fixed; narrow “purpose … never executed” and add safe legacy migration guidance. `requirements.txt` still says 110 tests; requirements-check documentation still calls AG Grid a CDN dependency although it is vendored.

Against the [official criteria and submission rules](https://paypalaihackathon.devpost.com/rules), checked this review:

| Criterion | Assessment |
|---|---|
| Technology | Substantial server-side policy, transactional reservations, approvals and recovery. PayPal is central; live AI and successful Sandbox buyer capture still need current end-to-end proof. |
| Design | Coherent ask → approval → payment → spend dashboard. Recovery/cancel controls are API-only; visual/browser behavior remains unverified here. |
| Impact | Credible small-team procurement problem; demonstrate one concrete onboarding scenario and measurable avoided overspend. No user-validation evidence reviewed. |
| Idea | Policy-gated agent commerce is relevant; race-safe accounting is the strongest demonstrated distinction. Avoid claiming universal split prevention or real multi-merchant routing (B7 remains unchanged). |
| AG Grid | Three functional Community grids, sorting, resizing, tooltips and action cells meet the “use any AG Grid tools” eligibility condition. Integration is useful but modest for a competitive sponsor entry. |

Presentation is also an equally weighted criterion. Repository materials still show public-repo/video links as placeholders and disclose the missing real buyer-payment clip; the draft narration was not rebuilt after the test-count update. A public open-source repository and a public demo video under three minutes are required. Clear local setup can satisfy demo access; hosting is optional. These submission assets were not published or independently verified here.

## Top 5 remaining fixes

1. Backfill legacy capture uncertainty conservatively and add an actual old-schema upgrade regression.
2. Parse offline quantities structurally, isolate purpose text, and reject ambiguous item/quantity combinations.
3. Enforce mock ownership for CLI/service entry points and correct shared-DB README guidance.
4. Finish and verify the Claude → Sandbox buyer approval → capture demonstration; rebuild narration, replace links and prepare the required public submission assets.
5. Add requester authentication/ownership and broader resource limits before shared hosting; retain explicit local-only scope until then.
