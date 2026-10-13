"""Cross-workflow reuse: the same library governs two unrelated domains with no code changes."""
import inspect

import agcp
from examples.langgraph_integration import run as lg
from examples.order_exception import run as oe
from examples.vendor_payment import run as vp


def test_order_exception_example_runs(capsys):
    k = oe.main([])
    assert k["audit_chain_valid"] and k["executed_without_policy_decision"] == 0
    assert k["sod_violations_blocked"] >= 1 and k["compensations"] >= 1 and k["idempotent_replays"] == 1
    assert k["approvals_expired_or_escalated"] >= 1
    assert "direct ERP write blocked" in capsys.readouterr().out


def test_vendor_payment_example_runs():
    k = vp.main([])
    assert k["audit_chain_valid"] and k["executed_without_policy_decision"] == 0
    assert k["sod_violations_blocked"] == 2 and k["compensations"] >= 2


def test_same_library_two_domains():
    cp1, *_ = oe.build()
    cp2, *_ = vp.build()
    assert type(cp1) is type(cp2) is agcp.ControlPlane
    assert cp1.policy.bundle.name != cp2.policy.bundle.name
    assert set(cp1.registered_actions).isdisjoint(cp2.registered_actions)


def test_library_has_no_domain_coupling():
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path(inspect.getfile(agcp)).parent.glob("*.py"))
    import re
    hits = re.findall(r"\b(order|orders|invoice|vendor|credit|erp|payment)\b", src)  # case-sensitive: SQL "ORDER BY" is fine
    assert hits == [], hits


def test_langgraph_integration_both_engines():
    e1, o1 = lg.run(["SO-70000", "SO-70016"], use_langgraph=True)
    e2, o2 = lg.run(["SO-70000", "SO-70016"], use_langgraph=False)
    assert e2 == "sequential-fallback"
    assert [o["outcome"] for o in o1] == [o["outcome"] for o in o2]
