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

NUMBER_WORDS = {"zero": 0, "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair": 2, "couple": 2,
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

# ---- positive-only grammar (B8) ------------------------------------------------
# The offline parser accepts ONLY requests built from this grammar; anything else is
# refused as a whole (never partially executed):
#
#   request  := clause ( ("," | ";" | "and" | "plus" | "also" | "&" | newline) clause )*
#   clause   := [filler] [verb] [quantity] item [purpose] [filler]
#   verb     := buy | get | order | purchase | need | grab   (optionally "i/we need", "me/us")
#   quantity := non-negative whole number | a | an | one … ten | single | pair | couple | dozen
#               (optionally "a dozen", "a pair of", …)
#   item     := words from the catalog vocabulary (product-name words, synonyms, sizes such
#               as 27-inch / 4k / 2m / 1kg, prices such as $100, and packaging words)
#   purpose  := ("for" | "as") up to 8 plain words, none of them a condition/negation word
#   filler   := please
#
# There is no denylist for the executed part: an unknown verb ("avoid"), a condition
# ("only when approved"), a negative or fractional number ("-1", "1.5") or any word
# outside the catalog vocabulary makes the clause ungrammatical, so nothing is bought.
GRAMMAR_HELP = (
    "Supported offline grammar: [buy|get|order|purchase|need] <quantity> <catalog item> "
    "[for/as <short purpose>], clauses joined by commas, \"and\", \"plus\" or \";\". "
    "<quantity> is a non-negative whole number or a/an/one…ten/pair/couple/dozen. "
    "Example: \"Buy 2 USB-C cables and a wireless mouse for the new hire\". "
    "Anything else (negation such as \"avoid\"/\"don't\", conditions such as \"only when\"/\"if\", "
    "negative or fractional numbers, unknown verbs or words) is refused as a whole.")
VERBS = {"buy", "get", "order", "purchase", "need", "grab"}
VERB_FILLERS = {"please", "pls", "kindly", "i", "we", "me", "us"}
QTY_WORDS = set(NUMBER_WORDS) | {"of"}
PURPOSE_MARKERS = {"for", "as"}
# Words that would turn a purpose label into a condition / negation. Only checked in the
# short free-text purpose tail, which is never executed (it is only a label).
PURPOSE_FORBIDDEN = {
    "if", "unless", "only", "when", "whenever", "until", "after", "before", "once", "provided",
    "approved", "approve", "approval", "not", "no", "never", "dont", "don't", "doesnt", "without",
    "except", "cancel", "stop", "avoid", "instead", "later", "maybe", "unless", "then", "while",
    "budget", "cheap", "cheaper", "under", "over", "below", "above", "less", "more", "than"}
SIGNED_OR_FRACTION_RE = re.compile(r"(?:^|[\s(,;:])[-−–+]\s*\d|\bminus\b|\bnegative\b|(?<![\$\d.])\d+[.,]\d+(?!\s*(?:kg|m\b))",
                                   re.I)
UNIT_TOKEN_RE = re.compile(r"^\d{1,4}(?:inch|in|k|m|kg|g|gb|tb|pack|sheets?)$", re.I)
PRICE_TOKEN_RE = re.compile(r"^\$\d{1,6}(?:\.\d{2})?$")


class GrammarError(ValueError):
    """The request is outside the supported offline grammar."""


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
    def _vocab(self) -> set[str]:
        if not hasattr(self, "_vocab_cache"):
            v = set()
            for entry in self._index.values():
                v |= entry["words"]
            v |= {_norm(w) for w in STOP} | {"a", "an", "the", "c", "post", "it", "card", "tool",
                                              "piece", "unit", "set", "pc", "pcs", "item"}
            self._vocab_cache = v - {""}
        return self._vocab_cache

    def validate_clause(self, clause: str) -> str:
        """Check one clause against the grammar. Returns the item phrase (verb, quantity
        and purpose removed); raises GrammarError otherwise."""
        toks = [t for t in re.split(r"\s+", clause.strip()) if t]
        low = [t.lower().strip(".!?") for t in toks]
        i = 0
        while i < len(low) and low[i] in VERB_FILLERS:
            i += 1
        if i < len(low) and low[i] in VERBS:
            i += 1
            while i < len(low) and low[i] in VERB_FILLERS:
                i += 1
        # quantity: digits or number words (e.g. "2", "a", "a dozen", "a pair of")
        q_start = i
        if i < len(low) and re.fullmatch(r"\d{1,3}", low[i]):
            i += 1
        else:
            while i < len(low) and low[i] in QTY_WORDS and i - q_start < 3:
                i += 1
        # item until a purpose marker
        j = i
        while j < len(low) and low[j] not in PURPOSE_MARKERS:
            j += 1
        item_toks, purpose = low[i:j], low[j + 1:]
        while item_toks and item_toks[-1] == "please":
            item_toks.pop()
        if not item_toks:
            raise GrammarError(f"\"{clause}\": no catalog item after the verb/quantity")
        vocab, unknown = self._vocab(), []
        for t in item_toks:
            if PRICE_TOKEN_RE.match(t):
                continue
            for part in re.split(r"[\-/()]+", t):
                part = part.strip(".!?'\"")
                if not part or PRICE_TOKEN_RE.match(part) or UNIT_TOKEN_RE.match(part):
                    continue
                if part.isdigit() and t != part and re.search(r"\d+-(?:inch|in)\b", t):
                    continue  # "27-inch"
                if _norm(part) not in vocab:
                    unknown.append(part)
        if unknown:
            raise GrammarError(f"\"{clause}\": unsupported word(s) " +
                               ", ".join(f"\"{u}\"" for u in dict.fromkeys(unknown)))
        if j < len(low):  # purpose tail
            words = [w for w in purpose if w != "please"]
            bad = [w for w in words if not re.fullmatch(r"[a-z']+", w) or w.replace("’", "'") in PURPOSE_FORBIDDEN]
            if not words or len(words) > 8 or bad:
                raise GrammarError(f"\"{clause}\": the \"{low[j]} …\" part must be a short plain purpose "
                                   f"(no conditions, negation or numbers)" +
                                   (f"; not allowed: {', '.join(bad)}" if bad else ""))
        return " ".join(toks[i:j])

    def validate(self, request: str) -> None:
        """Raise GrammarError unless every clause of the request is in the grammar."""
        m = SIGNED_OR_FRACTION_RE.search(request)
        if m:
            raise GrammarError(f"negative or fractional quantity \"{m.group(0).strip()}\"")
        if re.search(r"[^\w\s,;.!?&$'’\-/()\n]", request):
            ch = re.search(r"[^\w\s,;.!?&$'’\-/()\n]", request).group(0)
            raise GrammarError(f"unsupported character \"{ch}\"")
        clauses = [c.strip(" .!?") for c in SPLIT_RE.split(request)]
        clauses = [c for c in clauses if c]
        if not clauses:
            raise GrammarError("empty request")
        for c in clauses:
            self.validate_clause(c)

    def parse(self, request: str) -> list[dict[str, Any]]:
        """Split a (grammar-checked) request into (product_id, quantity, clause) lines.
        Clauses whose item words match no product get product_id None."""
        lines = []
        for clause in SPLIT_RE.split(request):
            clause = clause.strip(" .!?")
            if not clause:
                continue
            try:
                item = self.validate_clause(clause)
            except GrammarError:
                item = clause
            pid = self._match(item)
            if pid is None:
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
            return min(int(m.group(1)), 999)  # 0 is kept as 0 and refused in run()
        for w in re.findall(r"[a-z]+", clause.lower()):
            if w in NUMBER_WORDS and w not in ("a", "an"):
                return NUMBER_WORDS[w]
        return 1

    # ---- run -----------------------------------------------------------
    def _tool(self, res: AgentResult, name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = self._tools._run_tool(name, args, request_id=res.request_id)
        res.transcript.append({"role": "tool", "name": name, "input": args, "output": out})
        return out

    def refusal(self, request: str) -> str | None:
        """Why the whole request is refused (outside the positive-only grammar), or None."""
        try:
            self.validate(request)
        except GrammarError as e:
            return (f"- Offline mode refused the whole request: {e}. {GRAMMAR_HELP} "
                    "Nothing was ordered.")
        return None

    def run(self, request: str) -> AgentResult:
        res = AgentResult(final_text="", stopped_reason="offline_rules")
        res.transcript.append({"role": "user", "text": request})
        refused = self.refusal(request)
        if refused:
            res.turns = 1
            res.stopped_reason = "offline_refused"
            res.final_text = "[offline rule-based mode, no AI]\n" + refused
            return res
        lines = self.parse(request)
        notes = []
        for ln in lines:
            if ln["product_id"] is None:
                notes.append(f"- \"{ln['clause']}\": not found in the catalog (offline mode only matches catalog items)")
                continue
            if ln["quantity"] < 1:
                notes.append(f"- \"{ln['clause']}\": quantity 0, nothing ordered")
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
