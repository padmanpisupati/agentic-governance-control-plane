import copy
import sqlite3

import pytest

from agcp import AuditLog, InMemorySink, JsonlSink, SqliteSink


def filled(sink=None, n=6, **kw):
    log = AuditLog(sink or InMemorySink(), **kw)
    for i in range(n):
        log.append("evt", correlation_id=f"c{i % 2}", actor="a", action="x", data={"i": i})
    return log


@pytest.mark.parametrize("make", [lambda p: InMemorySink(), lambda p: JsonlSink(p / "a.jsonl"),
                                  lambda p: SqliteSink(p / "a.db")])
def test_all_sinks_chain_and_verify(tmp_path, make):
    log = filled(make(tmp_path))
    v = log.verify()
    assert v.ok and v.records == 6
    recs = log.records()
    assert recs[0]["prev_hash"] == "0" * 64 and recs[1]["prev_hash"] == recs[0]["hash"]


@pytest.mark.parametrize("tamper", ["edit", "delete", "reorder", "insert", "rehash_one"])
def test_tamper_detected(tamper):
    log = filled()
    recs = copy.deepcopy(log.records())
    if tamper == "edit":
        recs[2]["data"]["i"] = 999
    elif tamper == "delete":
        del recs[3]
    elif tamper == "reorder":
        recs[1], recs[2] = recs[2], recs[1]
    elif tamper == "insert":
        recs.insert(2, dict(recs[2]))
    elif tamper == "rehash_one":  # attacker recomputes the edited record's own hash
        from agcp.audit import record_hash
        recs[2]["data"]["i"] = 999
        recs[2]["hash"] = record_hash(recs[2])
    v = log.verify(recs)
    assert not v.ok and v.broken_at is not None


def test_jsonl_file_tamper_detected(tmp_path):
    p = tmp_path / "a.jsonl"
    log = filled(JsonlSink(p))
    p.write_text(p.read_text().replace('"i": 3', '"i": 4'))
    assert not log.verify().ok


def test_sqlite_sink_is_append_only(tmp_path):
    sink = SqliteSink(tmp_path / "a.db")
    filled(sink)
    with pytest.raises(sqlite3.DatabaseError):
        sink.conn.execute("UPDATE audit SET body='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        sink.conn.execute("DELETE FROM audit WHERE seq=1")


def test_chain_resumes_from_persisted_sink(tmp_path):
    filled(JsonlSink(tmp_path / "a.jsonl"), n=3)
    log2 = AuditLog(JsonlSink(tmp_path / "a.jsonl"))
    log2.append("more", correlation_id=None)
    assert log2.verify().ok and log2.head[0] == 4


def test_signed_checkpoints_and_external_anchor():
    log = filled(checkpoint_every=2)
    anchor = log.checkpoint()
    v = log.verify(anchor=anchor)
    assert v.ok and v.checkpoints_verified >= 3 and anchor["key_label"] == "DEMO"
    # Tail truncation: chain still links, but the external anchor exposes it.
    truncated = log.records()[:-3]
    assert log.verify(truncated).ok
    assert not log.verify(truncated, anchor=anchor).ok


def test_full_rewrite_without_key_fails_checkpoint():
    from agcp.audit import record_hash
    log = filled(checkpoint_every=3)
    recs = copy.deepcopy(log.records())
    recs[0]["data"]["i"] = 42  # attacker rewrites history and re-chains every hash...
    prev = "0" * 64
    for r in recs:
        r["prev_hash"] = prev
        r["hash"] = record_hash(r)
        prev = r["hash"]
    v = log.verify(recs)  # ...but cannot re-sign checkpoints without the HMAC key
    assert not v.ok and "checkpoint" in v.reason


def test_wrong_key_rejects_checkpoint():
    log = filled(checkpoint_every=2)
    other = AuditLog(InMemorySink(), hmac_key=b"another-key")
    assert not other.verify(log.records()).ok
