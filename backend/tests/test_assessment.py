"""금융보안 수준진단 자가진단 — 금융보안원 프레임워크(7개 분야 45개 항목) 카탈로그·근거 기계 확인·성숙도 판정·저장(app/security/assessment.py).
등급은 사람이 매기지 않고 근거 종류로 판정한다 — 근거가 사라지면 등급이 내려가고, 이 시험이 실패한다(증빙 무결성)."""
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import db
from app.api import app
from app.config import settings
from app.security import assessment, rbac, riskreg, totp
from app.services import auth

client = TestClient(app)
PW = "Assess-pass-2026"

# 금융보안원 「금융보안 수준진단 프레임워크」 v1.0 항목표(특강 자료 31쪽) — 7개 분야 45개 항목
FSI_ITEMS = {
    "1.1": "사이버보안 전략 수립 및 개선", "1.2": "경영진 참여 보장", "1.3": "법적 규제 및 요구사항 준수",
    "1.4": "역할 및 책임의 명확화", "1.5": "정보보호 자원(인력 및 예산) 확보", "1.6": "보안 정책 수립 및 관리",
    "1.7": "정보보호 교육 및 훈련", "1.8": "정보 노출 방지 및 업무 환경 보안",
    "2.1": "자산 식별 및 관리", "2.2": "자산 중요도 평가", "2.3": "자산의 생명주기에 따른 보안 관리", "2.4": "위험 평가",
    "2.5": "위험 처리",
    "3.1": "사용자 인증", "3.2": "사용자 권한 관리", "3.3": "물리적 접근통제", "3.4": "저장 데이터 보호", "3.5": "전송 데이터 보호",
    "3.6": "암호화 및 키 관리", "3.7": "백업 데이터 보호", "3.8": "로그 관리", "3.9": "악성코드 차단", "3.10": "자산 보안패치 관리",
    "3.11": "안전한 소프트웨어개발 생명주기 정의·설계", "3.12": "안전한 소프트웨어개발 생명주기 개발·테스트",
    "3.13": "네트워크 분리 및 접근통제", "3.14": "시스템 설정 관리", "3.15": "비인가 소프트웨어 설치 및 실행 방지",
    "3.16": "무선 네트워크 보호", "3.17": "원격 접속 보안 관리", "3.18": "단말기 보안", "3.19": "가용성 유지를 위한 복원력 확보",
    "3.20": "오픈소스 관리",
    "4.1": "네트워크 환경 모니터링", "4.2": "내부자 활동 모니터링", "4.3": "외부 연결구간 모니터링",
    "4.4": "소프트웨어 및 시스템 파일 모니터링", "4.5": "탐지된 이벤트 분석 및 사고 판단",
    "5.1": "사고대응대책 운영", "5.2": "보안사고 분석·관리", "5.3": "보안사고 조치", "5.4": "사고대응·복구 훈련 수행",
    "6.1": "복구 시 보안요건 반영",
    "7.1": "공급사 선정기준 및 계약 요건 정의", "7.2": "공급사 현황 관리",
}


def test_catalog_matches_the_fsi_framework_shape():
    cat = assessment.load_catalog()
    assert {i["id"]: i["name"] for i in cat["items"]} == FSI_ITEMS
    assert [(d["name"], d["items"]) for d in cat["domains"]] == [
        ("거버넌스", 8), ("식별", 5), ("보호", 20), ("탐지", 5), ("대응", 4), ("복구", 1), ("공급망", 2)]
    # NIST CSF 2.0 — 6개 기능 · 22개 범주 · 106개 하위 항목(특강 자료의 '6개 분야 106개 항목')
    assert len(assessment.CSF_CATEGORIES) == 22 and len(assessment.CSF_SUBCATEGORIES) == 106
    assert {s.split(".")[0] for s in assessment.CSF_SUBCATEGORIES} == set(assessment.CSF_FUNCTIONS)
    for it in cat["items"]:
        assert it["csf"] and all(s in assessment.CSF_SUBCATEGORIES for s in it["csf"]), it["id"]
        if it.get("excluded"):
            continue
        assert it["target"] in (1, 2, 3, 4) and it["evidence"], it["id"]
        if it.get("cap"):
            assert it["cap_reason"] and it["target"] <= it["cap"], it["id"]       # 상한보다 높은 목표는 모순


