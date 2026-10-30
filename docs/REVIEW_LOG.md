# Review log

Every cross-check round: what each reviewer found and how it was fixed. Earlier rounds (1–4 + UI) are
in `docs/CODEX_REVIEW_1..4.md`, `docs/CODEX_REVIEW_UI.md` and `docs/FIX_ROUND_4..7.md`.

## Round 5 — 2026-10-30 (Asia/Taipei), cross-check before release

Reviewers: **Hark** (judge-style reproduction from a fresh clone, rules check, privacy scan) and
**OpenAI Codex CLI** (independent code review + two re-reviews). Baseline commit `c2d5849`.

### A. Hark: judge-style reproduction (fresh `git clone` into a temp dir, README only, no secrets)

Followed the README "No keys, no network: copy-paste offline block" verbatim in a fresh bash shell,
then Repro steps 2–9 against that server. Python 3.12.

| Step | README says | Observed | Result |
|---|---|---|---|
| venv + `pip install -r requirements.txt` | installs | installs (pinned versions) | ✅ |
| `python -m pytest -q` | `221 passed, 1 skipped` | `221 passed, 1 skipped` (baseline) | ✅ |
| uvicorn + ready check | `server ready: …` | `server ready: http://127.0.0.1:8000/` | ✅ |
| agent request (mixed basket) | cables order, keyboard approval, gift card refused | exactly that, offline label shown | ✅ |
| `GET /spend` | `total 25.98` | `reserved 25.98, total 25.98` | ✅ |
| CLI one-shot | tool calls + verdicts | 2 approvals requested, `spent_today=0.00` | ✅ |
| `/policy` | caps + `spent_today` | ✅ (day is the policy-timezone day, UTC) | ✅ |
| approve without token / with `demo` | `401` / new order, `total 114.98` | `401` / order awaiting buyer, `total 114.98` | ✅ |
| mock checkout → Pay now | lands on dashboard, `COMPLETED` | 303 → `/?paid=ORD-…&status=COMPLETED`, capture id `MOCKCAP-…` | ✅ |
| reject | `REJECTED` | `REJECTED` | ✅ |
| `/decisions`, dashboard assets | audit rows, vendored AG Grid | rows with rule hits; all static assets 200 | ✅ |
| `pytest tests/test_ui_jsdom.py` with jsdom | Audit-tab regressions | `1 passed` | ✅ |

Problems found:

- **R1 (fixed) — `scripts/ui_smoke_jsdom.cjs` was timing-dependent.** It used fixed sleeps (1.5 s for the
  first refresh, 0.8 s after opening the Audit tab, 3 s for the detail panel). On a loaded 2-core machine it
  failed with `audit rows rendered: 0` / `detail missing Quote` although the app was fine. Fix: every wait is
  now a poll on the condition it needs (rows rendered, filter applied, CSV blob, detail rendered) with
  generous timeouts. Re-run: `UI SMOKE OK`.
- **R2 (fixed) — Claude mode without a key gave a bare 502.** Repro step 2 starts the server in the default
  Claude mode; a judge without an Anthropic key got `Messages API HTTP 401 … x-api-key header is required`.
  Fix: on 401/403 with no `ANTHROPIC_API_KEY` set the error now says to put the key in `.env` or restart with
  `AGENT_MODE=offline` (no key is ever included). Test `test_missing_key_401_hint`.
- **R3 (environment, not the repo)** — `npm i jsdom@24` failed in this sandbox because of an npm cache
  permission error; an existing jsdom 24 install was used via `NODE_PATH`. Nothing to change in the README.
- **R4 (doc drift, fixed)** — README status list still had "Full buyer payment … recorded for the video"
  unchecked although the payment was verified; split into "verified by hand" (done) and "recorded as video
  footage" (open). README "AI tools used" did not list Piper TTS or Codex; added (also in `docs/DEVPOST.md`).
  `docs/REQUIREMENTS_CHECK.md` had stale test counts (196 / 68); rewritten.

