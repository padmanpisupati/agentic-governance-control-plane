"""Tracing and metrics hooks.

get_tracer() returns an OpenTelemetry-backed tracer when opentelemetry-api is importable,
otherwise a no-op (or console) tracer with the identical interface:

    with tracer.span("agcp.policy.evaluate", {"agcp.action": "x"}) as span:
        span.set_attribute("agcp.outcome", "ALLOW")

Every span carries agcp.correlation_id, the same id written to audit records.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable

from .context import current_correlation_id


class _Span:
    def __init__(self, name: str, attrs: dict):
        self.name, self.attributes, self.events, self.status = name, dict(attrs), [], "OK"
        self.start, self.end = time.perf_counter(), None

    def set_attribute(self, k: str, v: Any) -> None:
        self.attributes[k] = v

    def add_event(self, name: str, attrs: dict | None = None) -> None:
        self.events.append((name, attrs or {}))

    def record_exception(self, exc: BaseException) -> None:
        self.status = f"ERROR: {type(exc).__name__}"


class NoopTracer:
    backend = "noop"

    @contextmanager
    def span(self, name: str, attributes: dict | None = None):
        s = _Span(name, {"agcp.correlation_id": current_correlation_id(), **(attributes or {})})
        try:
            yield s
        except BaseException as e:
            s.record_exception(e)
            raise
        finally:
            s.end = time.perf_counter()
            self._finish(s)

    def _finish(self, s: _Span) -> None:
        pass


class RecordingTracer(NoopTracer):
    """Keeps finished spans in memory (tests, demos)."""
    backend = "recording"

    def __init__(self):
        self.spans: list[_Span] = []

    def _finish(self, s):
        self.spans.append(s)


class ConsoleTracer(NoopTracer):
    backend = "console"

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def _finish(self, s):
        ms = (s.end - s.start) * 1000
        print(f"[span] {s.name} {ms:.2f}ms {s.status} {s.attributes}", file=self.stream)


class OTelTracer:
    backend = "opentelemetry"

    def __init__(self, name: str = "agcp"):
        from opentelemetry import trace
        self._trace = trace
        self._tracer = trace.get_tracer(name)

    @contextmanager
    def span(self, name: str, attributes: dict | None = None):
        attrs = {"agcp.correlation_id": current_correlation_id() or "", **(attributes or {})}
        attrs = {k: (v if isinstance(v, (str, bool, int, float)) else str(v)) for k, v in attrs.items()}
        with self._tracer.start_as_current_span(name, attributes=attrs) as s:
            yield s


def get_tracer(prefer: str = "auto"):
    """prefer: auto | otel | console | noop."""
    if prefer in ("auto", "otel"):
        try:
            return OTelTracer()
        except ImportError:
            if prefer == "otel":
                raise
    return ConsoleTracer() if prefer == "console" else NoopTracer()


class Metrics:
    """Minimal counter/histogram hook. Subscribe callbacks to forward to Prometheus, OTel, etc."""

    def __init__(self):
        self.counters: dict[tuple, float] = defaultdict(float)
        self.histograms: dict[str, list[float]] = defaultdict(list)
        self._subs: list[Callable[[str, str, float, dict], None]] = []

    def subscribe(self, fn: Callable[[str, str, float, dict], None]) -> None:
        self._subs.append(fn)

    def inc(self, name: str, value: float = 1, **labels) -> None:
        self.counters[(name, tuple(sorted(labels.items())))] += value
        for fn in self._subs:
            fn("counter", name, value, labels)

    def observe(self, name: str, value: float, **labels) -> None:
        self.histograms[name].append(value)
        for fn in self._subs:
            fn("histogram", name, value, labels)

    def count(self, name: str, **labels) -> float:
        want = set(labels.items())
        return sum(v for (n, ls), v in self.counters.items() if n == name and want <= set(ls))

    def snapshot(self) -> dict:
        out: dict = {"counters": {}, "histograms": {}}
        for (n, ls), v in sorted(self.counters.items()):
            out["counters"][n + ("{" + ",".join(f"{k}={v2}" for k, v2 in ls) + "}" if ls else "")] = v
        for n, vals in self.histograms.items():
            s = sorted(vals)
            out["histograms"][n] = {"count": len(s), "p50": s[len(s) // 2], "max": s[-1]} if s else {"count": 0}
        return out
