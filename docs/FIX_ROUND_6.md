# Fix round 6 — response to CODEX_REVIEW_3

Date: 2026-10-30. Baseline `fe14faa` (148 tests). Code commit `9709fba`. After this round: **196 tests pass**
(`python -m pytest -q`). No remote, push, publish, or live Claude/PayPal calls were made. Provider failures
are simulated with local fakes (mock provider, patched methods). That is not live Sandbox evidence.

## A. Residual bugs from review 3

| # | Fix | Regression tests (`tests/test_review3_fixes.py`) |
|---|---|---|
| **1** (P2) upgrade loses capture uncertainty | `Store.__init__`: if `orders.capture_unknown` did not exist yet (DB from a version before fix round 5), right after adding the column it runs `UPDATE orders SET capture_unknown=1 WHERE status IN ('NEEDS_RECONCILIATION','CAPTURING')`. This is conservative: a legacy order whose payment outcome was open can never go back to `PAYER_ACTION_REQUIRED`, be cancelled or be expired. It can only be resolved forward from the provider. The backfill runs only once, when the column is added. Reopening a migrated DB does not run it again. | `test_migration_backfills_capture_unknown_for_open_legacy_orders` builds a DB with the **exact 51b02f7 schema** (embedded in the test, so it also runs from a `git archive` without history). Only `NEEDS_RECONCILIATION` and `CAPTURING` rows get flag 1. A later reopen does not re-backfill. `test_legacy_ambiguous_capture_cannot_be_cancelled_after_upgrade` is the review repro: the legacy order is `NEEDS_RECONCILIATION`, the provider reports awaiting buyer, reconcile keeps `NEEDS_RECONCILIATION`, cancel returns `NOT_CANCELLABLE`, expiry does nothing and **$49 stays reserved**. A later confirmed capture reconciles it to `COMPLETED`. Both tests **fail with the backfill disabled** (checked). |
| **2** (P2) purpose text changes quantity | `OfflineAgent.parse_clause()` parses each clause **once** into `{verb, quantity, quantity_text, item, purpose}`. Quantity comes only from the slot directly before the item: a number, `a/an`, `one…ten`, `zero`, `pair`/`couple`/`dozen`/`single`, `a dozen`, `a pair (of)`, `a couple (of)`, `a single`. If another number or number word directly follows the quantity (`two three`, `2 3`, `2 two`, `a 2`, `two dozen`, `one a`), the clause is refused as an **ambiguous quantity**. A number word inside the item phrase is also refused. The purpose tail is still validated but is never read for quantity or item. `parse()` uses the same structured result. The old `_quantity()` whole-clause rescan and `QTY_RE` were removed. An ungrammatical clause gets `product_id=None, quantity=0`. | "Buy a USB-C cable for two interns" → qty **1** (and an end-to-end run orders 1 × $12.99). "…for zero downtime" → 1. "3 USB-C cables for one intern" → 3. "a couple of snack boxes for ten people" → 2. Eight ambiguous forms are refused before any tool call. There are also a structured-output test and a parse-marks-unexecutable test. The tests **fail on the old parser** (checked). All earlier B8 grammar tests still pass. |
| **3** (P2) CLI evades mock DB ownership | `app/cli.py` now calls `svc.invalidate_lost_mock_orders()` before running. That takes the same exclusive `<db>.mock.lock` as the server, then (as owner) marks lost mock orders `MOCK_RESET`. On `MockLockError` it prints a clear error to stderr and exits with **code 3**. The lock is released when the CLI exits. This does nothing for the default in-memory DB and for Sandbox mode. README guidance is corrected: never point `CLI_DB_PATH` at a running mock server's DB. Sharing a DB is only possible in Sandbox mode. | `test_cli_refuses_mock_db_held_by_server` (a real subprocess): with the server holding the lock → exit 3 with the message, and **no order inserted**. After release → the CLI runs with exit 0. `test_cli_default_in_memory_unaffected`. |
| **4** (P3) invalid `AGENT_RATE_LIMIT` crashes requests | `parse_rate_limit` is strict. It accepts `N/SECONDS` or `N` (meaning per 60 s), where both are positive whole numbers. `off`, `none` or a bare `0` disable it. Anything else raises `ConfigError`. `create_app` validates **before** opening the DB, and `RateLimiter` also rejects a non-positive or infinite limit. Module-level startup turns `ConfigError` into `SystemExit("ERROR: AGENT_RATE_LIMIT='0/60' is invalid: …")`, with no traceback. | 10 invalid values (`0/60`, `-1/60`, `5/0`, `abc`, `1.5/60`, `5/inf`, `/60`, `5/`, …) are rejected. 7 valid forms are accepted. `create_app` raises. A real `import app.main` subprocess exits non-zero with the message and no `IndexError` or traceback. |