### B. Codex independent review (`docs/CODEX_REVIEW_5.md`)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | P1 | Recovery errors could erase spend: (a) capture → `422 ORDER_ALREADY_CAPTURED` whose read-back GET failed (401) became `CAPTURE_FAILED`, spend released; (b) an unknown create whose replay was refused (401) became `FAILED`, spend released while PayPal had the order | **Fixed.** (a) `SandboxPayPal.capture_order` raises an *ambiguous* error when the `ORDER_ALREADY_CAPTURED` read-back fails, and `_capture` treats that issue as uncertain → `NEEDS_RECONCILIATION`, `capture_unknown=1`, reserved. (b) `_create_at_provider(replay=True)` (used by reconcile, incl. stuck `CREATING`): only `400`/`422` from the Orders API releases; anything else keeps the order reserved. Tests `test_already_captured_verification_401_keeps_reservation`, `test_uncertain_create_replay_refusal[...]` (6 cases), `test_first_create_refusal_still_fails_and_releases` |
| 2 | P2 | Tool dispatch coerced quantities (`True`→1, `1.9`→1, `"2"`→2); `1e309` raised `OverflowError` → HTTP 500 after a purchase was already captured | **Fixed.** `_quantity` / `_product_id` validate without coercion; `json_safe` turns non-finite floats into strings before they reach the transcript or go back to the model; `_run_tool` catches everything and returns a structured error. Tests `test_tool_quantity_types_are_rejected` (9 cases), `test_tool_product_id_must_be_string`, `test_invalid_quantity_preserves_partial_purchase_result` |
| 3 | P2 | Turn limit replaced the whole answer with "Stopped: too many steps", hiding a completed purchase | **Fixed.** The answer is the deterministic summary of what happened plus the stop reason. Test `test_max_turns_reports_orders_already_created` |
| 4 | P2 | Offline "Buy 101 USB-C cables" returned an empty answer | **Fixed.** `fallback_summary` reports tool errors (`not ordered: quantity must be …`). Test `test_offline_quantity_over_100_explains_refusal` |
| 5 | P3 | Non-ASCII cancel `state` → 500 (`compare_digest` on non-ASCII str) | **Fixed.** `verify_cancel_state` requires 32 lowercase hex chars before comparing → 403. Test `test_non_ascii_or_malformed_cancel_state_returns_403` (5 cases) |
| 6 | P3 | Docs drift: `.env.example` said empty token = open; README promised an immediate status check for every ambiguous outcome | **Fixed.** `.env.example` documents fail-closed 503 / `DEMO_MODE`; README payment-flow step 5 and Known limitations now say exactly which cases are checked right away |

### C. Codex re-review 1 (`docs/CODEX_REVIEW_5_RE1.md`)

Findings 4, 5, 6 FIXED; 1, 2, 3 PARTIALLY FIXED because of two new issues:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| N1 | P1 | A replay whose **OAuth token request** returned 400 still released the reservation (no Orders call was made) | **Fixed.** `PayPalError.source` = `"oauth"` on every OAuth failure path (HTTP error, transport error, and — added proactively — unreadable/missing token body); the replay rule releases only on `source == "orders"` and 400/422. Tests `test_replay_oauth_failure_keeps_reservation[400/401/422]`, `test_unreadable_oauth_body_is_an_oauth_error` (3 cases) |
| N2 | P2 | The new max-turns summary crashed (`inp.get`) on a non-object tool input → 500 after a capture | **Fixed.** `fallback_summary` normalises non-dict inputs/outputs. Test `test_summary_survives_non_object_tool_input` |

### D. Codex re-review 2 (`docs/CODEX_REVIEW_5_RE2.md`)

N1 and N2 **FIXED**; `source="oauth"` verified on all OAuth failure paths, summary normalisation verified
across max-turns, empty-final and LLM-error exits. **No new findings.** (Codex rounds used: 1 review + 2
re-reviews, the maximum for this round.)

### E. Privacy scan

