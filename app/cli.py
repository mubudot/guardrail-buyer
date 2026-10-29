"""Run the purchasing agent once from the command line (MockPayPal by default).

    python -m app.cli "Buy 2 USB-C cables and a mechanical keyboard"
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
    svc = build_service(db_path=os.environ.get("DB_PATH", ":memory:"))
    res = PurchasingAgent(svc).run(" ".join(argv))
    for step in res.transcript:
        if step["role"] == "tool":
            out = step["output"]
            tag = out.get("result") or out.get("verdict") or f"{len(out.get('products', []))} products"
            print(f"  [tool] {step['name']}({json.dumps(step['input'])}) -> {tag}")
        else:
            print(f"[{step['role']}] {step['text']}\n")
    print(f"turns={res.turns} usage={res.usage} stop={res.stopped_reason}")
    print(f"spent_today={svc.spent_today()} pending_approvals={len(svc.store.list('approvals', 'PENDING'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
