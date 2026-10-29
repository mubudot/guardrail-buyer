"""Offline agent mode (AGENT_MODE=offline): a deterministic, rule-based parser.

This is NOT an AI model. It exists so judges and reviewers can run the full flow
(request -> catalog -> guardrails -> PayPal order / approval / refusal) without an
Anthropic API key. It understands simple shopping lists over the demo catalog,
e.g. "Buy 2 USB-C cables, a mechanical keyboard and a $100 gift card".

It calls exactly the same tools as the Claude agent (search_catalog -> check_policy
-> create_order -> request_approval), through the same BuyerService, so every
guardrail is enforced identically and the dashboard shows the same transcript.
"""
from __future__ import annotations

import re
from typing import Any

from .agent import AgentResult, PurchasingAgent, fallback_summary
from .service import BuyerService

OFFLINE_MODEL_NAME = "offline-rules (no AI)"

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair": 2, "couple": 2,
                "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "dozen": 12}

# Extra words people use for catalog items (keyword -> product ids it points to).
SYNONYMS = {
    "charger": ["P-1001"], "charging": ["P-1001"], "cable": ["P-1001"], "cord": ["P-1001"],
    "usb": ["P-1001"], "usbc": ["P-1001"], "mice": ["P-1002"], "mouse": ["P-1002"],
    "keyboard": ["P-1003"], "monitor": ["P-1004"], "screen": ["P-1004"], "display": ["P-1004"],
    "chair": ["P-1005"], "seat": ["P-1005"], "paper": ["P-2001"], "printer": ["P-2001"],
    "postit": ["P-2002"], "sticky": ["P-2002"], "notes": ["P-2002"], "coffee": ["P-3001"],
    "beans": ["P-3001"], "snack": ["P-3002"], "snacks": ["P-3002"], "hosting": ["P-4001"],
    "cloud": ["P-4001"], "credit": ["P-4001"], "design": ["P-4002"], "license": ["P-4002"],
    "licence": ["P-4002"], "gift": ["P-5001"], "giftcard": ["P-5001"], "crypto": ["P-5002"],
    "voucher": ["P-5002"],
}
STOP = {"the", "of", "for", "and", "with", "our", "new", "box", "pack", "bag", "bags", "buy",
        "order", "get", "please", "some", "team", "sandbox", "co", "inch", "2m", "1kg", "x"}
SPLIT_RE = re.compile(r",|;|\n|\band also\b|\balso\b|\bplus\b|\bas well as\b|\band\b|&", re.I)
QTY_RE = re.compile(r"(?<![\$\d.])\b(\d{1,3})\b(?!\s*(?:-?\s*inch|%|kg|g\b|m\b|sheets|\.\d))", re.I)