Scope: every tracked file, the full git history (`git log -p --all`), and OCR (tesseract) of
`docs/dashboard.png` and all six `docs/screenshots/*.png`. Patterns: the personal email / mail provider,
real name (any case), home town, other personal accounts, API keys / OAuth tokens / client ids/secrets,
`trycloudflare` tunnel URLs, internal workspace paths.

- No hits for personal data, keys, tokens, client ids or tunnel URLs in files, history or screenshots.
- Present and acceptable: the PayPal-generated **Sandbox** test buyer `sb-…@personal.example.com` (screenshot
  1 and `docs/SANDBOX_E2E.md`), Sandbox order/capture ids, Sandbox-generated funding instruments on the
  checkout screenshot ("CREDIT UNION 1 ••9319", "Visa ••5612", avatar "JD"), and the commit author's GitHub
  noreply address.
- Internal absolute workspace paths that Codex wrote into its review links were stripped from
  `docs/CODEX_REVIEW_5*.md`. Those reviews mention repro scripts under `/tmp/` that are not part of the repo.
- The draft video (`docs/demo_draft.mp4`) is gitignored and not part of this scan.

### F. Result

`python -m pytest -q` → **255 passed, 1 skipped** (the skip is the jsdom UI test without jsdom; with jsdom it
passes). `scripts/ui_smoke_jsdom.cjs` → `UI SMOKE OK`.

Open (not code): demo-video buyer-payment footage + YouTube upload, public GitHub repo + push, decision on
publishing Sandbox credentials for judges (see `docs/REQUIREMENTS_CHECK.md` G1/G3/G4).

## Final re-check round (2026-10-30, ~09:00–10:00 Asia/Taipei)

Rules and overview re-read (Devpost rules + overview): unchanged. Fresh clone of `56465f2`, README offline block
verbatim in a new venv: `255 passed, 1 skipped`, server ready, mixed request → cables order $25.98 / keyboard
approval / gift card refused, spend `reserved 25.98`, CLI run OK. After this round: `268 passed, 1 skipped`.

| ID | Source | Finding | Fix |
|---|---|---|---|
| F02 | Codex | Repro step 6 `…/approvals/<APR-id>/approve` fails literally in bash (`<` redirection). | Steps 5–7 now capture `APR_ID` and use quoted URLs; verified: 401 without token, approve → total 114.98. |
| F03 | Codex | `docs/DEVPOST.md` and `requirements.txt` still said 196 tests. | Updated (261 tests: 260 + 1 jsdom). |
| F04 | Codex | Devpost testing instructions: after the offline exports, `.env` edits for Claude/Sandbox have no effect. | Added the fresh-shell / `unset` note. |
| F05 | Codex | README roadmap, video-pipeline sentence and limitations not synced with the recorded Sandbox run. | Synced (two real runs, scene 7 automated only with buyer env vars). |
| F06 | Codex | 18 broken relative links in `CODEX_REVIEW_5*.md` / `CODEX_REVIEW_UI.md`. | Turned into inline code; new test fails on any broken relative link or anchor in tracked Markdown. |
| F07 | Codex | Upstream Messages-API error bodies (first 300 chars) reached anonymous `/agent/request` 502 responses. | Only structured `error.type` + short `error.message`, with configured secrets and key-like tokens masked; non-JSON bodies never shown (+4 tests). |
| F08 | Codex | VIDEO_SCRIPT said buyer credentials are "never written anywhere"; the email appears in the raw recording. | Wording narrowed (password env-only; email blurred in the final cut). The committed screenshot shows only PayPal's generated `sb-…@personal.example.com` test buyer (kept). |
| F09 | Codex | `FIX_ROUND_7.md` carried an author email line. | Removed (noreply address, not personal). |
| H1 | Hark | Two docs named the third-party Messages-API gateway host. | Replaced by a generic description in the tree (still in two older commits; history not rewritten, see note). |
| H2 | Hark | Devpost "Known limitations" paragraph sat inside the Accomplishments list. | Moved below the list; demo-video run added. |
| H3 | Hark (upgrade) | No short judge path; AG Grid use not summarised for the sponsor prize; no NEAR disclosure. | "For judges, in 3 minutes", "How AG Grid is used" table, "Other projects by the author". |

