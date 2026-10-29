"""Run the purchasing agent once from the command line (MockPayPal by default).

    python -m app.cli "Buy 2 USB-C cables and a mechanical keyboard"

Uses a fresh in-memory ledger each run, so it does not share spend with a running
server; set CLI_DB_PATH=guardrail.db to use the server's database instead.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("GRB_NO_AUTOAPP", "1")

from .agent import PurchasingAgent  # noqa: E402
from .main import build_service  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    svc = build_service(db_path=os.environ.get("CLI_DB_PATH", ":memory:"))
    res = PurchasingAgent(svc).run(" ".join(argv))
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