## B. Docs

- `requirements.txt` comment now says 196 tests.
- `docs/REQUIREMENTS_CHECK.md`: AG Grid Community 32.3.3 is **vendored** (no CDN). There are now 4 grids, including the Audit grid. Test counts are updated.
- README:
  - Purpose-tail claim narrowed. It no longer says "never executed". It now says the tail is not used for quantity or item selection and is screened by a denylist, which is not a complete guarantee.
  - Quantity-slot rule documented.
  - CLI/`CLI_DB_PATH` guidance corrected in repro step 8 and in the mock single-process paragraph.
  - `AGENT_RATE_LIMIT` validation documented.
  - New known-limitation paragraph on **legacy DB upgrades**: what the conservative backfill does, to stop servers and back up first, and that old orders have no audit timeline.
  - `/decisions` and `/decisions/{id}` added to the API table.
  - New repro step 9 (Audit tab).
  - Test counts set to 196.
- `docs/DEVPOST.md`: dashboard/Audit description and test counts.
- **Not changed:** `docs/VIDEO_SCRIPT.md` and the draft narration still say "148 tests". The narration audio was not rebuilt, so the script text was kept consistent with the recorded audio. Update both when the video is rebuilt.

## C. AG Grid sponsor upgrade: Audit tab (Community 32.3.3 only, vendored, no Enterprise)

