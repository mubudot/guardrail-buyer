# GuardRail Buyer

> An AI purchasing agent that pays with PayPal — but only inside the spend guardrails you set.

*Draft README — work in progress for the PayPal AI Hackathon. Real PayPal Sandbox checkout and the AG Grid dashboard are coming in later steps.*

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
re-checks the deny rules in case the policy changed.

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
        │
        ▼
 FastAPI REST: /agent/request /orders /approvals /policy           app/main.py
```

## Quick start

Requires Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY (or point ANTHROPIC_BASE_URL at a gateway)
set -a; source .env; set +a
```

### Repro steps ("do X → see Y")

1. **Run the tests** — `python -m pytest -q` → see `37 passed` (no network, no API key needed).
2. **Start the API** — `uvicorn app.main:app --port 8000` → open <http://localhost:8000/docs> and see the Swagger UI.
3. **Check the policy** — `curl localhost:8000/policy` → see the caps, allowlists and `"spent_today": "0.00"`.
4. **Ask the agent to buy something mixed** —
   ```bash
   curl -X POST localhost:8000/agent/request -H 'content-type: application/json' \
     -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}'
   ```
   → see the agent's transcript: the cables ($25.98) are **bought** (mock PayPal order captured), the keyboard ($89) gets an **approval request**, and the gift card is **refused** (blocked merchant + category). See `docs/sample_transcript.json` for a real run.
5. **See what is waiting for you** — `curl localhost:8000/approvals?status=PENDING` → see the keyboard approval with the agent's justification and the policy reasons.
6. **Approve it** — `curl -X POST localhost:8000/approvals/<APR-id>/approve` → see a new `COMPLETED` order linked to the approval; `GET /orders` now lists two orders and `GET /policy` shows `spent_today: 114.98`.
7. **Or reject it** — `curl -X POST localhost:8000/approvals/<APR-id>/reject` → status `REJECTED`, no money moves.
8. **One-shot from the CLI** — `python -m app.cli "3 bags of coffee beans and 2 snack boxes"` → see each tool call and its verdict (see `docs/sample_cli_run.txt`).

### Changing the policy

Edit `policy.yaml` (or point `POLICY_PATH` at a `.json` file) and restart. Example: set
`per_order_cap: 100` → the keyboard in step 4 is bought directly instead of needing approval.

## REST API

| Method | Path | Description |
|---|---|---|
| POST | `/agent/request` | `{"request": "..."}` — run the agent; returns final text, transcript, token usage |
| GET | `/orders` | all orders (`?status=COMPLETED`) with the policy decision attached |
| GET | `/approvals` | approval requests (`?status=PENDING`) |
| POST | `/approvals/{id}/approve` | approve → re-check deny rules → pay via PayPal |
| POST | `/approvals/{id}/reject` | reject, nothing is paid |
| GET | `/policy` | active policy + today's spend |
| GET | `/catalog` | demo products (`?q=keyboard`) |
| GET | `/health` | PayPal mode + model |

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | any Messages-API-compatible endpoint |
| `ANTHROPIC_API_KEY` | – | sent as `x-api-key` if set |
| `MODEL` | `claude-haiku-4-5-20251001` | |
| `PAYPAL_MODE` | `mock` | `sandbox` uses the real PayPal Sandbox |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | – | Sandbox REST app credentials |
| `POLICY_PATH`, `CATALOG_PATH`, `DB_PATH` | `policy.yaml`, `data/catalog.json`, `guardrail.db` | |

## Status / roadmap

- [x] Guardrail engine + SQLite daily spend ledger, unit-tested
- [x] FastAPI backend with approvals workflow, MockPayPal
- [x] Claude tool-use agent wired to the guardrails
- [ ] PayPal Sandbox Orders v2 end to end (create → buyer approval → capture) — `SandboxPayPal` is written but not yet exercised
- [ ] AG Grid dashboard for orders, approvals and spend
- [ ] Demo video

## AI tools used

- **Claude (Anthropic)** — `claude-haiku-4-5` is the runtime purchasing agent (tool use via the Messages API).
- **Hark** (an AI agent, built on Claude) was used as a coding assistant to scaffold the backend, guardrail engine, tests and this README. All code was reviewed and tested by running the test suite and live agent calls.

## License

MIT — see [LICENSE](LICENSE).
