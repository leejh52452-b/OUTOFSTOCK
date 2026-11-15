"""변경관리(프로그램 통제) — 금감원 IT 내부통제(배포 전 영향 분석·제3자 검증)를 배포 게이트로(app/services/changes.py).
깨진 설치본은 어느 모드든 서비스를 멈추기 전에 막히고, 승인은 다른 사람만, 돈·인증·스키마 변경은 검증 근거가 있어야 한다."""
import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.config import settings
from app.security import drill, rbac, totp
from app.services import auth, changes

client = TestClient(app)
PW = "Change-pass-2026"
KST = timezone(timedelta(hours=9))


def _installer(tmp_path, name="good", broken=False):
    out = tmp_path / f"{name}.sh"
    changes.build_installer(drill._mini_release(tmp_path / name, broken=broken), out)
    return out


@pytest.fixture
def no_freeze(monkeypatch):
    monkeypatch.setattr(settings, "CHANGE_FREEZE", "none")


def test_installer_is_verified_and_extracted_safely(tmp_path):
    inst = _installer(tmp_path)
    n = changes.extract(inst, tmp_path / "x")
    assert n >= 3 and (tmp_path / "x" / "app" / "handler.py").is_file()
    raw = inst.read_text(encoding="utf-8")
    b64 = raw.split("__OOS_PAYLOAD_BEGIN__\n", 1)[1].split("__OOS_PAYLOAD_END__", 1)[0]
    blob = bytearray(base64.b64decode(b64))
    blob[len(blob) // 2] ^= 0xFF                                       # 전송 중 1바이트가 바뀐 설치본
    bad = tmp_path / "corrupt.sh"
    bad.write_text(raw.replace(b64, base64.encodebytes(bytes(blob)).decode()), encoding="utf-8")
    with pytest.raises(changes.ChangeError) as e:
        changes.extract(bad, tmp_path / "y")
    assert e.value.code == "corrupt"
    (tmp_path / "z.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    with pytest.raises(changes.ChangeError) as e:
        changes.extract(tmp_path / "z.sh", tmp_path / "z")
    assert e.value.code == "not_installer"


def test_gate_blocks_broken_release_even_in_record_mode(tmp_path, monkeypatch, no_freeze):
    bad = _installer(tmp_path, "bad", broken=True)
    for mode in ("record", "enforce"):
        monkeypatch.setattr(settings, "CHANGE_GATE", mode)
        g = changes.gate(bad, actor="ops")
        assert not g["allow"] and "파이썬 구문" in g["reason"], g          # 서비스를 멈추기 전에 중단
        assert any(c["check"] == "파이썬 구문" and not c["ok"] and "handler.py" in c["detail"] for c in g["prechecks"])
    g = changes.gate(bad, emergency_reason="장애 복구용 긴급 배포 — 원인 수정", actor="ops")
    assert not g["allow"]                                              # 긴급 변경도 깨진 설치본은 못 넘긴다
    monkeypatch.setattr(settings, "CHANGE_GATE", "off")
    assert changes.gate(bad)["allow"]                                  # off = 점검 자체를 끈 경우만(명시적 선택)
    assert db.query("SELECT COUNT(*) AS n FROM change_record")[0]["n"] == 0


def test_applied_migration_edit_is_a_blocking_precheck(tmp_path):
    rel = drill._mini_release(tmp_path / "mig")
    p = sorted((rel / "sql" / "migrations").glob("V*.sql"))[0]
    p.write_text(p.read_text(encoding="utf-8") + "\n-- 적용된 파일을 고침\n", encoding="utf-8")
    checks = {c["check"]: c for c in changes.precheck(rel)}
    assert not checks["적용된 마이그레이션 보존"]["ok"] and checks["적용된 마이그레이션 보존"]["blocking"]
    assert p.name in checks["적용된 마이그레이션 보존"]["detail"]
    assert checks["파이썬 구문"]["ok"] and checks["접근 정책 형식"]["ok"]


def test_approval_needs_a_second_person_and_evidence(tmp_path, monkeypatch, no_freeze):
    monkeypatch.setattr(settings, "CHANGE_GATE", "enforce")
    inst = _installer(tmp_path)
    r = changes.request(inst, "dev-a", "주문 화면 수정", "버그 수정 배포", evidence="")
    assert r["impact"]["risk"] == "high" and r["impact"]["needs_evidence"] and r["blocking"] == []
    with pytest.raises(changes.ChangeError) as e:
        changes.decide(r["id"], True, "dev-a")
    assert e.value.code == "four_eyes"                                 # 요청자 본인 승인 불가
    with pytest.raises(changes.ChangeError) as e:
        changes.decide(r["id"], True, "lead-b")
    assert e.value.code == "evidence_required"                         # 돈·인증·스키마 변경 — 검증 근거 없이는 승인 불가
    with pytest.raises(changes.ChangeError) as e:
        changes.request(inst, "dev-a", "주문 화면 수정", "같은 설치본 다시")
    assert e.value.code == "duplicate"
    assert changes.decide(r["id"], False, "lead-b", "시험 결과를 붙여 다시 요청")["status"] == "rejected"
    assert not changes.gate(inst, actor="ops")["allow"]                 # 반려된 설치본은 enforce 에서 못 나간다
    r2 = changes.request(inst, "dev-a", "주문 화면 수정", "버그 수정 배포", evidence="pytest 857 passed · 부하 시험 p95 354ms")
    assert changes.decide(r2["id"], True, "lead-b")["status"] == "approved"
    g = changes.gate(inst, actor="ops")
    assert g["allow"] and g["change_id"] == r2["id"] and g["warnings"] == []
    assert changes.applied(g["change_id"], "ops", baseline_id=None)["status"] == "applied"
    au = [a["action"] for a in db.query("SELECT action FROM audit_log WHERE target LIKE 'change:%' ORDER BY audit_id")]
    assert au == ["change.request", "change.rejected", "change.request", "change.approved", "change.applied"]
    tampered = tmp_path / "tampered.sh"                                 # 승인 뒤 설치본을 바꿔치기 — 해시가 달라 승인 무효
    tampered.write_bytes(inst.read_bytes() + "\n# 한 줄 추가\n".encode("utf-8"))
    assert not changes.gate(tampered, actor="ops")["allow"]


def test_freeze_window_and_emergency_change_needs_review(tmp_path, monkeypatch):
    wed10 = datetime(2026, 11, 4, 10, 0, tzinfo=KST)
    monkeypatch.setattr(settings, "CHANGE_FREEZE", "mon-fri 08:30-16:00")
    assert changes.in_freeze(wed10) and not changes.in_freeze(wed10.replace(hour=16))
    assert not changes.in_freeze(wed10.replace(hour=8, minute=29)) and not changes.in_freeze(datetime(2026, 11, 7, 10, tzinfo=KST))
    monkeypatch.setattr(settings, "CHANGE_FREEZE", "daily 00:00-24:00")      # 지금이 언제든 금지 시간
    monkeypatch.setattr(settings, "CHANGE_GATE", "enforce")
    inst = _installer(tmp_path)
    r = changes.request(inst, "dev-a", "긴급 수정", "로그인 장애 수정", evidence="재현 시험 통과")
    changes.decide(r["id"], True, "lead-b")
    g = changes.gate(inst, actor="ops")
    assert not g["allow"] and any("배포 금지 시간" in w for w in g["warnings"])   # 승인됐어도 장중에는 안 된다
    assert not changes.gate(inst, emergency_reason="짧음", actor="ops")["allow"]   # 긴급 사유는 10자 이상
    g = changes.gate(inst, emergency_reason="로그인 전면 장애 — 원인 수정 즉시 배포", actor="ops")
    assert g["allow"] and g["change_id"] != r["id"]
    row = db.query("SELECT kind, review_due, approved_by FROM change_record WHERE id=:i", {"i": g["change_id"]})[0]
    assert row["kind"] == "emergency" and row["approved_by"] is None
    assert timedelta(days=2) < row["review_due"] - clock.utc() <= timedelta(days=3)
    assert db.query("SELECT 1 FROM alert_log WHERE key=:k", {"k": f"change:emergency:{g['change_id']}"})
    changes.applied(g["change_id"], "ops")
    with pytest.raises(changes.ChangeError) as e:
        changes.review(g["change_id"], "ops", "배포한 사람이 스스로 검토함")
    assert e.value.code == "four_eyes"
    late = clock.utc() + timedelta(days=4)
    assert [x["id"] for x in changes.overdue_reviews(late)] == [g["change_id"]]
    assert changes.review(g["change_id"], "lead-b", "원인·영향 확인 — 정식 변경으로 인정")["reviewed"]
    assert changes.overdue_reviews(late) == []


def test_record_mode_lets_unapproved_release_through_but_records_it(tmp_path, monkeypatch, no_freeze):
    monkeypatch.setattr(settings, "CHANGE_GATE", "record")
    inst = _installer(tmp_path)
    g = changes.gate(inst, actor="ops")
    assert g["allow"] and g["mode"] == "record" and any("승인된 변경 요청이 없는" in w for w in g["warnings"])
    row = db.query("SELECT kind, status, release_sha256 FROM change_record WHERE id=:i", {"i": g["change_id"]})[0]
    assert row["kind"] == "unapproved" and row["status"] == "approved" and row["release_sha256"] == changes.sha256_file(inst)
    changes.applied(g["change_id"], "ops")
    from app.security import assessment
    assert assessment._run_probe("change_control")["status"] == "fail"     # 승인 없는 배포 = 통제 미작동(진단이 드러낸다)
    assert changes.rolled_back("ops", g["change_id"])["status"] == "rolled_back"
    assert changes.rolled_back("ops", g["change_id"]) is None               # 이미 되돌림 — 다시 바꾸지 않는다


def test_impact_analysis_groups_changed_files_by_area(tmp_path):
    root = tmp_path / "rel"
    (root / "app" / "services").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "app" / "services" / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    (root / "web" / "index.html").write_text("<p>hi</p>\n", encoding="utf-8")
    old = {"app/services/ledger.py": "0" * 64, "web/index.html": "1" * 64, "app/api.py": "2" * 64}
    im = changes.analyze(root, old)
    assert im["changed"] == 2 and im["removed"] == 1 and im["risk"] == "high" and im["needs_evidence"]
    assert set(im["areas"]) == {"돈(원장·송금·지갑·결제)", "화면", "그 밖의 서버 코드·배치"}
    im = changes.analyze(root, {"app/services/ledger.py": changes.sha256_file(root / "app/services/ledger.py"),
                                "web/index.html": "1" * 64})
    assert im["risk"] == "low" and not im["needs_evidence"] and im["performance_sensitive"] == []


@pytest.mark.skipif(not os.getenv("TEST_APP_DATABASE_URL"), reason="최소 권한 모드에서만(앱 계정 권한 확인)")
def test_app_account_cannot_delete_or_rewrite_change_records(tmp_path, monkeypatch, no_freeze):
    monkeypatch.setattr(settings, "CHANGE_GATE", "record")
    g = changes.gate(_installer(tmp_path), actor="ops")
    for sql in ("DELETE FROM change_record WHERE id=:i", "UPDATE change_record SET release_sha256=repeat('0', 64) WHERE id=:i",
                "UPDATE change_record SET prechecks='[]' WHERE id=:i", "DELETE FROM incident WHERE id=:i"):
        with pytest.raises(Exception) as e:
            db.execute(sql, {"i": g["change_id"]})
        assert getattr(getattr(e.value, "orig", None), "sqlstate", None) == "42501", sql
    assert changes.applied(g["change_id"], "ops")["status"] == "applied"   # 상태 칸은 바꿀 수 있다


def _admin(name, monkeypatch, clock_box, role):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {name})
    auth.register(name, PW)
    h = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]}
    rbac.bootstrap()
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    client.post("/auth/mfa/confirm", headers=h, json={"code": totp.code_at(s, totp.current_step())})
    clock_box[0] += 30
    r = client.post("/admin/elevate", headers=h, json={"role": role, "reason": "변경 승인 업무"})
    assert r.status_code == 200, r.text
    return h


