import pytest

from agcp import PolicyBundle, PolicyDenied, UnguardedExecutionError, require_grant
from agcp.errors import UnknownActionError
from conftest import AGENT, BUNDLE


def with_action(name):
    """Test bundle plus one extra declared action with an ALLOW rule."""
    return PolicyBundle.from_string(BUNDLE.replace("demo.nocomp,", f"demo.nocomp, {name},")
                                    + f"  - {{id: X-1, effect: ALLOW, when: {{action: {name}}}}}")


def test_allow_executes_and_audits(env):
    r = env["acts"]["demo.write"](actor=AGENT, key="k", v=1, value=10)
    assert r.status == "EXECUTED" and env["store"].data["k"] == 1
    ev = [x["event"] for x in env["cp"].audit.records()]
    assert ev.index("policy.decision") < ev.index("action.executed")
    ex = env["cp"].audit.records()[-1]
    assert ex["data"]["before"] == {"k": 0} and ex["data"]["after"] == {"k": 1}
    assert ex["data"]["policy_hash"] == env["cp"].policy.bundle.hash


def test_deny_does_not_execute(env):
    with pytest.raises(PolicyDenied):
        env["acts"]["demo.write"](actor=AGENT, key="k", v=1, value=10, frozen=True)
    assert env["store"].writes == 0
    assert env["cp"].metrics.count("agcp_denials_total") == 1


def test_unregistered_or_undeclared_action_denied(env):
    with pytest.raises(PolicyDenied):
        env["cp"].execute("demo.launch_missiles", actor=AGENT)
    env["cp"].policy.load(with_action("demo.ghost"))
    with pytest.raises(UnknownActionError):  # allowed by policy but has no registered implementation
        env["cp"].execute("demo.ghost", actor=AGENT)


def test_guard_bypass_impossible(env):
    act = env["acts"]["demo.write"]
    assert not hasattr(act, "__wrapped__") and not hasattr(act, "fn")
    with pytest.raises(AttributeError):
        act.__dict__
    with pytest.raises(UnguardedExecutionError):   # adapter refuses writes without a grant
        env["store"].set("k", 99)
    with pytest.raises(UnguardedExecutionError):
        require_grant()
    assert env["store"].data["k"] == 0


def test_grant_is_action_scoped(env):
    from agcp.context import ExecutionGrant, _grant
    with _grant(ExecutionGrant("demo.write", "r", "c", "d")):
        with pytest.raises(UnguardedExecutionError):
            require_grant("erp.something_else")


def test_dry_run_never_writes(env):
    r = env["acts"]["demo.write"](actor=AGENT, key="k", v=5, value=10, dry_run=True)
    assert r.status == "DRY_RUN" and env["store"].writes == 0
    assert env["cp"].audit.records()[-1]["event"] == "action.dry_run"


def test_control_plane_dry_run_mode(env):
    env["cp"].dry_run = True
    assert env["acts"]["demo.write"](actor=AGENT, key="k", v=5, value=10).status == "DRY_RUN"
    assert env["store"].writes == 0


def test_idempotency_key_prevents_double_write(env):
    a = env["acts"]["demo.write"]
    r1 = a(actor=AGENT, idempotency_key="op-1", key="k", v=1, value=10)
    r2 = a(actor=AGENT, idempotency_key="op-1", key="k", v=1, value=10)
    assert r1.status == "EXECUTED" and r2.status == "REPLAYED" and r2.request_id == r1.request_id
    assert env["store"].writes == 1
    assert any(r["event"] == "action.idempotent_replay" for r in env["cp"].audit.records())


def test_idempotency_for_pending_approval(env):
    a = env["acts"]["demo.write"]
    r1 = a(actor=AGENT, idempotency_key="op-2", key="k", v=1, value=500)
    r2 = a(actor=AGENT, idempotency_key="op-2", key="k", v=1, value=500)
    assert r1.status == "PENDING_APPROVAL" and r2.approval_id == r1.approval_id
    assert len(env["cp"].approvals.pending()) == 1


def test_failure_is_audited_and_reraised(env):
    @env["cp"].action("demo.boom")
    def boom():
        raise ValueError("downstream 500")
    env["cp"].policy.load(with_action("demo.boom"))
    with pytest.raises(ValueError):
        env["cp"].execute("demo.boom", actor=AGENT)
    assert env["cp"].audit.records()[-1]["event"] == "action.failed"


def test_policy_change_is_audited(env):
    from conftest import OPS
    env["cp"].load_policy(PolicyBundle.from_string(BUNDLE.replace("1.0.0", "2.0.0")), OPS)
    r = env["cp"].audit.records()[-1]
    assert r["event"] == "policy.changed" and r["data"]["version"] == "2.0.0"
    assert r["data"]["previous"]["version"] == "1.0.0"


def test_stale_approval_reevaluated_after_policy_change(env):
    from conftest import OPS
    from agcp import Principal
    r = env["acts"]["demo.write"](actor=AGENT, key="k", v=1, value=500)
    stricter = BUNDLE.replace("1.0.0", "1.1.0").replace(
        "- {id: T-2, effect: REQUIRE_APPROVAL, when: {action: demo.write, value: {gt: 100}}",
        "- {id: T-2, effect: DENY, when: {action: demo.write, value: {gt: 100}}")
    env["cp"].load_policy(PolicyBundle.from_string(stricter), OPS)
    with pytest.raises(PolicyDenied):
        env["cp"].approve(r.approval_id, Principal("u.m", "manager"))
    assert env["store"].writes == 0
