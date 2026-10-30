"""Owner-editable spend policy: stored in the DB (shared by every process), strictly validated,
revisioned for optimistic concurrency, with a change history. Mixed into BuyerService."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from decimal import Decimal
from typing import Any

from .guardrails import Policy, money


class PolicyConflict(ValueError):
    """An owner edit was based on a policy revision that is no longer current."""


class PolicyAdminMixin:
    # ---- owner-editable policy (stored in the DB, shared by every process) ----------
    POLICY_EDITABLE = ("per_order_cap", "daily_cap", "hard_max_per_order", "allowed_categories",
                       "allowed_merchants", "blocked_merchants")
    POLICY_MAX_AMOUNT = Decimal("1000000")
    POLICY_HISTORY_MAX = 200

    def sync_policy(self) -> Policy:
        """Adopt the owner's policy override if another call or process changed it. One
        indexed read; called at the start of every policy decision. Read and adopt happen
        under the store lock, which every policy write holds too, so a slow reader can never
        install an older policy over a newer one."""
        with self.store.lock:
            row = self.store.conn.execute("SELECT value FROM meta WHERE key='policy_override'").fetchone()
            raw = row[0] if row else None
            if raw != self._policy_raw:
                self.policy = Policy.from_dict(json.loads(raw)) if raw else self.base_policy
                self._policy_raw = raw
            return self.policy

    def policy_snapshot(self) -> tuple[dict[str, Any], str]:
        """The policy in force and its revision, read together (one consistent snapshot)."""
        with self.store.lock:
            return self.sync_policy().to_dict(), self.policy_revision()

    def policy_revision(self) -> str:
        """Opaque id of the policy in force (changes with every owner edit), for
        optimistic concurrency: PUT /policy with a stale base_revision is refused."""
        with self.store.lock:
            self.sync_policy()
            raw = self._policy_raw or "base:" + json.dumps(self.base_policy.to_dict(), sort_keys=True)
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @classmethod
    def validate_policy_update(cls, current: Policy, body: Any) -> Policy:
        """Strict validation of an owner edit. Only the caps and the lists are editable;
        currency and timezone stay as configured. Raises ValueError with a readable reason."""
        if not isinstance(body, dict) or not body:
            raise ValueError("send a JSON object with the fields to change")
        extra = sorted(set(body) - set(cls.POLICY_EDITABLE))
        if extra:
            raise ValueError(f"not editable: {', '.join(extra)[:120]} (editable: {', '.join(cls.POLICY_EDITABLE)})")
        merged = current.to_dict()
        for key in ("per_order_cap", "daily_cap", "hard_max_per_order"):
            if key not in body:
                continue
            v = body[key]
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ValueError(f"{key} must be an amount like \"50\" or \"49.99\"")
            s = str(v).strip()
            if not re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,2})?", s):
                raise ValueError(f"{key} must be a positive amount with at most 2 decimals, got {s[:20]!r}")
            amt = Decimal(s)
            if amt <= 0 or amt > cls.POLICY_MAX_AMOUNT:
                raise ValueError(f"{key} must be between 0.01 and {cls.POLICY_MAX_AMOUNT}")
            merged[key] = str(money(amt))
        for key in ("allowed_categories", "allowed_merchants", "blocked_merchants"):
            if key not in body:
                continue
            v = body[key]
            if not isinstance(v, list) or len(v) > 50 or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{key} must be a list of at most 50 names")
            names = [x.strip().lower() for x in v]
            if any(not n or len(n) > 80 or any(ord(ch) < 32 for ch in n) for n in names):
                raise ValueError(f"{key}: every name must be 1-80 printable characters")
            if len(set(names)) != len(names):
                raise ValueError(f"{key} contains duplicates")
            merged[key] = names
        for key in ("allowed_categories", "allowed_merchants"):
            if not merged[key]:  # the engine reads an empty allowlist as "anything goes"
                raise ValueError(f"{key} cannot be empty (an empty allowlist would allow everything)")
        both = sorted(set(merged["allowed_merchants"]) & set(merged["blocked_merchants"]))
        if both:
            raise ValueError(f"merchant(s) both allowed and blocked: {', '.join(both)[:120]}")
        if money(merged["per_order_cap"]) > money(merged["daily_cap"]):
            raise ValueError("per_order_cap must not exceed daily_cap")
        return Policy.from_dict(merged)  # also checks hard_max_per_order >= per_order_cap

    def update_policy(self, body: Any, actor: str = "owner", base_revision: str | None = None) -> dict[str, Any]:
        """Apply an owner edit atomically (validated against the policy current inside the
        write transaction) and append it to the policy history. With `base_revision` (the
        dashboard always sends it) an edit made on a policy that changed meanwhile is refused
        (PolicyConflict) instead of silently undoing the other edit. Orders already created are
        not revoked; pending approvals are re-checked against the deny rules when approved."""
        with self.store.tx() as c:
            current = self.sync_policy()
            if base_revision is not None and base_revision != self.policy_revision():
                raise PolicyConflict("the policy was changed by someone else since you loaded it; "
                                     "reload and apply your edit again")
            new = self.validate_policy_update(current, body)
            before, after = current.to_dict(), new.to_dict()
            changed = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
            if not changed:
                return {"result": "UNCHANGED", "policy": after, "revision": self.policy_revision()}
            raw = json.dumps(after, sort_keys=True)
            c.execute("INSERT INTO meta(key, value) VALUES ('policy_override', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
            row = c.execute("SELECT value FROM meta WHERE key='policy_history'").fetchone()
            hist = json.loads(row[0]) if row else []
            entry = {"id": "POL-" + uuid.uuid4().hex[:8].upper(), "at": self._ts(), "actor": actor,
                     "changes": changed}
            hist = ([entry] + hist)[:self.POLICY_HISTORY_MAX]
            c.execute("INSERT INTO meta(key, value) VALUES ('policy_history', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(hist),))
            self.policy, self._policy_raw = new, raw
            revision = self.policy_revision()
        return {"result": "UPDATED", "policy": after, "change": entry, "revision": revision}

    def policy_history(self) -> list[dict[str, Any]]:
        with self.store.lock:
            row = self.store.conn.execute("SELECT value FROM meta WHERE key='policy_history'").fetchone()
        return json.loads(row[0]) if row else []
