import pytest

from agcp import PolicyDenied, Principal
from agcp.errors import CompensationError
from agcp.guard import SagaStepNotExecuted
from conftest import AGENT, OPS


def test_saga_compensates_completed_steps_in_reverse(env):
    cp, A, store = env["cp"], env["acts"], env["store"]
    with pytest.raises(RuntimeError):
        with cp.saga("two-step", AGENT) as s:
            s.run(A["demo.step1"], key="k", v=1)
            s.run(A["demo.step2"], key="k", v=2)
            raise RuntimeError("step 3 failed")
    assert store.data["k"] == 0 and s.status == "COMPENSATED"
    ev = [(r["event"], r["action"]) for r in cp.audit.by_correlation(s.correlation_id)]
    comp = [a for e, a in ev if e == "action.compensated"]
    assert comp == ["demo.step2", "demo.step1"]
    assert ("saga.compensated", None) in ev
    assert cp.metrics.count("agcp_rollbacks_total") == 2


def test_saga_step_denied_triggers_compensation(env):
    cp, A, store = env["cp"], env["acts"], env["store"]
    with pytest.raises(PolicyDenied):
        with cp.saga("deny-mid", AGENT) as s:
            s.run(A["demo.step1"], key="k", v=1)
            s.run(A["demo.step2"], key="k", v=2, frozen=True)
    assert store.data["k"] == 0


def test_saga_step_pending_approval_is_not_completion(env):
    cp, A, store = env["cp"], env["acts"], env["store"]
    with pytest.raises(SagaStepNotExecuted):
        with cp.saga("needs-approval", AGENT) as s:
            s.run(A["demo.step1"], key="k", v=1)
            s.run(A["demo.write"], key="k", v=2, value=500)
    assert store.data["k"] == 0


def test_saga_success(env):
    with env["cp"].saga("ok", AGENT) as s:
        s.run(env["acts"]["demo.step1"], key="k", v=1)
    assert s.status == "COMPLETED" and env["store"].data["k"] == 1


def test_manual_rollback_by_correlation_id(env):
    from agcp import correlation
    cp, A, store = env["cp"], env["acts"], env["store"]
    with correlation("case-42"):
        A["demo.step1"](actor=AGENT, key="k", v=5)
        A["demo.step2"](actor=AGENT, key="k", v=6)
    assert cp.rollback("case-42", OPS, "customer reversal") == ["demo.step2", "demo.step1"]
    assert store.data["k"] == 0
    rec = [r for r in cp.audit.by_correlation("case-42") if r["event"] == "action.compensated"]
    assert all(r["data"]["matches_before"] for r in rec)
    assert cp.rollback("case-42", OPS) == []          # idempotent: nothing left to undo
    assert cp.audit.verify().ok


def test_rollback_is_governed(env):
    from agcp import correlation
    with correlation("case-7"):
        env["acts"]["demo.step1"](actor=AGENT, key="k", v=5)
    with pytest.raises(PolicyDenied):
        env["cp"].rollback("case-7", Principal("u.random", "analyst"))
    assert env["store"].data["k"] == 5


def test_missing_compensator_is_surfaced(env):
    from agcp import correlation
    with correlation("case-9"):
        env["acts"]["demo.nocomp"](actor=AGENT, key="k", v=5)
    with pytest.raises(CompensationError):
        env["cp"].rollback("case-9", OPS)
    assert env["cp"].audit.records()[-1]["event"] == "compensation.unavailable"
