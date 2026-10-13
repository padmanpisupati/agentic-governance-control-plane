"""Helpers shared by the examples: simulated human approvers and a control-KPI report."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from agcp import Principal

ROOT = Path(__file__).resolve().parents[1]


class SimulatedApprover:
    """Stands in for a human channel. Approves when `rule(request)` is True, rejects when False,
    and leaves the request pending (async) when it returns None."""

    def __init__(self, principal: Principal, rule):
        self.principal, self.rule = principal, rule

    def review(self, request):
        verdict = self.rule(request)
        if verdict is None:
            return None
        return (verdict, "simulated reviewer: " + ("approved" if verdict else "rejected"))


def control_kpis(cp) -> dict:
    recs = cp.audit.records()
    ev = Counter(r["event"] for r in recs)
    outcomes = Counter(r["data"]["outcome"] for r in recs if r["event"] == "policy.decision")
    executed = [r for r in recs if r["event"] == "action.executed"]
    unattended = sum(1 for r in executed if not r["data"].get("approval_id"))
    v = cp.audit.verify()
    m = cp.metrics.snapshot()
    return {
        "policy_bundle": f"{cp.policy.bundle.name}@{cp.policy.bundle.version} ({cp.policy.bundle.hash[:12]})",
        "decisions_by_outcome": dict(outcomes),
        "actions_executed": len(executed),
        "executed_auto_by_policy": unattended,
        "executed_with_approval_record": len(executed) - unattended,
        "executed_without_policy_decision": sum(1 for r in executed if not r["data"].get("decision_id")),
        "approvals_requested": ev["approval.requested"],
        "approvals_expired_or_escalated": ev["approval.expired"],
        "sod_violations_blocked": ev["approval.sod_violation"],
        "idempotent_replays": ev["action.idempotent_replay"],
        "dry_runs": ev["action.dry_run"],
        "action_failures": ev["action.failed"],
        "compensations": ev["action.compensated"],
        "approval_latency_p50_s": m["histograms"].get("agcp_approval_latency_seconds", {}).get("p50"),
        "audit_records": v.records,
        "audit_chain_valid": v.ok,
        "checkpoints_verified": v.checkpoints_verified,
    }


def write_outputs(name: str, cp, kpis: dict, anchor: dict) -> Path:
    out = ROOT / "out" / name
    out.mkdir(parents=True, exist_ok=True)
    with (out / "audit.jsonl").open("w") as f:
        for r in cp.audit.records():
            f.write(json.dumps(r, default=str) + "\n")
    (out / "kpis.json").write_text(json.dumps(kpis, indent=2))
    (out / "anchor.json").write_text(json.dumps(anchor, indent=2))
    return out


def print_kpis(kpis: dict) -> None:
    w = max(map(len, kpis))
    for k, v in kpis.items():
        print(f"  {k:<{w}}  {v}")
