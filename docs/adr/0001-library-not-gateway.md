# ADR 0001: In-process, framework-agnostic library rather than a network gateway

**Status:** Accepted

## Context
Agents run in many runtimes (LangGraph, bespoke loops, batch jobs). Controls must apply to all of
them, and must sit where the side effect happens: the tool call that writes to a system of record.

## Decision
Ship the control plane as a plain Python library (`agcp`) with no dependency on an agent framework.
Agents call `GuardedAction` objects; frameworks integrate by calling those objects from their
tool nodes (see `examples/langgraph_integration`). Runtime dependency: PyYAML only.
OpenTelemetry and LangGraph are optional.

## Consequences
- Same controls across workflows and domains, proven by two unrelated examples and a test that
  greps the library for domain vocabulary.
- No extra network hop; decisions are microseconds.
- A single process holds the idempotency cache. Multi-replica deployments need a shared idempotency
  store and a centrally hosted policy bundle, the next step toward a sidecar/gateway (see the README roadmap).
