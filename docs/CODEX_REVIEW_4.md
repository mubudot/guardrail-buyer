# Independent review 4 — `052727e` / `ceb6898`

Reviewed 2026-10-29 UTC, HEAD `ceb6898`, after reading `CODEX_REVIEW_3.md` and `FIX_ROUND_6.md`. **No P1 found. Two P2 issues remain:** incomplete legacy migration recovery and mock ownership through a DB symlink. The ordinary regression paths pass; the fixes are not complete under the reproductions below. No source modification, commit, push or publication; only this report was added.

## Verification

- Fresh `git archive HEAD`, new Python **3.12.15** venv and pinned install in `/tmp/grb-review4-33z4prym`; `pip check` clean. Full suite: **196 passed**, 4.80s, one Starlette/AnyIO deprecation warning.
- README's first bash block executed in a fresh `env -i` bash from that archive, changing only `PORT=8000` to **54931**, with an outer cleanup trap/readiness assertion. Its own suite: **196 passed**, 5.86s. Instance readiness succeeded; cables reserved **$25.98**, keyboard needed approval, gift card was denied, CLI succeeded. Port 8000 was not used. Servers started for README/UI checks were killed and waited for; final process inspection found none remaining.
- `node --check app/static/app.js` passes. Repository smoke script with **real vendored AG Grid + jsdom** passes: eight seeded audit rows, risk classes, floating filters, quick search, CSV, row detail and populated operations grids. Extra probes cover hostile text, response ordering and stale detail. For the hostile-text probe, columns were reduced to the existing plain merchant/justification fields to make them visible in jsdom's zero-width layout.
- Reproduction artifacts/logs are in that temp directory: `offline-freeport.sh/.log`, `full-suite.log`, `probes.py/.log`, `prior_upgrade.py` / `prior-upgrade.log`, `alias_probe.py` / `alias-probe.log`, `run-ui.sh`, `ui_extra.cjs`, `ui-smoke.log`, `ui-extra.log`. Python probes run with the archived `.venv` from that directory; their DB files must be removed before rerunning. `round5/` is a genuine `caed3a3` archive used in the upgrade probe.
- Local fakes only; no live Claude/Sandbox payment or real-browser layout/visual verification.

## Four residual fixes

| Review 3 item | Verdict |
|---|---|
| Legacy uncertainty backfill | **Partial.** Direct uninterrupted old-schema upgrade passes, including reserved $49, refused cancel/expiry and eventual confirmed completion. Previously migrated DBs and interrupted upgrades remain unsafe (N1). |
| Quantity/purpose parsing | **Fixed for the reported cases.** Purpose words no longer change quantities; repeated quantities refuse before tools. Structured parsing and end-to-end one-cable regression pass. Catalog matching remains heuristic. |
| CLI mock ownership | **Partial.** Same-path subprocess correctly exits 3 without inserting orders; in-memory CLI works. A symlink bypasses ownership (N2). |
| Invalid rate limit | **Fixed.** Positive integer forms and disable forms work; invalid values fail at startup before DB creation, without the previous `IndexError`. |

## New/residual findings

1. **N1 — P2: backfill is neither restart-safe nor sufficient for previously upgraded DBs** (`app/service.py:117–129`). Two independent reproductions:
   - Build the exact legacy schema with an uncertain $49 order; open it once with `caed3a3`'s `Store`, then with current code. The column already exists with flag **0**, so round 6 never backfills it.
   - On a direct upgrade, terminate the child process immediately before the backfill UPDATE (SQLite trace callback, exit 77). The preceding ALTER is already committed in autocommit mode. Reopening sees the column and skips repair.
   Both yield **flag 0 → reconcile to `PAYER_ACTION_REQUIRED` → cancel `CANCELLED` → spend $49.00 to $0.00** when the provider reports awaiting buyer. This reproduces the uncertainty invariant violation, not a proven live captured-payment loss. Use an atomic, versioned migration/backfill with a durable completion marker and explicitly repair DBs already migrated by round 5; add both upgrade regressions.

