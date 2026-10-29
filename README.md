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
re-checks the deny rules in case the policy changed. What exactly is enforced, and what is not,
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
4. `GET /paypal/cancel?token=…` cancels **only** an order still awaiting the buyer (`PAYER_ACTION_REQUIRED`),
   atomically. Orders being captured, captured, or of unknown outcome are left untouched.
5. If PayPal's answer is ambiguous (HTTP 5xx, timeout/transport error, capture status `PENDING` or missing),
   the order becomes `NEEDS_RECONCILIATION`, keeps its reservation, and the app asks PayPal for the order
   status. Only a confirmed `COMPLETED` capture is recorded as `COMPLETED`.
   `POST /orders/{id}/reconcile` re-checks later.

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
`pip install` line needs network (a package index). Paste into a bash shell at the repository root:

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                         # the only step that needs network

# --- everything below: no API key, no PayPal account, no network ---
export AGENT_MODE=offline          # rule-based parser instead of Claude (applies to server AND CLI)
export PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1          # mock PayPal with a local mock checkout page
export APPROVER_TOKEN=demo         # token for approve / reject / cancel
export GRB_NO_DOTENV=1             # ignore any .env file, so nothing switches back to Claude/Sandbox
export DB_PATH="$(mktemp -d)/guardrail-demo.db"         # throwaway database for this shell

python -m pytest -q                                     # -> 110 passed
uvicorn app.main:app --port 8000 &                      # dashboard: http://localhost:8000/ (token: demo)
sleep 3
curl -s -X POST localhost:8000/agent/request -H 'content-type: application/json' \
  -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["final_text"])'
curl -s localhost:8000/spend; echo                      # cables reserved: total 25.98
python -m app.cli "3 bags of coffee beans and 2 snack boxes"   # CLI: own in-memory DB each run
```

Then open <http://localhost:8000/>, enter `demo` as approver token, click **Pay with PayPal** on the
cables order (local mock checkout → **Pay now**) and **Approve** the keyboard.

**Reset / rerun:** `kill %1` (stop the server) and `rm -f "$DB_PATH"*` (or start a new shell and paste the
`export` lines again, which picks a new temp DB). The mock PayPal keeps its orders **in memory**: after a
server restart, open mock orders from the previous run can no longer be paid; on startup they are marked
`MOCK_RESET` and their reservations are released (completed orders stay as history). Use a fresh `DB_PATH`
for a clean demo.

### With Claude (and optionally the real PayPal Sandbox)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then put your Anthropic key in ANTHROPIC_API_KEY
```

`.env` is loaded automatically (python-dotenv); real environment variables win.
The defaults run with a **mock PayPal** (no PayPal account, no network) and the Claude agent.

> **About the offline agent.** `AGENT_MODE=offline` swaps Claude for a small deterministic, rule-based
> parser (`app/offline_agent.py`, clearly **not** AI) that understands simple positive shopping lists over
> the demo catalog ("2 USB-C cables, a mechanical keyboard and a $100 gift card"). Requests with negated or
> conditional wording ("don't", "no", "never", "if", …) are refused as a whole and quantity 0 is never
> ordered. It calls the *same* tools through the *same* `BuyerService`, so every guardrail, PayPal order,
> approval and refusal behaves exactly as with Claude; the dashboard header shows
> **OFFLINE rule-based agent (no AI)**. Claude remains the default and handles free-form requests.

Run all commands from the repository root with the venv active (paths in `.env` are relative).
The commands below use bash syntax (`VAR=value cmd`); on Windows set the variables in `.env` instead.
If you serve on a port other than 8000, also set `PUBLIC_BASE_URL` (used for the PayPal return links).

### Repro steps ("do X → see Y")

