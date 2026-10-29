| Re-review finding | Verdict | Rerun evidence |
|---|---|---|
| P1: OAuth 400 replay releases spend | **FIXED** | `review5_rereview.py`: remains `NEEDS_RECONCILIATION`, **$12.99 retained**, no second Orders request. |
| P2: Non-object input crashes `fallback_summary` | **FIXED** | Same script: list/string/integer inputs return **HTTP 200**, preserving the paid purchase at both turn limits. |

After `source .venv/bin/activate`, `python -m pytest -q`: **255 passed, 1 skipped**. All `/tmp/grb_*.py` scripts completed.

Additional checks verified `source="oauth"` for HTTP failures, transport errors, unreadable/non-object bodies, missing tokens and invalid expiry strings; replays retained reservations. Orders errors retain `source="orders"`. Summary normalization preserved purchases across maximum-turn, empty-final and LLM-error exits.

Current tree includes token-body handling beyond the supplied diff. No repository files modified.

**No new findings.**