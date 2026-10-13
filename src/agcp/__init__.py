"""agcp: a framework-agnostic governance and control plane for agentic actions."""
from .audit import AuditLog, InMemorySink, JsonlSink, SqliteSink, DEMO_HMAC_KEY
from .approvals import (ApprovalRouter, ApprovalService, InMemoryApprovalStore,
                        SqliteApprovalStore, Approver, Principal)
from .clock import FixedClock, SystemClock
from .context import correlation, current_correlation_id, require_grant
from .errors import *  # noqa: F401,F403
from .guard import ActionResult, ControlPlane
from .policy import Decision, Outcome, PolicyBundle, PolicyEngine
from .tracing import ConsoleTracer, Metrics, NoopTracer, RecordingTracer, get_tracer

from .bootstrap import build_control_plane

__version__ = "0.1.0"
