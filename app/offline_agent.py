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

# ---- positive-only grammar (B8) ------------------------------------------------
# The offline parser accepts ONLY requests built from this grammar; anything else is
# refused as a whole (never partially executed):
#
#   request  := clause ( ("," | ";" | "and" | "plus" | "also" | "&" | newline) clause )*
#   clause   := [filler] [verb] [quantity] item [purpose] [filler]
#   verb     := buy | get | order | purchase | need | grab   (optionally "i/we need", "me/us")
#   quantity := ONE of: non-negative whole number | a | an | one … ten | single | pair |
#               couple | dozen | a dozen | a pair (of) | a couple (of) | a single
#               It is read only from the slot immediately before the item; a second
#               quantity ("two three cables") is ambiguous and refused.
#   item     := words from the catalog vocabulary (product-name words, synonyms, sizes such
#               as 27-inch / 4k / 2m / 1kg, prices such as $100, and packaging words)
#   purpose  := ("for" | "as") up to 8 plain words, none of them a condition/negation word.
#               It is an inert label: parsed once, never read for quantity or item.
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
GROUP_WORDS = {"dozen", "pair", "couple", "single"}
PURPOSE_MARKERS = {"for", "as"}
# Words that would turn a purpose label into a condition / negation. Only checked in the
# short free-text purpose tail. The tail never feeds quantity or item selection (fix
# round 6); this list only keeps conditional wording out of the transcript label.
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

    def parse_clause(self, clause: str) -> dict[str, Any]:
        """Parse ONE clause, once, into its structured parts (fix round 6):

            {"verb": str|None, "quantity": int, "quantity_text": str|None,
             "item": str, "purpose": str|None}

        The quantity comes ONLY from the slot immediately before the item; the item and
        the purpose tail are never scanned for numbers, so "for two interns" cannot change
        what is bought. Raises GrammarError for anything outside the grammar, including an
        ambiguous quantity such as "two three cables"."""
        toks = [t for t in re.split(r"\s+", clause.strip()) if t]
        low = [t.lower().strip(".!?") for t in toks]
        i, verb = 0, None
        while i < len(low) and low[i] in VERB_FILLERS:
            i += 1
        if i < len(low) and low[i] in VERBS:
            verb = low[i]
            i += 1
            while i < len(low) and low[i] in VERB_FILLERS:
                i += 1
        # ---- quantity slot: immediately before the item ----
        q_start, quantity = i, 1
        nxt = low[i + 1] if i + 1 < len(low) else None
        if i < len(low) and re.fullmatch(r"\d{1,3}", low[i]):
            quantity, i = int(low[i]), i + 1
        elif i < len(low) and low[i] in ("a", "an") and nxt in GROUP_WORDS:
            quantity, i = NUMBER_WORDS[nxt], i + 2          # "a dozen", "a pair", "a single"
        elif i < len(low) and low[i] in ("a", "an"):
            quantity, i = 1, i + 1
        elif i < len(low) and low[i] in NUMBER_WORDS:
            quantity, i = NUMBER_WORDS[low[i]], i + 1      # "two", "zero", "dozen", "pair"
        if i > q_start and low[i - 1] in GROUP_WORDS - {"single"} and i < len(low) and low[i] == "of":
            i += 1                                           # "a pair of", "couple of"
        quantity_text = " ".join(toks[q_start:i]) or None
        if i < len(low) and (re.fullmatch(r"\d+", low[i]) or low[i] in NUMBER_WORDS):
            raise GrammarError(f"\"{clause}\": ambiguous quantity \"{' '.join(toks[q_start:i + 1])}\" "
                               f"(give exactly one quantity before the item)")
        # ---- item: up to the purpose marker ----
        j = i
        while j < len(low) and low[j] not in PURPOSE_MARKERS:
            j += 1
        item_toks, purpose = low[i:j], low[j + 1:]
        k = j
        while item_toks and item_toks[-1] == "please":
            item_toks.pop()
            k -= 1
        if not item_toks:
            raise GrammarError(f"\"{clause}\": no catalog item after the verb/quantity")
        vocab, unknown = self._vocab(), []
        for t in item_toks:
            if PRICE_TOKEN_RE.match(t):
                continue
            if t in NUMBER_WORDS and t not in ("a", "an"):
                unknown.append(t)   # a number word inside the item would be a second quantity
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
        purpose_text = None
        if j < len(low):  # purpose tail: an inert label, validated but never read for quantity/item
            words = [w for w in purpose if w != "please"]
            bad = [w for w in words if not re.fullmatch(r"[a-z']+", w) or w.replace("’", "'") in PURPOSE_FORBIDDEN]
            if not words or len(words) > 8 or bad:
                raise GrammarError(f"\"{clause}\": the \"{low[j]} …\" part must be a short plain purpose "
                                   f"(no conditions, negation or numbers)" +
                                   (f"; not allowed: {', '.join(bad)}" if bad else ""))
            purpose_text = " ".join(toks[j:])
        return {"verb": verb, "quantity": min(quantity, 999), "quantity_text": quantity_text,
                "item": " ".join(toks[i:k]), "purpose": purpose_text}

    def validate_clause(self, clause: str) -> str:
        """Backwards-compatible: the item phrase of a grammatical clause (GrammarError otherwise)."""
        return self.parse_clause(clause)["item"]

    @staticmethod
    def _clauses(request: str) -> list[str]:
        return [c for c in (c.strip(" .!?") for c in SPLIT_RE.split(request)) if c]

    def validate(self, request: str) -> None:
        """Raise GrammarError unless every clause of the request is in the grammar."""
        m = SIGNED_OR_FRACTION_RE.search(request)
        if m:
            raise GrammarError(f"negative or fractional quantity \"{m.group(0).strip()}\"")
        if re.search(r"[^\w\s,;.!?&$'’\-/()\n]", request):
            ch = re.search(r"[^\w\s,;.!?&$'’\-/()\n]", request).group(0)
            raise GrammarError(f"unsupported character \"{ch}\"")
        clauses = self._clauses(request)
        if not clauses:
            raise GrammarError("empty request")
        for c in clauses:
            self.parse_clause(c)

    def parse(self, request: str) -> list[dict[str, Any]]:
        """Split a request into structured lines {product_id, quantity, clause, verb, item,
        purpose}. The executed fields (product_id, quantity) come only from the parsed
        quantity slot and item phrase. A clause outside the grammar (or matching no
        product) gets product_id None and quantity 0, i.e. is never executed."""
        lines = []
        for clause in self._clauses(request):
            try:
                parsed = self.parse_clause(clause)
            except GrammarError as e:
                lines.append({"product_id": None, "quantity": 0, "clause": clause, "error": str(e)})
                continue
            pid = self._match(parsed["item"])
            lines.append({**parsed, "clause": clause, "product_id": pid,
                          "quantity": parsed["quantity"] if pid else 0})
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
