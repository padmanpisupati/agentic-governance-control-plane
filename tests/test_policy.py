import pytest

from agcp import Outcome, PolicyBundle, PolicyEngine
from agcp.errors import PolicyError
from conftest import BUNDLE, ROOT

CTX = {"actor": {"id": "a", "role": "agent", "kind": "agent"}, "resource": {}}


def ev(bundle, clock=None, **kw):
    return PolicyEngine(bundle, clock).evaluate({**CTX, **kw})


def test_unknown_action_default_denied():
    d = ev(PolicyBundle.from_string(BUNDLE), action="demo.delete_everything", value=1)
    assert d.outcome is Outcome.DENY and "not declared" in d.reason and d.rule_ids == []


def test_declared_action_without_matching_rule_is_denied():
    b = PolicyBundle.from_string(BUNDLE + "  - {id: T-9, effect: ALLOW, when: {action: demo.write, value: {lt: -5}}}")
    d = ev(b, action="demo.write", value=None)  # value missing => numeric rules fail closed
    assert d.outcome is Outcome.DENY and "no rule matched" in d.reason


def test_outcomes_and_matched_rules():
    b = PolicyBundle.from_string(BUNDLE)
    assert ev(b, action="demo.write", value=50).rule_ids == ["T-1"]
    d = ev(b, action="demo.write", value=500)
    assert d.outcome is Outcome.REQUIRE_APPROVAL and d.approval["role"] == "manager"


def test_deny_beats_allow():
    b = PolicyBundle.from_string(BUNDLE)
    d = ev(b, action="demo.write", value=5, resource={"frozen": True})
    assert d.outcome is Outcome.DENY and d.rule_ids == ["T-3"]


def test_every_decision_records_policy_version_and_hash():
    b1 = PolicyBundle.from_string(BUNDLE)
    b2 = PolicyBundle.from_string(BUNDLE.replace("version: 1.0.0", "version: 1.0.1").replace("lte: 100", "lte: 10")
                                .replace("gt: 100", "gt: 10"))
    d1, d2 = ev(b1, action="demo.write", value=50), ev(b2, action="demo.write", value=50)
    assert (d1.policy_version, d2.policy_version) == ("1.0.0", "1.0.1")
    assert d1.policy_hash != d2.policy_hash and len(d1.policy_hash) == 64
    assert d1.outcome is Outcome.ALLOW and d2.outcome is Outcome.REQUIRE_APPROVAL


def test_hash_is_stable_and_content_sensitive():
    assert PolicyBundle.from_string(BUNDLE).hash == PolicyBundle.from_string(BUNDLE).hash
    assert PolicyBundle.from_string(BUNDLE).hash != PolicyBundle.from_string(BUNDLE.replace("T-6", "T-7")).hash


def test_time_window(clock):
    b = PolicyBundle.from_yaml(ROOT / "policies" / "order_exception.yaml")
    ctx = dict(action="erp.reserve_stock", value=1, confidence=0.9, resource={"status": "PROCESSING"})
    assert ev(b, clock, **ctx).rule_ids == ["OE-ST-001"]          # Monday 10:00 UTC
    clock.advance(hours=12)                                          # Monday 22:00 UTC
    d = ev(b, clock, **ctx)
    assert d.outcome is Outcome.REQUIRE_APPROVAL and d.rule_ids == ["OE-ST-002"]


def test_confidence_and_actor_conditions():
    b = PolicyBundle.from_yaml(ROOT / "policies" / "order_exception.yaml")
    base = dict(action="erp.release_credit_hold", value=500, resource={"status": "ON_HOLD"})
    assert ev(b, confidence=0.9, **base).outcome is Outcome.ALLOW
    assert ev(b, confidence=0.4, **base).rule_ids == ["OE-GEN-001"]
    assert ev(b, confidence=None, **base).rule_ids == ["OE-GEN-003"]  # agent omitted confidence


@pytest.mark.parametrize("bad", [
    "bundle: x\nversion: 1\nactions: []\n",
    "bundle: x\nversion: 1\nactions: [a]\nrules: [{id: R, effect: MAYBE, when: {}}]",
    "bundle: x\nversion: 1\nactions: [a]\nrules: [{id: R, effect: ALLOW, when: {value: {approx: 1}}}]",
    "bundle: x\nversion: 1\nactions: [a]\nrules: [{id: R, effect: ALLOW}, {id: R, effect: DENY}]",
])
def test_malformed_bundles_rejected(bad):
    with pytest.raises(PolicyError):
        PolicyBundle.from_string(bad)


def test_shipped_bundles_load():
    for f in ("order_exception", "vendor_payment"):
        b = PolicyBundle.from_yaml(ROOT / "policies" / f"{f}.yaml")
        assert b.rules and "approval_routing" in b.raw
