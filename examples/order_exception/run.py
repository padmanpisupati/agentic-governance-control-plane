"""Example A: an order-exception agent governed by agcp, acting on a mock ERP.

    PYTHONPATH=src:. python -m examples.order_exception.run
"""
from __future__ import annotations

import argparse

from agcp import (FixedClock, PolicyDenied, Principal, UnguardedExecutionError, build_control_plane,
                  correlation, get_tracer)
from agcp.errors import SegregationOfDutiesError
from agcp.guard import SagaStepNotExecuted
from examples.common import ROOT, SimulatedApprover, control_kpis, print_kpis, write_outputs
from examples.order_exception.erp import MockOrderERP

AGENT = Principal("agent:order-exception", "agent", "agent")
OPS = Principal("u.ops01", "ops_admin")


def build(clock=None, tracer=None):
    clock = clock or FixedClock()
    erp = MockOrderERP(ROOT / "data" / "sample" / "orders_sample.csv")
    cp = build_control_plane(ROOT / "policies" / "order_exception.yaml", clock=clock,
                             tracer=tracer or get_tracer("noop"))
    snap = lambda p: erp.get(p["order_id"])  # noqa: E731
    restore = lambda p, before, after, out: erp.restore(p["order_id"], before)  # noqa: E731

    @cp.action("erp.release_credit_hold", value=lambda p: erp.get(p["order_id"])["sales"],
               resource=snap, snapshot=snap, compensator=restore)
    def release_credit_hold(order_id):
        erp.release_hold(order_id)
        return {"released": order_id}

    @cp.action("erp.upgrade_shipping", value=lambda p: p["incremental_cost"], resource=snap,
               snapshot=snap, compensator=restore)
    def upgrade_shipping(order_id, mode, incremental_cost):
        erp.set_shipping(order_id, mode)
        return {"mode": mode}

    @cp.action("erp.reserve_stock", value=lambda p: p["qty"], resource=snap, snapshot=snap, compensator=restore)
    def reserve_stock(order_id, warehouse, qty):
        erp.reserve(order_id, warehouse, qty)
        return {"warehouse": warehouse}

    @cp.action("erp.update_promise_date", resource=snap, snapshot=snap, compensator=restore)
    def update_promise_date(order_id, offset_days):
        erp.set_promise(order_id, offset_days)
        return {"offset_days": offset_days}

    @cp.action("erp.cancel_order", value=lambda p: erp.get(p["order_id"])["sales"], resource=snap,
               snapshot=snap, compensator=restore)
    def cancel_order(order_id, reason):
        erp.cancel(order_id)
        return {"canceled": order_id}

    acts = dict(release=release_credit_hold, ship=upgrade_shipping, reserve=reserve_stock,
                promise=update_promise_date, cancel=cancel_order)
    return cp, erp, clock, acts


