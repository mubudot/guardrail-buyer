# Fix round 7 — response to CODEX_REVIEW_4

Base `ceb6898` → fix commit `c714baa` (+ this report). Local only: nothing pushed, no remote, no
publication, no PayPal call (live or Sandbox).
`docs/CODEX_REVIEW_4.md` is committed in `c714baa`.

**Tests: 196 → 214** (213 pass + 1 jsdom UI test that is skipped without node/jsdom; 214 pass with
`GRB_JSDOM_NODE_PATH` set). New: `tests/test_review4_fixes.py` (17), `tests/test_ui_jsdom.py` (1, drives
`scripts/ui_review4_jsdom.cjs`). Four assertions in `tests/test_review3_fixes.py` were adapted to the new
paged `/decisions` shape (`["items"]`, rules now loaded from the detail).

## N1 (P2) — versioned, atomic, corrective migrations — fixed

`app/service.py` `Store.migrate()`: the schema version is `PRAGMA user_version` (in the DB header,
transactional). Migrations:

| v | migration | content |
|---|---|---|
| 1 | `_m1_base` | base tables, round 1–4 columns, spend, meta, indexes |
| 2 | `_m2_recovery_columns` | `op_gen` + `capture_unknown` ALTERs **and** the conservative backfill (when it added the column) |
| 3 | `_m3_capture_unknown_corrective` | re-backfill `capture_unknown=1` for every `NEEDS_RECONCILIATION`/`CAPTURING` order — repairs DBs already migrated by round 5 (column, no backfill) or round 6 (backfill skipped / ALTER autocommitted before a crash) |
| 4 | `_m4_audit` | decisions, order_events, indexes incl. new `decisions_req(request_id, product_id, created_at)` |

Each migration runs in **one** `BEGIN IMMEDIATE … COMMIT` holding its DDL, backfill and `PRAGMA user_version = N`
(execute() only — `executescript()` would commit implicitly). The version is re-read under the write lock, so
concurrent first opens migrate once. Any exception rolls the whole migration back. A DB with a version newer than
the code is refused. Rounds ≤ 6 never set `user_version`, so every existing DB is version 0 and gets v1–v4 once;
later restarts run nothing (a NEEDS_RECONCILIATION created after the upgrade keeps its real flag).

Regression tests: round-5-migrated and round-6-migrated DB (flag 0 → 1, reconcile stays
`NEEDS_RECONCILIATION`, cancel `NOT_CANCELLABLE`, nothing released); interrupted upgrade via a child process
that `os._exit(77)`s from a SQLite trace callback (a) right before the backfill UPDATE, (b) between the two v2
ALTERs (verifies no lone ALTER survives), (c) inside v4 — each reopen completes the upgrade with flag 1 and
$49 still reserved; injected failure rolls back fully; runs-once; 6 concurrent first opens; newer-schema refusal.

Reviewer repro with the **real old code** (`git archive caed3a3` / `052727e` Stores, then current code), log
`/tmp/grb7probe/probe.log`:
```
r5+round7    {'schema': 4, 'flag': 1, 'reconcile': 'NEEDS_RECONCILIATION', 'cancel': 'NOT_CANCELLABLE', 'before': '49.00', 'after': '49.00'}
r5+r6+round7 {... same ...}
r6+round7    {... same ...}
```
(review 4 had `flag 0 → PAYER_ACTION_REQUIRED → CANCELLED → $0.00`).

## N2 (P2) — mock lock on canonical DB identity — fixed

`BuyerService.db_identity()` = `(os.path.realpath(db), st_dev, st_ino)`. `acquire_mock_lock()` puts the lock
file next to the **resolved** file (`<realpath>.mock.lock`), so symlink / relative / `..` aliases contend for one
lock; the error names the real path and the alias it was resolved from. A hard link has no canonical path, so a DB
file with `st_nlink > 1` is refused in mock mode (fail closed). After locking, dev/inode are re-checked (file
swapped between resolve and lock → refuse). The lock file records `pid dev ino db`. Lock is still taken before
startup recovery (`invalidate_lost_mock_orders`) in server and CLI.

Tests: real subprocess `python -m app.cli` with `CLI_DB_PATH=links/alias.db → owned.db` while a service owns
`owned.db` with an awaiting $49 order → **exit 3**, order still `PAYER_ACTION_REQUIRED`, no new order, $49
reserved, no `alias.db.mock.lock` created; after the owner stops, the alias may own it (exit 0). Plus `../sub/../x.db`
relative alias, and hard-link refusal.

## N3 (P3) — Audit detail identity/freshness — fixed