Note: the demo video's closing card says "256 tests" (true at recording); the suite now has 269. The video
understates, nothing on screen is false, so it was not re-recorded.

Codex re-verify of the round (`work/` report, not committed): F02/F04/F05/F06/F08/F09 fixed. Follow-ups fixed:

| ID | Finding | Fix |
|---|---|---|
| F03b | `requirements.txt` header count still stale. | Updated. |
| F07b | `error.type` was passed through (sanitised characters only), so a gateway could echo a key in it; secrets shorter than 4 chars were not masked. | `error.type` must be a documented Messages API type, else `unknown_error`; a message containing any configured secret (any length, incl. `PAYPAL_CLIENT_ID`) is withheld entirely (+6 tests). |
| R1 | Intermittent `database is locked` in `test_concurrent_first_open_migrates_once` (1 of Codex's 2 runs). Reproduced deterministically: SQLite does not apply the busy timeout to the rollback-journal → WAL switch while another connection holds a write lock. | `Store` retries the WAL switch up to the 30 s busy timeout (+1 regression test that fails on the old code). |
| G1 | README AG Grid table said every Spend-grid row shows use/headroom; only the daily-cap row does. | Wording fixed. |

Codex re-verify 2: F03, R1 (fails on the parent `service.py`, passes now; finite deadline; 30/30 repeated runs), G1
and all counts pass. One F07 edge case: an object-valued `error.message` was `str()`-ed (escaping a `\` in a
secret) before the secret check. Fixed: non-string `error.type` / `error.message` are dropped (+1 test covering
object, list, number, null and boolean values). Not sent to Codex a third time (loop limit); verified by the
test suite (`268 passed, 1 skipped`) and a manual probe.

## Upgrade round: MCP server + Policy & spend tab (2026-10-30)

Codex (CLI 0.165.0) reviewed the upgrade diff with live probes. Round 1 ran the full suite and ~40 probes but
stopped on a model-capacity error before writing its summary, so the findings were taken from its probe
output (`work/` report, not committed):

| ID | Sev | Finding | Fix |
|---|---|---|---|
| M1 | high | `sync_policy` read the DB override outside the store lock: a slow reader could install an older policy after a newer one (probe: persisted per-order cap 1.00, decision used 100.00). | Read + adopt under the store lock every policy write holds (+ regression test: the writer waits, the next decision uses 1.00). |
| M2 | medium | External split-order aggregation keyed on the client's `session_id`; rotating it split 2 × $329 past a $500 hard max (with loose caps). | All external tool orders are grouped server-side per policy day (`ext-<day>`); `session_id` is only validated (+2 tests). |
| M3 | medium | Concurrent owner edits: a stale full-list save undid another owner's change. | `GET /policy` returns `revision`; `PUT /policy` honours `If-Match` → 409; the dashboard sends it (+1 test). |
| M4 | medium | Dashboard: a poll returning during an edit discarded it. | Re-check dirty / open editor after the await, reload only on a new revision (+1 node test). |
| M5 | low | `max_price` 1e30 / 10**400 crashed the call. | Bounded at 1,000,000, big ints handled (+3 cases). |
| M6 | low | `tools/call` accepted before `initialize`. | Kept lenient (clients SHOULD initialize first; all tested clients do). |

Codex re-verify: M1, M2, M4, M5 fixed; M3 fixed for ordinary stale saves. Two follow-ups, both fixed:

| ID | Finding | Fix |
|---|---|---|
| M3b | `GET /policy` read the fields and the revision separately, so a write in between paired old fields with the new revision. | One locked snapshot (`policy_snapshot`) (+1 test). |
| M2b | An external approval requested before midnight and executed after it aggregated with the old day's group: $658 booked today against the $500 hard max. | The external group is re-resolved to the booking day inside the decision transaction (place, request, approve) (+1 test, fails on the parent code). |

Not sent to Codex a third time (loop limit); verified by the suite (`353 passed, 2 skipped`) and the probes
turned into regression tests.

## Bring your own model (2026-10-30)

`app/llm/`: Anthropic Messages (`ANTHROPIC_API_KEY` or, new, `ANTHROPIC_AUTH_TOKEN` as Bearer) and any
OpenAI-compatible Chat Completions endpoint with tool calling (`LLM_PROVIDER=openai`, `OPENAI_BASE_URL`,
`OPENAI_API_KEY`, `OPENAI_MODEL`). The agent loop keeps Anthropic-shaped messages; the OpenAI adapter
translates requests and responses, so the four tools and `BuyerService` are unchanged. Pure-move refactor:
`Catalog` → `app/catalog.py`, `Store` → `app/store.py` (re-exported from `app/service.py`). Mocked-HTTP tests in
`tests/test_llm_providers.py`. Live smoke, one CLI run per provider (mock PayPal, `claude-sonnet-5-5` through a
gateway that serves both formats): both bought the cable (ORDER_PLACED) and requested approval for the keyboard.

| ID | Source | Finding | Fix |
|---|---|---|---|
| L1 | Codex (medium) | A malformed Chat Completions reply (non-numeric usage, odd tool-call shapes) raised outside `LLMError`, losing the partial result after an order. | Defensive parsing; malformed shapes are an `LLMError`, so the run reports what was already ordered (+tests, incl. purchase-then-garbage). |
| L2 | Codex (medium) | `raise … from e` kept the raw transport exception; an uncaught CLI error printed it in a traceback. | `from None` in both providers; the CLI prints the redacted `LLMError` and exits 1 (+2 tests). |
| L3 | Codex (low) | Numbers / booleans as tool arguments became `{}`. | Only a JSON object is accepted; blank = no arguments; anything else is refused before a tool runs (+12 cases). |
| L4 | Codex (low) | Placeholder hosts in a test and the README. | `localhost` / `<Messages-compatible base URL>`. |

Not re-sent to Codex (one review per change, by plan); verified by the suite (`402 passed, 2 skipped`).

## Pre-submission release check (2026-10-30)

Fresh `git clone` to /tmp, fresh venv, README offline copy-paste block run verbatim (no keys, mock PayPal, rule-based
agent): tests, server ready check, the mixed request (order to pay / approval / refusal), `/spend` and the CLI all as
documented. One Codex release review (judge-readability, modularity, correctness), report kept outside the repo.

| ID | Source | Finding | Fix |
| --- | --- | --- | --- |
| RC1 | Codex (must-fix) | Expired unpaid orders kept counting against the caps in `check` / `place_order` / `request_approval` / `approve` until something read `/spend`. | Those paths call `expire_stale()` first (+test). |
| RC2 | Codex (must-fix) | A malformed HTTP 200 from the Messages API after a purchase raised a 500 and hid the paid order. | `validate_messages_response`: shape-checked, malformed → `LLMError`, so the agent reports the partial result (+9 tests). |
| RC3 | Codex (must-fix) | README repro step 10 promised `ALLOW` after raising only the per-order cap; the daily cap still bites ($114.98 + $89 > $150). | Step 10 also raises the daily cap to 250 (verified). |
| RC4 | Codex (must-fix) | Bring-your-own-model examples were multi-assignment lines that break when pasted into `.env`. | One `VAR=value` per line; one-shot command-prefix example separately. |
| RC5 | Codex (must-fix) | `scripts/sandbox_smoke.py` ignored `.env` and defaulted to `PAYPAL_AUTH=proxy`. | Loads `.env` like the app (unless `GRB_NO_DOTENV=1`); OAuth stays the default. |
| RC6 | Codex (should-fix) | `app/service.py` was 1,037 lines mixing payments, audit views and policy admin. | Split into `app/reporting.py` (audit/spend views) and `app/policy_admin.py` (owner policy edits) as mixins; order statuses moved to `app/store.py`; `service.py` keeps reservation/capture/approval orchestration (743 lines). Public API unchanged. |
| RC7 | Codex (should-fix/nit) | Stale counts in REQUIREMENTS_CHECK, DEVPOST step reference. | Updated to 414 (412 passed + 2 skipped). |

## Dashboard polish + demo video v2 (2026-10-30)

Dashboard polish (2e39522) reviewed by Codex in three UI passes with headless Chromium and the vendored AG Grid in
jsdom; video v2 reviewed by Codex in two passes. Reports kept outside the repo.

| ID | Source | Finding | Fix |
| --- | --- | --- | --- |
| U1 | Codex UI pass 1 | 15 findings (double Ctrl+Enter submit, capture ids as HTML, escalation counts, "unpaid" wording for unconfirmed payments, one connection flag for all resources, Tab skipping grid buttons, polling destroying focus, dialog re-entry, endless first-load spinner, contrast, long tool trace, clipped pill, toast over dialog, phone-width ids, unclear demo example). | 91ae591 (+tests). |
| U2 | Codex UI pass 2 | Checkout URL check accepted `null` (opened `/null`) and any http(s) host. | `safeUrl` accepts only https `paypal.com` / subdomains or the app's own `/paypal/mock-checkout` (same origin or loopback); a checkout opens only for `PAYER_ACTION_REQUIRED` (143f547, +12 URL cases, jsdom open check). |
| U3 | Codex UI pass 2 | Outcome list keyed by product hid a second purchase of the same product. | One line per purchase attempt (product + quantity); intermediate calls collapse; finished attempts stay separate. Policy dry-run denies (e.g. a gift card refused at `check_policy` with no order call) are listed too (143f547, +node test). |
| U4 | Codex UI pass 2 | "Could not load" stayed after recovery; an audit that never loaded said "No guardrail decisions yet". | Separate error / loaded state per resource; error overlay on a failed first load (143f547). |
| U5 | Codex UI pass 2 | Shift+Tab skipped the Approve / Reject buttons. | Backward entry focuses the last control, then the previous one, then leaves the cell (143f547, jsdom test; checked in Chromium). |
| U6 | Codex UI pass 2 | At 390 px Approve and Pay were off screen. | Below 700 px technical columns hide, the action column is pinned right and a "swipe sideways" cue appears when columns still overflow (143f547; Chromium: Approve at x=174, Pay at x=192 inside the 27–363 px grid). |
| U7 | Codex UI pass 3 | AG Grid keeps a mounted overlay when its template changes, so the U4 text still did not change on screen. | Re-show the no-rows overlay after changing the template; the jsdom test now asserts the rendered overlay text (ce25429; the test fails with the re-show removed). |
| V1 | Codex video pass 1 | No P1/P2. P3: brief dashboard reload flash at the end of the inserted clip. | The clip's last COMPLETED frame is held until the closing card. Other P3s left (captions briefly over the spend legend; the capture-before-approval sentence is narrated, shown in `docs/SANDBOX_E2E.md` / `scripts/sandbox_smoke.py`; PayPal's fake Sandbox persona details). |
| V2 | Codex video pass 2 | No P1/P2; splice clean. | — |
| D1 | Codex docs check (25914ef) | No P1/P2; counts (418/3, 420/1 with jsdom, 415/6 without node) and video length/order verified. P3s: README step-10 cap arithmetic reversed; Approve/Reject steps did not mention the confirmation dialog; "added after the video was recorded" no longer accurate for v2; "submitted" cut / YouTube link wording while the upload is pending; v2 reproducibility overstated. | All five reworded. |

Counts after these changes: 421 tests — `418 passed, 3 skipped` with Python + node (no jsdom), `420 passed, 1 skipped`
with jsdom, `415 passed, 6 skipped` without node. Video v2: 2:18 (138.4 s), OCR-checked (1 fps).
