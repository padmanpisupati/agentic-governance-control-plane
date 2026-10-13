# ADR 0002: Declarative, versioned policy bundles with default-deny

**Status:** Accepted

## Context
Process owners, not developers, should own the thresholds for autonomy, and every decision must be
reproducible: "which rule, in which version, allowed this write?"

## Decision
- Policies are YAML bundles: `bundle`, `version`, a declared `actions` allow-list, and `rules`.
- Conditions cover action (glob/list), value, confidence, actor, resource state (dotted paths)
  and time windows. Operators: eq, ne, gt, gte, lt, lte, in, not_in, exists.
- Precedence: DENY > REQUIRE_APPROVAL > ALLOW. An action not declared, or with no matching rule,
  is DENY. Type errors in comparisons fail closed (the rule does not match).
- The bundle's SHA-256 over canonical JSON is recorded on every decision and audit record.
- Policy swaps go through `ControlPlane.load_policy()`, which audits old/new version and hash.
  Approvals granted under an older hash are re-evaluated on resume.

## Alternatives considered
OPA/Rego or Cedar would add a stronger language and tooling. They were not used, to keep the
reference dependency-free. The `PolicyEngine.evaluate(ctx) -> Decision` seam is where an external
engine would plug in.

## Consequences
Rules are reviewable in pull requests (change management). The rule language is deliberately small;
complex logic belongs in resource loaders that compute facts for the rules.
