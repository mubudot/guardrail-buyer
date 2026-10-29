import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.guardrails import (OrderRequest, Policy, SpendLedger, Verdict, evaluate,
                            hard_rules_only, money)


def order(amount, merchant="Sandbox Electronics", category="office-supplies", currency="USD"):
    return OrderRequest(merchant=merchant, category=category, amount=money(amount), currency=currency)


def test_allow_within_caps(policy):
    d = evaluate(policy, order("12.99"))
    assert d.verdict == Verdict.ALLOW and d.reasons


def test_exactly_at_per_order_cap_is_allowed(policy):
    assert evaluate(policy, order("50.00")).verdict == Verdict.ALLOW


def test_over_per_order_cap_needs_approval(policy):
    d = evaluate(policy, order("50.01"))
    assert d.verdict == Verdict.NEEDS_APPROVAL
    assert any("per-order cap" in r for r in d.reasons)


def test_daily_cap_needs_approval(policy):
    d = evaluate(policy, order("40"), spent_today=Decimal("120"))
    assert d.verdict == Verdict.NEEDS_APPROVAL
    assert any("daily" in r for r in d.reasons)


def test_daily_cap_boundary_allowed(policy):
    assert evaluate(policy, order("30"), spent_today=Decimal("120")).verdict == Verdict.ALLOW


def test_both_caps_report_two_reasons(policy):
    d = evaluate(policy, order("100"), spent_today=Decimal("100"))
    assert d.verdict == Verdict.NEEDS_APPROVAL and len(d.reasons) == 2


def test_hard_max_denies(policy):
    d = evaluate(policy, order("500.01"))
    assert d.verdict == Verdict.DENY


def test_blocked_merchant_denied_even_if_cheap(policy):
    d = evaluate(policy, order("1", merchant="Shady Gifts Sandbox", category="office-supplies"))
    assert d.verdict == Verdict.DENY and "blocked" in d.reasons[0]


def test_merchant_not_on_allowlist(policy):
    d = evaluate(policy, order("5", merchant="Random Store"))
    assert d.verdict == Verdict.DENY and "allowlist" in d.reasons[0]


def test_category_not_on_allowlist(policy):
    d = evaluate(policy, order("5", category="gift-cards"))
    assert d.verdict == Verdict.DENY


def test_allowlists_case_insensitive(policy):
    assert evaluate(policy, order("5", merchant="SANDBOX electronics", category="Office-Supplies")).verdict == Verdict.ALLOW


def test_wrong_currency_denied(policy):
    assert evaluate(policy, order("5", currency="EUR")).verdict == Verdict.DENY


@pytest.mark.parametrize("amt", ["0", "-3"])
def test_non_positive_amount_denied(policy, amt):
    assert evaluate(policy, order(amt)).verdict == Verdict.DENY


def test_deny_beats_needs_approval(policy):
    d = evaluate(policy, order("600", merchant="Random Store"), spent_today=Decimal("149"))
    assert d.verdict == Verdict.DENY and len(d.reasons) == 2


def test_empty_allowlists_mean_any():
    p = Policy.from_dict({"per_order_cap": 10, "daily_cap": 20, "hard_max_per_order": 100})
    assert evaluate(p, order("5", merchant="Anyone", category="anything")).verdict == Verdict.ALLOW


def test_hard_rules_only_waives_caps_but_keeps_denies(policy):
    assert hard_rules_only(policy, order("300")).verdict == Verdict.ALLOW
    assert hard_rules_only(policy, order("5", category="gift-cards")).verdict == Verdict.DENY


def test_policy_validation():
    with pytest.raises(ValueError):
        Policy.from_dict({"per_order_cap": 100, "hard_max_per_order": 50})
    with pytest.raises(ValueError):
        Policy.from_dict({"daily_cap": 0})


def test_policy_loads_json(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"per_order_cap": 20, "daily_cap": 40, "allowed_categories": ["Food"]}))
    p = Policy.load(f)
    assert p.per_order_cap == Decimal("20.00") and p.allowed_categories == ("food",)


def test_money_rounding():
    assert money("0.105") == Decimal("0.11") and money(1) == Decimal("1.00")


def test_today_respects_timezone():
    p = Policy.from_dict({"timezone": "Asia/Taipei"})
    late_utc = datetime(2026, 10, 30, 17, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 next day in Taipei
    assert p.today(late_utc) == "2026-10-31"


def test_ledger_sums_per_day_and_currency(tmp_path):
    led = SpendLedger(str(tmp_path / "l.db"))
    led.record("2026-10-30", Decimal("10.10"), "USD", "a")
    led.record("2026-10-30", Decimal("0.20"), "usd", "b")
    led.record("2026-10-31", Decimal("99"), "USD", "c")
    led.record("2026-10-30", Decimal("5"), "EUR", "d")
    assert led.spent_on("2026-10-30", "USD") == Decimal("10.30")
    assert led.spent_on("2026-10-31", "USD") == Decimal("99.00")
    assert led.spent_on("2026-11-01", "USD") == Decimal("0.00")
    # persists across connections
    assert SpendLedger(str(tmp_path / "l.db")).spent_on("2026-10-30", "EUR") == Decimal("5.00")
