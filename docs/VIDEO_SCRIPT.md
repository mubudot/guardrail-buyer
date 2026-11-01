# Demo video script — GuardRail Buyer (published v3 cut 2:33, hard limit < 3:00)

No music (narration only). Voice: Piper TTS, voice `en_US-libritts_r-medium` (trained on LibriTTS-R, CC BY 4.0,
credited on the closing card). Only on-screen brands: PayPal's own Sandbox checkout (the sponsor's product), AG Grid
(sponsor), Claude (named as the model used). Narration for the v2 take lives in `scripts/video/scenes.json`; keep it in sync
with this file. The new v3 lines (scenes 1 and 7–10 below) were generated outside the repo and are quoted here.

## TTS source and license evidence

These are attribution notes only; the published narration and scene text below are unchanged.

- **Engine:** [Piper source, original project](https://github.com/rhasspy/piper),
  [MIT license](https://github.com/rhasspy/piper/blob/master/LICENSE.md).
  The maintained successor is [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl), with a
  [GPL v3 license](https://github.com/OHF-Voice/piper1-gpl/blob/main/COPYING).
  `scripts/video/tts.py` invokes `python -m piper`; the documented installation is unpinned
  `pip install piper-tts`. No installed version or binary hash for the published take is recorded in
  this repository, so these links identify the source lineages and their licenses, not a verified
  engine release used for that take.
- **Exact voice named in the recording notes:** `en_US-libritts_r-medium.onnx`, from
  [the voice model directory](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/libritts_r/medium),
  with [model weights](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx)
  and [configuration](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx.json).
  The script defaults to speaker `0`, length scale `1.08`, and sentence silence `0.25` seconds;
  `scenes.json` names this voice too. The downloaded model's hash was not recorded.
- **Voice license evidence:** the [voice-specific model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/libritts_r/medium/MODEL_CARD)
  names the [LibriTTS-R dataset](https://www.openslr.org/141/) and labels its dataset license
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The hosting repository's
  [metadata](https://huggingface.co/rhasspy/piper-voices/blob/main/README.md) declares MIT.
  The voice card does not separately state a weights/output license; the dataset license alone
  does not establish every model asset's reuse terms. No broader rights clearance is claimed here.

## v3 cut (2026-11-01, published: https://youtu.be/RhKllaIB0rg)

2:33 (153.0 s), 1920×1080, 30 fps, H.264 + AAC, kept outside the repo; same Piper voice, no music. It re-edits the v2
footage below (live Sandbox + Claude take and the labelled buyer-approval clip) and adds two labelled segments: an MCP
gateway card whose rows come from the offline scenario benchmark (`scripts/scenario_benchmark.py`, payments mocked)
and a short audit / Policy & spend segment recorded on a local build ("Local build · offline agent · mock payments").
The one-off assembly (ffmpeg, cards, new narration) is kept outside the repo. Checks: OCR at 2 fps (306 frames: no
personal email, name, key or private host) and three independent review passes ending in "no material issues".

| # | Time | On screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:08 | Title card (who it is for). | For office managers: let an AI prepare purchases, while you control the budget. Here is one request, with three outcomes, on the real PayPal Sandbox. |
| 2–3 | 0:08–0:38 | v2 scenes 4–5: the mixed request and its three outcomes (order / approval / deny). | v2 lines, trimmed. |
| 4 | 0:38–1:05 | v2 scene 8: the real Sandbox buyer approval clip → `COMPLETED`, labelled "recorded 2026-10-30". | v2 scene-8 lines. |
| 5–6 | 1:05–1:32 | v2 scenes 6–7: approve with confirmation dialog; buyer cancel releases the reservation. | v2 lines, trimmed. |
| 7 | 1:32–1:52 | "MCP gateway: direct tool attempts": MCP `create_order` (blocked merchant) → `DENIED`; MCP `create_order` `price=0.01` → `INVALID_ARGUMENTS`; MCP `request_approval` escalating the gift-card deny → `DENIED`; HTTP approve without token → `401`; HTTP capture before buyer approval → `409 NOT_APPROVED_YET`. | The same gate protects any MCP client. In our offline scenario benchmark, with payments mocked, direct attempts are refused: a blocked merchant is denied, a price override is rejected as an invalid argument, and a denial cannot be escalated. Approving without the token returns four oh one, and capturing before the buyer approves returns four oh nine. Spend does not move. |
| 8 | 1:52–2:05 | Local mock build: Audit tab (filter · search · export CSV) → row detail (quote, rules evaluated, payment timeline) → Policy & spend cap edit with change history. | Every decision lands in an audit trail that a finance lead can filter and export. Click a row to see the quote, each rule evaluated, and the payment timeline. The owner edits caps right in the grid. The server validates each change and keeps a history. |
| 9 | 2:05–2:18 | Policy recap card (default policy). | Under the hood, a deterministic policy engine sits between the model and PayPal. Orders within the caps go to PayPal for the buyer to approve, anything over a cap waits for a human, and a deny is final. The model cannot argue past it. |
| 10 | 2:18–2:33 | Closing card: stack, MIT, 400+ automated tests, offline mode, TTS credit. | GuardRail Buyer is built with Claude tool use, PayPal Orders version two, FastAPI, and AG Grid. It is open source, with more than four hundred automated tests, and an offline mode lets judges run it without an AI key. Let agents spend, inside your guardrails. |

## v2 cut (2026-10-30, previous; still on YouTube at https://youtu.be/6pp8X-yFJwk)

2:18 (138.4 s), 1280×720, H.264 + AAC, kept outside the repo. Order: scenes 1–7 are one live take of the polished
dashboard (`scripts/video/record_demo.py --skip-checkout`) against the **real PayPal Sandbox API** with **Claude
(`claude-sonnet-5-5`)** as the agent; `scripts/video/assemble.py` builds that part (3 s of model wait cut). Scene 8
is the real Sandbox buyer approval cut from the earlier take (below), labelled on screen as recorded earlier, narrated
with the `s5_checkout` / `s5_return` lines, and placed **before the closing card**, after the approval and cancel
scenes, so the live scenes never contradict the clip's order state. `make_demo.sh` produces a full-flow draft; recreating the v2 base take means running
`record_demo.py --skip-checkout` against a Sandbox + Claude server, then `assemble.py`. The final clip insertion (cut,
label, narration, concat) is a one-off ffmpeg step kept outside the repo. Checks: frame-by-frame contact sheet, OCR
(tesseract, 1 fps, 138 frames: no personal email, name, client id, key, password or tunnel URL; visible are Sandbox
order / capture ids, the checkout `token=` which is the Sandbox order id, and PayPal's own fake Sandbox persona
"John Doe" with test funding sources), and two Codex video passes (both "no material issues"; remaining P3s:
captions briefly cover the spend legend at 0:54–1:04, the capture-before-approval sentence in scene 7 is narrated
but not shown on screen — it is covered by `scripts/sandbox_smoke.py` and `docs/SANDBOX_E2E.md`).

| # | Time | On screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:11 | Title card: "GuardRail Buyer — an AI purchasing agent that pays with PayPal, but only inside the spend guardrails you set." | AI agents are starting to buy things for us. But handing an agent your PayPal account is risky. GuardRail Buyer lets an AI agent shop for your team and pay with PayPal, but only inside the spend limits you set. |
| 2 | 0:11–0:26 | Policy card: ALLOW / NEEDS APPROVAL / DENY with the default caps ($50 per order, $150 per day, $500 hard max). | Between the language model and the wallet sits a deterministic policy engine. Orders under the per-order cap and the daily budget go straight to PayPal. Anything over a cap waits for a human. Blocked merchants, blocked categories, and anything above a hard maximum are refused outright. The model cannot argue its way past that. |
| 3 | 0:26–0:38 | Live dashboard on the real Sandbox: header chips `PayPal Sandbox` · `Agent: claude-sonnet-5-5` · approver token state; four summary cards (spend vs daily cap with captured / reserved bar, pending approvals, blocked attempts, auto-approved). Caption: "Live dashboard · PayPal mode: Sandbox (api-m.sandbox.paypal.com)". | This is the dashboard, built with AG Grid, running against the real PayPal Sandbox. The summary cards show today's spend against the daily cap, split into captured and reserved money, plus pending approvals and blocked attempts. |
| 4 | 0:38–0:54 | The mixed request is typed into *Ask the agent* → Send; caption lists the tool chain. (3 s of model thinking time are cut.) | Let's ask the agent, Claude, for a mixed order: two USB-C cables and a mechanical keyboard for a new hire, plus a one hundred dollar gift card. The agent searches the catalog, dry-runs the policy, and places orders through our server. It never calls PayPal directly. |
| 5 | 0:54–1:08 | Agent's answer with the outcome list (cables `AWAITING PAYER APPROVAL` $25.98, keyboard `APPROVAL REQUESTED`, gift card `DENIED` — refused at the policy check) and the collapsible trace of all tool calls; cards update (reserved $25.98, 1 pending, 1 blocked, 1 auto-approved); Orders row with **Pay with PayPal**. | The cables fit the policy, so a real PayPal Sandbox order is created and the budget is reserved. The keyboard is over the fifty dollar per-order cap, so the agent files an approval request with its justification. The gift card is refused: blocked merchant and category. |
| 6 | 1:08–1:22 | Pending approvals row (justification, rule hit) → **Approve** → confirmation dialog (item, amount, merchant, rule hit, agent's justification, what happens next) → *Approve & create PayPal order* → new order awaiting the buyer. | Now the human in the loop. The keyboard request shows the agent's justification and exactly which rule it hit. A confirmation dialog shows what I am approving, and with the approver token, I approve it. The server re-checks the deny rules, then creates a new PayPal order for the buyer. |
| 7 | 1:22–1:36 | The keyboard's real Sandbox checkout → "Cancel and return" → back in the app: toast "Buyer cancelled … reserved budget released", order `CANCELLED`. | Unpaid orders count against the daily budget, so an agent cannot open lots of small orders to dodge the cap. If the buyer cancels on PayPal, the reservation is released. And a capture attempted before the buyer approves is rejected by PayPal, and changes nothing. |
| 8 | 1:36–2:03 | **Inserted clip, recorded earlier the same day with the previous dashboard design**, labelled top-right "Sandbox buyer approval — recorded 2026-10-30": Pay with PayPal → real Sandbox login (buyer email blurred, password masked) → review $25.98 → **Pay** → `/paypal/return` → toast "Payment returned from PayPal … COMPLETED", order `ORD-F79F379F1E` `COMPLETED` with capture `14579671MH105231J`; the last frame is held until the closing card. | Paying works the same way for real. In this clip, recorded earlier the same day, Pay with PayPal opens the real Sandbox checkout for the two USB-C cables, and a Sandbox test buyer logs in and approves the payment. PayPal redirects back to our app, which captures the payment. The order turns completed, with its PayPal capture ID, and the reserved amount becomes captured spend. |
| 9 | 2:03–2:18 | Closing card: stack, MIT, 400+ automated tests, `AGENT_MODE=offline`, TTS credit. | GuardRail Buyer is built with Claude tool use, PayPal Orders version two in the Sandbox, FastAPI, and AG Grid. It is open source, with more than four hundred automated tests, and an offline mode lets judges run the whole flow without an AI key. Let agents spend, inside your guardrails. |

Times are for the v2 cut.

## Earlier take (2:24, source of the scene-8 clip)

The video is produced by `scripts/video/make_demo.sh` (TTS → app in Sandbox mode with Claude → headless
Chromium recording via Playwright → ffmpeg). Output: `docs/demo_draft.mp4` (not committed). Narration text lives
in `scripts/video/scenes.json`; keep it in sync with this file. With `SANDBOX_BUYER_EMAIL` and
`SANDBOX_BUYER_PASSWORD` set (a Sandbox *personal* test account; the password is read from the environment only and never written anywhere; the buyer email is visible in the raw screen recording, which stays outside the repo, and is blurred in the final cut), the
recorder also logs in on the real Sandbox checkout, clicks **Pay** and follows the redirect back, so scene 7 is real
footage; the buyer's email is blurred by `assemble.py`. Without them, scene 7 is a labelled placeholder card.

**Final cut (2026-10-30):** 2:24 (143.8 s), 1280×720, H.264 + AAC, real Sandbox run `ORD-F79F379F1E` →
capture `14579671MH105231J` (see `docs/SANDBOX_E2E.md`, second run). Kept outside the repo. Two post steps
were applied by hand to that take: (1) under heavy CPU load the Playwright screencast lagged the scene clock by up
to ~3.8 s from scene 6 on, so narration for scenes 6–10 was placed on the measured visual scene starts instead of
the timeline (scenes 1–5 unchanged); (2) the post-cancel app view was on screen only ~1.6 s, so 3 s of the same
app state (same database, opened at `/?cancelled=ORD-062B118B1C`, exactly where PayPal's cancel redirect landed)
was inserted before the closing card. OCR (tesseract) of every 0.5 s of the final cut (287 frames) found no
personal email, name, address, client id, token or password; the Sandbox buyer's email is blurred. Visible on
screen: Sandbox order/capture ids, the PayPal checkout `token=` (= the Sandbox order id), and PayPal's own fake
Sandbox profile data ("John Doe", PayPal's San Jose address, test funding sources).

**Closing-card update (2026-10-30, later):** the take originally ended with "256 tests". Only scene 10 was
replaced: the card was re-rendered from `CARDS["s8_close"]` in `record_demo.py` (now "400+ automated tests") and the
narration re-synthesized from `scenes.json` with the same Piper voice and settings, placed 0.25 s after the card
starts (as `assemble.py` does). Everything before 2:08.2 is the same footage and audio as the 2:23 cut.

## Buyer approval recording (scene 7 of the earlier take, scene 8 of v2)

Recorded automatically when `SANDBOX_BUYER_EMAIL` / `SANDBOX_BUYER_PASSWORD` are set (developer.paypal.com →
Testing Tools → Sandbox Accounts → the *personal* account → View/Edit account). Pass them as env vars for that one
run only. The recorder logs the email region as a blur window in `timeline.json`; dead air while PayPal logs in
and redirects is cut. Because the browser is then logged in as the buyer, the keyboard's checkout in scene 9 shows
the review page directly and is cancelled with "Return to Test Store".

## Optional extras if time allows (keep under 3:00)

- 5 s of `scripts/sandbox_smoke.py` output showing `422 ORDER_NOT_APPROVED → 409 NOT_APPROVED_YET`.
- 5 s of the offline mode header ("OFFLINE rule-based agent (no AI)").
