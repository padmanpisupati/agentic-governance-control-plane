"""Example B: vendor payment release, a different domain on the same control plane.

    PYTHONPATH=src:. python -m examples.vendor_payment.run

Reuse proof: no agcp code changes; only a new policy bundle, new adapters and new compensators.
A released payment is a saga: post ledger entry -> schedule bank transfer. If the transfer fails,
the ledger entry is reversed automatically.
"""
from __future__ import annotations

from agcp import (FixedClock, PolicyDenied, Principal, build_control_plane, correlation, get_tracer,
                  require_grant)
from agcp.errors import SegregationOfDutiesError
from examples.common import ROOT, SimulatedApprover, control_kpis, print_kpis, write_outputs
from examples.vendor_payment.ledger import MockPayables

AGENT = Principal("agent:ap-release", "agent", "agent")
SVC = Principal("svc:payments", "payment_service", "service")
TREASURY = Principal("u.treasury01", "ops_admin")


def build(clock=None, tracer=None):
    clock = clock or FixedClock()
    ap = MockPayables(ROOT / "data" / "sample" / "invoices_sample.csv")
    cp = build_control_plane(ROOT / "policies" / "vendor_payment.yaml", clock=clock,
                             tracer=tracer or get_tracer("noop"))
    snap = lambda p: ap.state(p["invoice_id"])  # noqa: E731

    @cp.action("ap.post_ledger_entry", value=lambda p: p["amount"], snapshot=snap,
               compensator=lambda p, b, a, o: ap.post(p["invoice_id"], -p["amount"], "reversal"))
    def post_ledger_entry(invoice_id, amount):
        ap.post(invoice_id, amount, "payment")

    @cp.action("ap.schedule_transfer", value=lambda p: p["amount"], resource=lambda p: ap.get(p["invoice_id"]),
               snapshot=snap, compensator=lambda p, b, a, o: ap.cancel_transfer(p["invoice_id"]))
    def schedule_transfer(invoice_id, amount):
        ap.schedule(invoice_id, amount)

    @cp.action("ap.release_payment", value=lambda p: ap.get(p["invoice_id"])["amount"],
               resource=lambda p: ap.get(p["invoice_id"]), snapshot=snap,
               compensator=lambda p, b, a, o: ap.set_status(p["invoice_id"], "OPEN"))
    def release_payment(invoice_id):
        inv = ap.get(invoice_id)
        with cp.saga("pay-invoice", SVC) as s:   # nested saga inherits the correlation id
            s.run(post_ledger_entry, invoice_id=invoice_id, amount=inv["amount"])
            s.run(schedule_transfer, invoice_id=invoice_id, amount=inv["amount"])
        ap.set_status(invoice_id, "PAID")
        return {"paid": inv["amount"]}

    @cp.action("ap.place_hold", resource=lambda p: ap.get(p["invoice_id"]), snapshot=snap,
               compensator=lambda p, b, a, o: ap.set_status(p["invoice_id"], "OPEN"))
    def place_hold(invoice_id, reason):
        ap.set_status(invoice_id, "ON_HOLD", reason)

    @cp.action("ap.update_bank_details", resource=lambda p: {"vendor_status": "ACTIVE"})
    def update_bank_details(vendor_id, iban_last4):
        require_grant("ap.update_bank_details")   # a real adapter would write to the vendor master
        return {"vendor_id": vendor_id, "iban_last4": iban_last4}

    return cp, ap, clock, dict(release=release_payment, hold=place_hold, bank=update_bank_details)


