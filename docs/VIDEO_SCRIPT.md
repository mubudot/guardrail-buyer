# Demo video script — GuardRail Buyer (target ≤ 2:45, hard limit < 3:00)

No music (narration only). Voice in the draft: Piper TTS, voice `en_US-libritts_r-medium`
(trained on LibriTTS-R, CC BY 4.0, credited on the closing card). Only on-screen brands: PayPal's own
Sandbox checkout (the sponsor's product), AG Grid (sponsor), Claude (named as the model used).

The draft is produced reproducibly by `scripts/video/make_demo.sh` (TTS → app in Sandbox mode with
Claude → headless Chromium recording via Playwright → ffmpeg). Output: `docs/demo_draft.mp4`
(not committed). Narration text lives in `scripts/video/scenes.json`; keep it in sync with this file.

| # | Time (approx.) | On screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:11 | Title card: "GuardRail Buyer — an AI purchasing agent that pays with PayPal, but only inside the spend guardrails you set." | AI agents are starting to buy things for us. But handing an agent your PayPal account is risky. GuardRail Buyer lets an AI agent shop for your team and pay with PayPal, but only inside the spend limits you set. |
| 2 | 0:11–0:27 | Policy card: ALLOW / NEEDS APPROVAL / DENY table with the default caps ($50 per order, $150 per day, $500 hard max). | Between the language model and the wallet sits a deterministic policy engine. Orders under the per-order cap and the daily budget go straight to PayPal. Anything over a cap waits for a human. Blocked merchants, blocked categories, and anything above a hard maximum are refused outright. The model cannot argue its way past that. |
| 3 | 0:27–0:37 | Dashboard (AG Grid), header shows `PayPal: sandbox · claude-sonnet-5-5`; KPI tiles. Caption: "Live dashboard · PayPal mode: Sandbox". | This is the dashboard, built with AG Grid, running against the real PayPal Sandbox. Up top: today's captured spend, money reserved for unpaid orders, and the remaining daily headroom. |
| 4 | 0:37–0:52 | Typing into *Ask the agent*: "Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present." → Send. Caption lists the tool chain. (The model's thinking time is cut.) | Let's ask the agent, Claude, for a mixed order: two USB-C cables and a mechanical keyboard for a new hire, plus a one hundred dollar gift card. The agent searches the catalog, dry-runs the policy, and places orders through our server. It never calls PayPal directly. |
| 5 | 0:52–1:06 | Agent answer + tool steps; scroll to Pending approvals and Orders (cables `AWAITING PAYER` with **Pay with PayPal**, keyboard approval, no gift-card order). | The cables fit the policy, so a real PayPal Sandbox order is created and the budget is reserved. The keyboard is over the fifty dollar per-order cap, so the agent files an approval request with its justification. The gift card is refused: blocked merchant and category. |
| 6 | 1:06–1:13 | Click **Pay with PayPal** → real `www.sandbox.paypal.com/checkoutnow` page for that order. | Pay with PayPal opens the real PayPal Sandbox checkout for that order. The buyer logs in with a Sandbox test account and approves the payment. |
| 7 | 1:13–1:22 | **MANUAL STEP (user):** log in with the Sandbox *personal* test account, click Pay → redirected to `/paypal/return` → dashboard toast "Payment returned from PayPal … COMPLETED", order row shows the capture id, KPI "Spent today (captured)" = $25.98. *In the draft this is a labelled placeholder card.* | PayPal redirects back to our app, which captures the payment. The order turns completed, with its PayPal capture ID, and the reserved amount becomes captured spend. |
| 8 | 1:22–1:34 | Pending approvals row (justification + "amount 89.00 exceeds per-order cap 50.00 USD") → **Approve** with the approver token → toast, new order awaiting the buyer. | Now the human in the loop. The keyboard request shows the agent's justification and exactly which rule it hit. With the approver token, I approve it. The server re-checks the deny rules, then creates a new PayPal order for the buyer. |
| 9 | 1:34–1:48 | Open the keyboard's Sandbox checkout → click "Cancel and return" → back in the app: toast "Buyer cancelled … reserved budget released", order `CANCELLED`. | Unpaid orders count against the daily budget, so an agent cannot open lots of small orders to dodge the cap. If the buyer cancels on PayPal, the reservation is released. And a capture attempted before the buyer approves is rejected by PayPal, and changes nothing. |
| 10 | 1:48–2:02 | Closing card: stack, MIT, 148 tests, `AGENT_MODE=offline`, TTS credit. | GuardRail Buyer is built with Claude tool use, PayPal Orders version two in the Sandbox, FastAPI, and AG Grid. It is open source, with one hundred forty-eight tests, and an offline mode lets judges run the whole flow without an AI key. Let agents spend, inside your guardrails. |

Total narration ≈ 1:54; with on-screen actions the draft runs ≈ 2:00–2:20, leaving room for the
manual approval clip (scene 7).

## The one manual step (scene 7)

The PayPal Sandbox checkout requires the Sandbox buyer's password, which the recording script does not
have. To finish the video:

1. Run the app in Sandbox mode, ask the agent (or run `scripts/video/make_demo.sh` and stop after scene 5).
2. Screen-record (e.g. OBS / QuickTime, no audio): click **Pay with PayPal**, log in with the Sandbox
   personal test account (developer.paypal.com → Testing Tools → Sandbox Accounts), click **Pay**, land
   back on the dashboard with the order `COMPLETED` (~20 s).
3. Replace the placeholder (scene 7) in `docs/demo_draft.mp4` with that clip, e.g.
   `ffmpeg -i demo_draft.mp4 -i approval.mp4 …` or any editor; the narration for scene 7 already fits.

## Optional extras if time allows (keep under 3:00)

- 5 s of `scripts/sandbox_smoke.py` output showing `422 ORDER_NOT_APPROVED → 409 NOT_APPROVED_YET`.
- 5 s of the offline mode header ("OFFLINE rule-based agent (no AI)").
