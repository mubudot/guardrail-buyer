"""Run the purchasing agent once from the command line (MockPayPal by default).

    python -m app.cli "Buy 2 USB-C cables and a mechanical keyboard"

AGENT_MODE=offline uses the rule-based parser (no AI key needed).

Uses a fresh in-memory ledger each run, so it does not share spend with a running
server. CLI_DB_PATH=<file> uses a database file instead. With the mock PayPal
(PAYPAL_MODE=mock, the default) the CLI must own that file exclusively, exactly like a
server (mock PayPal state is in-memory): it takes the same lock and refuses with exit
code 3 while a mock server (or another CLI) uses the database. Sharing a database with a
running server is only possible in Sandbox mode.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("GRB_NO_AUTOAPP", "1")

from .offline_agent import make_agent  # noqa: E402
from .main import build_service  # noqa: E402
from .llm import LLMError  # noqa: E402
from .service import MockLockError  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    svc = build_service(db_path=os.environ.get("CLI_DB_PATH", ":memory:"))
    try:
        # same single-process ownership as the server (CODEX_REVIEW_3 #3); a no-op for
        # the in-memory default and for Sandbox mode
        svc.invalidate_lost_mock_orders()
    except MockLockError as e:
        print(f"ERROR: {e}\nThe CLI cannot share a mock-mode database with a running server: "
              f"unset CLI_DB_PATH (own in-memory DB) or stop the server.", file=sys.stderr)
        return 3
    try:
        return _run(svc, argv)
    except LLMError as e:  # already redacted; no traceback (it could carry the raw upstream error)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        svc.release_mock_lock()


def _run(svc, argv: list[str]) -> int:
    res = make_agent(svc).run(" ".join(argv))
    for step in res.transcript:
        if step["role"] == "tool":
            out = step["output"]
            tag = out.get("result") or out.get("verdict") or f"{len(out.get('products', []))} products"
            print(f"  [tool] {step['name']}({json.dumps(step['input'])}) -> {tag}")
        elif step["role"] == "user":
            print(f"[user] {step['text']}\n")
    print(f"\n[agent]\n{res.final_text}\n")
    print(f"turns={res.turns} usage={res.usage} stop={res.stopped_reason}")
    print(f"spent_today={svc.spent_today()} pending_approvals={len(svc.store.list('approvals', 'PENDING'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