def test_catalog_errors_fail_closed(tmp_path):
    cat = json.loads(assessment.CATALOG_PATH.read_text(encoding="utf-8"))
    bad = dict(cat, items=[dict(cat["items"][0], csf=["GV.XX-99"])] + cat["items"][1:])
    p = tmp_path / "c.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(assessment.CatalogError, match="CSF 2.0"):
        assessment.load_catalog(p)
    p.write_text(json.dumps(dict(cat, items=cat["items"][:-1]), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(assessment.CatalogError, match="항목 수"):                 # 항목이 빠지면 진단을 시작하지 않는다
        assessment.load_catalog(p)


def test_every_evidence_reference_resolves():
    r = assessment.evaluate("static")
    assert r["broken"] == [], r["broken"]                 # 근거로 건 파일·함수·시험·규칙·작업이 모두 실제로 있다
    assert r["overall"]["in_scope"] == 42 and r["overall"]["excluded"] == 3
    assert r["csf"]["total"] == 106 and r["csf"]["covered"] >= 70


def test_levels_follow_the_evidence_rules():
    lv = assessment.level_of
    assert lv(set()) == 1 and lv({"doc"}) == 1 and lv({"code", "test", "auto"}) == 1        # 기준(문서)이 없으면 초기
    assert lv({"doc", "code"}) == 2 and lv({"doc", "record"}) == 2
    assert lv({"doc", "code", "test"}) == 2                                                # 시험만 있고 운영 감시가 없다
    assert lv({"doc", "code", "test", "probe"}) == 3 and lv({"doc", "test", "auto"}) == 3
    assert lv({"doc", "code", "test", "probe", "record"}) == 3                             # 자동 대응이 없으면 발전까지
    assert lv({"doc", "code", "test", "auto", "record"}) == 4


def _catalog_with(item_id, **change):
    cat = assessment.load_catalog()
    return dict(cat, items=[dict(i, **change) if i["id"] == item_id else i for i in cat["items"]])


def test_removing_evidence_lowers_the_level():
    before = {i["id"]: i["level"] for i in assessment.evaluate("static")["items"]}
    assert before["3.19"] == 4
    cat = assessment.load_catalog()
    ev = [e for e in next(i for i in cat["items"] if i["id"] == "3.19")["evidence"] if not e.startswith("record:")]
    ev.append("record:docs/perf/no_such_measurement.md")                  # 지워진 실측 기록을 가리키는 근거
    r = assessment.evaluate("static", catalog=_catalog_with("3.19", evidence=ev))
    item = next(i for i in r["items"] if i["id"] == "3.19")
    assert item["level"] == 3 and r["broken"] == [{"item": "3.19", "ref": "record:docs/perf/no_such_measurement.md",
                                                  "kind": "record", "ok": False, "detail": "파일 없음"}]
    no_tests = [e for e in ev[:-1] if not e.startswith("test:")] + ["test:tests/test_capacity.py::test_renamed_away"]
    r = assessment.evaluate("static", catalog=_catalog_with("3.19", evidence=no_tests))
    assert next(i for i in r["items"] if i["id"] == "3.19")["level"] == 2          # 시험 이름이 바뀌어 사라지면 '발전'도 아니다


def _all_ok(**override):
    res = {k: {"status": "ok", "detail": ""} for k in assessment.PROBES}
    res.update({k: {"status": v, "detail": "시험"} for k, v in override.items()})
    return res


def test_failing_probe_caps_the_item_at_defined():
    ok = assessment.evaluate("runtime", probe_results=_all_ok())
    bad = assessment.evaluate("runtime", probe_results=_all_ok(audit_chain="fail"))
    lv = lambda r: {i["id"]: i["level"] for i in r["items"]}                        # noqa: E731
    assert lv(ok)["3.8"] == 4 and lv(bad)["3.8"] == 2                    # 감사 체인이 깨졌으면 로그 관리는 '기반'
    assert lv(bad)["4.2"] == 2 and lv(bad)["5.2"] == 2
    assert lv(bad)["3.19"] == lv(ok)["3.19"] == 4                        # 상관없는 항목은 그대로
    assert bad["overall"]["probe_failures"] == 3
    na = assessment.evaluate("runtime", probe_results=_all_ok(backup_recent="na"))
    assert lv(na)["3.7"] == lv(ok)["3.7"]                                # 확인할 수 없음(개발 환경)은 내리지 않는다


def test_baseline_catalog_still_reproduces_the_baseline():
    """개선 전 기준선은 그때의 카탈로그로 다시 돌려도 같은 등급 — 근거가 지워지지 않았다는 증거(퇴보 없음)."""
    base = assessment.load_baseline()
    old = assessment.load_catalog(assessment.HISTORY_DIR / "2026-11-02_baseline_catalog.json")
    again = {i["id"]: i["level"] for i in assessment.evaluate("static", catalog=old)["items"]}
    assert again == {i["id"]: i["level"] for i in base["items"]}
    now = {i["id"]: i["level"] for i in assessment.evaluate("static")["items"]}
    assert all(now[k] >= v for k, v in again.items() if v is not None)          # 이번 개선으로 내려간 항목 없음
    assert assessment.evaluate("static")["overall"]["avg"] > base["overall"]["avg"]


def test_generated_docs_match_committed_files():
    r, base = assessment.evaluate("static"), assessment.load_baseline()
    assert assessment.DOC_PATH.read_text(encoding="utf-8") == assessment.render_markdown(r, base), \
        "python run.py assurance-docs 로 다시 생성"
    assert assessment.RADAR_PATH.read_text(encoding="utf-8") == assessment.render_radar_svg(r, base)
    assert riskreg.DOC_PATH.read_text(encoding="utf-8") == riskreg.render_markdown()


def _admin(name, monkeypatch, clock_box, role="security_admin"):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {name})
    auth.register(name, PW)
    h = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]}
    rbac.bootstrap()
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    client.post("/auth/mfa/confirm", headers=h, json={"code": totp.code_at(s, totp.current_step())})
    clock_box[0] += 30
    r = client.post("/admin/elevate", headers=h, json={"role": role, "reason": "분기 수준진단 수행"})
    assert r.status_code == 200, r.text
    return h


