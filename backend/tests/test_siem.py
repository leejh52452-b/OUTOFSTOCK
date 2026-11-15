"""외부 관제(SIEM) 연동 형식 — 보안 이벤트·감사 기록·운영 경보를 CEF·JSON Lines 로(app/security/siem.py).
아이디는 로그 줄과 같은 HMAC 가명, 대상 번호(change:3)는 그대로, 내보내기 자체도 감사 기록."""
import json

from fastapi.testclient import TestClient

from app.api import app
from app.security import events, keys, siem
from app.services import alerts, audit

client = TestClient(app)


def test_cef_export_pseudonymizes_users_and_escapes_fields():
    events.emit("login_fail", "medium", username="kim|lee=1", respond=False, reason="drill")
    audit.record("admin.unlock", "sec-admin", target="victim-user")
    audit.record("change.approved", "lead-b", target="change:7")
    alerts.notify("job:x", "예약 작업 실패: x|y", "", level="error")
    r = siem.export(1, "cef", actor="tester")
    assert r["count"] >= 4
    text = "\n".join(r["lines"])
    assert "kim|lee" not in text and "sec-admin" not in text and "victim-user" not in text      # 원문 아이디 없음
    assert f"suser=u:{keys.hmac_hex('user', 'kim|lee=1')}" in text                            # 로그 줄과 같은 가명
    ev = next(x for x in r["lines"] if "|login_fail|" in x)
    assert ev.startswith("CEF:0|OUTOFSTOCK|stock-backend|") and "|5|" in ev                     # medium → 5
    assert next(x for x in r["lines"] if "|change.approved|" in x).count("cs3=change:7") == 1   # 대상 번호는 그대로
    al = next(x for x in r["lines"] if "alert:job" in x)
    assert "x\\|y" in al and "|9|" in al                                                       # 머리 칸의 | 는 이스케이프
    for line in r["lines"]:
        assert line.count("|") - line.count("\\|") >= 7                                       # CEF 머리 7칸 구조 유지


def test_jsonl_export_and_admin_api_is_audited(monkeypatch):
    events.emit("csrf_fail", "medium", username="park", respond=False)
    r = siem.export(1, "jsonl", actor="tester")
    rows = [json.loads(x) for x in r["lines"]]
    assert any(x["source"] == "security_event" and x["signature"] == "csrf_fail" and x["user"].startswith("u:") for x in rows)
    assert all("park" not in json.dumps(x, ensure_ascii=False) for x in rows)
    from app.services import audit as audit_svc
    last = audit_svc.recent(5, action="siem.export")
    assert last and last[0]["actor"] == "tester"
    assert client.get("/admin/security/export").status_code in (401, 403)
    try:
        siem.export(1, "xml")
        raise AssertionError("허용되지 않은 형식")
    except ValueError:
        pass
