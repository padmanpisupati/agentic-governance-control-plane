# ADR 0004: Hash-chained audit log with pluggable sinks and signed checkpoints

**Status:** Accepted

## Context
Auditors need evidence that the trail is complete and unaltered, not just present.

## Decision
- Each record: seq, ts, event, correlation_id, actor, action, data, prev_hash, and
  hash = SHA-256(canonical JSON without hash). `verify()` checks sequence, linkage and content.
- Sinks: in-memory, JSONL, SQLite (UPDATE/DELETE blocked by triggers). The chain resumes from a
  persisted sink on restart.
- Every N records (and on demand) a checkpoint record carries HMAC-SHA256(key, "seq:head").
  Exporting that anchor to an external store detects tail truncation and full re-chaining.
- The shipped key `DEMO_HMAC_KEY` is clearly labeled demo-only. Production loads keys from a KMS/HSM.

## Consequences
Edits, deletes, reorders and inserts are detected, and so is a full rewrite when the attacker lacks
the key. HMAC is symmetric: anyone who can verify can also sign. Use asymmetric signatures (for
example Ed25519) or a WORM/ledger store when verifiers must not be able to sign.
