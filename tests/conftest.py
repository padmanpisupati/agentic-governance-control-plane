import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from agcp import (ApprovalRouter, ApprovalService, AuditLog, ControlPlane, FixedClock,  # noqa: E402
                  Metrics, PolicyBundle, PolicyEngine, Principal, RecordingTracer, require_grant)

BUNDLE = """
bundle: test
version: 1.0.0
actions: [demo.write, demo.big, demo.step1, demo.step2, demo.nocomp, agcp.rollback]
rules:
  - {id: T-1, effect: ALLOW, when: {action: demo.write, value: {lte: 100}}}
  - {id: T-2, effect: REQUIRE_APPROVAL, when: {action: demo.write, value: {gt: 100}}, approval: {role: manager, ttl_minutes: 60}}
  - {id: T-3, effect: DENY, when: {action: "demo.*", resource.frozen: true}}
  - {id: T-4, effect: ALLOW, when: {action: [demo.step1, demo.step2, demo.nocomp]}}
  - {id: T-5, effect: REQUIRE_APPROVAL, when: {action: demo.big}, approval: {role: manager, min_approvers: 2}}
  - {id: T-6, effect: ALLOW, when: {action: agcp.rollback, actor.role: ops_admin}}
"""
ROUTING = {"roles": ["team_lead", "manager", "director"],
           "tiers": [{"max_value": 1000, "role": "team_lead"}, {"max_value": None, "role": "director"}],
           "escalation": {"team_lead": "manager", "manager": "director"}, "default_ttl_minutes": 60}

AGENT = Principal("agent:x", "agent", "agent")
OPS = Principal("u.ops", "ops_admin")


class Store:
    """A tiny system of record whose writes require an agcp grant."""

    def __init__(self):
        self.data = {"k": 0}
        self.writes = 0

    def set(self, key, v):
        require_grant()
        self.data[key] = v
        self.writes += 1


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def env(clock):
    store = Store()
    bundle = PolicyBundle.from_string(BUNDLE)
    audit = AuditLog(clock=clock)
    tracer = RecordingTracer()
    cp = ControlPlane(PolicyEngine(bundle, clock), audit,
                      ApprovalService(ApprovalRouter(ROUTING), clock=clock), tracer, Metrics(), clock)
    snap = lambda p: dict(store.data)  # noqa: E731
    restore = lambda p, before, after, out: store.set(p["key"], before[p["key"]])  # noqa: E731
    acts = {}
    for name in ("demo.write", "demo.big", "demo.step1", "demo.step2"):
        def fn(key, v, value=0, frozen=False):
            store.set(key, v)
            return {"set": v}
        acts[name] = cp.action(name, value=lambda p: p.get("value", 0),
                               resource=lambda p: {"frozen": p.get("frozen", False)},
                               snapshot=snap, compensator=restore)(fn)

    @cp.action("demo.nocomp", snapshot=snap)
    def nocomp(key, v):
        store.set(key, v)
    acts["demo.nocomp"] = nocomp
    return {"cp": cp, "store": store, "acts": acts, "clock": clock, "tracer": tracer}