def main(argv=None):
    cp, ap, clock, A = build()
    print(f"== Example B: vendor payments | policy {cp.policy.bundle.name}@{cp.policy.bundle.version}")
    auto = [i for i, v in ap.invoices.items() if v["vendor_status"] == "ACTIVE"
            and v["match_variance_pct"] <= 2 and v["amount"] <= 5000]
    flaky = auto[1]
    ap.bank_down_for.add(flaky)   # inject a bank-gateway failure for one auto-releasable invoice
    tally = {"EXECUTED": 0, "PENDING_APPROVAL": 0, "DENY": 0, "COMPENSATED": 0}
    pending = []
    for inv in list(ap.invoices):
        o = ap.get(inv)
        conf = 0.95 if o["match_variance_pct"] <= 2 else 0.7
        with correlation(f"ap-{inv}"):
            try:
                r = A["release"](actor=AGENT, confidence=conf, idempotency_key=f"{inv}:release", invoice_id=inv)
                tally[r.status] += 1
                if r.status == "PENDING_APPROVAL":
                    pending.append(r)
            except PolicyDenied as e:
                tally["DENY"] += 1
                A["hold"](actor=AGENT, confidence=1.0, invoice_id=inv, reason=f"denied by {e.decision.rule_ids}")
            except ConnectionError:
                tally["COMPENSATED"] += 1
    s = ap.state(flaky)
    print(f"agent pass: {tally}")
    print(f"{flaky} bank failure -> ledger net {s['ledger_net']} transfer={s['transfer']} status={s['invoice']['status']}")

    clerk = SimulatedApprover(Principal("u.clerk01", "ap_clerk"), lambda r: True)
    mgr = SimulatedApprover(Principal("u.apmgr01", "manager"), lambda r: r.value < 40000)
    dirs = [SimulatedApprover(Principal(f"u.fin-dir0{i}", "director"), lambda r: True) for i in (1, 2)]
    clock.advance(hours=3)
    for p in pending:
        role = cp.approvals.get(p.approval_id).required_role
        for a in {"ap_clerk": [clerk], "manager": [mgr], "director": dirs}[role]:
            if cp.approvals.get(p.approval_id).status == "PENDING":
                cp.ask(p.approval_id, a)
    paid = sum(1 for i in ap.invoices.values() if i["status"] == "PAID")
    print(f"after approvals: paid={paid} rejected="
          f"{sum(1 for r in cp.approvals.store.all() if r.status == 'REJECTED')}")

    # Bank-detail change: agents are denied outright; humans need two independent approvers.
    try:
        A["bank"](actor=AGENT, vendor_id="V-101", iban_last4="9911")
    except PolicyDenied as e:
        print(f"agent bank-detail change: DENY {e.decision.rule_ids}")
    req = A["bank"](actor=Principal("u.apmgr01", "manager"), vendor_id="V-101", iban_last4="9911")
    try:
        cp.approve(req.approval_id, Principal("u.apmgr01", "manager"))
    except SegregationOfDutiesError:
        print("bank-detail change: requester self-approval blocked (SoD)")
    r1 = cp.approve(req.approval_id, Principal("u.apmgr02", "manager"))
    try:
        cp.approve(req.approval_id, Principal("u.apmgr02", "manager"))
    except SegregationOfDutiesError:
        print("bank-detail change: same approver twice blocked (four-eyes)")
    r2 = cp.approve(req.approval_id, Principal("u.fin-dir01", "director"))
    print(f"bank-detail change: after 1 approval={r1.status}, after 2={r2.status}")

    # Manual rollback: a duplicate-payment alert arrives for a paid invoice.
    victim = next(i for i, v in ap.invoices.items() if v["status"] == "PAID")
    undone = cp.rollback(f"ap-{victim}", TREASURY, reason="duplicate payment alert")
    s = ap.state(victim)
    print(f"rollback ap-{victim}: undid {undone}; ledger net {s['ledger_net']}, "
          f"transfer {s['transfer']['status']}, invoice {s['invoice']['status']}")
    try:
        cp.rollback(f"ap-{victim}", AGENT, reason="agent tries rollback")
    except PolicyDenied as e:
        print(f"agent-initiated rollback: DENY ({e.decision.reason})")

    anchor = cp.audit.checkpoint()
    k = control_kpis(cp)
    out = write_outputs("vendor_payment", cp, k, anchor)
    print("control KPIs:")
    print_kpis(k)
    print(f"outputs: {out.relative_to(ROOT)}/")
    assert k["audit_chain_valid"] and k["executed_without_policy_decision"] == 0
    return k


if __name__ == "__main__":
    main()
