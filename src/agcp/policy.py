"""Policy-as-code engine: versioned, declarative YAML rules evaluated before every action.

Bundle shape (see policies/*.yaml):

    bundle: case-management
    version: 1.2.0
    actions: [sor.update_case, sor.close_case]      # anything else is default-denied
    rules:
      - id: CM-002
        description: High-value case updates need a manager
        effect: REQUIRE_APPROVAL                   # ALLOW | REQUIRE_APPROVAL | DENY
        when:
          action: sor.update_case                  # string, glob, or list
          value: {gt: 5000}
          confidence: {gte: 0.8}
          actor.role: {in: [agent]}
          resource.status: {eq: OPEN}
          time_window: {days: [mon, tue, wed, thu, fri], start: "06:00", end: "20:00", tz_offset_minutes: 0}
        approval: {role: manager, min_approvers: 1, ttl_minutes: 240}

Precedence: DENY > REQUIRE_APPROVAL > ALLOW. No matching rule => DENY (default-deny).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .context import new_id
from .errors import PolicyError


class Outcome(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


_PRECEDENCE = {Outcome.DENY: 3, Outcome.REQUIRE_APPROVAL: 2, Outcome.ALLOW: 1}
_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b, "gte": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b, "lte": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b,
    "exists": lambda a, b: (a is not None) == bool(b),
}
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Rule:
    id: str
    effect: Outcome
    when: dict
    description: str = ""
    approval: dict | None = None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    action: str
    matched_rules: tuple[Rule, ...]
    policy_bundle: str
    policy_version: str
    policy_hash: str
    reason: str
    decision_id: str = field(default_factory=lambda: new_id("dec-"))

    @property
    def rule_ids(self) -> list[str]:
        return [r.id for r in self.matched_rules]

    @property
    def approval(self) -> dict | None:
        """Strictest approval requirement among matched REQUIRE_APPROVAL rules."""
        reqs = [r.approval or {} for r in self.matched_rules if r.effect is Outcome.REQUIRE_APPROVAL]
        if not reqs:
            return None
        return max(reqs, key=lambda a: (a.get("min_approvers", 1), a.get("tier", 0)))

    def to_dict(self) -> dict:
        return {"decision_id": self.decision_id, "outcome": self.outcome.value, "action": self.action,
                "rules": self.rule_ids, "policy_bundle": self.policy_bundle,
                "policy_version": self.policy_version, "policy_hash": self.policy_hash,
                "reason": self.reason}


class PolicyBundle:
    def __init__(self, raw: dict):
        for key in ("bundle", "version", "actions", "rules"):
            if key not in raw:
                raise PolicyError(f"policy bundle missing '{key}'")
        self.raw = raw
        self.name: str = raw["bundle"]
        self.version: str = str(raw["version"])
        self.actions: frozenset[str] = frozenset(raw["actions"])
        seen: set[str] = set()
        rules = []
        for r in raw["rules"]:
            if r["id"] in seen:
                raise PolicyError(f"duplicate rule id {r['id']}")
            seen.add(r["id"])
            try:
                effect = Outcome(r["effect"])
            except ValueError as e:
                raise PolicyError(f"rule {r['id']}: bad effect {r.get('effect')}") from e
            for k, cond in (r.get("when") or {}).items():
                if k not in ("action", "time_window") and isinstance(cond, dict):
                    bad = set(cond) - set(_OPS)
                    if bad:
                        raise PolicyError(f"rule {r['id']}: unknown operator(s) {sorted(bad)}")
            rules.append(Rule(r["id"], effect, r.get("when") or {}, r.get("description", ""), r.get("approval")))
        self.rules: tuple[Rule, ...] = tuple(rules)
        # Content hash over canonical JSON: any rule change => new hash, recorded on every decision.
        self.hash = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
                                   .encode()).hexdigest()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PolicyBundle":
        return cls(yaml.safe_load(Path(path).read_text()))

    @classmethod
    def from_string(cls, text: str) -> "PolicyBundle":
        return cls(yaml.safe_load(text))


def _get(ctx: dict, dotted: str) -> Any:
    cur: Any = ctx
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _match_action(spec, action: str) -> bool:
    specs = spec if isinstance(spec, list) else [spec]
    return any(fnmatch.fnmatchcase(action, s) for s in specs)


def _match_window(spec: dict, now: datetime) -> bool:
    local = now.astimezone(timezone(timedelta(minutes=spec.get("tz_offset_minutes", 0))))
    if "days" in spec and _DAYS[local.weekday()] not in [d.lower()[:3] for d in spec["days"]]:
        return False
    hhmm = local.strftime("%H:%M")
    start, end = spec.get("start", "00:00"), spec.get("end", "24:00")
    inside = start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)
    return inside != bool(spec.get("outside", False))


def _match_cond(value: Any, cond: Any) -> bool:
    if not isinstance(cond, dict):
        return value == cond
    try:
        return all(_OPS[op](value, arg) for op, arg in cond.items())
    except TypeError:  # e.g. comparing None/str with a number: fail closed (rule does not match)
        return False


class PolicyEngine:
    """Evaluates an action context against one active bundle. Stateless and side-effect free."""

    def __init__(self, bundle: PolicyBundle, clock=None):
        from .clock import SystemClock
        self.bundle, self.clock = bundle, clock or SystemClock()

    def load(self, bundle: PolicyBundle) -> None:
        """Hot-swap the active bundle (a change-management event; the guard audits it)."""
        self.bundle = bundle

    def evaluate(self, ctx: dict) -> Decision:
        """ctx keys: action, value, confidence, actor{id,role}, resource{...}, params{...}."""
        b, action = self.bundle, ctx.get("action", "")

        def decide(outcome, rules, reason):
            return Decision(outcome, action, tuple(rules), b.name, b.version, b.hash, reason)

        if action not in b.actions:
            return decide(Outcome.DENY, [], "default-deny: action not declared in policy bundle")
        now = self.clock.now()
        matched = []
        for rule in b.rules:
            ok = True
            for key, cond in rule.when.items():
                if key == "action":
                    ok = _match_action(cond, action)
                elif key == "time_window":
                    ok = _match_window(cond, now)
                else:
                    ok = _match_cond(_get(ctx, key), cond)
                if not ok:
                    break
            if ok:
                matched.append(rule)
        if not matched:
            return decide(Outcome.DENY, [], "default-deny: no rule matched")
        top = max(_PRECEDENCE[r.effect] for r in matched)
        outcome = next(o for o, p in _PRECEDENCE.items() if p == top)
        winners = [r for r in matched if r.effect is outcome]
        return decide(outcome, winners, "; ".join(r.description or r.id for r in winners))
