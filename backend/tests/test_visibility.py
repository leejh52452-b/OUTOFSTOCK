"""가시성 — 구조화(JSON) 로그·요청 ID·감사 체인 외부 증인(app/logging_conf.py, app/services/audit.py)."""
import json
import logging

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.logging_conf import JsonFormatter
from app.services import audit, observability

client = TestClient(app)


def test_json_log_lines_carry_request_id_and_structured_fields(caplog):
    fmt = JsonFormatter()
    with caplog.at_level(logging.INFO, logger="access"):
        r = client.get("/indices", headers={"X-Request-ID": "trace-0123456789"})
    rec = next(x for x in caplog.records if x.name == "access")
    line = json.loads(fmt.format(rec))
    assert line["route"] == "/indices" and line["status"] == 200 and line["decision"] == "allow"
    assert line["rid"] == r.headers["x-request-id"] == "trace-0123456789"
    assert "ip" in line and "203." not in json.dumps(line)                 # IP 는 가명값만


def test_access_log_has_no_query_values_or_raw_identifiers(caplog):
    with caplog.at_level(logging.INFO, logger="access"):
        client.get("/search/suggest", params={"q": "개인적인검색어"})
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "access")
    assert "개인적인검색어" not in text and "/search/suggest" in text


def test_metrics_snapshot_and_prometheus_format():
    observability.reset()
    for _ in range(3):
        client.get("/indices")
    snap = observability.snapshot()
    row = next(x for x in snap["routes"] if x["route"] == "GET /indices")
    assert row["count"] == 3 and row["p95_ms"] is not None
    prom = observability.prometheus()
    assert 'oos_http_requests_total{method="GET",route="/indices"} 3' in prom


def test_external_witness_detects_a_fully_recomputed_chain(tmp_path, monkeypatch, as_owner):
    path = tmp_path / "witness.jsonl"
    monkeypatch.setattr(settings, "AUDIT_WITNESS_FILE", str(path))
    for i in range(3):
        audit.record("demo.action", f"user{i}")
    w = audit.witness("test")
    assert path.read_text(encoding="utf-8").count("\n") == 1 and w["rows"] == 3
    assert audit.verify_witnesses()["ok"] and audit.verify()["ok"]
    # DB 소유자 권한을 가진 공격자: 첫 행을 고치고 이후 체인을 전부 다시 계산 → 내부 검증은 통과한다
    rows = db.query("SELECT audit_id, at, actor, action, target, detail, ip_hash, result FROM audit_log ORDER BY audit_id")
    prev = audit.GENESIS
    for r in rows:
        actor = "attacker" if r["audit_id"] == rows[0]["audit_id"] else r["actor"]
        h = audit._hash(prev, audit._canon(r["at"], actor, r["action"], r["target"], r["detail"], r["ip_hash"], r["result"]))
        db.execute("UPDATE audit_log SET actor=:a, prev_hash=:p, row_hash=:h WHERE audit_id=:i",
                   {"a": actor, "p": prev, "h": h, "i": r["audit_id"]})
        prev = h
    assert audit.verify()["ok"]                                             # 체인만으로는 못 알아챈다
    res = audit.verify_witnesses()
    assert not res["ok"] and res["mismatches"][0]["reason"] == "head_mismatch"   # 밖에 남긴 머리 해시와 어긋남
