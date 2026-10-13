# ADR 0005: Async approvals with SoD, and saga compensation rebuilt from the audit log

**Status:** Accepted

## Context
Human approval takes hours, outlives processes, and is only a control if the requester cannot
approve their own action. Multi-step actions against systems of record have no distributed transaction.

## Decision
- **Approvals:** the router picks the stricter of the value tier and the rule's `approval` block
  (role, min_approvers, TTL). Requests are persisted (SQLite store). Checks: approver != requester,
  distinct approvers (four-eyes), role rank >= required, human principals only. Expired requests
  escalate along a configured chain, or end EXPIRED. `Approver` is a protocol, so inbox, chat or
  ticket channels plug in. `review()` returning `None` means "still pending".
- **Sagas:** `cp.saga(name, actor)` shares a correlation id. If a step raises or doesn't execute
  (denied, pending, rejected), executed steps are compensated in reverse.
- **Rollback:** `cp.rollback(correlation_id, actor)` is a governed action (`agcp.rollback` in the
  bundle). It rebuilds the open steps from `action.executed` records (params, before, after, output),
  so it works after a restart. It is idempotent and records whether the restored state matches the before-image.

## Consequences
Compensators are domain code and must be safe to run more than once. Missing compensators are reported
(`compensation.unavailable`), never silently skipped. Compensation runs under a compensation grant
without a second policy check, by design, so a failure can always be unwound.
