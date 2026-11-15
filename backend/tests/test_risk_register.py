"""자산·위험 등록부 — 자산 식별·중요도 → 위협 시나리오 → 리스크 측정 → 처리·검증(app/security/assets.py · riskreg.py).
위협 모델의 '확인' 열에 적힌 시험이 실제로 있는지까지 본다 — 문서가 '시험으로 막았다'고 적었는데 시험이 없으면 실패."""
import copy
import os

import pytest
from sqlalchemy import create_engine, text

from app import db
from app.security import assets, classification, egress, pdp, riskreg


def test_every_threat_is_scored_and_its_checks_exist():
    ev = riskreg.evaluate()
    assert ev["problems"] == [], ev["problems"]
    ids = [r["id"] for r in ev["rows"]]
    assert ids == [f"T-{n:02d}" for n in range(1, 43)]                         # 위협 모델 T-01~T-42 빠짐없이
    for r in ev["rows"]:
        assert r["residual_score"] <= r["inherent_score"]
        assert r["checks"]["tests"] or r["manual"], r["id"]
    assert ev["summary"]["tests"] >= 50 and ev["summary"]["above_appetite"] == []


def test_threat_model_checks_are_really_resolved():
    cell = ("`test_zero_trust.py::test_credential_stuffing…` · `test_jit_elevation…` · `test_ledger.py` · "
            "`test_nope.py::test_gone` · `test_not_a_real_test_name` · `docs/models/fds_eval.md` · `db_roles_check --strict` · A-1")
    r = riskreg.resolve_checks(cell)
    assert r["tests"] == ["test_zero_trust.py::test_credential_stuffing…", "test_zero_trust.py::test_jit_elevation…",
                          "test_ledger.py"]
    assert r["missing"] == ["test_nope.py::test_gone", "test_not_a_real_test_name"]
    assert r["other"] == ["docs/models/fds_eval.md", "db_roles_check --strict"]


def _with(monkeypatch, tid, **change):
    reg = copy.deepcopy(riskreg.load())
    reg["risks"][tid].update(change)
    monkeypatch.setattr(riskreg, "load", lambda path=None: reg)
    return reg


def test_high_residual_risk_needs_a_plan_or_an_acceptance(monkeypatch):
    _with(monkeypatch, "T-01", residual=[4, 4], plan="")                       # 잔여 16 > 위험 성향 9
    assert any("T-01: 잔여 16" in p for p in riskreg.evaluate()["problems"])
    _with(monkeypatch, "T-01", residual=[4, 4], plan="상위 WAF 도입", due="2027-03-31")
    assert riskreg.evaluate()["problems"] == []
    _with(monkeypatch, "T-01", residual=[4, 4], plan="", accepted_by="보안 관리자(위험 수용 결재)")
    assert riskreg.evaluate()["problems"] == []
    _with(monkeypatch, "T-02", residual=[5, 5])                                # 통제 후가 더 위험할 수는 없다
    assert any("T-02: 잔여 위험(25)이 고유 위험(8)보다 크다" in p for p in riskreg.evaluate()["problems"])
    _with(monkeypatch, "T-29", accepted_by="")                                 # 수용에는 승인자
    assert any("T-29: 수용은 승인자" in p for p in riskreg.evaluate()["problems"])


def test_review_dates_expire_into_the_runtime_probe():
    from datetime import date
    ev = riskreg.evaluate(today=date(2026, 11, 3))
    assert ev["overdue"] == []
    late = riskreg.evaluate(today=date(2027, 6, 1))                            # 180일 넘게 재검토하지 않음
    assert len(late["overdue"]) == 42 and late["rows"][0]["next_review"] == "2027-05-01"


