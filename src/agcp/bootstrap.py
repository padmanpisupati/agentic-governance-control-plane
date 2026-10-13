"""One-call wiring from a policy bundle file (router config lives in the bundle's approval_routing)."""
from __future__ import annotations

from pathlib import Path

from .approvals import ApprovalRouter, ApprovalService, InMemoryApprovalStore, SqliteApprovalStore
from .audit import AuditLog, InMemorySink, JsonlSink, SqliteSink
from .guard import ControlPlane
from .policy import PolicyBundle, PolicyEngine
from .tracing import Metrics, get_tracer


def build_control_plane(bundle_path: str | Path, *, clock=None, audit_path: str | Path | None = None,
                        approvals_db: str | Path | None = None, tracer=None, dry_run: bool = False,
                        checkpoint_every: int | None = 25) -> ControlPlane:
    bundle = PolicyBundle.from_yaml(bundle_path)
    if audit_path is None:
        sink = InMemorySink()
    elif str(audit_path).endswith((".db", ".sqlite")):
        sink = SqliteSink(audit_path)
    else:
        sink = JsonlSink(audit_path)
    audit = AuditLog(sink, clock=clock, checkpoint_every=checkpoint_every)
    metrics = Metrics()
    store = SqliteApprovalStore(approvals_db) if approvals_db else InMemoryApprovalStore()
    approvals = ApprovalService(ApprovalRouter(bundle.raw["approval_routing"]), store, audit, metrics, clock)
    engine = PolicyEngine(bundle, clock=clock)
    return ControlPlane(engine, audit, approvals, tracer or get_tracer(), metrics, clock, dry_run)
