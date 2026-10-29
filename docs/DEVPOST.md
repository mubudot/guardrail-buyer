# Devpost submission draft — GuardRail Buyer

> Draft for the PayPal AI Hackathon form. Paste section by section. Contains no secrets.
> Placeholders to fill at submission time: `<GITHUB_URL>`, `<YOUTUBE_URL>`.

**Project name:** GuardRail Buyer

**Tagline (≤ 200 chars):** An AI purchasing agent that pays with PayPal, but only inside the spend guardrails you set: per-order and daily caps, human approval over limits, hard denies the model can't talk past.

**Suggested categories / prizes:** Best Use of Agentic Commerce · Best Use of PayPal + AI · Best Use of AG Grid

---

## Inspiration

AI agents are getting good at doing errands, and buying things is the obvious next errand. But today
the choice is all or nothing: either the agent can't pay, or it holds credentials that let it spend
anything. Small teams, office managers and ops people want the convenience of "just order what the
new hire needs" without waking up to a surprise bill or a gift-card scam the model fell for. We wanted
the same kind of controls a company card has (limits, approvals, blocked merchants) for an AI agent
that pays with PayPal.

## What it does

You describe what you need in plain language ("2 USB-C cables and a mechanical keyboard for our new
hire, and a $100 gift card as a welcome present"). The agent (Claude, using tool calls) finds the items
in a catalog, dry-runs the spend policy and places orders, and every order passes a **deterministic
policy engine on the server** before it reaches PayPal:

- **ALLOW** (under the per-order cap and today's budget) → a real **PayPal Orders v2** order is created
  in the Sandbox and the amount is **reserved** against the daily budget. The buyer approves on PayPal,
  PayPal redirects back, and the app **captures** the payment.
- **NEEDS APPROVAL** (over a cap) → the agent files an approval request with a business justification;
  a human approves or rejects it in the dashboard (approver token required). Approval re-checks the
  deny rules before a PayPal order is created.
- **DENY** (blocked merchant or category, wrong currency, above the hard maximum) → refused, and a
  denial can never be escalated to approval.

The AG Grid dashboard shows pending approvals (with the agent's justification and the exact rule hit),
every order with its PayPal status and a **Pay with PayPal** button, and spend vs. caps (captured,
reserved, headroom).

## How we built it

- **Backend:** Python, FastAPI, SQLite. A pure guardrail engine (`evaluate(policy, order, spent_today)`)
  decides ALLOW / NEEDS_APPROVAL / DENY with human-readable reasons; `BuyerService` is the only code
  path that can create PayPal orders and re-evaluates the policy on every call.
- **PayPal:** Orders v2 in the Sandbox: create with `intent: CAPTURE` and `PayPal-Request-Id`
  idempotency, `payer-action` approval link, return/cancel URLs, live `GET` status, capture after
  approval, structured error handling (e.g. a capture before approval returns `422 ORDER_NOT_APPROVED`,
  which the app turns into `409 NOT_APPROVED_YET` without touching the order or the budget).
- **Spend accounting:** an order's amount is reserved when the PayPal order is created, so an agent
  can't open many unpaid orders to get around the daily cap; cancel, expiry and failed captures release
  the reservation.
- **AI:** Claude (`claude-sonnet-5-5`) through the Anthropic Messages API with four tools:
  `search_catalog`, `check_policy`, `create_order`, `request_approval`. The model never sees PayPal
  credentials.
- **Dashboard:** plain HTML/JS with **AG Grid Community** (approvals grid with action buttons, orders
  grid with PayPal links, spend-vs-caps grid).
- **Offline mode:** `AGENT_MODE=offline` replaces Claude with a small hand-written rule parser (clearly
  labelled, not AI) so anyone can run the full flow without an AI key.
- **Tests:** 68 pytest tests (guardrails, payment flow, reservations/expiry, API, agent loop with a
  scripted fake LLM, offline mode) plus a live Sandbox smoke script.

## Challenges we ran into

- **Making the limit real, not advisory.** A prompt that says "don't spend more than $50" isn't a
  control. We moved every decision server-side and gave the model tools only, then made sure approvals
  re-check deny rules in case the policy changed in between.
- **Unpaid orders vs. daily caps.** With a buyer-approval flow, money moves later than the order is
  created. Counting only captured payments would let an agent open ten $49 orders at once. We reserve
  at creation and release on cancel/expiry/capture failure.
- **PayPal edge cases:** idempotent capture on a refreshed return URL, a lost redirect (manual capture
  endpoint), captures attempted before approval, and expiring stale orders.
- **LLM output quirks:** occasionally an empty final message after parallel tool calls; we added a
  deterministic summary built from the tool results.

## Accomplishments that we're proud of

- The full loop works end to end against the real PayPal Sandbox: natural-language request → policy →
  Sandbox order → buyer approval → capture, with approvals and refusals in the same run.
- The guardrails are boring in the best way: deterministic, unit-tested, and impossible for the model to
  bypass.
- Judges can run everything locally in a few minutes, with or without an AI key and with or without a
  PayPal Sandbox app (a local mock covers the PayPal side).

## What we learned

- Agentic commerce needs the same primitives as corporate spend management (limits, approvals,
  allowlists, audit trail), and they belong outside the model.
- PayPal's Orders v2 buyer-approval step is a natural second safety layer: the agent can create an
  order, but a person still confirms the payment.

## What's next for GuardRail Buyer

- PayPal webhooks (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`) instead of relying on the
  return redirect.
- Vaulted payment methods / PayPal Agent Toolkit for trusted, pre-approved recurring purchases within caps.
- Per-requester budgets, multiple approvers, and Slack/email approval links.
- Real merchant catalogs instead of the demo catalog.

## Built with

python · fastapi · sqlite · paypal-orders-api (v2, Sandbox) · anthropic-claude (Messages API, tool use) · ag-grid · javascript · html/css · pytest · httpx · playwright (demo recording) · ffmpeg

## AI tools used (disclosure)

- **Claude (Anthropic), model `claude-sonnet-5-5`** — the runtime purchasing agent (tool use via the
  Messages API). It only calls our four tools; all spending decisions are made by deterministic code.
- **Hark** (an AI agent built on Claude) was used as a coding assistant to scaffold the backend,
  guardrail engine, tests, dashboard, docs and the demo-recording pipeline. All code was reviewed and
  verified by running the test suite, live Sandbox calls and live agent runs.
- **Piper TTS** (offline, open source; voice trained on LibriTTS-R, CC BY 4.0) generated the demo
  video narration. No music is used.
- The optional `AGENT_MODE=offline` parser is hand-written rules, not an AI model.

## Testing instructions for judges

Everything runs locally; no account is needed for the default mode.

```bash
git clone <GITHUB_URL> && cd paypal-guardrail-buyer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m pytest -q                      # 68 passed, no network

# Full flow without any key (mock PayPal + offline rule-based agent):
AGENT_MODE=offline APPROVER_TOKEN=demo PAYPAL_MOCK_APPROVAL=1 uvicorn app.main:app --port 8000
# open http://localhost:8000 , enter approver token "demo", then ask:
#   Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card
# → cables: order + "Pay with PayPal" (local mock checkout) · keyboard: approval request · gift card: refused
```

- **With Claude:** put an Anthropic API key in `.env` (`ANTHROPIC_API_KEY`) and leave `AGENT_MODE`
  unset; free-form requests then work.
- **With the real PayPal Sandbox:** create a free Sandbox REST app at developer.paypal.com (Apps &
  Credentials → Sandbox), set `PAYPAL_MODE=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` in
  `.env`, and pay with a Sandbox *personal* test account (Testing Tools → Sandbox Accounts).
  `python scripts/sandbox_smoke.py` checks the live Sandbox integration without a buyer login.
- No real money is involved anywhere; the PayPal side is Sandbox or a local mock.

**Repo:** `<GITHUB_URL>` (MIT) · **Video:** `<YOUTUBE_URL>`
