"""Point-in-time(시점) 재무 + look-ahead bias 방지 검증.
핵심: 아직 공시되지 않은 미래 실적을 과거 시점 평가에 쓰지 않는다(bitemporal 데이터 거버넌스)."""

from app import db
from app.services import pit, soundness
from app.collectors import dart


def _seed(ticker, year, rev, op, net, debt, disclosed):
    db.execute("INSERT INTO financial_statement"
               "(ticker, fiscal_year, report_code, revenue, operating_profit, net_income, "
               " debt_ratio, disclosed_at, ingested_at) "
               "VALUES(:t, :y, '11011', :r, :o, :n, :d, :disc, :ing)",
               {"t": ticker, "y": year, "r": rev, "o": op, "n": net, "d": debt,
                "disc": disclosed, "ing": f"{disclosed}T10:00:00"})


def test_asof_excludes_not_yet_disclosed():
    _seed("005930", 2023, 1000, 200, 150, 60, "2024-03-15")
    _seed("005930", 2024, 1200, 250, 190, 55, "2025-03-15")
    # 2025-01-15 시점엔 2024 실적이 아직 공시 전 → 최신 known 은 2023
    f = pit.financials_asof("005930", "2025-01-15")
    assert f["fiscal_year"] == 2023
    # 2025-06-01 시점엔 2024 공시됨 → 2024
    assert pit.financials_asof("005930", "2025-06-01")["fiscal_year"] == 2024


def test_asof_before_any_disclosure_is_none():
    _seed("005930", 2023, 1000, 200, 150, 60, "2024-03-15")
    assert pit.financials_asof("005930", "2024-01-01") is None      # 공시 전엔 아무것도 모름


def test_default_asof_returns_latest_known():
    _seed("005930", 2023, 1000, 200, 150, 60, "2024-03-15")
    _seed("005930", 2024, 1200, 250, 190, 55, "2025-03-15")
    assert pit.financials_asof("005930")["fiscal_year"] == 2024     # 오늘 기준 둘 다 공시됨


def test_soundness_asof_is_point_in_time():
    _seed("005930", 2023, 1000, 50, 30, 300, "2024-03-15")          # 부실(고부채)
    _seed("005930", 2024, 1000, 250, 200, 40, "2025-03-15")         # 우량
    old = soundness.soundness("005930", as_of="2025-01-01")         # 2023만 알던 시점
    new = soundness.soundness("005930", as_of="2025-06-01")         # 2024 반영
    assert old["fiscal_year"] == 2023 and new["fiscal_year"] == 2024
    assert new["score"] > old["score"]                             # 시점에 따라 평가가 달라진다


def test_dart_parse_extracts_disclosed_at():
    data = {"status": "000", "list": [
        {"account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": "1,000", "rcept_no": "20250315000123"},
        {"account_nm": "부채총계", "fs_div": "CFS", "thstrm_amount": "500", "rcept_no": "20250315000123"},
        {"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": "1,000", "rcept_no": "20250315000123"},
    ]}
    p = dart.parse_financials(data)
    assert p["disclosed_at"] == "2025-03-15"                        # rcept_no 앞 8자리
    assert p["revenue"] == 1000 and p["debt_ratio"] == 50.0


