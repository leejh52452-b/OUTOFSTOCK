"""가용성 감시 · IT 장애 자동 판정 · 사고보고서 초안 — 전자금융감독규정 시행세칙 제7조의4(사고보고)를 설계 기준으로
(app/services/incidents.py). 1분 합성 모니터링 → 10분 장애 판정 → 30분 보고 대상·기한 → 정상 3번 종료."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.config import settings
from app.security import assessment, rbac, totp
from app.services import alerts, auth, incidents

client = TestClient(app)
PW = "Incident-pass-2026"
FAIL, OK = {"ok": False, "ms": 10_000, "code": 0}, {"ok": True, "ms": 25, "code": 200}


def _series(now, ok_before=True, fail_minutes=0):
    if ok_before:
        incidents.record_check(OK, at=now - timedelta(minutes=fail_minutes + 1))
    for m in range(fail_minutes, 0, -1):
        incidents.record_check(FAIL, at=now - timedelta(minutes=m))


def test_reporting_rule_follows_enforcement_detail_7_4():
    r = incidents.reportable
    assert r(30, 0) and not r(29.9, 9_999)                     # 가목 — 지연·중단 30분 이상
    assert r(10, 10_000) and not r(9.9, 50_000) and not r(15, 9_999)   # 나목 — 10분 이상 + 가입자 1만 명 이상
    assert incidents.REPORT_FIELDS[0] == "보고형태" and "사고원인(변경시점 및 사유 등)" in incidents.REPORT_FIELDS
    assert len(incidents.REPORT_FIELDS) == 12 and incidents.FIRST_REPORT_H == 24


def test_outage_is_opened_marked_reportable_and_closed():
    now = clock.utc()
    _series(now, fail_minutes=9)
    assert "opened" not in incidents.evaluate(now)                 # 9분 — 아직 사건 아님
    incidents.record_check(FAIL, at=now)
    ev = incidents.evaluate(now + timedelta(minutes=1))
    iid = ev["opened"]
    inc = incidents.get(iid)
    assert inc["status"] == "open" and inc["kind"] == "outage" and not inc["reportable"]
    assert db.query("SELECT level FROM alert_log WHERE key=:k", {"k": f"incident:{iid}"})[0]["level"] == "error"
    assert incidents.evaluate(now + timedelta(minutes=2)).get("opened") is None     # 같은 장애로 두 번 열지 않는다
    for m in range(1, 25):
        incidents.record_check(FAIL, at=now + timedelta(minutes=m))
    ev = incidents.evaluate(now + timedelta(minutes=25))           # 시작부터 34분 — 보고 대상
    assert ev["reportable"] and ev["open"] == iid
    inc = incidents.get(iid)
    assert inc["reportable"] and inc["deadlines"]["first"] and not inc["deadlines"]["first_overdue"]
    assert clock.parse_utc(inc["report_due"]) - clock.parse_utc(inc["detected_at"]) == timedelta(hours=24)
    for m in (26, 27, 28):
        incidents.record_check(OK, at=now + timedelta(minutes=m))
    ev = incidents.evaluate(now + timedelta(minutes=28))
    assert ev["closed"] == iid
    inc = incidents.get(iid)
    assert inc["status"] == "resolved" and inc["details"]["minutes"] == 35.0 and inc["reportable"]
    au = [a["action"] for a in db.query("SELECT action FROM audit_log WHERE target=:t ORDER BY audit_id",
                                        {"t": f"incident:{iid}"})]
    assert au == ["incident.open", "incident.reportable", "incident.close"]


def test_short_blip_is_not_an_incident_and_never_healthy_target_is_not_an_outage():
    now = clock.utc()
    _series(now, fail_minutes=5)
    incidents.record_check(OK, at=now)
    assert incidents.evaluate(now + timedelta(minutes=20)) == {}  # 5분 실패 뒤 정상 — 사건 아님
    db.execute("DELETE FROM uptime_check")
    _series(now, ok_before=False, fail_minutes=30)                # 한 번도 정상 응답이 없었다 = 주소 설정 문제
    ev = incidents.evaluate(now)
    assert ev.get("unverified") and "opened" not in ev
    assert db.query("SELECT COUNT(*) AS n FROM incident")[0]["n"] == 0
    assert db.query("SELECT level FROM alert_log WHERE key='uptime:unverified'")[0]["level"] == "warn"


def test_monitoring_gap_warns_once():
    now = clock.utc()
    incidents.record_check(OK, at=now - timedelta(minutes=45))
    incidents.record_check(OK, at=now)                           # 45분 동안 확인 기록 없음(서버·스케줄러 중단?)
    assert incidents.evaluate(now)["gap_min"] == 45
    incidents.evaluate(now)
    rows = db.query("SELECT delivered FROM alert_log WHERE key LIKE 'uptime:gap:%' ORDER BY alert_id")
    assert len(rows) == 2 and rows[0]["delivered"] == 1 and rows[1]["delivered"] is None   # 두 번째는 중복이라 안 보냄


def test_security_incident_is_reportable_from_the_start_and_report_draft_has_all_fields():
    iid = incidents.open_manual("security", "관리자 계정 탈취 의심 로그인", "sec-a")
    inc = incidents.get(iid)
    assert inc["reportable"] and inc["deadlines"]["first"] and inc["opened_by"] == "sec-a"
    assert db.query("SELECT 1 FROM alert_log WHERE key=:k", {"k": f"incident:report:{iid}"})
    db.write_query("INSERT INTO change_record(created_at, kind, title, reason, requested_by, release_sha256, risk, status, "
                   "approved_at, applied_at, applied_by) VALUES(:t, 'planned', '로그인 정책 변경', '세션 만료 조정', 'dev-a', :s, "
                   "'high', 'applied', :t, :t, 'ops') RETURNING id", {"t": clock.utc() - timedelta(hours=2), "s": "e" * 64})
    md = incidents.report_md(iid)
    for k in incidents.REPORT_FIELDS:
        assert f"| {k} |" in md, k
    assert "로그인 정책 변경" in md and "보고 대상: **예**" in md and "(작성 필요)" in md   # 사건 전 변경 = 변경시점 후보
    with pytest.raises(ValueError):
        incidents.update(iid, "sec-a", reported="종결")                # 끝나지 않은 사건은 종결 보고 불가
    incidents.update(iid, "sec-a", cause="재사용된 비밀번호", action="세션 전체 종료·계정 잠금", measures="관리자 FIDO 도입",
                     reported="최초", ended_at=clock.utc())
    inc = incidents.get(iid)
    assert inc["first_reported_at"] and inc["status"] == "resolved" and not inc["deadlines"]["first_overdue"]
    md = incidents.report_md(iid, "종결")
    assert "| 보고형태 | 종결 |" in md and "재사용된 비밀번호" in md and "관리자 FIDO 도입" in md
    assert incidents.update(iid, "sec-a", reported="종결")["status"] == "closed"
    with pytest.raises(ValueError):
        incidents.open_manual("weird", "알 수 없는 종류의 사건", "sec-a")


def test_major_incident_needs_two_of_five_indicators():
    base = {"kind": "outage", "details": {"minutes": 200, "subscribers": 100}, "affected_users": 10}
    m = incidents.major(base)
    assert m["flags"]["impact"] and m["flags"]["duration"] and m["major"]          # 3시간 넘는 핵심 장애 = 영향도·시간
    m = incidents.major({"kind": "data", "details": {}, "data_damage": True})
    assert m["count"] == 1 and not m["major"]
    assert incidents.major({"kind": "data", "details": {}, "data_damage": True, "loss_amount": 200_000_000})["major"]


def test_report_deadline_reminders_warn_then_escalate_when_overdue():
    iid = incidents.open_manual("security", "의심 접근 — 보고 기한 시험", "sec-a")
    soon = clock.utc() + timedelta(hours=20)
    assert incidents.remind_deadlines(soon) == {"due": [iid]}
    assert db.query("SELECT level FROM alert_log WHERE key=:k", {"k": f"incident:due:{iid}:soon"})[0]["level"] == "warn"
    late = clock.utc() + timedelta(hours=25)
    incidents.remind_deadlines(late)
    assert db.query("SELECT level FROM alert_log WHERE key=:k", {"k": f"incident:due:{iid}:late"})[0]["level"] == "error"
    db.execute("UPDATE incident SET report_due = now() - interval '1 hour' WHERE id=:i", {"i": iid})
    assert assessment._run_probe("incident_reports")["status"] == "fail"
    incidents.update(iid, "sec-a", reported="최초")
    assert incidents.remind_deadlines(late) == {} and assessment._run_probe("incident_reports")["status"] == "ok"


def test_availability_coverage_and_probe():
    now = clock.utc()
    assert assessment._run_probe("uptime_slo")["status"] == "na"
    for m in range(200, 0, -1):
        incidents.record_check(OK if m % 50 else FAIL, at=now - timedelta(minutes=m))
    av = incidents.availability(30)
    assert av["checks"] == 200 and av["downtime_min"] == 4 and av["availability"] == 98.0 and not av["met"]
    assert av["coverage"] >= 99 and av["daily"]
    assert assessment._run_probe("uptime_slo")["status"] == "fail"
    db.execute("UPDATE uptime_check SET ok=true")
    assert assessment._run_probe("uptime_slo")["status"] == "ok"


def test_target_url_auto_off_and_probe(monkeypatch):
    monkeypatch.setattr(settings, "UPTIME_URL", "auto")
    monkeypatch.delenv("PORT", raising=False)
    assert incidents.target_url() == "http://127.0.0.1:8000/health"
    monkeypatch.setenv("PORT", "8123")
    assert incidents.target_url() == "http://127.0.0.1:8123/health"
    monkeypatch.setattr(settings, "UPTIME_URL", "off")
    assert incidents.target_url() == "" and incidents.run_check() == {}
    import httpx

    class _Resp:
        status_code = 503

    class _Client:
        def __init__(self, **kw):
            assert kw.get("trust_env") is False                    # 내부 호출 — 프록시(egress)를 거치지 않는다
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            return _Resp()
    monkeypatch.setattr(httpx, "Client", _Client)
    r = incidents.probe("http://127.0.0.1:8123/health")
    assert r["ok"] is False and r["code"] == 503


def _admin(name, monkeypatch, clock_box, role):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {name})
    auth.register(name, PW)
    h = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]}
    rbac.bootstrap()
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    client.post("/auth/mfa/confirm", headers=h, json={"code": totp.code_at(s, totp.current_step())})
    clock_box[0] += 30
    r = client.post("/admin/elevate", headers=h, json={"role": role, "reason": "장애 대응 당번"})
    assert r.status_code == 200, r.text
    return h


def test_admin_incident_api_roles_and_report_draft(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    sec = _admin("inc-sec", monkeypatch, clock_box, "security_admin")
    aud = _admin("inc-aud", monkeypatch, clock_box, "auditor")
    body = {"kind": "data", "title": "시세 적재 오류로 등락률 잘못 표시", "started_at": "2026-10-30T09:00:00"}
    assert client.post("/admin/incidents", headers=aud, json=body).status_code == 403     # 감사 역할은 조회만
    r = client.post("/admin/incidents", headers=sec, json=body)
    assert r.status_code == 200, r.text
    inc = r.json()
    assert inc["kind"] == "data"
    assert clock.parse_utc(inc["started_at"]) == datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)   # 시간대 없는 시각 = 한국시간
    ov = client.get("/admin/incidents", headers=aud).json()
    assert [i["id"] for i in ov["open"]] == [inc["id"]] and ov["rules"]["report_min"] == 30 and "availability" in ov
    r = client.post(f"/admin/incidents/{inc['id']}", headers=sec,
                    json={"cause": "KRX 응답 형식 변경", "reportable": True, "ended_at": "2026-10-30T10:30:00"})
    assert r.status_code == 200 and r.json()["reportable"] and r.json()["status"] == "resolved"
    assert r.json()["details"]["minutes"] == 90.0
    md = client.get(f"/admin/incidents/{inc['id']}?fmt=md&stage=중간", headers=aud)
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "| 보고형태 | 중간 |" in md.text and "KRX 응답 형식 변경" in md.text
    assert client.post(f"/admin/incidents/{inc['id']}", headers=sec, json={}).status_code == 400
    assert client.post(f"/admin/incidents/{inc['id']}", headers=sec, json={"reported": "끝"}).status_code == 422
    assert client.get("/admin/incidents/999", headers=aud).status_code == 404
    plain_name = "inc-plain"
    auth.register(plain_name, PW)
    plain = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": plain_name, "password": PW}).json()["token"]}
    assert client.get("/admin/incidents", headers=plain).status_code == 403


@pytest.mark.skipif(not os.getenv("TEST_APP_DATABASE_URL"), reason="최소 권한 모드에서만")
def test_retention_purge_cannot_delete_incidents_with_the_app_account():
    from app.services import privacy
    iid = incidents.open_manual("other", "보관 기간 시험용 사건", "ops")
    db.execute("UPDATE incident SET status='closed', started_at = now() - interval '6 years' WHERE id=:i", {"i": iid})
    out = privacy.retention_purge()
    assert out["incident"] == "skipped(권한)" and incidents.get(iid)     # 사고 기록 파기는 소유자 배치로만
    assert isinstance(out["uptime_check"], int)


def test_alert_board_lists_incident_alerts():
    iid = incidents.open_manual("security", "보드 표시 시험 사건", "sec-a")
    b = alerts.board(10)
    assert any(a["key"] == f"incident:report:{iid}" for a in b["alerts"]) and "stats" in b


def test_dr_rto_over_target_fails_the_drill_probe():
    import json
    from app.services import monitoring
    now = clock.utc().isoformat()
    db.set_meta("backup_last", json.dumps({"status": "ok", "last_ok_at": now, "encrypted": True}))
    db.set_meta("dr_drill_last", json.dumps({"ok": True, "at": now, "rto_seconds": 95.0}))
    assert monitoring.dr_status()["issues"] == [] and assessment._run_probe("dr_drill_recent")["status"] == "ok"
    db.set_meta("dr_drill_last", json.dumps({"ok": True, "at": now, "rto_seconds": settings.DR_RTO_TARGET_MIN * 60 + 1}))
    assert "dr_rto_exceeded" in monitoring.dr_status()["issues"]                  # 복구는 됐지만 목표(3시간)보다 오래 걸렸다
    assert assessment._run_probe("dr_drill_recent")["status"] == "fail"
