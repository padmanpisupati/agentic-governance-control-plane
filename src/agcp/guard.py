"""Enforcement point: no registered action executes without a policy decision.

    cp = ControlPlane(policy=PolicyEngine(bundle), audit=AuditLog(), approvals=service)

    @cp.action("sor.update_case", value=lambda p: p["amount"],
               resource=lambda p: sor.get(p["case_id"]),
               snapshot=lambda p: sor.get(p["case_id"]),
               compensator=lambda p, before, after, result: sor.restore(p["case_id"], before))
    def update_case(case_id, amount): ...

    result = update_case(actor=agent, idempotency_key="case-123-update", case_id="C1", amount=900)

Defence in depth against bypass:
  1. The decorator returns a GuardedAction, not the function; no __wrapped__ is exposed.
  2. The raw callable lives in a private registry keyed by action name.
  3. Adapters call agcp.require_grant() before writing; a grant only exists inside the guard.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from .approvals import ApprovalService, Principal
from .audit import AuditLog
from .clock import SystemClock
from .context import ExecutionGrant, _grant, correlation, current_correlation_id, new_id
from .errors import (ApprovalError, CompensationError, GovernanceError, PolicyDenied,
                     UnknownActionError)
from .policy import Decision, Outcome, PolicyBundle, PolicyEngine
from .tracing import Metrics, NoopTracer

ROLLBACK_ACTION = "agcp.rollback"


@dataclass
class ActionResult:
    status: str                    # EXECUTED | DRY_RUN | PENDING_APPROVAL | REJECTED | REPLAYED
    action: str
    correlation_id: str
    request_id: str
    decision: dict | None = None
    output: Any = None
    approval_id: str | None = None
    before: Any = None
    after: Any = None
    audit_seq: int | None = None


@dataclass
class _Spec:
    name: str
    fn: Callable
    value: Callable | None
    resource: Callable | None
    snapshot: Callable | None
    compensator: Callable | None


class GuardedAction:
    __slots__ = ("_cp", "name", "__doc__")

    def __init__(self, cp: "ControlPlane", name: str, doc: str | None):
        self._cp, self.name, self.__doc__ = cp, name, doc

    def __call__(self, *, actor: Principal, idempotency_key: str | None = None, dry_run: bool | None = None,
                 confidence: float | None = None, **params) -> ActionResult:
        return self._cp.execute(self.name, actor=actor, idempotency_key=idempotency_key,
                                dry_run=dry_run, confidence=confidence, **params)

    def __repr__(self):
        return f"<GuardedAction {self.name}>"


class ControlPlane:
    def __init__(self, policy: PolicyEngine, audit: AuditLog | None = None,
                 approvals: ApprovalService | None = None, tracer=None, metrics: Metrics | None = None,
                 clock=None, dry_run: bool = False):
        self.clock = clock or policy.clock or SystemClock()
        self.policy, self.audit = policy, audit or AuditLog(clock=self.clock)
        self.approvals = approvals
        self.tracer, self.metrics = tracer or NoopTracer(), metrics or Metrics()
        self.dry_run = dry_run
        self.__registry: dict[str, _Spec] = {}
        self._idem: dict[str, ActionResult] = {}
        if approvals is not None:
            approvals.audit = approvals.audit or self.audit
            approvals.metrics = approvals.metrics or self.metrics
        self._audit_policy_load("policy.loaded", policy.bundle)

    # ---------- registry ----------
    def action(self, name: str, *, value: Callable | None = None, resource: Callable | None = None,
               snapshot: Callable | None = None, compensator: Callable | None = None):
        def deco(fn: Callable) -> GuardedAction:
            if name in self.__registry:
                raise GovernanceError(f"action {name} already registered")
            self.__registry[name] = _Spec(name, fn, value, resource, snapshot, compensator)
            return GuardedAction(self, name, fn.__doc__)
        return deco

    @property
    def registered_actions(self) -> list[str]:
        return sorted(self.__registry)

    def load_policy(self, bundle: PolicyBundle, actor: Principal) -> None:
        """Change management: policy swaps are audited with old/new version and hash."""
        old = self.policy.bundle
        self.policy.load(bundle)
        self._audit_policy_load("policy.changed", bundle, actor.id,
                                previous={"version": old.version, "hash": old.hash})

    def _audit_policy_load(self, event, b: PolicyBundle, actor="agcp", **extra):
        self.audit.append(event, correlation_id=None, actor=actor,
                          data={"bundle": b.name, "version": b.version, "hash": b.hash, **extra})

    # ---------- enforcement ----------
    def _ctx(self, spec: _Spec | None, name, actor, params, confidence):
        value = spec.value(params) if spec and spec.value else params.get("value")
        resource = spec.resource(params) if spec and spec.resource else {}
        return {"action": name, "value": value, "confidence": confidence,
                "actor": {"id": actor.id, "role": actor.role, "kind": actor.kind},
                "resource": resource or {}, "params": params}

    def _decide(self, ctx: dict, cid: str) -> Decision:
        with self.tracer.span("agcp.policy.evaluate", {"agcp.action": ctx["action"]}) as s:
            d = self.policy.evaluate(ctx)
            s.set_attribute("agcp.outcome", d.outcome.value)
            s.set_attribute("agcp.policy_version", d.policy_version)
        self.audit.append("policy.decision", correlation_id=cid, actor=ctx["actor"]["id"], action=ctx["action"],
                          data={**d.to_dict(), "value": ctx["value"], "confidence": ctx["confidence"]})
        self.metrics.inc("agcp_decisions_total", outcome=d.outcome.value, action=ctx["action"])
        if d.outcome is Outcome.DENY:
            self.metrics.inc("agcp_denials_total", action=ctx["action"])
        return d

    def execute(self, name: str, *, actor: Principal, idempotency_key: str | None = None,
                dry_run: bool | None = None, confidence: float | None = None, **params) -> ActionResult:
        cid = current_correlation_id() or new_id("corr-")
        dry = self.dry_run if dry_run is None else dry_run
        with correlation(cid), self.tracer.span("agcp.action", {"agcp.action": name, "agcp.actor": actor.id,
                                                                "agcp.dry_run": dry}) as span:
            if idempotency_key and idempotency_key in self._idem:
                prior = self._idem[idempotency_key]
                self.audit.append("action.idempotent_replay", correlation_id=cid, actor=actor.id, action=name,
                                  data={"idempotency_key": idempotency_key, "original_request": prior.request_id})
                self.metrics.inc("agcp_idempotent_replays_total", action=name)
                span.set_attribute("agcp.replayed", True)
                return ActionResult(**{**prior.__dict__, "status": "REPLAYED"})
            rid = new_id("req-")
            spec = self.__registry.get(name)
            ctx = self._ctx(spec, name, actor, params, confidence)
            d = self._decide(ctx, cid)
            span.set_attribute("agcp.outcome", d.outcome.value)
            if spec is None:  # unknown to the registry: nothing to run; never ALLOW
                if d.outcome is not Outcome.DENY:
                    self.audit.append("action.unregistered", correlation_id=cid, actor=actor.id, action=name)
                    raise UnknownActionError(f"{name} is not registered")
                raise PolicyDenied(name, d)
            if d.outcome is Outcome.DENY:
                raise PolicyDenied(name, d)
            if dry:
                rec = self.audit.append("action.dry_run", correlation_id=cid, actor=actor.id, action=name,
                                        data={"request_id": rid, "would_be": d.outcome.value, "params": params})
                return ActionResult("DRY_RUN", name, cid, rid, d.to_dict(), audit_seq=rec["seq"])
            if d.outcome is Outcome.REQUIRE_APPROVAL:
                if self.approvals is None:
                    raise PolicyDenied(name, d)  # fail closed: no approval channel configured
                apr = self.approvals.request(
                    action=name, correlation_id=cid, requester=actor, value=ctx["value"],
                    requirement=d.approval,
                    payload={"params": params, "idempotency_key": idempotency_key, "request_id": rid,
                             "confidence": confidence, "decision": d.to_dict(),
                             "actor": {"id": actor.id, "role": actor.role, "kind": actor.kind}})
                res = ActionResult("PENDING_APPROVAL", name, cid, rid, d.to_dict(), approval_id=apr.id)
                if idempotency_key:
                    self._idem[idempotency_key] = res
                return res
            return self._run(spec, actor, params, cid, rid, d.to_dict(), idempotency_key)

    def _run(self, spec: _Spec, actor, params, cid, rid, decision, idem_key, approval_id=None) -> ActionResult:
        before = spec.snapshot(params) if spec.snapshot else None
        grant = ExecutionGrant(spec.name, rid, cid, decision["decision_id"])
        t0 = time.perf_counter()
        with self.tracer.span("agcp.action.execute", {"agcp.action": spec.name}), _grant(grant):
            try:
                out = spec.fn(**params)
            except Exception as e:
                self.audit.append("action.failed", correlation_id=cid, actor=actor.id, action=spec.name,
                                  data={"request_id": rid, "error": f"{type(e).__name__}: {e}"})
                self.metrics.inc("agcp_action_failures_total", action=spec.name)
                raise
        after = spec.snapshot(params) if spec.snapshot else None
        rec = self.audit.append("action.executed", correlation_id=cid, actor=actor.id, action=spec.name,
                                data={"request_id": rid, "decision_id": decision["decision_id"],
                                      "policy_version": decision["policy_version"],
                                      "policy_hash": decision["policy_hash"], "approval_id": approval_id,
                                      "params": params, "before": before, "after": after, "output": out,
                                      "idempotency_key": idem_key,
                                      "duration_ms": round((time.perf_counter() - t0) * 1000, 3)})
        self.metrics.inc("agcp_actions_executed_total", action=spec.name)
        res = ActionResult("EXECUTED", spec.name, cid, rid, decision, out, approval_id, before, after, rec["seq"])
        if idem_key:
            self._idem[idem_key] = res
        return res

    # ---------- human in the loop ----------
    def approve(self, approval_id: str, approver: Principal, approve: bool = True, reason: str = "",
                auto_execute: bool = True) -> ActionResult:
        r = self.approvals.decide(approval_id, approver, approve, reason)
        p = r.payload
        if r.status == "REJECTED":
            return ActionResult("REJECTED", r.action, r.correlation_id, p["request_id"], p["decision"],
                                approval_id=r.id)
        if r.status == "APPROVED" and auto_execute:
            return self.resume(approval_id)
        return ActionResult("PENDING_APPROVAL", r.action, r.correlation_id, p["request_id"], p["decision"],
                            approval_id=r.id)

    def ask(self, approval_id: str, approver) -> ActionResult | None:
        """Route a pending request to an Approver adapter; None means it stays pending."""
        verdict = approver.review(self.approvals.get(approval_id))
        if verdict is None:
            return None
        return self.approve(approval_id, approver.principal, verdict[0], verdict[1])

    def resume(self, approval_id: str) -> ActionResult:
        r = self.approvals.get(approval_id)
        if r.status != "APPROVED":
            raise ApprovalError(f"approval {approval_id} is {r.status}")
        p = r.payload
        spec = self.__registry[r.action]
        actor = Principal(**p["actor"])
        with correlation(r.correlation_id):
            decision = p["decision"]
            if decision["policy_hash"] != self.policy.bundle.hash:
                # Policy changed while waiting: re-evaluate; a new DENY wins over a stale approval.
                d = self._decide(self._ctx(spec, r.action, actor, p["params"], p["confidence"]), r.correlation_id)
                if d.outcome is Outcome.DENY:
                    raise PolicyDenied(r.action, d)
                decision = d.to_dict()
            res = self._run(spec, actor, p["params"], r.correlation_id, p["request_id"], decision,
                            p["idempotency_key"], approval_id)
        self.approvals.mark_executed(approval_id)
        return res

    # ---------- compensation / rollback ----------
    def _executed_open(self, cid: str) -> list[dict]:
        recs = self.audit.by_correlation(cid)
        done = {r["data"].get("exec_seq") for r in recs if r["event"] == "action.compensated"}
        return [r for r in recs if r["event"] == "action.executed" and r["seq"] not in done]

    def _compensate(self, rec: dict, actor: Principal, reason: str) -> None:
        spec = self.__registry.get(rec["action"])
        cid, d = rec["correlation_id"], rec["data"]
        if spec is None or spec.compensator is None:
            self.audit.append("compensation.unavailable", correlation_id=cid, actor=actor.id,
                              action=rec["action"], data={"exec_seq": rec["seq"]})
            raise CompensationError(f"no compensator for {rec['action']} (seq {rec['seq']})")
        grant = ExecutionGrant(spec.name, d["request_id"], cid, d["decision_id"], compensation=True)
        with self.tracer.span("agcp.compensate", {"agcp.action": spec.name}), _grant(grant):
            try:
                spec.compensator(d["params"], d["before"], d["after"], d["output"])
            except Exception as e:
                self.audit.append("compensation.failed", correlation_id=cid, actor=actor.id, action=spec.name,
                                  data={"exec_seq": rec["seq"], "error": f"{type(e).__name__}: {e}"})
                self.metrics.inc("agcp_compensation_failures_total", action=spec.name)
                raise CompensationError(f"compensator for {spec.name} failed: {e}") from e
        restored = spec.snapshot(d["params"]) if spec.snapshot else None
        self.audit.append("action.compensated", correlation_id=cid, actor=actor.id, action=spec.name,
                          data={"exec_seq": rec["seq"], "reason": reason, "restored": restored,
                                "matches_before": restored == d["before"] if spec.snapshot else None})
        self.metrics.inc("agcp_rollbacks_total", action=spec.name, reason=reason.split(":")[0])

    def rollback(self, correlation_id: str, actor: Principal, reason: str = "manual") -> list[str]:
        """Manual rollback of every executed, not-yet-compensated action for a correlation id,
        newest first. Rollback is itself a governed action (policy action 'agcp.rollback')."""
        with correlation(correlation_id):
            d = self._decide(self._ctx(None, ROLLBACK_ACTION, actor, {"correlation_id": correlation_id}, None),
                             correlation_id)
            if d.outcome is not Outcome.ALLOW:
                raise PolicyDenied(ROLLBACK_ACTION, d)
            undone = []
            for rec in reversed(self._executed_open(correlation_id)):
                self._compensate(rec, actor, f"manual: {reason}")
                undone.append(rec["action"])
            self.audit.append("rollback.completed", correlation_id=correlation_id, actor=actor.id,
                              data={"undone": undone, "reason": reason})
            return undone

    @contextmanager
    def saga(self, name: str, actor: Principal, correlation_id: str | None = None):
        """All steps share one correlation id. If any step raises, or does not EXECUTE
        (denied, pending approval, rejected), completed steps are compensated in reverse."""
        saga = Saga(self, name, actor)
        with correlation(correlation_id or current_correlation_id()) as cid:
            saga.correlation_id = cid
            self.audit.append("saga.started", correlation_id=cid, actor=actor.id, data={"saga": name})
            try:
                yield saga
            except Exception as e:
                self.audit.append("saga.compensating", correlation_id=cid, actor=actor.id,
                                  data={"saga": name, "cause": f"{type(e).__name__}: {e}"})
                for rec in reversed(self._executed_open(cid)):
                    self._compensate(rec, actor, f"saga: {name}")
                self.audit.append("saga.compensated", correlation_id=cid, actor=actor.id, data={"saga": name})
                saga.status = "COMPENSATED"
                raise
            self.audit.append("saga.completed", correlation_id=cid, actor=actor.id,
                              data={"saga": name, "steps": saga.steps})
            saga.status = "COMPLETED"


class SagaStepNotExecuted(GovernanceError):
    def __init__(self, result: ActionResult):
        self.result = result
        super().__init__(f"saga step {result.action} ended {result.status}")


@dataclass
class Saga:
    cp: ControlPlane
    name: str
    actor: Principal
    correlation_id: str = ""
    steps: list = field(default_factory=list)
    status: str = "RUNNING"

    def run(self, action: GuardedAction, **params) -> ActionResult:
        res = action(actor=self.actor, **params)
        if res.status not in ("EXECUTED", "REPLAYED"):
            raise SagaStepNotExecuted(res)
        self.steps.append(action.name)
        return res
