**248 passed, 1 skipped**. Separately enabled jsdom: **1 passed**; changed smoke script: **UI SMOKE OK**. No repository files modified.

| Original finding | Verdict | Repro evidence |
|---|---|---|
| 1. Recovery erases spend | **PARTIALLY FIXED** | `grb_already_captured.py` and `grb_wire_replay.py`: retain **$12.99**, `NEEDS_RECONCILIATION`. OAuth `400` still wrongly releases; see new finding 1. |
| 2. Tool validation loses partial results | **PARTIALLY FIXED** | `grb_current_review.py` / `grb_agent_edges.py`: coercions rejected; overflow returns **200** with purchases preserved. Non-object input still causes **500** during summary; see new finding 2. |
| 3. Turn limit hides purchases | **PARTIALLY FIXED** | `grb_max_turns.py`: includes paid order and stop reason. Newly added fallback crashes on malformed input; see new finding 2. |
| 4. Offline quantity >100 gives empty answer | **FIXED** | `grb_agent_edges.py`: explicit `1..100` refusal; no order. |
| 5. Non-ASCII cancel state gives 500 | **FIXED** | `grb_current_review.py`: **403**. Additional checks accept generated states with DB secrets and ASCII/Unicode `STATE_SECRET`. |
| 6. Documentation drift | **FIXED** | `grb_doc_checks.py`: empty token **503**; ambiguous create/pending capture make zero immediate reads, matching revised wording. Test count matches. |

Additional checks: first-create definitive refusals release normally; stuck-`CREATING` recovery uses `replay=True`. `ORDER_ALREADY_CAPTURED` recovery eventually completes with **one capture call**. Nonfinite values serialize safely; catch-all returns structured errors; the new 401/403 hint contains no key. No polling regression reproduced.

1. **P1 — Replay can release spend on an OAuth error.** `app/service.py:486`  
   **Repro:** `PYTHONPATH=. python /tmp/review5_rereview.py`, case `oauth400`. Original create completes upstream but its response is lost; token refresh returns `400`.  
   **Observed:** `NEEDS_RECONCILIATION → FAILED`, spend **$12.99 → $0**, with **no replayed Orders request**. HTTP `400`/`422` alone cannot establish the original outcome.  
   **Fix:** distinguish OAuth failures from Orders responses and retain reservations without definitive evidence about the original create.

2. **P2 — New maximum-turn summary crashes on rejected tool input.** `app/agent.py:302`  
   **Repro:** same script, case `nonobject`: successful purchase followed by `create_order(input=["P-1001"])`, `max_turns=1`.  
   **Observed:** `_run_tool` rejects the input, but `fallback_summary` calls `inp.get()` at line 135: HTTP **500**, **$12.99 captured**, purchase response lost. Confirmed HEAD returned successfully for this case.  
   **Fix:** validate/normalize transcript inputs inside `fallback_summary` and summarize the rejection safely.