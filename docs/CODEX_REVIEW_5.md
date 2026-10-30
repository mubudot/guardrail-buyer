**221 passed, 1 skipped. Six reproduced findings below, including one P1 accounting issue. No repository files modified. Provider failures were simulated locally; no live PayPal calls.**

1. **P1 — Recovery errors can erase spend for an earlier payment.**  
   `app/paypal_client.py:257`, `app/service.py:476`

   Two recovery paths incorrectly treat a later request’s refusal as proof that the original payment failed:

   - Capture returns `422 ORDER_ALREADY_CAPTURED`; its verification GET returns `401`. Observed: HTTP **200**, local **CAPTURE_FAILED**, spend **0.00**.
   - Original create outcome is lost after a simulated provider completion; replay uses the same request ID but returns `401`. Observed: **NEEDS_RECONCILIATION → FAILED**, spend **12.99 → 0.00**, provider still **COMPLETED**.

   Exact reproductions, from the repository root:

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_already_captured.py
   PYTHONPATH=. .venv/bin/python /tmp/grb_wire_replay.py
   ```

   Scripts: `capture verification`, `create replay`.

   **Fix:** preserve the reservation when verification of `ORDER_ALREADY_CAPTURED` fails. Persist prior create uncertainty and prevent a refused replay from releasing it without establishing the original outcome. Add `test_already_captured_verification_401_keeps_reservation` and `test_uncertain_create_replay_401_keeps_reservation`.

2. **P2 — Tool dispatch defeats strict quantity validation and can lose partial results.**  
   `app/agent.py:213`

   `int()` runs before the service validates quantity. Observed: `True` purchases **1**, `1.9` purchases **1**, and `"2"` purchases **2**. A numeric overflow (`json.loads("1e309")`) raises uncaught `OverflowError`. When preceded by a successful purchase, `/agent/request` returns **500** although **$12.99 was captured**.

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_current_review.py
   PYTHONPATH=. .venv/bin/python /tmp/grb_agent_edges.py
   ```

   **Fix:** validate tool arguments without coercion; require a non-boolean integer in `1..100`. Return structured validation errors while preserving prior results. Add `test_tool_quantity_types_are_rejected` and `test_invalid_quantity_preserves_partial_purchase_result`.

3. **P2 — Reaching the turn limit hides completed purchases from the final answer.**  
   `app/agent.py:261`

   A scripted model creates one cable order, then searches until the default eight-turn limit. Observed: **$12.99 captured**, but the entire final answer is `"Stopped: too many steps without finishing."` It omits the purchase and order ID, inviting an unnecessary retry.

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_max_turns.py
   ```

   **Fix:** include `fallback_summary(res.transcript)` when exhausting turns, followed by the stop reason. Add `test_max_turns_reports_orders_already_created`.

4. **P2 — Offline quantities above 100 produce an empty answer.**  
   `app/offline_agent.py:313`

   `"Buy 101 USB-C cables"` passes parsing. Both policy and order tools return quantity errors, which the summary discards. Observed: HTTP **200**, no order, final text only `"[offline rule-based mode, no AI]\n"`.

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_agent_edges.py
   ```

   **Fix:** reject unsupported quantities before dispatch or include tool errors in the summary. Add `test_offline_quantity_over_100_explains_refusal`.

5. **P3 — Non-ASCII cancel state returns 500 instead of 403.**  
   `app/service.py:454`

   For a known order, `/paypal/cancel?token=<id>&state=é&format=json` raises `TypeError` because `compare_digest()` does not accept non-ASCII strings. Observed: **500 Internal Server Error**; reservation remains intact.

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_current_review.py
   ```

   **Fix:** validate the expected ASCII hexadecimal format before comparison. Add `test_non_ascii_cancel_state_returns_403`.

6. **P3 — Configuration and reconciliation documentation drift.**  
   `.env.example:22`, `README.md:64`, `README.md:364`

   - `.env.example` says an empty approver token means open access. Observed: **503** unless `DEMO_MODE=1`; POST cancel also requires the token.
   - README says ambiguous outcomes receive an immediate provider status check. Observed: ambiguous create and `COMPLETED/PENDING` capture both enter reconciliation with **zero immediate `get_order()` calls**. Immediate checking occurs after capture exceptions.

   ```bash
   PYTHONPATH=. .venv/bin/python /tmp/grb_doc_checks.py
   ```

   **Fix:** update the sample comment and narrow the immediate-reconciliation claim.

## Checked and OK

- Exact cap boundaries, Decimal rounding, timezone conversion, merchant/category/currency denial, DENY precedence, quote binding, and approval waiving only soft caps passed.
- Concurrent reservations and approval claims, split aggregation, stale `op_gen` rejection, and capture versus cancellation/expiry regressions passed.
- Parallel HTTP return and POST capture produced **one provider capture**, with **$12.99 retained**.
- Header/Bearer authentication, empty-token fail-closed behavior, and explicit demo mode passed.
- Unknown/path-like return tokens yielded **404**; both mock endpoints yielded **404** with the sandbox client.
- OAuth caching and refresh passed. Callback URLs use configured URLs; return redirects stay local.
- SQL values are parameterized; inspected user/model text rendering uses text nodes. No reachable XSS found in those paths. The jsdom test was skipped.
- README’s **221 passed, 1 skipped** claim matches. Remaining documented API defaults and accounting statuses match the inspected code. Commands implemented in excluded scripts and linked live-payment evidence were not independently verified.