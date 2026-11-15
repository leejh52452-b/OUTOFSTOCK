"""재무 건전성 스코어 — 수익성·안정성 비율 → 0~100 종합 + 등급(신용분석 축약) 검증."""
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import soundness


def _seed_fin(ticker, revenue, op, net, debt, year=2025, rc="11011"):
    db.execute("INSERT INTO financial_statement"
               "(ticker, fiscal_year, report_code, revenue, operating_profit, net_income, debt_ratio) "
               "VALUES(:t, :y, :rc, :rev, :op, :net, :d)",
               {"t": ticker, "y": year, "rc": rc, "rev": revenue, "op": op, "net": net, "d": debt})


def test_strong_company_scores_high():
    _seed_fin("005930", revenue=1000, op=200, net=150, debt=50)   # 영업20%·순15%·부채50%
    s = soundness.soundness("005930")
    assert s["ok"]
    assert s["ratios"]["operating_margin"] == 20.0
    assert s["score"] == 100.0
    assert s["grade"] == "안정"


def test_weak_company_scores_low():
    _seed_fin("005930", revenue=1000, op=0, net=0, debt=200)      # 무수익·고부채
    s = soundness.soundness("005930")
    assert s["score"] == 0.0
    assert s["grade"] == "주의"


def test_mid_company_is_ordinary():
    _seed_fin("005930", revenue=1000, op=100, net=75, debt=125)   # 영업10%·순7.5%·부채125%
    s = soundness.soundness("005930")
    assert s["score"] == 50.0
    assert s["grade"] == "보통"


def test_latest_year_used():
    _seed_fin("005930", revenue=1000, op=0, net=0, debt=300, year=2023)
    _seed_fin("005930", revenue=1000, op=200, net=150, debt=50, year=2025)   # 최신 = 우수
    s = soundness.soundness("005930")
    assert s["grade"] == "안정"                # 최신 연도(2025) 기준


def test_no_financials():
    assert soundness.soundness("999999")["ok"] is False


def test_soundness_api():
    _seed_fin("005930", revenue=1000, op=180, net=120, debt=80)
    r = TestClient(app).get("/stocks/005930/soundness")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "score" in body and "grade" in body


# ── 부실예측 Altman Z''(정책금융 여신심사의 축약) ──
def test_dart_parses_balance_sheet_items_for_altman():
    from app.collectors import dart
    rows = [{"account_nm": n, "fs_div": "CFS", "thstrm_amount": v, "rcept_no": "20260310000123"} for n, v in [
        ("매출액", "1,000"), ("영업이익", "120"), ("당기순이익", "90"), ("자산총계", "2,000"), ("유동자산", "900"),
        ("유동부채", "500"), ("부채총계", "800"), ("자본총계", "1,200"), ("이익잉여금", "700")]]
    f = dart.parse_financials({"status": "000", "list": rows})
    assert f["total_assets"] == 2000 and f["current_assets"] == 900 and f["current_liabilities"] == 500
    assert f["total_liabilities"] == 800 and f["retained_earnings"] == 700 and f["disclosed_at"] == "2026-03-10"
    deficit = dart.parse_financials({"status": "000", "list": [{"account_nm": "결손금", "fs_div": "CFS",
                                                                 "thstrm_amount": "300", "rcept_no": ""}]})
    assert deficit["retained_earnings"] == -300                                   # 결손금은 음수로


def test_altman_z_zones():
    safe = {"total_assets": 2000, "current_assets": 900, "current_liabilities": 500, "total_liabilities": 800,
            "retained_earnings": 700, "operating_profit": 120, "total_equity": 1200}
    z = soundness.altman_z(safe)
    # 6.56*0.2 + 3.26*0.35 + 6.72*0.06 + 1.05*1.5 = 1.312 + 1.141 + 0.4032 + 1.575
    assert z["z"] == 4.43 and z["zone"] == "안전"
    weak = {**safe, "current_assets": 300, "retained_earnings": -600, "operating_profit": -50,
            "total_equity": 200, "total_liabilities": 1800}
    assert soundness.altman_z(weak)["zone"] == "부실 위험"
    assert soundness.altman_z(safe, "KB금융")["z"] is None                       # 금융업 제외
    assert soundness.altman_z({"operating_profit": 1})["z"] is None              # 항목 부족


def test_soundness_api_includes_distress():
    db.execute("INSERT INTO financial_statement(ticker, fiscal_year, report_code, revenue, operating_profit, "
               "net_income, total_equity, debt_ratio, total_assets, current_assets, current_liabilities, "
               "total_liabilities, retained_earnings) VALUES('005930', 2025, '11011', 1000, 120, 90, 1200, 66.7, "
               "2000, 900, 500, 800, 700)")
    j = TestClient(app).get("/stocks/005930/soundness").json()
    assert j["ok"] and j["distress"]["zone"] == "안전" and j["score"] is not None