def _owner_exec(sql, params=None):
    """소유자 계정으로 실행(최소 권한 모드면 TEST_DATABASE_URL — 앱 계정은 못 하는 '내부자 변조' 재현)."""
    url = os.getenv("TEST_DATABASE_URL") if os.getenv("TEST_APP_DATABASE_URL") else None
    eng = create_engine(url, future=True) if url else db.get_engine()
    try:
        with eng.begin() as c:
            c.execute(text(sql), params or {})
    finally:
        if url:
            eng.dispose()


def test_admin_assessment_runs_stores_and_anchors_in_the_audit_chain(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    h = _admin("as-auditor", monkeypatch, clock_box, role="auditor")
    assert client.get("/admin/assessment", headers=h).json()["latest"] is None
    r = client.post("/admin/assessment/run", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"]["in_scope"] == 42 and len(body["items"]) == 45 and body["broken"] == []
    names = {p["name"] for i in body["items"] for p in i["probes"]}
    assert {"audit_chain", "policy", "rls_forced", "risk_register", "asset_drift", "egress_register"} <= names
    st = {p["name"]: p["status"] for i in body["items"] for p in i["probes"]}
    assert st["audit_chain"] == st["policy"] == st["rls_forced"] == st["risk_register"] == "ok"
    assert st["integrity"] == "na" and st["drill_recent"] == "na"            # 기준선·훈련 기록이 없는 시험 DB
    ov = client.get("/admin/assessment", headers=h).json()
    assert ov["latest"]["id"] == body["id"] and ov["latest"]["intact"] and not ov["stale"]
    one = client.get(f"/admin/assessment/{body['id']}", headers=h).json()
    assert one["intact"] and one["digest"] == body["digest"]
    au = db.query("SELECT actor, detail FROM audit_log WHERE action='assessment.run'")
    assert au[-1]["actor"] == "as-auditor" and body["digest"][:16] in au[-1]["detail"]
    risk = client.get("/admin/risk", headers=h).json()
    assert risk["register"]["problems"] == [] and risk["supplier_problems"] == [] and risk["drift"]["unclassified"] == []
    plain_name = "as-plain"
    auth.register(plain_name, PW)
    plain = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": plain_name, "password": PW}).json()["token"]}
    assert client.post("/admin/assessment/run", headers=plain).status_code == 403
    # 저장 뒤 결과를 고치면(소유자 권한을 가진 내부자) 다이제스트가 맞지 않는다 — 앱 계정은 고칠 권한 자체가 없다(V016)
    tamper = "UPDATE security_assessment SET result = replace(result, '\"mode\":\"runtime\"', '\"mode\":\"static\"') WHERE id=:i"
    if os.getenv("TEST_APP_DATABASE_URL"):
        with pytest.raises(Exception) as e:
            db.execute(tamper, {"i": body["id"]})
        assert getattr(getattr(e.value, "orig", None), "sqlstate", None) == "42501"
    _owner_exec(tamper, {"i": body["id"]})
    assert client.get(f"/admin/assessment/{body['id']}", headers=h).json()["intact"] is False


_REAL_EVALUATE = assessment.evaluate


def _fake_eval(lower=None):
    """정적 판정을 운영 결과처럼 쓰고, 한 항목만 등급을 낮춘다(퇴보 경보 시험용)."""
    def ev(mode="runtime", **_k):
        r = _REAL_EVALUATE("static")
        r["mode"] = mode
        for it in r["items"]:
            if it["id"] == lower:
                it["level"] -= 1
        return r
    return ev


def test_regression_since_last_run_is_alerted(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda key, title, msg="", **k: sent.append((key, title, msg)) or {"sent": []})
    monkeypatch.setattr(assessment, "evaluate", _fake_eval())
    first = assessment.run(actor="system")
    assert first["regressions"] == [] and not sent
    monkeypatch.setattr(assessment, "evaluate", _fake_eval(lower="3.8"))
    second = assessment.run(actor="system")
    assert [r["id"] for r in second["regressions"]] == ["3.8"]
    assert sent and sent[-1][0] == "assessment:regression" and "3.8" in sent[-1][2]
    assert [x["id"] for x in assessment.history()] == [second["id"], first["id"]]
    assert assessment.get(first["id"])["intact"] and assessment.get(second["id"])["intact"]