**Persistence** (new tables, created on any DB, including legacy ones):
- `decisions`: one row per guardrail evaluation, written in the **same transaction** as the reservation/approval it leads to. Stages:
  - `check` (the agent's read-only `check_policy`)
  - `order` (`create_order`: `RESERVED` / `NEEDS_APPROVAL` / `DENIED`)
  - `approval_request` (`APPROVAL_REQUESTED` / `DENIED` / `NOT_NEEDED`)
  - `approval_execute` (human approval: `RESERVED` with rule hit `human_approval` / `DENIED` by a re-checked deny rule)

  Each row stores: timestamp, request_id, product, merchant, category, quantity, amount, currency, verdict, outcome, `rule_hit`, reasons, every rule evaluated, spend before, order id and approval id.
- `order_events`: every order status transition (`None→CREATING` at reservation, every `_transition`, and expiry), with op generation and note. This is the payment timeline.
- `guardrails.explain_rules()` lists every rule `evaluate()` applies:
  - Rules: currency, positive amount, merchant blocklist/allowlist, category allowlist, hard max (incl. split aggregate), per-order cap, daily cap.
  - Outcomes: PASS / DENY / NEEDS_APPROVAL / WAIVED (human approval) / SKIPPED (a deny already decided).

  A test checks that its verdict agrees with `evaluate()` across every catalog product × 3 quantities × 2 spend levels.

**API:**
- `GET /decisions?limit=` returns newest first, joined with the current order status (also through the approval's order) and approval status, plus a computed `risk` (`NEEDS_RECONCILIATION` > `DENY` > `NEEDS_APPROVAL` > `ALLOW`).
- `GET /decisions/{id}` returns the decision, quote, rules, approval, order, timeline and related decisions (same request + SKU). It returns 404 if the decision is unknown.

**Dashboard:**
- **Operations | Audit** tabs. The existing three grids are unchanged, apart from added `getRowId`.
- The Audit grid shows timestamp, decision (allow/approval/deny), rule hit, amount, merchant, item, stage, outcome, payment status, request id and decision id. It has:
  - `floatingFilter` column filters (text/number/date)
  - a quick-filter search box (`quickFilterText`)
  - a "hide read-only checks" external filter
  - risk highlighting through `rowClassRules` plus `cellClassRules` for DENY / NEEDS_APPROVAL / NEEDS_RECONCILIATION, with a legend
  - **Export CSV** (`exportDataAsCsv`)
  - `getRowId` for stable delta updates on the 5 s refresh (selection kept)
  - single-row selection
- Clicking a row opens the detail panel with the decision, quote, rules evaluated (colour-coded), approval, and the payment timeline (`CREATING → PAYER_ACTION_REQUIRED → CAPTURING → COMPLETED`, with timestamps and notes). The panel is built with DOM `textContent` only (no data put into `innerHTML`).

**Tests (API/asset level):**
- Every decision is persisted with the right verdict, rule hit and risk.
- Detail: quote, rules, timeline across a full capture with op_gen 1–4.
- Approval execution is audited, with soft caps `WAIVED`, and linked to its order.
- `NEEDS_RECONCILIATION` highlighting after a 503 capture.
- `explain_rules` agrees with `evaluate`.
- The decisions table migrates into a legacy DB.
- Dashboard references resolve through TestClient: all 5 `/static` assets return 200, every `$("id")` the script looks up exists in the HTML, the required AG Grid APIs are present, and there is no Enterprise/Set filter.

**Rendering check:** a Playwright Chromium screenshot was not possible (blocked here). Instead:
- `node --check app/static/app.js` passes.
- New `scripts/ui_smoke_jsdom.cjs` loads the served page with the **real vendored AG Grid bundle** and `app.js` in jsdom against a live mock server. It checks:
  - 7 audit rows rendered, with `risk-DENY` ×2 and `risk-NEEDS_APPROVAL` ×3 row classes and DENY cell classes
  - floating filters present
  - quick filter "Shady" → exactly the 2 Shady Gifts rows
  - CSV export → `guardrail-audit-YYYY-MM-DD.csv` with 7 data lines and the expected header
  - row click → detail panel with Quote / Rules evaluated / Payment timeline / `per_order_cap`
  - orders and approvals grids still populated
  - no page errors

  Result: `UI SMOKE OK`. jsdom has no layout engine, so this checks behaviour, **not visuals**. Column widths, colours and the tab layout are still unverified in a real browser.

## Verification

- Full suite: **196 passed** (5.1 s, one Starlette/AnyIO deprecation warning).
- Concurrency/recovery: `tests/test_concurrency.py tests/test_review2_fixes.py tests/test_review3_fixes.py` run **5×**: 77 passed each time. `tests/test_concurrency.py` was also run alone 5× (9 passed each).
- Fresh `git archive HEAD` (`9709fba`) in `/tmp/grb-r6-qn5j`, README offline block extracted unedited (`offline-verbatim.sh`) and sourced in `env -i bash` with a new `.venv`, pinned install:
  - Tests: **196 passed**.
  - Port 8000 is **occupied in this sandbox by an unrelated process** (a cloudflared tunnel), so the block's ready check correctly printed `SERVER NOT READY … port 8000 busy?`, killed its server, and still ran the CLI (`log: offline-verbatim.log`).
  - Rerun changing only `PORT=41379` (`offline-freeport.sh/.log`, as review 3 also did): 196 passed, `server ready`, mixed request reserved **$25.98**, keyboard needs approval, gift card refused, CLI worked, `/decisions` returned the 7 expected rows.
  - The servers started were killed and waited for; no uvicorn left running.
- UI smoke log: `/tmp/grb-r6-qn5j/ui-smoke.log`.

### Repro

```bash
python -m pytest -q                                   # 196 passed
python -m pytest -q tests/test_review3_fixes.py       # 48 round-6 regressions
# UI behaviour (needs node + `npm i jsdom@24`), against a running offline/mock server with some requests made:
NODE_PATH=<dir>/node_modules node scripts/ui_smoke_jsdom.cjs http://127.0.0.1:8000
```

## Behaviour changes worth knowing

- "two dozen sticky notes" is now refused as ambiguous. Before, it silently ordered 2.
- Every `check_policy` call now writes an audit row. The `decisions` table grows with use. `GET /decisions` returns at most 1000 rows by default (max 5000), and there is no pruning.
- A legacy order in `NEEDS_RECONCILIATION` with no PayPal id (create outcome unknown) is also flagged `capture_unknown=1`. Reconcile replays the create. If the replay finds the order awaiting the buyer, it becomes `PAYER_ACTION_REQUIRED`, but it stays non-cancellable and non-expirable because the flag remains. That is deliberately conservative: it can still be captured, or resolved by hand.

## What's left

1. Real Claude → Sandbox buyer approval → capture recording. Then rebuild the narration (test count and the Audit tab), replace the repo/video placeholders, and publish (user action).
2. Requester authentication/ownership, auth on listings/`/decisions`, a shared rate limit and resource limits before any shared hosting. The local-demo scope is unchanged.
3. Visual check of the Audit tab in a real browser, and an updated `docs/dashboard.png`. Audit-log retention/pruning.
4. B7 multi-merchant routing / split aggregation across SKUs is unchanged (README known limitations).