2. **N2 — P2: DB aliases bypass the mock lock** (`app/service.py:682`, `app/cli.py:34–38`). Have a service own `owned.db` with an awaiting $49 mouse order; create `alias.db` as a symlink to it; run `CLI_DB_PATH=alias.db AGENT_MODE=offline PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1 python -m app.cli 'a USB-C cable'`. CLI exits **0**, acquiring `alias.db.mock.lock` independently of `owned.db.mock.lock`. It marks the live server's original order **`MOCK_RESET`**, releases its $49 and inserts a $12.99 order unknown to the server's provider. The original provider order still exists. Derive ownership from a canonical DB identity before startup recovery; handle/reject aliases consistently and add a real subprocess symlink regression. The pathname-lock weakness predates this round but leaves residual fix 3 incomplete.

3. **N3 — P3: detail identity and freshness are unreliable** (`app/static/app.js:164–193,209–216`). Click A then B; delay A's detail response until after B's. Title/selection stay **B**, but A overwrites the body, including quote and justification. Independently, open a pending approval detail, reject it through the API, then call `refresh()`: API reports **REJECTED**, panel still says **PENDING**. Both reproduced with the real script. Guard responses with the current selection/generation and refresh or label the selected detail snapshot.

4. **N4 — P3: CSV does not neutralise formulas** (`app/static/app.js:149–152`). A merchant value `=1+1` exports as `"=1+1"`; CSV quoting does not make it literal spreadsheet text. Verified through AG Grid's export path; no spreadsheet application was launched. Merchant data currently comes from the local catalog, so this requires catalog-controlled input, not an arbitrary request-only exploit. Neutralise formula prefixes in text cells before export.

5. **N5 — P3: filters/export silently cover only the newest 1000 decisions** (`app/service.py:734–744`, `app/static/app.js:211–214`). Seed 1001+ decisions, then search/export an older row: it is absent even though persisted. Count displays `(1000)` without an overflow warning; there is no paging/history search. API `limit` can increase this to 5000 but cannot page beyond it. Expose the window/total and add cursor-based history search/export.

## Audit correctness, XSS and performance

Decision/reservation writes and status-event writes share their relevant transactions; timeline ordering, approval-to-order joins, rule explanations, risk precedence, unknown-ID 404 and bounded list size pass the suite/code review. No new payment-state transition bug was found in those paths.

**No reachable XSS found in the requested text surfaces.** An `<img … onerror="window.__xss=1">` merchant/justification remained literal text in Audit/approval cells and detail; no injected element or execution occurred. Plain AG Grid cells/tooltips use text, and the detail builder creates text nodes. `statusCell` does interpolate HTML, but its current callers supply internally controlled verdict/status enums. CSV formula handling is a separate issue (N4).

Synthetic **100,001-row** audit history: newest-1000 list took **17ms**, producing **1,385,000 bytes** of JSON. The timestamp index serves ordering; this is not a full-history list scan. Nevertheless, every dashboard polls all rules/reasons every five seconds even with Audit hidden (~277KB/s of JSON per client for this fixture). Related-decision lookup has no request/product index and no result bound: a synthetic 100,000-row same-request group returned **14.7MB**, taking **287ms** before HTTP encoding. This group is a stress fixture, not a normal agent run. Prefer compact list projections, lazy detail loading, bounded/indexed related queries, non-overlapping polling and retention. No P2 performance failure demonstrated at demo scale.

## Top 5 remaining

1. Make uncertainty migration atomic, resumable and corrective for round-5-upgraded DBs (N1).
2. Enforce mock ownership across DB aliases before recovery (N2).
3. Fix Audit detail identity/freshness, CSV text safety and bounded history/polling (N3–N5).
4. Record real Claude → Sandbox buyer approval → capture; update narration/dashboard evidence and replace submission placeholders.
5. Add requester ownership, listing/audit authentication and shared resource limits before shared hosting; existing local-demo limitations remain.
