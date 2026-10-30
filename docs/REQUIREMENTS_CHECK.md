# PayPal AI Hackathon — requirements check

Source: Official Rules <https://paypalaihackathon.devpost.com/rules> and the Overview page
<https://paypalaihackathon.devpost.com/>, re-read 2026-10-30 (cross-check round 5, ~07:00 Asia/Taipei; again at 08:55 in the final re-check: no change to
dates, requirements, judging criteria or prizes).
Deadline: **Nov 12, 2026 12:00 PM PST** (= Nov 13 04:00 Asia/Taipei). Judging Dec 1–15, 2026; testing
access must stay free until judging ends.

Legend: ✅ done · 🟡 partly / needs a decision · ❌ not done yet

## Project requirements (Rules §4 "Project Requirements")

| # | Requirement (rules wording, shortened) | Status | Evidence / gap |
|---|---|---|---|
| 1 | Integrates the PayPal developer platform **using the free Sandbox** | ✅ | Orders v2 against `api-m.sandbox.paypal.com`: OAuth2, create (intent CAPTURE, payer-action link, `PayPal-Request-Id`), GET status, capture, structured errors (`app/paypal_client.py`). Live smoke `docs/sandbox_smoke_run.txt`; real buyer approval → capture `COMPLETED` verified 2026-10-30 (`docs/SANDBOX_E2E.md`, `docs/screenshots/`) |
| 2 | "Any AI tool works, as long as PayPal integration is central" | ✅ | Every purchase is a PayPal order; budget reserved at order creation, released only on cancel/expiry/definitive failure; capture only after buyer approval |
| 3 | Uses an AI tool/model | ✅ | Claude (Messages API, tool use) is the purchasing agent (`app/agent.py`); live transcripts `docs/sample_transcript.json`, `docs/live_claude_smoke_transcript.json` |
| 4 | Functionality: installs and runs consistently, functions as in video/description | ✅ | Round-5 judge repro from a fresh clone (README offline block, no keys): `255 passed, 1 skipped` (final re-check 2026-10-30 after new tests: `268 passed, 1 skipped`), server ready check, ask → cables order / keyboard approval / gift card refused, mock Pay now → `COMPLETED` + capture id, approve 401 without token / OK with token, spend 25.98 captured + 89.00 reserved, reject, Audit API, CLI. See `docs/REVIEW_LOG.md` |
| 5 | Platforms: runs on the intended platform | ✅ | Local web app (Python 3.11+, any desktop browser). Stated in README |
| 6 | New, or significantly updated during the Submission Period (Oct 1 – Nov 12) | ✅ | New project; first commit 2026-10-29 (UTC). Devpost text says "built from scratch during the hackathon" |
| 7 | Third-party SDK/APIs used within their terms | ✅ | PayPal Sandbox, Anthropic API, AG Grid Community 32.3.3 (MIT, vendored with its LICENSE), Piper TTS voice (CC BY 4.0, credited) |
| 8 | Sponsor-prize requirements (§8): AG Grid prize = "use any AG Grid tools" | ✅ | Dashboard uses AG Grid Community 32.3.3 (vendored, no CDN): approvals grid with action buttons, orders grid with Pay links, spend-vs-caps grid, and an **Audit** grid (floating filters, quick search, `cellClassRules`/`rowClassRules` risk colouring, CSV export with formula neutralisation, `getRowId` delta updates, row-click detail panel, keyset paging). Community features only |

