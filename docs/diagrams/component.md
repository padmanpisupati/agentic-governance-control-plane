# Component view

```mermaid
flowchart TB
    subgraph Agents["Any agent runtime (LangGraph, custom loop, service)"]
        A1[Order-exception agent]
        A2[Vendor-payment agent]
        H[Human requester]
    end
    subgraph AGCP["agcp control plane (library, in-process)"]
        G[Enforcement point<br/>GuardedAction · registry · idempotency · dry-run]
        P[Policy engine<br/>YAML bundle · version · SHA-256 hash]
        AP[Approval service<br/>router · SoD/four-eyes · expiry · escalation]
        S[Saga & rollback<br/>snapshots · compensators]
        AU[(Audit log<br/>hash chain · HMAC checkpoints)]
        T[Tracer + metrics<br/>OTel or no-op]
    end
    subgraph Stores["Pluggable persistence"]
        S1[(In-memory)]
        S2[(JSONL file)]
        S3[(SQLite append-only)]
        AS[(SQLite approval store)]
    end
    subgraph SoR["Systems of record (adapters)"]
        E1[Mock ERP orders]
        E2[Mock AP ledger + bank gateway]
    end
    A1 & A2 & H -->|call tool| G
    G --> P
    G -->|REQUIRE_APPROVAL| AP
    G -->|grant| SoR
    S --> G
    G & AP & S --> AU
    G & AP & S --> T
    AU --> S1 & S2 & S3
    AP --> AS
    SoR -. require_grant() .-> G
```

| Component | Module | Responsibility |
|---|---|---|
| Policy engine | `agcp.policy` | Pure function: context → ALLOW / REQUIRE_APPROVAL / DENY + matched rules + bundle version/hash |
| Enforcement point | `agcp.guard.ControlPlane` | Only path to execute a registered action; issues the execution grant |
| Execution grant | `agcp.context` | Context-local capability; adapters call `require_grant()` before any write |
| Approval service | `agcp.approvals` | Routing by role and value tier, durable pending requests, SoD, expiry, escalation |
| Saga / rollback | `agcp.guard` | Compensation in reverse order, before/after snapshots, rebuilt from the audit log |
| Audit log | `agcp.audit` | Append-only, hash-chained, pluggable sinks, signed checkpoints, `verify()` |
| Observability | `agcp.tracing` | OTel-compatible spans, metrics hook, correlation ids shared with audit |
