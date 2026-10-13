import builtins
import io

import pytest

from agcp import ConsoleTracer, Metrics, NoopTracer, RecordingTracer, correlation, get_tracer
from conftest import AGENT


def test_tracer_falls_back_without_opentelemetry(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("opentelemetry"):
            raise ImportError("not installed")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    t = get_tracer()
    assert isinstance(t, NoopTracer) and t.backend == "noop"
    assert get_tracer("console").backend == "console"
    with pytest.raises(ImportError):
        get_tracer("otel")
    with t.span("x", {"a": 1}) as s:      # same interface as the OTel tracer
        s.set_attribute("b", 2)
        s.add_event("e")


def test_otel_tracer_when_available():
    pytest.importorskip("opentelemetry")
    t = get_tracer()
    assert t.backend == "opentelemetry"
    with correlation("c-otel"), t.span("agcp.test", {"obj": {"x": 1}}) as s:
        s.set_attribute("k", "v")


def test_console_tracer_prints():
    buf = io.StringIO()
    with ConsoleTracer(buf).span("hello"):
        pass
    assert "[span] hello" in buf.getvalue()


def test_spans_and_audit_share_correlation_id(env):
    with correlation("corr-shared"):
        env["acts"]["demo.write"](actor=AGENT, key="k", v=1, value=10)
    spans = env["tracer"].spans
    assert {s.name for s in spans} >= {"agcp.action", "agcp.policy.evaluate", "agcp.action.execute"}
    assert all(s.attributes["agcp.correlation_id"] == "corr-shared" for s in spans)
    assert {r["correlation_id"] for r in env["cp"].audit.records() if r["action"]} == {"corr-shared"}


def test_metrics_hook(env):
    seen = []
    env["cp"].metrics.subscribe(lambda kind, name, v, labels: seen.append(name))
    A = env["acts"]
    A["demo.write"](actor=AGENT, key="k", v=1, value=10)
    A["demo.write"](actor=AGENT, key="k", v=1, value=500)
    with pytest.raises(Exception):
        A["demo.write"](actor=AGENT, key="k", v=1, value=10, frozen=True)
    m = env["cp"].metrics
    assert m.count("agcp_decisions_total", outcome="ALLOW") == 1
    assert m.count("agcp_decisions_total", outcome="REQUIRE_APPROVAL") == 1
    assert m.count("agcp_denials_total") == 1
    assert "agcp_decisions_total" in seen and "agcp_approvals_requested_total" in seen
    assert "counters" in m.snapshot()


def test_failed_span_records_error():
    t = RecordingTracer()
    with pytest.raises(ValueError):
        with t.span("bad"):
            raise ValueError()
    assert t.spans[0].status.startswith("ERROR")
