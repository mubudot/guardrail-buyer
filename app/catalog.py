"""Sandbox product catalog (data/catalog.json): lookup and keyword search."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .guardrails import money


class Catalog:
    def __init__(self, products: list[dict[str, Any]]):
        self.products = {p["id"]: p for p in products}

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, product_id: str) -> dict[str, Any] | None:
        return self.products.get(product_id)

    def search(self, query: str = "", category: str | None = None,
               max_price: Any = None, limit: int = 8) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if w]
        out = []
        for p in self.products.values():
            hay = f"{p['name']} {p['merchant']} {p['category']}".lower()
            score = sum(1 for w in words if w in hay) if words else 1
            if words and score == 0:
                continue
            if category and p["category"].lower() != category.lower():
                continue
            if max_price is not None and money(p["price"]) > money(max_price):
                continue
            out.append((score, p))
        out.sort(key=lambda t: (-t[0], money(t[1]["price"])))
        return [p for _, p in out[:limit]]
