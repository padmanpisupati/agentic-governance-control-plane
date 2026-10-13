"""Append-only, SHA-256 hash-chained audit log with pluggable sinks and signed checkpoints.

Each record stores prev_hash and hash = sha256(canonical_json(record without hash)).
Editing, deleting, reordering or inserting any record breaks the chain at that point.
A checkpoint record carries HMAC-SHA256(key, "seq:head_hash") so the chain head can be anchored
outside the log (ticket, WORM bucket, separate DB). Truncating the tail is then detectable too.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from .clock import SystemClock

GENESIS = "0" * 64
# DEMO ONLY. Never use a hard-coded key in production: load it from a KMS/HSM and rotate it.
DEMO_HMAC_KEY = b"agcp-demo-key-NOT-FOR-PRODUCTION"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def record_hash(rec: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in rec.items() if k != "hash"})).hexdigest()


class AuditSink(Protocol):
    def append(self, record: dict) -> None: ...
    def read_all(self) -> list[dict]: ...


class InMemorySink:
    def __init__(self):
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(dict(record))

    def read_all(self) -> list[dict]:
        return [dict(r) for r in self.records]


class JsonlSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def read_all(self) -> list[dict]:
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class SqliteSink:
    """Append-only table: UPDATE and DELETE are rejected by triggers."""

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
              BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
              BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
        """)

    def append(self, record: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO audit (seq, body) VALUES (?, ?)",
                              (record["seq"], json.dumps(record, sort_keys=True, default=str)))

    def read_all(self) -> list[dict]:
        return [json.loads(b) for (b,) in self.conn.execute("SELECT body FROM audit ORDER BY seq")]


@dataclass
class VerifyResult:
    ok: bool
    records: int
    broken_at: int | None = None
    reason: str = ""
    checkpoints_verified: int = 0


class AuditLog:
    def __init__(self, sink: AuditSink | None = None, clock=None, hmac_key: bytes = DEMO_HMAC_KEY,
                 checkpoint_every: int | None = None):
        self.sink = sink or InMemorySink()
        self.clock = clock or SystemClock()
        self._key = hmac_key
        self.checkpoint_every = checkpoint_every
        self._lock = threading.Lock()
        existing = self.sink.read_all()  # resume a persisted chain
        self._seq = existing[-1]["seq"] if existing else 0
        self._head = existing[-1]["hash"] if existing else GENESIS
        self._since_cp = 0

    @property
    def head(self) -> tuple[int, str]:
        return self._seq, self._head

    def append(self, event: str, *, correlation_id: str | None, actor: str | None = None,
               action: str | None = None, data: dict | None = None) -> dict:
        with self._lock:
            rec = {"seq": self._seq + 1, "ts": self.clock.now().isoformat(), "event": event,
                   "correlation_id": correlation_id, "actor": actor, "action": action,
                   "data": data or {}, "prev_hash": self._head}
            rec["hash"] = record_hash(rec)
            self.sink.append(rec)
            self._seq, self._head = rec["seq"], rec["hash"]
            self._since_cp += 1
            due = self.checkpoint_every and self._since_cp >= self.checkpoint_every and event != "checkpoint"
        if due:
            self.checkpoint()
        return rec

    def _sign(self, seq: int, head: str) -> str:
        return hmac.new(self._key, f"{seq}:{head}".encode(), hashlib.sha256).hexdigest()

    def checkpoint(self) -> dict:
        """Sign the current head and append it as a checkpoint record. Export the returned
        anchor {seq, head_hash, signature} to an external store to detect tail truncation."""
        seq, head = self.head
        anchor = {"anchored_seq": seq, "head_hash": head, "signature": self._sign(seq, head),
                  "alg": "HMAC-SHA256", "key_label": "DEMO" if self._key == DEMO_HMAC_KEY else "custom"}
        self.append("checkpoint", correlation_id=None, actor="agcp", data=anchor)
        self._since_cp = 0
        return anchor

    def records(self) -> list[dict]:
        return self.sink.read_all()

    def by_correlation(self, cid: str) -> list[dict]:
        return [r for r in self.records() if r["correlation_id"] == cid]

    def verify(self, records: Iterable[dict] | None = None, anchor: dict | None = None) -> VerifyResult:
        recs = list(self.records() if records is None else records)
        prev, by_seq, cps = GENESIS, {}, 0
        for i, r in enumerate(recs, start=1):
            if r.get("seq") != i:
                return VerifyResult(False, len(recs), i, f"sequence gap or reordering: expected {i}, got {r.get('seq')}")
            if r.get("prev_hash") != prev:
                return VerifyResult(False, len(recs), i, "prev_hash does not match previous record")
            if record_hash(r) != r.get("hash"):
                return VerifyResult(False, len(recs), i, "record content does not match its hash")
            if r["event"] == "checkpoint":
                d = r["data"]
                if by_seq.get(d["anchored_seq"]) != d["head_hash"] or not hmac.compare_digest(
                        self._sign(d["anchored_seq"], d["head_hash"]), d["signature"]):
                    return VerifyResult(False, len(recs), i, "checkpoint signature invalid", cps)
                cps += 1
            by_seq[i], prev = r["hash"], r["hash"]
        if anchor is not None:  # externally held anchor: detects truncation / rewrite of the tail
            if not hmac.compare_digest(self._sign(anchor["anchored_seq"], anchor["head_hash"]), anchor["signature"]):
                return VerifyResult(False, len(recs), None, "external anchor signature invalid", cps)
            if by_seq.get(anchor["anchored_seq"]) != anchor["head_hash"]:
                return VerifyResult(False, len(recs), anchor["anchored_seq"],
                                    "log does not contain the anchored head (truncated or rewritten)", cps)
        return VerifyResult(True, len(recs), checkpoints_verified=cps)
