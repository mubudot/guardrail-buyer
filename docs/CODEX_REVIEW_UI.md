# Codex review: UI E2E fixes (gpt-6.1-sol, high)

Scope: uncommitted changes to app/agent.py, app/static/app.js, app/static/app.css, app/static/index.html, tests/test_agent.py.

## Round 1

1. **Medium — [app/agent.py:121](app/agent.py:121):** Every non-COMPLETED status becomes “awaiting buyer approval,” including `PAYMENT_UNCONFIRMED`/`NEEDS_RECONCILIATION` and `FAILED`. This gives incorrect payment guidance. Choose replacement wording by status; preserve unconfirmed or failed outcomes.

2. **Medium — [app/agent.py:117](app/agent.py:117):** One unpaid ID triggers rewriting the entire line. With completed A and awaiting B, `Bought cable (A); mouse (B) awaits approval` incorrectly relabels A as awaiting approval. Rewrite individual order clauses, or regenerate ambiguous lines from tool results.

3. **Medium — [app/agent.py:123](app/agent.py:123):** Unconditional replacement corrupts correct negations: `ORD-ABC not bought yet` becomes `ORD-ABC not ordered (awaiting buyer approval) yet`, falsely denying order creation. Restrict replacements to affirmative purchase claims and add negation regression tests.

4. **Medium — [app/agent.py:118](app/agent.py:118):** Once any order completes, every ID-less “Bought” line passes unchanged—even one describing another, unpaid order. Associate claims with tool inputs/products; use deterministic status wording when attribution is ambiguous. Add a mixed completed/awaiting test with an ID-less unpaid claim.

**Handled:** all four valid. `correct_paid_wording` no longer edits lines: when any order of the run is not COMPLETED and the text affirmatively claims a purchase, the summary is replaced by `fallback_summary`, which words each order by its own status (paid / awaiting buyer / unconfirmed / failed). Negated claims are left alone. Tests: `test_negated_bought_is_left_alone`, `test_mixed_paid_and_unpaid_orders_use_per_order_wording`, `test_unconfirmed_and_failed_orders_keep_their_own_wording`, `test_idless_bought_reworded_when_nothing_completed`.

## Round 2

1. **P2 — Negation in a separate clause bypasses correction** ([app/agent.py:108](app/agent.py:108)). `claims_purchase("- Nothing refused. Bought 2 cables")` returns `False`, so an unpaid order retains the false purchase claim. `_NEGATION_RE` crosses punctuation and treats “Nothing refused” as negating “Bought.” Fix negation scope to the purchase clause and add regression tests for unrelated preceding negations.

**Handled:** valid. Negation now only counts inside the same clause (text after the last `. ; : ! ?` or newline). Test: `test_negation_in_another_clause_does_not_hide_a_claim`. No UI (JS/CSS/HTML) findings in either round.
