# PayPal AI Hackathon — requirements check

Source: Official Rules <https://paypalaihackathon.devpost.com/rules> and the Overview page
<https://paypalaihackathon.devpost.com/>, re-read 2026-10-30; status updated in review iteration 3 (2026-10-30 ~03:30 Asia/Taipei).
Deadline: **Nov 12, 2026 12:00 PM PST** (= Nov 13 04:00 Asia/Taipei).

Legend: ✅ done · 🟡 partly / needs a decision · ❌ not done yet

## Project requirements (Rules §4 "Project Requirements", Overview "Your project should")

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| 1 | Integrates the PayPal developer platform **using the free Sandbox** | ✅ | Orders v2 against `api-m.sandbox.paypal.com`: create (intent CAPTURE, payer-action link), GET status, capture, error handling (`app/paypal_client.py`). Verified live with `scripts/sandbox_smoke.py` → `docs/sandbox_smoke_run.txt` |
| 2 | "PayPal integration is central to the project" / meaningful | ✅ | Every purchase is a PayPal order; spend is reserved at order creation and released on cancel/expiry/capture failure; capture only after buyer approval |
| 3 | Meaningfully incorporates an AI tool/model | ✅ | Claude (Messages API, tool use) is the purchasing agent (`app/agent.py`); live transcripts in `docs/sample_transcript.json`, `docs/sample_cli_run.txt` |
| 4 | Working prototype, functions as shown in video/description | ✅ | 110 offline tests (fix round 4, incl. concurrency regressions); iteration 3 fresh clone (new venv) → `68 passed` at that time, and the judge flow with **no key** (`AGENT_MODE=offline`, mock PayPal with mock checkout): ask → cables order → mock pay → `COMPLETED` + capture id; keyboard approval → 401 without token, approve with token → new order; gift card refused; spend 25.98 captured + 89.00 reserved |
| 5 | Sufficiently documented for judges | ✅ | README: architecture, payment flow, repro steps "do X → see Y", REST API, configuration |
| 6 | New, or significantly updated during the Submission Period (Oct 1 – Nov 12) | ✅ | New project; first commit 2026-10-30. Say "built from scratch during the hackathon" in the Devpost text |
| 7 | Third-party APIs used within their terms | ✅ | PayPal Sandbox, Anthropic API, AG Grid Community (MIT) via CDN |
| 8 | Sponsor-prize specific requirements (§8) | ✅ | Rules only require "use any AG Grid tools" — the dashboard uses AG Grid Community (3 grids: approvals with action buttons, orders with Pay links, spend vs caps). No further AG Grid conditions are listed |

## Submission requirements

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| 9 | Text description of features and functionality | ✅ (draft) | `docs/DEVPOST.md`: inspiration, what it does, how built, challenges, accomplishments, learned, next, built with, AI disclosure, testing instructions. Fill `<GITHUB_URL>` / `<YOUTUBE_URL>` at submission |
| 10 | Functional demo judges can run (setup instructions **or** hosted URL; mockups don't count) | ✅ | Fresh clone runs the full flow with no keys at all: `AGENT_MODE=offline` (rule-based, labelled "no AI" in UI and output) + mock PayPal. Claude mode needs an Anthropic key; real Sandbox needs the judge's own free Sandbox app (G3) |
| 11 | Public repo on GitHub/GitLab/Bitbucket with all source, assets, instructions | ❌ | Repo is local only (by design until the user confirms). Needs: create public GitHub repo, push |
| 12 | Open-source licence file, detectable in the repo "About" section | 🟡 | `LICENSE` = MIT (GitHub detects it). Visible in About once pushed |
| 13 | Tools: identify which AI tools/models were used and how | ✅ | README "AI tools used" + `docs/DEVPOST.md` disclosure (Claude runtime agent, Hark coding assistant, Piper TTS for narration; offline parser is not AI) |
| 14 | Demo video < 3 min, shows the project working, public on YouTube, link on form | 🟡 | Draft `docs/demo_draft.mp4` (not committed) built by `scripts/video/make_demo.sh` from `docs/VIDEO_SCRIPT.md`: real Sandbox + Claude run, real Sandbox checkout page, approve, buyer cancel. Missing: the real Sandbox buyer login/pay clip (scene 7 placeholder, needs the user's one manual step); YouTube upload by the user |
| 15 | Video without third-party trademarks / copyrighted music without permission | ✅ (draft) | No music. Narration = Piper TTS, voice trained on LibriTTS-R (CC BY 4.0, credited on closing card). Only PayPal's own Sandbox page, AG Grid, Claude named |
| 16 | Testing access free of charge until judging ends; include any test credentials / sandbox login details or clear instructions to obtain access | 🟡 | README explains how to create a Sandbox app + personal test account. See G3 |
| 17 | English materials | ✅ | All repo docs in English |

## Judging criteria (equal weight) — where we stand

| Criterion | Strength | To strengthen |
|---|---|---|
| Technological implementation | Real Orders v2 lifecycle, idempotency, reservation accounting, structured PayPal errors, server-side guardrails (atomic check+reserve, single-use quote-bound approvals, reconciliation of ambiguous captures; see README Known limitations), 110 tests, offline no-key mode | Optional: PayPal webhooks (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`) instead of relying on the redirect; PayPal Agent Toolkit / MCP mention |
| Design | One coherent dashboard: ask box → approvals → orders → spend vs caps | Order ids now fully visible; possible: show agent answer as cards, item column still truncates long names |
| Potential impact | Clear problem: letting AI agents spend money safely (small teams, ops/procurement) | State the audience and a concrete scenario in the video's first 20 s |
| Innovation | Deterministic policy gate between LLM and wallet; reserve-at-create stops "many unpaid orders" cap evasion | Mention in pitch |
| Presentation | Video draft with narration, scene script, Devpost text | Real buyer-approval clip (G1) |

Prize fit: Grand prizes; honorable mentions **Best Use of Agentic Commerce**, **Best Use of PayPal + AI**; sponsor prize **Best Use of AG Grid**.

## Gaps / next actions

- **G1 Demo video** — draft exists (`docs/demo_draft.mp4`). Needs one ~20 s clip: Sandbox *personal* test account login → Pay → `COMPLETED` (password not available to the automation), spliced into scene 7; then the user uploads to YouTube.
- ~~G2 Agent without an API key~~ — done (iteration 3): `AGENT_MODE=offline`, rule-based, clearly labelled, 9 tests. Hosted demo (Render) remains optional.
- **G3 Sandbox credentials for judges** (current choice: "create your own Sandbox app" instructions; nothing published). Rules ask for "sandbox login details necessary to run". Options: keep "create your own Sandbox app" instructions (safe; mock mode works without it), or publish a throwaway Sandbox app's client id/secret in the Devpost testing instructions (Sandbox only, no real money, but anyone can use it). Decision needed — do not commit secrets to the repo.
- **G4 Public GitHub repo + push** (user confirmation required), then check the licence shows in About.
- **G5 Devpost text** — drafted in `docs/DEVPOST.md`; paste into the form at submission.
- **G6 (nice to have)** webhooks, Render deploy.