def test_asset_inventory_covers_every_table_action_and_external_host():
    inv = assets.inventory()
    assert {d["name"] for d in inv["data"]} == set(classification.REGISTRY)
    assert {s["action"] for s in inv["services"]} == set(pdp.load()["actions"])
    assert {e["host"] for e in inv["external"]} == set(egress.ALLOWED) and all(e["supplier"] for e in inv["external"])
    assert inv["software"]["packages"] >= 40 and inv["software"]["hashes"] >= inv["software"]["packages"]
    for group in (assets.MONEY, assets.AUTH, assets.EVIDENCE, assets.OPS_LOW, assets.ONLINE):
        assert group <= set(classification.REGISTRY), group - set(classification.REGISTRY)   # 묶음에 오타·폐기된 표 없음


def test_criticality_follows_the_cia_rule():
    assert (assets.grade(3, 3, 3), assets.grade(3, 3, 2), assets.grade(3, 2, 2), assets.grade(2, 2, 2),
            assets.grade(1, 2, 2), assets.grade(2, 1, 1)) == (1, 1, 2, 2, 3, 3)
    g = {d["name"]: d for d in assets.data_assets()}
    assert g["wallet"]["grade"] == 1 and (g["wallet"]["c"], g["wallet"]["i"], g["wallet"]["a"]) == (3, 3, 3)
    assert g["user_mfa"]["grade"] == 1 and g["audit_log"]["grade"] == 1          # 인증 비밀·감사 체인은 핵심
    assert g["daily_price"]["grade"] == 3 and g["search_log"]["grade"] == 3       # 공개 시세·운영 기록은 일반
    assert g["watchlist"]["grade"] == 2                                            # 일반 개인 활동 기록
    comp = {c["key"]: c for c in assets.component_assets()}
    assert comp["db"]["zone"] == "제한" and comp["edge"]["zone"] == "DMZ" and comp["db"]["grade"] == 1


def test_every_supplier_row_answers_the_selection_criteria():
    text_ = assets.REGISTER_PATH.read_text(encoding="utf-8")
    assert all(f"| S{n} |" in text_ for n in range(1, 9))                    # 선정 기준·이용 요건 8개
    assert assets.supplier_problems() == []
    broken = text_.replace("| 대체 원천 없음 → 직전 재무 + 공시일 표시 | 공공 데이터 — 갱신만 멈추고",
                           "| — | 공공 데이터 — 갱신만 멈추고")
    assert broken != text_
    assert assets.supplier_problems(broken) == ["금융감독원 OpenDART: '대체·완화' 빈 칸(선정 기준)"]


def test_allowlist_and_register_never_drift_apart(monkeypatch):
    monkeypatch.setitem(egress.ALLOWED, "api.newvendor.example", "새 원천(등록부 누락 시험)")
    assert assets.supplier_problems() == ["허용 목록에만 있음(등록부 누락): api.newvendor.example"]
    monkeypatch.delitem(egress.ALLOWED, "api.newvendor.example")
    monkeypatch.delitem(egress.ALLOWED, "finnhub.io")
    assert assets.supplier_problems() == ["등록부에만 있음(허용 목록에 없음): finnhub.io"]


def test_asset_drift_sees_tables_outside_the_registry():
    assert assets.drift()["unclassified"] == [] and assets.drift()["missing"] == []
    url = os.getenv("TEST_DATABASE_URL") if os.getenv("TEST_APP_DATABASE_URL") else None
    eng = create_engine(url, future=True) if url else db.get_engine()          # 표 만들기는 소유자 계정만
    try:
        with eng.begin() as c:
            c.execute(text("CREATE TABLE zz_shadow_copy(id int)"))
        assert assets.drift()["unclassified"] == ["zz_shadow_copy"]            # 분류 없이 생긴 표(사본·임시 표) = 목록 밖 자산
    finally:
        with eng.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS zz_shadow_copy"))
        if url:
            eng.dispose()


@pytest.mark.parametrize("token", ["`test_query_budget.py::test_money_writes_stay_within_budget`",
                                   "`test_money_writes_stay_within_budget`"])
def test_resolver_accepts_both_reference_styles(token):
    assert riskreg.resolve_checks(token)["tests"] == ["test_query_budget.py::test_money_writes_stay_within_budget"]
