# GuardRail Buyer

> An AI purchasing agent that pays with PayPal — but only inside the spend guardrails you set.

*Built for the PayPal AI Hackathon (2026). Runs with no network at runtime (mock PayPal, rule-based offline agent, vendored AG Grid), or against the real PayPal Sandbox with Claude. Installing the Python dependencies needs a package index.*

![dashboard](docs/dashboard.png)

Letting an AI agent spend money is risky. GuardRail Buyer puts a deterministic
policy engine between the LLM and the wallet:

| Rule | Effect |
|---|---|
| Per-order cap (default $50) | above it → **NEEDS_APPROVAL** (a human must approve) |
| Daily cap (default $150, per policy timezone) | today's spend + this order above it → **NEEDS_APPROVAL** |
| Hard maximum per order (default $500) | above it → **DENY** |
| Merchant allowlist / blocklist | not allowed → **DENY** |
| Category allowlist | not allowed → **DENY** |
| Currency must match policy | otherwise → **DENY** |

The agent (Claude, via tool use) can search the catalog, dry-run the policy,
place an order, or ask for human approval. **It never calls PayPal directly** —
every order goes through `BuyerService`, which re-evaluates the policy on the
server. A DENY can never be escalated to approval, and approving a request
re-checks every deny rule (including the split-order hard maximum) in case the policy or the
request's other purchases changed; a human approval waives only the soft caps. What exactly is enforced, and what is not,
is listed under [Known limitations](#known-limitations).

## Architecture

```
natural-language request
        │
        ▼
 Claude agent (Messages API, tool use)        app/agent.py
   tools: search_catalog · check_policy · create_order · request_approval
        │
        ▼
 BuyerService ── guardrail engine (pure) ── SpendLedger (SQLite)   app/service.py, app/guardrails.py
        │
        ▼
 PayPalClient: MockPayPal | SandboxPayPal (Orders v2)              app/paypal_client.py
        │   create order → buyer approves on PayPal → /paypal/return → capture
        ▼
 FastAPI: REST API + AG Grid dashboard at /                        app/main.py, app/static/
```

### Payment flow and spend accounting

1. **Check + reserve atomically.** The policy check and the budget reservation run in one SQLite write
   transaction (`BEGIN IMMEDIATE`) *before* PayPal is called, so concurrent requests (threads, or several
   processes sharing the DB file) cannot all pass against the same spend.
2. A policy-ALLOWed (or human-approved) order is then created with PayPal Orders v2 (`intent: CAPTURE`,
   `PayPal-Request-Id` = our order id, so a retry returns the same PayPal order). PayPal returns
   `PAYER_ACTION_REQUIRED` and a `payer-action` link, shown as **Pay with PayPal** in the dashboard.
3. The buyer approves on PayPal and is redirected to `GET /paypal/return?token=<paypal order id>`. The order
   is atomically moved to `CAPTURING`, captured, and redirected to the dashboard (a refresh or a parallel
   return never captures twice).
4. `GET /paypal/cancel?token=…&state=…` cancels **only** an order still awaiting the buyer
   (`PAYER_ACTION_REQUIRED`), atomically. `state` is an HMAC of our order id with a server secret (stored
   in the DB, or `STATE_SECRET`), generated when the order is created and embedded in that order's PayPal
   `cancel_url`; a request without it, or with a forged/other order's state, gets `403`. Orders being
   captured, captured, or of unknown outcome are left untouched.
5. If PayPal's answer is ambiguous (HTTP 5xx, timeout/transport error, capture status `PENDING` or missing),
   the order becomes `NEEDS_RECONCILIATION`, keeps its reservation, and the app asks PayPal for the order
   status. Only a confirmed `COMPLETED` capture is recorded as `COMPLETED`.
   `POST /orders/{id}/reconcile` re-checks later.
6. **Recovery is monotonic and exclusive.** Every status change bumps a persisted per-order generation
   (`op_gen`). A reconcile/capture result is applied only if the generation it started from is unchanged;
   a delayed, superseded provider read is rejected (`result: SUPERSEDED`). The one result accepted
   regardless is a confirmed `COMPLETED` capture (it is final). Once a capture outcome was ambiguous
   (`capture_unknown=1`), the order never returns to `PAYER_ACTION_REQUIRED` and cannot be cancelled or
   expired; it stays reserved until PayPal reports it `COMPLETED`, declined or `VOIDED`. A create replay
   that returns an already `APPROVED`/`COMPLETED` PayPal order is captured/verified, never shown as
   awaiting the buyer.

| Order status | Counts toward the daily cap? |
|---|---|
| `CREATING` (budget reserved, PayPal create in flight) | **yes, reserved** |
| `PAYER_ACTION_REQUIRED` (created, awaiting buyer) | **yes, reserved**, so an agent cannot open many unpaid orders to dodge the cap |
| `CAPTURING` (capture in flight; cannot be cancelled or expired) | **yes, reserved** |
| `NEEDS_RECONCILIATION` (payment outcome unknown) | **yes, reserved** until PayPal confirms either way |
| `COMPLETED` (capture confirmed `COMPLETED`) | yes, captured |
| `CANCELLED` / `EXPIRED` / `CAPTURE_FAILED` / `FAILED` / `MOCK_RESET` | no, reservation released |

Approvals: a human approval is **claimed once** (`PENDING → EXECUTING → APPROVED`) in the same transaction
that creates its single order; repeating the approve call returns the existing order. An approval is
**bound to its quote** (product, quantity, amount, currency, merchant, category): if the catalog changed in
between, approving returns `409 QUOTE_CHANGED`, nothing is bought, and a fresh approval is needed.
Executing an approval re-checks the hard rules inside the same transaction, including the split-order
hard maximum over what the same agent request has already ordered (so two queued $329 monitor approvals
from one request cannot both execute against the $500 hard maximum); the per-order and daily caps are
the only rules a human approval waives.

Split orders: within one agent request (`request_id`, one per `/agent/request` run), orders for the
**same SKU from the same merchant** count together toward the per-order cap and the hard maximum. Three
separate one-mouse orders ($24.50 each) in one request → the third needs approval, exactly like one
three-mouse order ($73.50).

Unapproved orders expire after `PAYPAL_APPROVAL_TTL_MIN` (default 180 min); expiry is checked
lazily on reads and on return. A reservation stays on the day it was made (policy timezone).
`GET /spend` reports `captured`, `reserved`, `total`, `remaining` and `needs_reconciliation` against the caps.

## Quick start

Requires Python 3.11+ (tested with 3.12) and git. `requirements.txt` pins the exact tested versions.

### No keys, no network: copy-paste offline block

Runs the tests, the server and the CLI with the rule-based offline agent and the mock PayPal. Only the
`pip install` line needs network (a package index). Paste into a **fresh** bash shell at the repository root
(the exports below override `.env` for the rest of that shell; see *Switching modes*):

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                         # the only step that needs network

# --- everything below: no API key, no PayPal account, no network ---
export AGENT_MODE=offline          # rule-based parser instead of Claude (applies to server AND CLI)
export PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1          # mock PayPal with a local mock checkout page
export APPROVER_TOKEN=demo         # token for approve / reject / cancel
export DEMO_MODE=1                 # explicit local-demo flag (only an empty APPROVER_TOKEN depends on it)
export GRB_NO_DOTENV=1             # ignore any .env file, so nothing switches back to Claude/Sandbox
export DB_PATH="$(mktemp -d)/guardrail-demo.db"         # throwaway database for this shell
export PORT=8000                   # any free port; if it is busy, pick another one ...
export PUBLIC_BASE_URL="http://127.0.0.1:$PORT"         # ... and keep this in sync (mock checkout + PayPal links)
export GRB_INSTANCE="demo-$$-$RANDOM"                   # lets the ready check recognise THIS server

python -m pytest -q                                     # -> 213 passed, 1 skipped (jsdom UI test)

uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "${DB_PATH%/*}/server.log" 2>&1 &
SERVER_PID=$!
READY=0
for i in $(seq 1 40); do                                # wait up to ~20 s for /health of THIS instance
  kill -0 "$SERVER_PID" 2>/dev/null || break            # server exited: port busy, or mock DB locked
  curl -sf "$PUBLIC_BASE_URL/health" | grep -q "\"instance\":\"$GRB_INSTANCE\"" && { READY=1; break; }
  sleep 0.5
done
if [ "$READY" = 1 ]; then echo "server ready: $PUBLIC_BASE_URL/ (pid $SERVER_PID)"; else
  echo "SERVER NOT READY - see ${DB_PATH%/*}/server.log (port $PORT busy? export another PORT and the"
  echo "matching PUBLIC_BASE_URL, then rerun from the uvicorn line)"; kill "$SERVER_PID" 2>/dev/null; fi

if [ "$READY" = 1 ]; then
  curl -s -X POST "$PUBLIC_BASE_URL/agent/request" -H 'content-type: application/json' \
    -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}' \
    | python -c 'import json,sys; print(json.load(sys.stdin)["final_text"])'
  curl -s "$PUBLIC_BASE_URL/spend"; echo               # cables reserved: total 25.98