def _norm(word: str) -> str:
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(w) > 3 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("es") and w[:-2].endswith(("ch", "sh", "x")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


class OfflineAgent:
    """Same interface as PurchasingAgent: .model and .run(request) -> AgentResult."""

    model = OFFLINE_MODEL_NAME

    def __init__(self, service: BuyerService):
        self.service = service
        self._tools = PurchasingAgent(service, post=lambda p: {})  # reuse its tool dispatcher
        self._index: dict[str, dict[str, set[str]]] = {}
        for pid, p in service.catalog.products.items():
            words = {_norm(w) for w in re.split(r"[\s\-/()]+", p["name"])} - {_norm(s) for s in STOP} - {""}
            self._index[pid] = {"words": {w for w in words if not w.isdigit()}}
        for word, pids in SYNONYMS.items():
            for pid in pids:
                if pid in self._index:
                    self._index[pid]["words"].add(_norm(word))

    # ---- parsing -------------------------------------------------------
    def parse(self, request: str) -> list[dict[str, Any]]:
        """Split a request into (product_id, quantity, clause) lines. Unmatched clauses get product_id None."""
        lines = []
        for clause in SPLIT_RE.split(request):
            clause = clause.strip(" .!?")
            if not clause:
                continue
            pid = self._match(clause)
            if pid is None:
                # only report clauses that look like an item, not filler like "for our new hire"
                if re.search(r"\b(\d+|an?|one|two|three|some)\b", clause, re.I) and not re.match(r"^(for|to)\b", clause, re.I):
                    lines.append({"product_id": None, "quantity": 0, "clause": clause})
                continue
            lines.append({"product_id": pid, "quantity": self._quantity(clause), "clause": clause})
        return lines

    def _match(self, clause: str) -> str | None:
        toks = {_norm(t) for t in re.split(r"[\s\-/()]+", clause)} - {""}
        if "usb" in toks and "c" in toks:
            toks.add("usbc")
        if "post" in toks and "it" in toks:
            toks.add("postit")
        if "gift" in toks and "card" in toks:
            toks.add("giftcard")
        best, best_score = None, 0
        for pid, entry in self._index.items():
            score = len(toks & entry["words"])
            if score == 0:
                continue
            price = float(self.service.catalog.get(pid)["price"])
            if score > best_score or (score == best_score and best is not None
                                      and price < float(self.service.catalog.get(best)["price"])):
                best, best_score = pid, score
        return best

    @staticmethod
    def _quantity(clause: str) -> int:
        m = QTY_RE.search(clause)
        if m:
            return max(1, min(int(m.group(1)), 999))
        for w in re.findall(r"[a-z]+", clause.lower()):
            if w in NUMBER_WORDS and w not in ("a", "an"):
                return NUMBER_WORDS[w]
        return 1

    # ---- run -----------------------------------------------------------
    def _tool(self, res: AgentResult, name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = self._tools._run_tool(name, args)
        res.transcript.append({"role": "tool", "name": name, "input": args, "output": out})
        return out

    def run(self, request: str) -> AgentResult:
        res = AgentResult(final_text="", stopped_reason="offline_rules")
        res.transcript.append({"role": "user", "text": request})
        lines = self.parse(request)
        notes = []
        for ln in lines:
            if ln["product_id"] is None:
                notes.append(f"- \"{ln['clause']}\": not found in the catalog (offline mode only matches catalog items)")
                continue
            p = self.service.catalog.get(ln["product_id"])
            self._tool(res, "search_catalog", {"query": p["name"].lower()})
            args = {"product_id": p["id"], "quantity": ln["quantity"]}
            chk = self._tool(res, "check_policy", args)
            if chk.get("verdict") == "DENY":
                # the server re-checks anyway; record the refusal through create_order like the AI agent does
                self._tool(res, "create_order", args)
                continue
            out = self._tool(res, "create_order", args)
            if out.get("result") == "NEEDS_APPROVAL":
                self._tool(res, "request_approval", {**args, "justification":
                           f"Requested by user: \"{ln['clause']}\" ({p['name']} x{ln['quantity']}). "
                           f"Exceeds an automatic spend cap, so a human must approve."})
        res.turns = 1
        summary = fallback_summary(res.transcript)
        if notes:
            summary = "\n".join(x for x in (summary, *notes) if x)
        if not lines:
            summary = ("- Offline mode could not match anything in the catalog. Name a product, "
                       "e.g. \"2 USB-C cables and a wireless mouse\".")
        res.final_text = "[offline rule-based mode, no AI]\n" + summary
        return res


def make_agent(service: BuyerService, mode: str | None = None):
    """AGENT_MODE=claude (default) -> PurchasingAgent; AGENT_MODE=offline -> OfflineAgent."""
    import os
    mode = (mode or os.environ.get("AGENT_MODE") or "claude").strip().lower()
    if mode in ("offline", "rules", "rule", "scripted"):
        return OfflineAgent(service)
    if mode not in ("claude", "llm", "ai", ""):
        raise ValueError(f"unknown AGENT_MODE {mode!r} (use 'claude' or 'offline')")
    return PurchasingAgent(service)