1. **Run the tests** — `python -m pytest -q` → see `110 passed` (no network, no API key needed).
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
8. **One-shot from the CLI** — `python -m app.cli "3 bags of coffee beans and 2 snack boxes"` → see each tool call and its verdict (see `docs/sample_cli_run.txt`; without a key: `AGENT_MODE=offline python -m app.cli "…"`, sample in `docs/sample_offline_run.txt`). The CLI starts from an empty in-memory ledger each run (`CLI_DB_PATH=guardrail.db` to share the server's).

### Against the real PayPal Sandbox

Create a free REST app at <https://developer.paypal.com/> (Apps & Credentials → Sandbox), copy its
client id and secret into `.env`, then:

```bash
# in .env: PAYPAL_MODE=sandbox  PAYPAL_CLIENT_ID=…  PAYPAL_CLIENT_SECRET=…
APPROVER_TOKEN=demo uvicorn app.main:app --port 8000
python scripts/sandbox_smoke.py          # no LLM: guardrails -> real Sandbox orders -> status -> capture-before-approval -> cancel
python scripts/sandbox_smoke.py --app http://localhost:8000 --agent --token demo   # same, through the running app + Claude agent
```

The smoke test creates real Sandbox orders (status `PAYER_ACTION_REQUIRED`, approve link on
`www.sandbox.paypal.com/checkoutnow`), reads their live status, shows that a capture before the buyer
approved is refused by PayPal (`422 ORDER_NOT_APPROVED` → the app answers `409 NOT_APPROVED_YET`, order
and budget reservation unchanged), and cancels them so the reservation is released. Sample output:
`docs/sandbox_smoke_run.txt`.

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
| POST | `/agent/request` | `{"request": "..."}` — run the agent; returns final text, transcript, token usage |
| GET | `/orders` | all orders (`?status=COMPLETED`) with the policy decision attached |
| GET | `/orders/{id}` | one order; `?refresh=true` (default) adds `paypal_live_status` from PayPal |
| POST | `/orders/{id}/capture` | capture after buyer approval (fallback for a lost redirect); not approved yet → `409 NOT_APPROVED_YET` |
| POST | `/orders/{id}/reconcile` | resolve a `NEEDS_RECONCILIATION` order from PayPal's order status (never charges twice) |
| POST | `/orders/{id}/cancel` | cancel an order still awaiting the buyer, release its reservation (approver token); otherwise `409` |
| GET | `/approvals` | approval requests (`?status=PENDING`) |
| POST | `/approvals/{id}/approve` | approve once → quote unchanged? deny rules re-checked → PayPal order; a repeat returns the same order; changed quote → `409` (needs `X-Approver-Token` when `APPROVER_TOKEN` is set) |
| POST | `/approvals/{id}/reject` | reject, nothing is paid (same token) |
| GET | `/paypal/return?token=…` | PayPal redirect after buyer approval → capture |
| GET | `/paypal/cancel?token=…` | PayPal redirect on cancel → cancels only if still awaiting the buyer |
| GET | `/spend` | today's captured / reserved / remaining vs caps |
| GET | `/policy` | active policy + today's spend |
| GET | `/` | AG Grid dashboard |
| GET | `/catalog` | demo products (`?q=keyboard`) |
| GET | `/health` | PayPal mode + model |

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | any Messages-API-compatible endpoint |
| `ANTHROPIC_API_KEY` | – | sent as `x-api-key` if set |
| `MODEL` | `claude-sonnet-5-5` | |
| `AGENT_MODE` | `claude` | `offline` = deterministic rule-based parser, no AI and no key (for judges / CI) |
| `PAYPAL_MODE` | `mock` | `sandbox` uses the real PayPal Sandbox |
| `PAYPAL_MOCK_APPROVAL` | `0` | mock only: `1` = orders wait for a simulated buyer approval |
| `PAYPAL_AUTH` | `oauth` | `oauth`: client id + secret → OAuth2 token → Bearer. `proxy`: no credentials in the app, an egress proxy injects `Authorization: Basic` |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | – | Sandbox REST app credentials (`oauth` mode) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | base of the PayPal return / cancel URLs |
| `PAYPAL_APPROVAL_TTL_MIN` | `180` | unapproved orders expire and release their reservation |
| `APPROVER_TOKEN` | – | required for approve / reject; empty = open (dev only, logs a warning) |
| `POLICY_PATH`, `CATALOG_PATH`, `DB_PATH` | `policy.yaml`, `data/catalog.json`, `guardrail.db` | |

## Status / roadmap

- [x] Guardrail engine + SQLite daily spend ledger, unit-tested
- [x] FastAPI backend with approvals workflow, MockPayPal
- [x] Claude tool-use agent wired to the guardrails
- [x] Buyer-approval round trip (create → approve → return → capture), reservations, cancel / expiry — tested with the mock and a wire-format test of the Sandbox client
- [x] Live run against the PayPal Sandbox: orders created, live status, capture-before-approval handled, cancel (`scripts/sandbox_smoke.py`)
- [ ] Full buyer payment with a Sandbox personal account, recorded for the video
- [x] AG Grid dashboard for orders, approvals and spend, approver token
- [x] Offline rule-based agent mode for running without an AI key (`AGENT_MODE=offline`)
- [~] Demo video: draft pipeline done; real Sandbox buyer-approval clip pending

## Known limitations

This is a hackathon demo meant to run locally. Be precise about what it does and does not protect:

- **Authentication is local-demo scope (review B6).** There is no requester identity, ownership or rate
  limit: anyone who can reach the server can call `/agent/request` (and spend Claude tokens), list all orders
  and approvals, and trigger capture of an order a buyer already approved. The single shared
  `APPROVER_TOKEN` protects approve / reject / cancel; it is stored in the browser's `localStorage`, and an
  empty token leaves those endpoints open (a warning is logged). `GET /paypal/cancel` is unauthenticated, but
  it can only cancel an order that is still awaiting buyer approval (no money has moved; worst case the
  order has to be placed again). Do not expose the app on a public network.
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
  `POST /orders/{id}/reconcile` (or a capture/return retry); there is no webhook or background job yet.
- **Mock PayPal state is in memory** (review B9). See the reset note in the offline block above.
- **Concurrency guarantees assume one SQLite file on local disk** shared by all server processes (SQLite
  locking is unreliable on network file systems).
- **The offline agent is a restricted grammar**, not a language model: positive shopping lists only.
- **A full real Sandbox buyer approval + capture is not yet recorded.** Live Sandbox runs so far cover order
  creation, live status, capture-before-approval refusal and cancel (`docs/sandbox_smoke_run.txt`); the
  buyer-approval → capture loop is tested against the mock and a wire-format fake of the Sandbox API.

## AI tools used

- **Claude (Anthropic)** — `claude-sonnet-5-5` is the runtime purchasing agent (tool use via the Messages API).
- The optional `AGENT_MODE=offline` parser is plain hand-written rules, not a model.
- **Hark** (an AI agent, built on Claude) was used as a coding assistant to scaffold the backend, guardrail engine, tests and this README. All code was reviewed and tested by running the test suite and live agent calls.

## License

MIT — see [LICENSE](LICENSE).