fi
python -m app.cli "3 bags of coffee beans and 2 snack boxes"   # CLI: own in-memory DB each run
```

Then open the printed URL (`$PUBLIC_BASE_URL/`), enter `demo` as approver token, click **Pay with PayPal**
on the cables order (local mock checkout → **Pay now**) and **Approve** the keyboard.

**Reset / rerun:** `kill $SERVER_PID` (stop the server) and `rm -f "$DB_PATH"*` (or start a new shell and paste
the `export` lines again, which picks a new temp DB). The ready check fails, instead of silently talking to
another program, when the port is already taken: it only accepts a `/health` answer carrying this shell's
`GRB_INSTANCE`.

**Mock mode is single-process.** The mock PayPal keeps its orders **in memory**, so only one server process
may use a database in mock mode: on startup it takes an exclusive lock file next to the **resolved** DB file
(`realpath($DB_PATH).mock.lock`, so a symlink, relative or `..` alias of the same file contends for the same
lock; a DB file with several hard links is refused in mock mode) and exits with a clear error if another
process holds it (do not use `uvicorn --workers N` with the mock). Only after taking the lock does it treat open mock orders from a
previous run as lost: they are marked `MOCK_RESET` and their reservations released (completed orders stay
as history). The CLI takes the same lock when `CLI_DB_PATH` points at a file (it refuses, exit code 3, while
a mock server holds it). Use a fresh `DB_PATH` for a clean demo. Sandbox mode has no such restriction.

**Database upgrades.** The schema is versioned (`PRAGMA user_version`, currently 4). On startup every pending
migration runs in its own `BEGIN IMMEDIATE … COMMIT` transaction that holds its DDL, its data backfill and the
version bump, so an interrupted upgrade leaves the DB at the previous version and is redone on the next start.
Upgrading a DB from an earlier release marks every order then open in `NEEDS_RECONCILIATION` / `CAPTURING`
as "capture may have happened" (it stays reserved and can only be resolved from PayPal, never cancelled). A DB
written by a newer version is refused.

**Switching modes.** Exported variables win over `.env`, and the offline block sets `AGENT_MODE`,
`PAYPAL_MODE` and `GRB_NO_DOTENV=1` (which disables `.env` entirely). Before following the Claude or
Sandbox instructions below, stop the server and open a **new shell**, or run:
`unset AGENT_MODE PAYPAL_MODE PAYPAL_MOCK_APPROVAL APPROVER_TOKEN DEMO_MODE GRB_NO_DOTENV DB_PATH PORT PUBLIC_BASE_URL GRB_INSTANCE`.

### With Claude (and optionally the real PayPal Sandbox)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then put your Anthropic key in ANTHROPIC_API_KEY
```