## Submission requirements

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| 9 | Text description of features and functionality | ✅ (draft) | `docs/DEVPOST.md`; fill `<GITHUB_URL>` / `<YOUTUBE_URL>` at submission |
| 10 | Functional demo judges can run (setup instructions **or** hosted URL; mockups don't count) | ✅ | Fresh clone runs the full flow with no keys (`AGENT_MODE=offline`, labelled "no AI", + mock PayPal with a mock checkout). Claude mode needs an Anthropic key (now an actionable error if missing); real Sandbox needs the judge's own free Sandbox app (G3) |
| 11 | Public repo (GitHub/GitLab/Bitbucket) with all source, assets, instructions | ❌ | Repo is local only by design until the user confirms. Needs: create public GitHub repo, push (G4) |
| 12 | Open-source licence file, detectable in the repo "About" section | 🟡 | `LICENSE` = MIT (GitHub detects it). Check About once pushed |
| 13 | Identify which AI tools were used | ✅ | README "AI tools used" + `docs/DEVPOST.md`: Claude (runtime agent), Hark (coding assistant), OpenAI Codex CLI (independent reviewer), Piper TTS (narration); offline parser is not AI |
| 14 | Demo video < 3 min, shows the project working, public on YouTube, link on form | 🟡 | Final cut 2:23 (kept outside the repo) with real Sandbox buyer login → Pay → capture `COMPLETED` footage in scene 7 (run `ORD-F79F379F1E` / capture `14579671MH105231J`, `docs/SANDBOX_E2E.md`); OCR-checked for personal data. Remaining: YouTube upload by the user (G1) |
| 14b | Video numbers match the repo | ✅ | Closing card says "256 tests" (true when recorded: 255 + 1 jsdom). The final re-check added 13 tests (now 269), so the video understates by 13; nothing on screen is false. No re-record needed |
| 15 | Video: no third-party trademarks / copyrighted music without permission | ✅ (draft) | No music; Piper narration credited; only PayPal's own Sandbox pages, AG Grid, Claude named |
| 16 | Testing access free until judging ends; include test credentials / sandbox login details or clear instructions to obtain access | 🟡 | README explains creating a free Sandbox app + personal test account; mock mode needs nothing. Decision on publishing throwaway Sandbox credentials still open (G3) |
| 17 | English materials | ✅ | All repo docs in English |
| 18 | Original work, solely owned; open-source deps used under their licences | ✅ | Own code; vendored AG Grid MIT licence kept |
| 19 | Privacy: no personal data in submission | ✅ | Round-5 scan of tracked files, git history and OCR of every screenshot: no personal email, real name, address, keys, tokens, account ids or tunnel URLs. Only the PayPal-generated Sandbox test buyer `sb-…@personal.example.com` and Sandbox order/capture ids appear (see `docs/REVIEW_LOG.md`) |

## Judging (Stage One pass/fail: fits the theme, applies the PayPal APIs → ✅). Stage Two, equal weight

| Criterion (rules wording) | Strength | To strengthen |
|---|---|---|
| Technological implementation — "how thoroughly and skillfully does the project use PayPal Developer Platform and AI tool(s)? … non-trivial" | Full Orders v2 lifecycle with idempotency, reservation accounting, ambiguous-outcome reconciliation (now also safe for refused replays and unreadable `ORDER_ALREADY_CAPTURED`), server-side guardrails, quote-bound approvals, strict tool-argument validation, 269 tests (268 + 1 jsdom), offline no-key mode | Optional: PayPal webhooks (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`) |
| Design — "complete, coherent product experience" | One dashboard: ask box → approvals → orders → spend vs caps, plus Audit tab | Agent answer as cards (optional) |
| Potential impact — "credible, specific case … real audience" | Letting AI agents spend money safely (small teams, ops/procurement) | State audience + scenario in the video's first 20 s |
| Innovation/Idea | Deterministic policy gate between LLM and wallet; reserve-at-create blocks "many unpaid orders" cap evasion; split-order aggregation | Say it in the pitch |
| Presentation — "video clearly demonstrates the project working end-to-end" | Narrated video with real Sandbox buyer approval + capture on screen, scene script, Devpost text, two documented real buyer-approval runs | Upload (G1) |

Prize fit: Grand prizes; honorable mentions **Best Use of Agentic Commerce**, **Best Use of PayPal + AI**
(a project can win one grand *or* one honorable mention, plus one sponsor prize); sponsor prize
**Best Use of AG Grid** (only condition: uses any AG Grid tools).

## Gaps / next actions

- **G1 Demo video** — done except the upload: the final cut (2:23) includes real scene-7 footage (Sandbox personal account login → Pay → `COMPLETED`). The user uploads it to YouTube (public) and pastes the link.
- **G3 Sandbox credentials for judges** — decision needed: keep "create your own Sandbox app" (current, safe; mock mode works without it) or publish a throwaway Sandbox app's credentials in the Devpost testing instructions only. Never commit secrets.
- **G4 Public GitHub repo + push** (user confirmation required), then check the licence shows in About.
- **G5 Devpost text** — `docs/DEVPOST.md`; fill the two placeholders.
- **G6 (nice to have)** webhooks, hosted demo (Render).
