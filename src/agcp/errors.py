"""Exception types raised by the control plane."""


class GovernanceError(Exception):
    """Base class for all control-plane errors."""


class PolicyError(GovernanceError):
    """Policy bundle is malformed."""


class PolicyDenied(GovernanceError):
    def __init__(self, action: str, decision):
        self.action, self.decision = action, decision
        super().__init__(f"DENY {action}: rules={decision.rule_ids} reason={decision.reason}")


class UnguardedExecutionError(GovernanceError):
    """A protected side effect was attempted outside an enforcement point."""


class UnknownActionError(GovernanceError):
    """Action is not in the action registry."""


class ApprovalError(GovernanceError):
    """Approval cannot be recorded (wrong state, expired, or wrong role)."""


class SegregationOfDutiesError(ApprovalError):
    """Approver is the requester, or has already approved this request."""


class CompensationError(GovernanceError):
    """A compensating action failed; manual intervention required."""