def triage(o: dict):
    """Deterministic stand-in for an LLM specialist: proposes (kind, params, confidence)."""
    conf = {"Corporate": 0.93, "Consumer": 0.88, "Home Office": 0.78}[o["segment"]]
    if o["status"] in ("ON_HOLD", "SUSPECTED_FRAUD"):
        return "release", {"order_id": o["order_id"]}, conf
    if o["status"] == "PENDING" and o["late_delivery_risk"]:
        return "ship", {"order_id": o["order_id"], "mode": "First Class", "incremental_cost": 35.0 * o["qty"]}, 0.9
    if o["status"] == "PROCESSING" and o["late_delivery_risk"]:
        return "saga", {"order_id": o["order_id"], "qty": o["qty"]}, 0.9
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--console-spans", action="store_true")
    args = ap.parse_args(argv)
    cp, erp, clock, A = build(tracer=get_tracer("console") if args.console_spans else None)
    print(f"== Example A: order exceptions | policy {cp.policy.bundle.name}@{cp.policy.bundle.version} "
          f"| tracer={cp.tracer.backend}")

    first = next(o for o in erp.orders.values() if o["status"] == "ON_HOLD")
    r = A["release"](actor=AGENT, confidence=0.95, dry_run=True, order_id=first["order_id"])
    print(f"dry-run {first['order_id']}: {r.status}, writes so far={len(erp.writes)}")

    results, sagas, corr_ids = [], [], {}
    for oid, o in list(erp.orders.items()):
        t = triage(o)
        if not t:
            continue
        kind, params, conf = t
        with correlation(f"oe-{oid}") as cid:
            corr_ids[oid] = cid
            try:
                if kind == "saga":
                    if len(sagas) == 1:
                        erp.fail_promise_for.add(oid)  # inject one ERP failure mid-saga
                    with cp.saga("reallocate-and-repromise", AGENT, cid) as s:
                        s.run(A["reserve"], confidence=conf, idempotency_key=f"{oid}:reserve",
                              order_id=oid, warehouse="WH-ALT-2", qty=params["qty"])
                        s.run(A["promise"], confidence=conf, idempotency_key=f"{oid}:promise",
                              order_id=oid, offset_days=2)
                    sagas.append((oid, "COMPLETED"))
                else:
                    res = A[kind](actor=AGENT, confidence=conf, idempotency_key=f"{oid}:{kind}", **params)
                    results.append(res)
            except PolicyDenied as e:
                results.append(e)
            except (RuntimeError, SagaStepNotExecuted) as e:
                sagas.append((oid, f"COMPENSATED ({e})"))

    denied = [e for e in results if isinstance(e, PolicyDenied)]
    pending = [r for r in results if not isinstance(r, PolicyDenied) and r.status == "PENDING_APPROVAL"]
    print(f"triaged: {len(results) + len(sagas)} | executed={sum(1 for r in results if getattr(r, 'status', '') == 'EXECUTED')}"
          f" pending={len(pending)} denied={len(denied)} sagas={sagas}")
    for e in denied[:3]:
        print(f"  DENY {e.decision.action} rules={e.decision.rule_ids}")

    # Idempotency: an agent retry after a timeout must not double-write.
    done = next(r for r in results if getattr(r, "status", "") == "EXECUTED"
                and r.action == "erp.release_credit_hold")
    oid, n = done.output["released"], len(erp.writes)
    with correlation(done.correlation_id):
        again = A["release"](actor=AGENT, confidence=0.95, idempotency_key=f"{oid}:release", order_id=oid)
    print(f"retry of release {oid}: {again.status}, ERP writes unchanged={len(erp.writes) == n}")

    # Bypass attempt: calling the adapter directly is refused.
    try:
        erp.release_hold(first["order_id"])
    except UnguardedExecutionError as e:
        print(f"direct ERP write blocked: {e}")

    # Human in the loop: managers approve some, one stays unanswered and escalates.
    silent = {"first": True}

    def mgr_rule(r):  # the first manager-tier request is never answered -> expires -> escalates
        if silent["first"]:
            silent["first"] = False
            return None
        return r.value is None or r.value < 9000

    mgr = SimulatedApprover(Principal("u.mgr01", "manager"), mgr_rule)
    dirs = [SimulatedApprover(Principal(f"u.dir0{i}", "director"), lambda r: True) for i in (1, 2)]
    clock.advance(minutes=45)
    for p in pending:
        ap_ = cp.approvals.get(p.approval_id)
        chain = [mgr] if ap_.required_role in ("team_lead", "manager") else dirs
        for a in chain:
            if cp.approvals.get(p.approval_id).status == "PENDING":
                cp.ask(p.approval_id, a)
    clock.advance(hours=4)
    escalated = cp.approvals.sweep()
    for e in escalated:
        for a in dirs:
            if cp.approvals.get(e.id).status == "PENDING":
                cp.ask(e.id, a)
    print(f"approvals: escalated={len(escalated)} still pending={len(cp.approvals.pending())}")

    # Segregation of duties: a human who requests a cancellation cannot approve it.
    victim = next(oid for oid, o in erp.orders.items() if o["status"] == "PENDING")
    with correlation(f"oe-{victim}-cancel"):
        c = A["cancel"](actor=Principal("u.mgr01", "manager"), order_id=victim, reason="customer request")
    try:
        cp.approve(c.approval_id, Principal("u.mgr01", "manager"))
    except SegregationOfDutiesError as e:
        print(f"SoD enforced: {e}")
    cp.approve(c.approval_id, Principal("u.mgr02", "manager"), reason="second pair of eyes")
    print(f"cancel {victim}: {erp.orders[victim]['status']} after independent approval")

    # Manual rollback by correlation id (e.g. customer reversed the request).
    undone = cp.rollback(f"oe-{victim}-cancel", OPS, reason="customer withdrew cancellation")
    print(f"rollback oe-{victim}-cancel: undid {undone}, status now {erp.orders[victim]['status']}")

    # Change management: a tightened policy (v1.1.0) is hot-swapped; the change is audited and
    # every later decision records the new version and hash.
    from agcp import PolicyBundle
    raw = (ROOT / "policies" / "order_exception.yaml").read_text()
    v11 = PolicyBundle.from_string(raw.replace("version: 1.0.0", "version: 1.1.0")
                                   .replace("value: {lte: 2000}, confidence", "value: {lte: 1000}, confidence")
                                   .replace("value: {gt: 2000}", "value: {gt: 1000}"))
    probe = {"action": "erp.release_credit_hold", "value": 1500.0, "confidence": 0.95,
             "actor": {"id": AGENT.id, "role": "agent", "kind": "agent"}, "resource": {"status": "ON_HOLD"}}
    before = cp.policy.evaluate(probe)
    cp.load_policy(v11, Principal("u.change-board", "director"))
    after = cp.policy.evaluate(probe)
    print(f"policy change 1.0.0 -> 1.1.0: release of 1,500 {before.outcome.value} {before.rule_ids} "
          f"-> {after.outcome.value} {after.rule_ids} (hash {after.policy_hash[:12]})")

    anchor = cp.audit.checkpoint()
    k = control_kpis(cp)
    out = write_outputs("order_exception", cp, k, anchor)
    print("control KPIs:")
    print_kpis(k)
    print(f"outputs: {out.relative_to(ROOT)}/ (audit.jsonl, kpis.json, anchor.json)")
    assert k["audit_chain_valid"] and k["executed_without_policy_decision"] == 0
    return k


if __name__ == "__main__":
    main()
