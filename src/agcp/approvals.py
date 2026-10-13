"""Approval thresholds and human-in-the-loop: routing, durable pending approvals, SoD, expiry, escalation."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .clock import SystemClock
from .context import new_id
from .errors import ApprovalError, SegregationOfDutiesError


@dataclass(frozen=True)
class Principal:
    """Anyone who requests or approves: an agent, a service, or a human."""
    id: str
    role: str
    kind: str = "human"  # human | agent | service


class Approver(Protocol):
    """Adapter for a human channel (inbox, chat, ticket) or a simulated reviewer.

    review() returns (approve, reason), or None to leave the request pending (async).
    """
    principal: Principal

    def review(self, request: "ApprovalRequest") -> tuple[bool, str] | None: ...


@dataclass
class ApprovalRequest:
    id: str
    action: str
    correlation_id: str
    requester_id: str
    required_role: str
    min_approvers: int
    value: float | None
    created_at: str
    expires_at: str
    payload: dict = field(default_factory=dict)        # params, idempotency key, decision snapshot
    status: str = "PENDING"                             # PENDING|APPROVED|REJECTED|EXPIRED|ESCALATED|EXECUTED
    approvals: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    escalated_from: str | None = None
    escalated_to: str | None = None


class InMemoryApprovalStore:
    def __init__(self):
        self._d: dict[str, dict] = {}

    def put(self, r: ApprovalRequest) -> None:
        self._d[r.id] = json.loads(json.dumps(asdict(r), default=str))

    def get(self, rid: str) -> ApprovalRequest | None:
        d = self._d.get(rid)
        return ApprovalRequest(**json.loads(json.dumps(d))) if d else None

    def all(self) -> list[ApprovalRequest]:
        return [self.get(k) for k in self._d]


class SqliteApprovalStore:
    """Durable store: pending approvals survive process restarts (async human latency)."""

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, status TEXT, body TEXT)")

    def put(self, r: ApprovalRequest) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO approvals VALUES (?, ?, ?)",
                              (r.id, r.status, json.dumps(asdict(r), default=str)))

    def get(self, rid: str) -> ApprovalRequest | None:
        row = self.conn.execute("SELECT body FROM approvals WHERE id=?", (rid,)).fetchone()
        return ApprovalRequest(**json.loads(row[0])) if row else None

    def all(self) -> list[ApprovalRequest]:
        return [ApprovalRequest(**json.loads(b)) for (b,) in self.conn.execute("SELECT body FROM approvals")]


class ApprovalRouter:
    """Maps (value, policy requirement) to a role, approver count and TTL.

    config = {
      "roles": ["team_lead", "manager", "director", "cfo"],       # ascending authority
      "tiers": [{"max_value": 1000, "role": "team_lead"},
                {"max_value": 25000, "role": "manager"},
                {"max_value": None, "role": "director", "min_approvers": 2}],
      "escalation": {"team_lead": "manager", "manager": "director", "director": "cfo"},
      "default_ttl_minutes": 240,
    }
    The stricter of the value tier and the policy rule's approval block wins.
    """

    def __init__(self, config: dict):
        self.roles: list[str] = list(config["roles"])
        self.tiers: list[dict] = list(config.get("tiers", []))
        self.escalation: dict[str, str] = dict(config.get("escalation", {}))
        self.ttl = int(config.get("default_ttl_minutes", 240))

    def rank(self, role: str) -> int:
        return self.roles.index(role) if role in self.roles else -1

    def route(self, value: float | None, requirement: dict | None = None) -> dict:
        req = requirement or {}
        tier = next((t for t in self.tiers if value is not None and t.get("max_value") is not None
                     and value <= t["max_value"]), None)
        if tier is None and value is None:   # no monetary value: the policy rule decides the role
            tier = {"role": self.roles[0]}
        if tier is None:
            tier = next((t for t in self.tiers if t.get("max_value") is None), {"role": self.roles[0]})
        role = max([tier["role"], req.get("role", tier["role"])], key=self.rank)
        return {"role": role,
                "min_approvers": max(int(tier.get("min_approvers", 1)), int(req.get("min_approvers", 1))),
                "ttl_minutes": int(req.get("ttl_minutes", tier.get("ttl_minutes", self.ttl)))}


class ApprovalService:
    def __init__(self, router: ApprovalRouter, store=None, audit=None, metrics=None, clock=None):
        self.router, self.store = router, store or InMemoryApprovalStore()
        self.audit, self.metrics, self.clock = audit, metrics, clock or SystemClock()

    def _log(self, event, r: ApprovalRequest, actor=None, **data):
        if self.audit:
            self.audit.append(event, correlation_id=r.correlation_id, actor=actor, action=r.action,
                              data={"approval_id": r.id, "status": r.status, **data})

    def request(self, *, action, correlation_id, requester: Principal, value, requirement=None,
                payload=None, role_override=None, escalated_from=None) -> ApprovalRequest:
        route = self.router.route(value, requirement)
        if role_override:
            route["role"] = role_override
        now = self.clock.now()
        r = ApprovalRequest(id=new_id("apr-"), action=action, correlation_id=correlation_id,
                            requester_id=requester.id, required_role=route["role"],
                            min_approvers=route["min_approvers"], value=value, created_at=now.isoformat(),
                            expires_at=(now + timedelta(minutes=route["ttl_minutes"])).isoformat(),
                            payload=payload or {}, escalated_from=escalated_from)
        self.store.put(r)
        self._log("approval.requested", r, requester.id, required_role=r.required_role,
                  min_approvers=r.min_approvers, expires_at=r.expires_at, value=value)
        if self.metrics:
            self.metrics.inc("agcp_approvals_requested_total", role=r.required_role)
        return r

    def get(self, rid: str) -> ApprovalRequest:
        r = self.store.get(rid)
        if r is None:
            raise ApprovalError(f"unknown approval {rid}")
        return r

    def _expired(self, r: ApprovalRequest) -> bool:
        return self.clock.now() >= datetime.fromisoformat(r.expires_at)

    def decide(self, rid: str, approver: Principal, approve: bool, reason: str = "") -> ApprovalRequest:
        r = self.get(rid)
        if r.status != "PENDING":
            raise ApprovalError(f"approval {rid} is {r.status}, not PENDING")
        if self._expired(r):
            self._expire(r)
            raise ApprovalError(f"approval {rid} expired at {r.expires_at}")
        if approver.id == r.requester_id or any(a["approver"] == approver.id for a in r.approvals):
            self._log("approval.sod_violation", r, approver.id, attempted_by=approver.id)
            if self.metrics:
                self.metrics.inc("agcp_sod_violations_total")
            raise SegregationOfDutiesError(
                f"{approver.id} cannot approve {rid}: requester or duplicate approver (four-eyes)")
        if approver.kind != "human":
            raise ApprovalError("only human principals may approve")
        if self.router.rank(approver.role) < self.router.rank(r.required_role):
            self._log("approval.insufficient_role", r, approver.id, role=approver.role)
            raise ApprovalError(f"role {approver.role} below required {r.required_role}")
        entry = {"approver": approver.id, "role": approver.role, "ts": self.clock.now().isoformat(), "reason": reason}
        if approve:
            r.approvals.append(entry)
            if len(r.approvals) >= r.min_approvers:
                r.status = "APPROVED"
        else:
            r.rejections.append(entry)
            r.status = "REJECTED"
        self.store.put(r)
        self._log("approval.decided", r, approver.id, approve=approve, reason=reason,
                  approvals=len(r.approvals), min_approvers=r.min_approvers)
        if self.metrics and r.status in ("APPROVED", "REJECTED"):
            latency = (self.clock.now() - datetime.fromisoformat(r.created_at)).total_seconds()
            self.metrics.observe("agcp_approval_latency_seconds", latency)
            self.metrics.inc("agcp_approvals_decided_total", status=r.status)
        return r

    def mark_executed(self, rid: str) -> None:
        r = self.get(rid)
        r.status = "EXECUTED"
        self.store.put(r)

    def _expire(self, r: ApprovalRequest) -> ApprovalRequest | None:
        nxt = self.router.escalation.get(r.required_role)
        r.status = "ESCALATED" if nxt else "EXPIRED"
        new = None
        if nxt:
            new = self.request(action=r.action, correlation_id=r.correlation_id,
                               requester=Principal(r.requester_id, "requester", "agent"), value=r.value,
                               requirement={"min_approvers": r.min_approvers}, payload=r.payload,
                               role_override=nxt, escalated_from=r.id)
            r.escalated_to = new.id
        self.store.put(r)
        self._log("approval.expired", r, "agcp", escalated_to=r.escalated_to)
        if self.metrics:
            self.metrics.inc("agcp_approvals_expired_total", escalated=bool(nxt))
        return new

    def sweep(self) -> list[ApprovalRequest]:
        """Expire overdue PENDING requests; returns newly created escalation requests."""
        out = []
        for r in self.store.all():
            if r.status == "PENDING" and self._expired(r):
                new = self._expire(r)
                if new:
                    out.append(new)
        return out

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self.store.all() if r.status == "PENDING"]
