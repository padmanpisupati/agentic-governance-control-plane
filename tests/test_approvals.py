import pytest

from agcp import (ApprovalRouter, ApprovalService, AuditLog, FixedClock, Principal, SqliteApprovalStore)
from agcp.errors import ApprovalError, SegregationOfDutiesError
from conftest import AGENT, ROUTING

MGR, MGR2, DIR = Principal("u.m1", "manager"), Principal("u.m2", "manager"), Principal("u.d1", "director")
LEAD = Principal("u.l1", "team_lead")


def svc(clock=None, store=None):
    return ApprovalService(ApprovalRouter(ROUTING), store, AuditLog(), clock=clock or FixedClock())


def test_router_tiers_and_policy_override():
    r = ApprovalRouter(ROUTING)
    assert r.route(500)["role"] == "team_lead"
    assert r.route(5000)["role"] == "director"
    assert r.route(500, {"role": "manager"})["role"] == "manager"       # stricter wins
    assert r.route(5000, {"role": "manager"})["role"] == "director"
    assert r.route(None, {"role": "manager", "min_approvers": 2}) == {"role": "manager", "min_approvers": 2,
                                                                      "ttl_minutes": 60}


def test_requester_cannot_approve_own_action():
    s = svc()
    r = s.request(action="a", correlation_id="c", requester=MGR, value=10, requirement={"role": "manager"})
    with pytest.raises(SegregationOfDutiesError):
        s.decide(r.id, MGR, True)
    assert s.get(r.id).status == "PENDING"
    assert any(x["event"] == "approval.sod_violation" for x in s.audit.records())


def test_four_eyes_distinct_approvers():
    s = svc()
    r = s.request(action="a", correlation_id="c", requester=AGENT, value=10,
                  requirement={"role": "manager", "min_approvers": 2})
    assert s.decide(r.id, MGR, True).status == "PENDING"
    with pytest.raises(SegregationOfDutiesError):
        s.decide(r.id, MGR, True)
    assert s.decide(r.id, DIR, True).status == "APPROVED"


def test_role_and_kind_checks():
    s = svc()
    r = s.request(action="a", correlation_id="c", requester=AGENT, value=10, requirement={"role": "manager"})
    with pytest.raises(ApprovalError):
        s.decide(r.id, LEAD, True)                                       # below required role
    with pytest.raises(ApprovalError):
        s.decide(r.id, Principal("agent:y", "director", "agent"), True)  # agents never approve
    assert s.decide(r.id, DIR, True).status == "APPROVED"               # higher role may approve


def test_rejection_is_final():
    s = svc()
    r = s.request(action="a", correlation_id="c", requester=AGENT, value=10)
    assert s.decide(r.id, LEAD, False, "no").status == "REJECTED"
    with pytest.raises(ApprovalError):
        s.decide(r.id, MGR, True)


def test_expiry_then_escalation(clock):
    s = svc(clock)
    r = s.request(action="a", correlation_id="c", requester=AGENT, value=10)   # team_lead, 60 min
    clock.advance(minutes=61)
    with pytest.raises(ApprovalError):
        s.decide(r.id, LEAD, True)
    old = s.get(r.id)
    assert old.status == "ESCALATED"
    new = s.get(old.escalated_to)
    assert new.required_role == "manager" and new.escalated_from == r.id and new.status == "PENDING"


def test_sweep_expires_end_of_chain(clock):
    s = svc(clock)
    r = s.request(action="a", correlation_id="c", requester=AGENT, value=5000)  # director: no escalation
    clock.advance(hours=2)
    assert s.sweep() == [] and s.get(r.id).status == "EXPIRED"


def test_durable_sqlite_store_survives_restart(tmp_path, clock):
    db = tmp_path / "apr.db"
    s1 = svc(clock, SqliteApprovalStore(db))
    r = s1.request(action="a", correlation_id="c", requester=AGENT, value=10, payload={"params": {"x": 1}})
    s2 = svc(clock, SqliteApprovalStore(db))                                   # "new process"
    assert s2.get(r.id).payload == {"params": {"x": 1}}
    assert s2.decide(r.id, LEAD, True).status == "APPROVED"


def test_approval_flow_through_control_plane(env):
    cp = env["cp"]
    r = env["acts"]["demo.write"](actor=AGENT, key="k", v=7, value=500)
    assert r.status == "PENDING_APPROVAL" and env["store"].writes == 0
    env["clock"].advance(minutes=30)
    done = cp.approve(r.approval_id, MGR, reason="ok")
    assert done.status == "EXECUTED" and env["store"].data["k"] == 7
    assert cp.approvals.get(r.approval_id).status == "EXECUTED"
    assert cp.metrics.histograms["agcp_approval_latency_seconds"] == [1800.0]
    with pytest.raises(ApprovalError):
        cp.resume(r.approval_id)                                              # cannot execute twice


def test_async_approver_protocol(env):
    class Inbox:
        principal = MGR
        answer = None

        def review(self, req):
            return self.answer
    cp, inbox = env["cp"], Inbox()
    r = env["acts"]["demo.write"](actor=AGENT, key="k", v=3, value=500)
    assert cp.ask(r.approval_id, inbox) is None                              # no answer yet
    inbox.answer = (True, "looks fine")
    assert cp.ask(r.approval_id, inbox).status == "EXECUTED"