def test_admin_change_api_enforces_four_eyes(tmp_path, monkeypatch, no_freeze):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    a = _admin("chg-a", monkeypatch, clock_box, "admin")
    b = _admin("chg-b", monkeypatch, clock_box, "security_admin")
    aud = _admin("chg-aud", monkeypatch, clock_box, "auditor")
    r = changes.request(_installer(tmp_path), "chg-a", "화면 문구 수정", "오탈자 수정", evidence="시험 통과")
    ov = client.get("/admin/changes", headers=aud).json()                     # 감사 역할은 조회만
    assert [c["id"] for c in ov["pending"]] == [r["id"]] and ov["mode"] == settings.CHANGE_GATE
    assert client.post(f"/admin/changes/{r['id']}/approve", headers=aud, json={}).status_code == 403
    x = client.post(f"/admin/changes/{r['id']}/approve", headers=a, json={"note": "내가 올린 것"})
    assert x.status_code == 403 and x.json()["code"] == "four_eyes"
    x = client.post(f"/admin/changes/{r['id']}/approve", headers=b, json={"note": "시험 결과 확인"})
    assert x.status_code == 200 and x.json()["status"] == "approved"
    assert client.post(f"/admin/changes/{r['id']}/approve", headers=b, json={}).status_code == 404
    assert client.post(f"/admin/changes/{r['id']}/merge", headers=b, json={}).status_code == 404