`.env` is loaded automatically (python-dotenv); real environment variables win.
The defaults run with a **mock PayPal** (no PayPal account, no network) and the Claude agent.

> **About the offline agent.** `AGENT_MODE=offline` swaps Claude for a small deterministic, rule-based
> parser (`app/offline_agent.py`, clearly **not** AI) with a **positive-only grammar**:
> `[buy|get|order|purchase|need] <quantity> <catalog item> [for/as <short purpose>]`, clauses joined by
> commas, "and", "plus" or ";"; `<quantity>` is a non-negative whole number or a/an/one…ten/pair/dozen, read only from the slot
> right before the item ("two three cables" is refused as ambiguous), and item words must come from the
> catalog vocabulary. Anything else ("Avoid buying …", "… only when
> approved", "Buy -1 …", "1.5", unknown verbs or words) is refused as a whole with the supported grammar in
> the reply, and quantity 0 is never ordered. It calls the *same* tools through the *same* `BuyerService`, so every guardrail, PayPal order,
> approval and refusal behaves exactly as with Claude; the dashboard header shows
> **OFFLINE rule-based agent (no AI)**. Claude remains the default and handles free-form requests.

Run all commands from the repository root with the venv active (paths in `.env` are relative).
The commands below use bash syntax (`VAR=value cmd`); on Windows set the variables in `.env` instead.
If you serve on a port other than 8000, also set `PUBLIC_BASE_URL` to the same port (used for the PayPal
return / cancel links and the mock checkout).

### Repro steps ("do X → see Y")

1. **Run the tests** — `python -m pytest -q` → see `196 passed` (no network, no API key needed).
2. **Start the app** — `APPROVER_TOKEN=demo PAYPAL_MOCK_APPROVAL=1 uvicorn app.main:app --port 8000` (add `AGENT_MODE=offline`, or use the exports from the offline block, if you have no Anthropic key) →
   open <http://localhost:8000/> for the dashboard (or `/docs` for Swagger). Enter `demo` in the
   *Approver token* box. `PAYPAL_MOCK_APPROVAL=1` makes the offline mock behave like the real
   Sandbox: every order waits for a buyer approval on a local mock checkout page.
3. **Check the policy** — `curl localhost:8000/policy` → see the caps, allowlists and `"spent_today": "0.00"`.
4. **Ask the agent to buy something mixed** —
   ```bash
   curl -X POST localhost:8000/agent/request -H 'content-type: application/json' \
     -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}'
   ```
   (or type it into *Ask the agent* on the dashboard) → the cables ($25.98) get a PayPal order
   (**Pay with PayPal**, $25.98 reserved), the keyboard ($89) gets an **approval request**, and the
   gift card is **refused** (blocked merchant + category). See `docs/sample_transcript.json` for a real run.
   Click **Pay with PayPal** → approve → you land back on the dashboard with the order `COMPLETED`.
5. **See what is waiting for you** — `curl localhost:8000/approvals?status=PENDING` → see the keyboard approval with the agent's justification and the policy reasons.
6. **Approve it** — click **Approve** in *Pending approvals*, or
   `curl -X POST -H 'X-Approver-Token: demo' localhost:8000/approvals/<APR-id>/approve` → a new order
   linked to the approval, awaiting the buyer; `GET /spend` shows `total: 114.98`. Without the token → `401`.
7. **Or reject it** — **Reject** / `POST /approvals/<APR-id>/reject` → status `REJECTED`, no money moves.
8. **One-shot from the CLI** — `python -m app.cli "3 bags of coffee beans and 2 snack boxes"` → see each tool call and its verdict (see `docs/sample_cli_run.txt`; without a key: `AGENT_MODE=offline python -m app.cli "…"`, sample in `docs/sample_offline_run.txt`). The CLI starts from an empty in-memory ledger each run. `CLI_DB_PATH=<file>` makes it use a database file; in mock mode the CLI must then own that file exclusively (same lock as the server) and exits with code 3 while a mock server uses it, so do **not** point the CLI at a running mock server's database. Sharing a database between CLI and server works only in Sandbox mode.
9. **Audit every decision** — open the **Audit** tab (or `curl localhost:8000/decisions`) → one row per guardrail decision: the gift card is a red `deny` (rule hit `merchant_blocklist`/`category_allowlist`), the keyboard an amber `approval` (`per_order_cap`), an order whose payment is unconfirmed is highlighted orange. Filter any column, search with the box, **Export CSV**, and click a row → quote, every rule evaluated and the payment timeline (`CREATING → PAYER_ACTION_REQUIRED → CAPTURING → COMPLETED`). The Audit tab says how much of the history it shows ("Showing newest 1000 of N"); **Load older** pages back with the API cursor; it polls only while the tab is visible, and the open detail refreshes with each poll (labelled with its snapshot time). CSV cells that start with `= + - @` (or tab / CR) are prefixed with `'` so spreadsheets do not evaluate them. Headless check without a browser: `node scripts/ui_smoke_jsdom.cjs http://127.0.0.1:8000` (needs `npm i jsdom@24`); `GRB_JSDOM_NODE_PATH=<that node_modules> pytest tests/test_ui_jsdom.py` runs the Audit-tab regressions (skipped without jsdom).

### Against the real PayPal Sandbox

Create a free REST app at <https://developer.paypal.com/> (Apps & Credentials → Sandbox), copy its
client id and secret into `.env`, then:

Use a fresh shell (see *Switching modes*), then:

```bash
# in .env: PAYPAL_MODE=sandbox  PAYPAL_CLIENT_ID=…  PAYPAL_CLIENT_SECRET=…  (PUBLIC_BASE_URL if not :8000)
APPROVER_TOKEN=demo uvicorn app.main:app --port 8000
python scripts/sandbox_smoke.py          # no LLM: guardrails -> real Sandbox orders -> status -> capture-before-approval -> cancel
python scripts/sandbox_smoke.py --app http://localhost:8000 --agent --token demo   # same, through the running app + Claude agent
```

The smoke test creates real Sandbox orders (status `PAYER_ACTION_REQUIRED`, approve link on
`www.sandbox.paypal.com/checkoutnow`), reads their live status, shows that a capture before the buyer
approved is refused by PayPal (`422 ORDER_NOT_APPROVED` → the app answers `409 NOT_APPROVED_YET`, order
and budget reservation unchanged), and cancels them so the reservation is released. Sample output:
`docs/sandbox_smoke_run.txt`.

**Verified end to end (2026-10-30):** real Claude agent → real Sandbox order → a Sandbox personal test buyer logs in
and pays on `www.sandbox.paypal.com` → `/paypal/return` → capture `COMPLETED` (PayPal order `34M919590A962473E`,
capture `8VA9873790040992M`). Timeline, API checks and screenshots: [`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md).

To finish a payment, click **Pay with PayPal**, log in with a Sandbox *personal* test account (Testing
Tools → Sandbox Accounts in the developer dashboard) and pay → PayPal redirects to `/paypal/return`, the
order is captured and shows `COMPLETED` with its capture id. If that redirect is lost (e.g. the app
ran on another host), `POST /orders/{id}/capture` captures it afterwards.

`PAYPAL_AUTH=proxy` is for deployments where an egress proxy injects `Authorization: Basic` for
`api-m.sandbox.paypal.com`; the app then holds no PayPal credentials at all (PayPal REST accepts the
client credentials as Basic auth directly on the Orders API).

### Changing the policy

Edit `policy.yaml` (or point `POLICY_PATH` at a `.json` file) and restart. Example: set
`per_order_cap: 100` → the keyboard in step 4 is bought directly instead of needing approval.

## Demo video pipeline

`scripts/video/make_demo.sh` rebuilds the demo-video draft reproducibly: Piper TTS narration per scene
(`scripts/video/scenes.json`, script in `docs/VIDEO_SCRIPT.md`) → app in Sandbox mode → headless
Chromium (Playwright) drives the dashboard and records the screen → ffmpeg cuts the model's thinking
time and lays the narration on top → `docs/demo_draft.mp4`. No music. The Sandbox buyer login is the
one step it does not automate (see `docs/VIDEO_SCRIPT.md`, scene 7).

## REST API

| Method | Path | Description |
|---|---|---|
| POST | `/agent/request` | `{"request": "..."}` — run the agent; returns final text, transcript, token usage. Rate-limited per client IP (`AGENT_RATE_LIMIT`, default 20/60 s) → `429` |
| GET | `/orders` | all orders (`?status=COMPLETED`) with the policy decision attached |
| GET | `/orders/{id}` | one order; `?refresh=true` (default) adds `paypal_live_status` from PayPal |
| POST | `/orders/{id}/capture` | capture after buyer approval (fallback for a lost redirect); not approved yet → `409 NOT_APPROVED_YET` |
| POST | `/orders/{id}/reconcile` | resolve a `NEEDS_RECONCILIATION` order from PayPal's order status (never charges twice) |
| POST | `/orders/{id}/cancel` | cancel an order still awaiting the buyer, release its reservation (approver token); otherwise `409`; `503` when no `APPROVER_TOKEN` is configured (unless `DEMO_MODE=1`) |
| GET | `/approvals` | approval requests (`?status=PENDING`) |
| POST | `/approvals/{id}/approve` | approve once → quote unchanged? deny rules (incl. split-order hard max) re-checked → PayPal order; a repeat returns the same order; changed quote → `409`. Needs `X-Approver-Token`; with no `APPROVER_TOKEN` configured → `503` unless `DEMO_MODE=1` |
| POST | `/approvals/{id}/reject` | reject, nothing is paid (same token) |
| GET | `/paypal/return?token=…` | PayPal redirect after buyer approval → capture |
| GET | `/paypal/cancel?token=…&state=…` | PayPal redirect on cancel → requires the signed `state` from the order's cancel_url (`403` otherwise); cancels only if still awaiting the buyer |
| GET | `/decisions` | audit log: every guardrail decision (policy check, create order, approval request, approval execution) with verdict, rule hit, amount, merchant, request id, timestamp and the current status of the order/approval it led to newest first, compact rows (rules load in the detail). Paged: `{items, total, limit, has_more, next_before_id}`; `?limit=` (1–5000, default 1000), `?before_id=<decision id>` = keyset cursor for older decisions |
| GET | `/decisions/{id}` | one decision: quote, every rule evaluated (PASS / DENY / NEEDS_APPROVAL / WAIVED / SKIPPED), approval, and the order's payment timeline (status transitions) |
| GET | `/spend` | today's captured / reserved / remaining vs caps |
| GET | `/policy` | active policy + today's spend |
| GET | `/` | AG Grid dashboard |
| GET | `/catalog` | demo products (`?q=keyboard`) |
| GET | `/health` | PayPal mode, model, approver mode, rate limit, `instance` (= `GRB_INSTANCE`) |

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | any Messages-API-compatible endpoint |
| `ANTHROPIC_API_KEY` | – | sent as `x-api-key` if set; leave unset behind a gateway that injects auth on the wire |
| `MODEL` | `claude-sonnet-5-5` | |
| `AGENT_MODE` | `claude` | `offline` = deterministic rule-based parser, no AI and no key (for judges / CI) |
| `PAYPAL_MODE` | `mock` | `sandbox` uses the real PayPal Sandbox |
| `PAYPAL_MOCK_APPROVAL` | `0` | mock only: `1` = orders wait for a simulated buyer approval |
| `PAYPAL_AUTH` | `oauth` | `oauth`: client id + secret → OAuth2 token → Bearer. `proxy`: no credentials in the app, an egress proxy injects `Authorization: Basic` |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | – | Sandbox REST app credentials (`oauth` mode) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | base of the PayPal return / cancel URLs |
| `PAYPAL_APPROVAL_TTL_MIN` | `180` | unapproved orders expire and release their reservation |
| `APPROVER_TOKEN` | – | required for approve / reject / cancel. Empty = those endpoints answer `503` (fail closed) |
| `DEMO_MODE` | `0` | `1` = explicit local demo: an empty `APPROVER_TOKEN` leaves approve / reject / cancel open (logs a warning) |
| `AGENT_RATE_LIMIT` | `20/60` | max `/agent/request` calls per client IP per window (`N/SECONDS`, both positive whole numbers); `off` disables. Anything else (e.g. `0/60`) stops the server at startup with a clear error. In-memory, per process |
| `STATE_SECRET` | random, stored in the DB | HMAC key for the signed cancel `state` |
| `PORT`, `GRB_INSTANCE` | – | used by the README launch/ready check only (`GRB_INSTANCE` is echoed by `/health`) |
| `POLICY_PATH`, `CATALOG_PATH`, `DB_PATH` | `policy.yaml`, `data/catalog.json`, `guardrail.db` | |

## Status / roadmap

- [x] Guardrail engine + SQLite daily spend ledger, unit-tested
- [x] FastAPI backend with approvals workflow, MockPayPal
- [x] Claude tool-use agent wired to the guardrails
- [x] Buyer-approval round trip (create → approve → return → capture), reservations, cancel / expiry — tested with the mock and a wire-format test of the Sandbox client
- [x] Live run against the PayPal Sandbox: orders created, live status, capture-before-approval handled, cancel (`scripts/sandbox_smoke.py`)
- [ ] Full buyer payment with a Sandbox personal account, recorded for the video
- [x] AG Grid dashboard for orders, approvals and spend, approver token
- [x] AG Grid **Audit** tab: every guardrail decision with column filters, quick search, risk highlighting, CSV export and a detail panel (quote, rules evaluated, payment timeline)
- [x] Offline rule-based agent mode for running without an AI key (`AGENT_MODE=offline`)
- [~] Demo video: draft pipeline done; real Sandbox buyer-approval clip pending

## Known limitations

This is a hackathon demo meant to run locally. Be precise about what it does and does not protect:

- **Authentication is local-demo scope (review B6, partly addressed).** There is still no requester identity
  or ownership: anyone who can reach the server can call `/agent/request`, list all orders and approvals,
  and trigger capture/reconcile of an order (capture only succeeds if the buyer approved on PayPal). What
  exists: `/agent/request` has a simple **in-memory per-IP rate limit** (`AGENT_RATE_LIMIT`, per process,
  reset on restart, keyed on the socket address, so it is not a defence behind a proxy or against many
  IPs); approve / reject / cancel need the single shared `APPROVER_TOKEN` (stored in the browser's
  `localStorage`), and an empty token **fails closed** (`503`) unless `DEMO_MODE=1` is set explicitly.
  The public `GET /paypal/cancel` callback is **not** covered by the approver token; it requires the
  per-order HMAC `state` from the order's cancel_url (review B3), and even then only cancels an order
  still awaiting buyer approval. Anyone who obtains that cancel URL (it is in the buyer's PayPal redirect)
  can cancel that one unpaid order. Do not expose the app on a public network.
- **Merchants are catalog labels (review B7).** Every order is paid to the one Sandbox merchant behind the
  configured PayPal REST app; the catalog merchant is not mapped to a payee and item details are not sent
  to PayPal. Merchant allow/block rules therefore enforce trusted catalog metadata, not real multi-merchant
  payment routing.
- **Reservation-day semantics.** Spend counts on the day the order was *reserved* (policy timezone). An order
  reserved yesterday and captured today counts toward yesterday, so the daily cap is not a cap on cash
  captured per calendar day.
- **Policy changes grandfather existing orders.** Orders already created (awaiting the buyer) are still
  captured after a policy change; only pending *approvals* are re-checked against the current deny rules
  when approved. Cancel open orders by hand (`POST /orders/{id}/cancel`) after tightening the policy.
- **Split-order aggregation is narrow.** Only orders for the same SKU from the same merchant within one
  agent request are aggregated. Different SKUs, or separate requests, are judged per order; the daily cap is
  the backstop.
- **Reconciliation is on demand.** Ambiguous outcomes are checked once right away and then via
  `POST /orders/{id}/reconcile` (or a capture/return retry); there is no webhook or background job yet. An
  order whose capture outcome was ambiguous stays reserved (never cancellable or expirable) until PayPal
  reports it `COMPLETED`, declined or `VOIDED`; if PayPal no longer returns the order at all, resolve it by
  hand.
- **Mock PayPal state is in memory and mock mode is single-process** (reviews B9/N3): an exclusive lock
  file next to the resolved DB file enforces one mock server per database (aliases included; hard-linked DB
  files are refused). See the offline block above.
- **Audit history in the dashboard is paged**: the grid loads the newest 1000 decisions and older pages on
  request; search, filters and CSV export cover the loaded rows (the label says how many of the total).
  Older loaded rows are not re-polled; there is no retention/pruning of the audit log yet.
- **Upgrading a database from an older release** (any DB without `PRAGMA user_version` 4, including DBs
  already opened by rounds 5/6) is conservative: every order that was `NEEDS_RECONCILIATION` or
  `CAPTURING` at upgrade time is treated as "a capture may have happened", so it
  stays reserved and can no longer be cancelled or expired, only resolved forward with
  `POST /orders/{id}/reconcile` once PayPal reports it `COMPLETED`, declined or `VOIDED`. Stop all servers
  before upgrading, and back up the `.db` file first. Older orders get no audit timeline (the detail panel
  shows only their creation and current status), and decisions made before the upgrade are not in the audit log.
- **Concurrency guarantees assume one SQLite file on local disk** shared by all server processes (SQLite
  locking is unreliable on network file systems).
- **The offline agent is a restricted grammar**, not a language model: positive shopping lists only, item
  words from the catalog vocabulary. Each clause is parsed once into verb / quantity / item / purpose; only
  the quantity slot directly before the item and the item phrase decide what is ordered (so "Buy a USB-C
  cable for two interns" orders one cable). The optional `for …`/`as …` purpose tail is not used for
  quantity or item selection; it is only screened against a short list of condition/negation words (a
  denylist, so it is not a complete guarantee that unusual wording there is meaningless to a human
  reader). Verb, quantity and item are validated positively.
- **The real Sandbox buyer approval → capture loop was verified once by hand (2026-10-30)**: one order, a single
  Sandbox personal test buyer, the redirect through a tunnel ([`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md)). The
  automated tests still exercise this loop only against the mock and a wire-format fake of the Sandbox API.

## AI tools used

- **Claude (Anthropic)** — `claude-sonnet-5-5` is the runtime purchasing agent (tool use via the Messages API).
- The optional `AGENT_MODE=offline` parser is plain hand-written rules, not a model.
- **Hark** (an AI agent, built on Claude) was used as a coding assistant to scaffold the backend, guardrail engine, tests and this README. All code was reviewed and tested by running the test suite and live agent calls.

## License

MIT — see [LICENSE](LICENSE).
