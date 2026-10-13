# ADR 0003: Enforcement by guard and execution grant (defence in depth)

**Status:** Accepted

## Context
A policy check that an agent can skip is not a control. In Python nothing is truly private, so
"bypass impossible" needs more than a decorator.

## Decision
1. `@cp.action(name, ...)` stores the raw function in a private registry and returns a
   `GuardedAction` (slots, no `__wrapped__`). Calling it always runs the policy decision.
2. When execution is allowed, the guard sets a context-local `ExecutionGrant` (action, request id,
   correlation id, decision id). System-of-record adapters call `require_grant(action)` before any
   write and raise `UnguardedExecutionError` without one.
3. Idempotency keys: a repeated key returns the prior result (`REPLAYED`), audited, with no new write.
   This also applies to pending approvals, so retries do not create duplicate approval requests.
4. Dry-run: per call or plane-wide; produces a decision and audit record, never a write.

## Consequences
Bypass requires deliberately forging a grant through private APIs. That stands out in code review,
and tests cover it. Production adapters should also verify grants server-side, for example with a
signed token per request, which this reference does not implement.
