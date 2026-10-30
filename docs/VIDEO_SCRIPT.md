# Demo video script — GuardRail Buyer (final cut 2:23, hard limit < 3:00)

No music (narration only). Voice in the draft: Piper TTS, voice `en_US-libritts_r-medium`
(trained on LibriTTS-R, CC BY 4.0, credited on the closing card). Only on-screen brands: PayPal's own
Sandbox checkout (the sponsor's product), AG Grid (sponsor), Claude (named as the model used).

The video is produced by `scripts/video/make_demo.sh` (TTS → app in Sandbox mode with Claude → headless
Chromium recording via Playwright → ffmpeg). Output: `docs/demo_draft.mp4` (not committed). Narration text lives
in `scripts/video/scenes.json`; keep it in sync with this file. With `SANDBOX_BUYER_EMAIL` and
`SANDBOX_BUYER_PASSWORD` set (a Sandbox *personal* test account; the password is read from the environment only and never written anywhere; the buyer email is visible in the raw screen recording, which stays outside the repo, and is blurred in the final cut), the
recorder also logs in on the real Sandbox checkout, clicks **Pay** and follows the redirect back, so scene 7 is real
footage; the buyer's email is blurred by `assemble.py`. Without them, scene 7 is a labelled placeholder card.

**Final cut (2026-10-30):** 2:23 (143.3 s), 1280×720, H.264 + AAC, real Sandbox run `ORD-F79F379F1E` →
capture `14579671MH105231J` (see `docs/SANDBOX_E2E.md`, second run). Kept outside the repo. Two post steps
were applied by hand to that take: (1) under heavy CPU load the Playwright screencast lagged the scene clock by up
to ~3.8 s from scene 6 on, so narration for scenes 6–10 was placed on the measured visual scene starts instead of
the timeline (scenes 1–5 unchanged); (2) the post-cancel app view was on screen only ~1.6 s, so 3 s of the same
app state (same database, opened at `/?cancelled=ORD-062B118B1C`, exactly where PayPal's cancel redirect landed)
was inserted before the closing card. OCR (tesseract) of every 0.5 s of the final cut (287 frames) found no
personal email, name, address, client id, token or password; the Sandbox buyer's email is blurred. Visible on
screen: Sandbox order/capture ids, the PayPal checkout `token=` (= the Sandbox order id), and PayPal's own fake
Sandbox profile data ("John Doe", PayPal's San Jose address, test funding sources).

| # | Time (approx.) | On screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:11 | Title card: "GuardRail Buyer — an AI purchasing agent that pays with PayPal, but only inside the spend guardrails you set." | AI agents are starting to buy things for us. But handing an agent your PayPal account is risky. GuardRail Buyer lets an AI agent shop for your team and pay with PayPal, but only inside the spend limits you set. |
| 2 | 0:11–0:27 | Policy card: ALLOW / NEEDS APPROVAL / DENY table with the default caps ($50 per order, $150 per day, $500 hard max). | Between the language model and the wallet sits a deterministic policy engine. Orders under the per-order cap and the daily budget go straight to PayPal. Anything over a cap waits for a human. Blocked merchants, blocked categories, and anything above a hard maximum are refused outright. The model cannot argue its way past that. |
| 3 | 0:27–0:37 | Dashboard (AG Grid), header shows `PayPal: sandbox · claude-sonnet-5-5`; KPI tiles. Caption: "Live dashboard · PayPal mode: Sandbox". | This is the dashboard, built with AG Grid, running against the real PayPal Sandbox. Up top: today's captured spend, money reserved for unpaid orders, and the remaining daily headroom. |
| 4 | 0:37–0:52 | Typing into *Ask the agent*: "Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present." → Send. Caption lists the tool chain. (The model's thinking time is cut.) | Let's ask the agent, Claude, for a mixed order: two USB-C cables and a mechanical keyboard for a new hire, plus a one hundred dollar gift card. The agent searches the catalog, dry-runs the policy, and places orders through our server. It never calls PayPal directly. |
| 5 | 0:52–1:06 | Agent answer + tool steps; scroll to Pending approvals and Orders (cables `AWAITING PAYER` with **Pay with PayPal**, keyboard approval, no gift-card order). | The cables fit the policy, so a real PayPal Sandbox order is created and the budget is reserved. The keyboard is over the fifty dollar per-order cap, so the agent files an approval request with its justification. The gift card is refused: blocked merchant and category. |
| 6 | 1:06–1:10 | Click **Pay with PayPal** → real `www.sandbox.paypal.com/checkoutnow` page for that order. | Pay with PayPal opens the real PayPal Sandbox checkout for that order. The buyer logs in with a Sandbox test account and approves the payment. |
| 7 | 1:10–1:37 | Real footage: log in with the Sandbox *personal* test account (email blurred, password masked), review $25.98 → **Pay** → redirected to `/paypal/return` → dashboard toast "Payment returned from PayPal … COMPLETED", Orders row `COMPLETED` with capture id `14579671MH105231J`, KPI "Spent today (captured)" = $25.98, reserved $0.00. Captions: "Buyer logs in with the Sandbox personal test account" → "Review the $25.98 Sandbox payment → Pay" → "PayPal redirects to /paypal/return → the app captures → COMPLETED" → "Order COMPLETED with its PayPal capture id · reserved → captured spend". | PayPal redirects back to our app, which captures the payment. The order turns completed, with its PayPal capture ID, and the reserved amount becomes captured spend. |
| 8 | 1:37–1:50 | Pending approvals row (justification + "amount 89.00 exceeds per-order cap 50.00 USD") → **Approve** with the approver token → toast, new order awaiting the buyer. | Now the human in the loop. The keyboard request shows the agent's justification and exactly which rule it hit. With the approver token, I approve it. The server re-checks the deny rules, then creates a new PayPal order for the buyer. |
| 9 | 1:50–2:08 | Open the keyboard's Sandbox checkout → click "Cancel and return" (or "Return to Test Store" when the buyer is already logged in) → back in the app: toast "Buyer cancelled … reserved budget released", order `CANCELLED`. | Unpaid orders count against the daily budget, so an agent cannot open lots of small orders to dodge the cap. If the buyer cancels on PayPal, the reservation is released. And a capture attempted before the buyer approves is rejected by PayPal, and changes nothing. |
| 10 | 2:08–2:23 | Closing card: stack, MIT, 256 tests, `AGENT_MODE=offline`, TTS credit. | GuardRail Buyer is built with Claude tool use, PayPal Orders version two in the Sandbox, FastAPI, and AG Grid. It is open source, with two hundred fifty-six tests, and an offline mode lets judges run the whole flow without an AI key. Let agents spend, inside your guardrails. |

Times in the table are approximate for the final cut (scene 4's model wait is cut). Total narration ≈ 1:55.

## Scene 7 (buyer approval)

Recorded automatically when `SANDBOX_BUYER_EMAIL` / `SANDBOX_BUYER_PASSWORD` are set (developer.paypal.com →
Testing Tools → Sandbox Accounts → the *personal* account → View/Edit account). Pass them as env vars for that one
run only. The recorder logs the email region as a blur window in `timeline.json`; dead air while PayPal logs in
and redirects is cut. Because the browser is then logged in as the buyer, the keyboard's checkout in scene 9 shows
the review page directly and is cancelled with "Return to Test Store".

## Optional extras if time allows (keep under 3:00)

- 5 s of `scripts/sandbox_smoke.py` output showing `422 ORDER_NOT_APPROVED → 409 NOT_APPROVED_YET`.
- 5 s of the offline mode header ("OFFLINE rule-based agent (no AI)").