`app/static/app.js`: each detail request carries a selection generation (`detail.gen`); a response for an older
selection, or arriving after **Close**, is dropped. While the Audit tab is visible every poll re-fetches the open
detail quietly (no "Loading…" flash; a failed refresh keeps the old snapshot and says so) and the panel shows
`Snapshot <time> · refreshes while the Audit tab is open` (from the API's new `as_of`).
jsdom test: A delayed 700 ms then B → panel stays B (no A justification); Close with A in flight → panel stays
hidden; open pending approval → reject via API → poll → panel shows `REJECTED`.

## N4 (P3) — CSV formula neutralisation — fixed

`processCellCallback: csvCell` → `csvSafe()` prefixes `'` to any text starting with `= + - @ TAB CR` (numbers left
numeric); headers go through the same function. jsdom test with a catalog merchant `=1+1`: export contains
`"'=1+1"` and zero cells starting with a formula character.

## N5 (P3) — bounded history, paging, polling, projection — fixed

- API `GET /decisions` now returns `{items, total, limit, before_id, has_more, next_before_id}`; `limit` 1–5000
  (default 1000); `before_id` = keyset cursor on `(created_at, rowid)` (stable while new rows arrive, ties on equal
  timestamps handled); unknown cursor → 404. *Breaking change of the response shape* (was a bare list); README updated.
- Compact list projection (no `rules`/`spent_before`); detail stays lazy (`/decisions/{id}`).
- Related-decision query bounded (`RELATED_MAX = 50`) with `related_total`, served by the new `decisions_req` index
  (asserted via `EXPLAIN QUERY PLAN`).
- Dashboard: label `Showing newest N of TOTAL decisions · search, filters and CSV cover loaded rows only` (or
  `Showing all N`), tab count = total, **Load older** button follows `next_before_id` and merges by id; polls keep
  loaded older rows. `/decisions` is fetched **only while the Audit tab is visible** (and the page is visible); the
  audit refresh has its own non-overlapping guard, and the main refresh no longer overlaps (a request during a
  running refresh schedules exactly one more).
- Measured on a synthetic 100,001-decision DB (same fixture shape as review 4): newest page 14 ms / **586 KB**
  (was 1.385 MB, and now only while the tab is open); page 2 by cursor 13 ms; detail of a 100k same-request group
  8 ms / **11 KB** (was 14.7 MB / 287 ms).
- Tests: 1003 decisions with identical timestamps → page 1 = 1000, page 2 = exactly the 3 oldest, no gaps/dupes;
  cursor stable after a new decision; an old decision beyond the first 1000 is reachable; compact list vs full
  detail; bounded related + index. jsdom: hidden tab → 0 `/decisions` requests; tab open → "newest 1000 of 1003",
  Load older → "Showing all 1003", poll keeps rows; leaving the tab stops audit polling.

## Live Claude smoke (AI mode, mock PayPal)

One run, no retry: `AGENT_MODE=claude MODEL=claude-sonnet-5-5 ANTHROPIC_BASE_URL=<Messages-API-compatible gateway>
PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1`, auth injected by the gateway (`ANTHROPIC_API_KEY` unset). Request
"Buy 2 USB-C cables, a mechanical keyboard and a $100 gift card for the team." → 4 turns, 12.5 s,
8,763 in / 377 out tokens; cables ALLOW → order $25.98 reserved awaiting payer; keyboard NEEDS_APPROVAL → approval
requested; gift card DENY, not ordered. Matches the offline demo. Note: the model **invented** a justification
detail ("failing shared desk peripheral"). Record: `docs/LIVE_CLAUDE_SMOKE.md`, redacted transcript
`docs/live_claude_smoke_transcript.json` (no secrets; grep-checked).

## Verification / repro

- Working tree: `python -m pytest -q` → 213 passed, 1 skipped; with `GRB_JSDOM_NODE_PATH=<node_modules with jsdom@24>`
  → **214 passed**.
- Concurrency (`tests/test_concurrency.py` + concurrent-first-open + symlink CLI) **5×** in the working tree and
  5× in the fresh archive: 11 passed each run.
- Fresh `git archive HEAD` (`c714baa`) in `/tmp/grb-r7b-JYn5`, new Python 3.12.15 venv, pinned install, `pip check`
  clean. README's first bash block extracted verbatim (`offline-verbatim.sh`), only `PORT=8000` → free port
  **54189** (`offline-freeport.sh`), run in `env -i bash` with a cleanup trap: its suite **213 passed, 1 skipped**;
  server ready; cables reserved $25.98, keyboard needs approval, gift card refused; CLI ran; trap stopped the server.
  Port 8000 not used; no uvicorn left afterwards. Same archive with jsdom: 214 passed.
- Existing `scripts/ui_smoke_jsdom.cjs` (real AG Grid) against a fresh server: `UI SMOKE OK`.
- `node --check app/static/app.js` passes.

## What's left

1. Real Claude → PayPal **Sandbox** buyer approval → capture, recorded for the video; replace submission placeholders.
2. Requester ownership / auth on listing + audit endpoints and shared rate limits before any shared hosting.
3. Audit: loaded older rows are not re-polled (their payment status can go stale until reload); no server-side
   search across unloaded history and no retention/pruning; CSV covers loaded rows only (label says so).
4. Hard-linked DBs are refused in mock mode rather than supported; Windows lock path (msvcrt) untested.
5. Agent prompt: keep approval justifications to the user's stated purpose (live run showed an invented detail).
6. No real-browser visual/layout check of the new label/button (jsdom has no layout).
