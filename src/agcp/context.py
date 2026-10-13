"""Correlation ids and the execution capability shared by traces and audit records."""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import UnguardedExecutionError

_correlation: contextvars.ContextVar[str | None] = contextvars.ContextVar("agcp_correlation", default=None)
_execution: contextvars.ContextVar["ExecutionGrant | None"] = contextvars.ContextVar("agcp_execution", default=None)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def current_correlation_id() -> str | None:
    return _correlation.get()


@contextmanager
def correlation(correlation_id: str | None = None):
    """Bind a correlation id for everything executed inside the block."""
    cid = correlation_id or new_id("corr-")
    token = _correlation.set(cid)
    try:
        yield cid
    finally:
        _correlation.reset(token)


@dataclass(frozen=True)
class ExecutionGrant:
    """Issued by the enforcement point only after a policy decision allows execution."""
    action: str
    request_id: str
    correlation_id: str
    decision_id: str
    compensation: bool = False


@contextmanager
def _grant(g: ExecutionGrant):
    token = _execution.set(g)
    try:
        yield g
    finally:
        _execution.reset(token)


def require_grant(action: str | None = None) -> ExecutionGrant:
    """Call inside any system-of-record adapter write. Raises unless invoked through a guard.

    This is the second line of defence: even if someone obtains the raw function, the adapter
    refuses to write without a grant issued for a policy-decided action.
    """
    g = _execution.get()
    if g is None:
        raise UnguardedExecutionError("write attempted outside an enforcement point")
    if action is not None and g.action != action and not g.compensation:
        raise UnguardedExecutionError(f"grant is for {g.action!r}, not {action!r}")
    return g
