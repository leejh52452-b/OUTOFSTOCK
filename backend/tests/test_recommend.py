from app import db
from app.services.recommend import build_recommendations


def _seed_financials():
    rows = [("005930", 300000000, 18000000, 15000000, 25), ("000660", 50000000, 2500000, 2000000, 40),
            ("035420", 10000000, 1500000, 1200000, 45), ("035720", 8000000, 1040000, 640000, 50),
            ("005380", 160000000, 12800000, 9600000, 170), ("051910", 50000000, 2000000, 1500000, 90)]
    for tk, rev, op, net, debt in rows:
        db.execute("INSERT INTO financial_statement"
                   "(ticker, fiscal_year, report_code, revenue, operating_profit, net_income, debt_ratio) "
                   "VALUES(:t, 2025, '11011', :rev, :op, :net, :debt)",
                   {"t": tk, "rev": rev, "op": op, "net": net, "debt": debt})


def test_recommendation_is_intuitive():
    _seed_financials()
    build_recommendations(top_k=3)
    naver = db.query("SELECT similar_ticker FROM recommendation WHERE base_ticker='035420' ORDER BY rank")
    assert naver[0]["similar_ticker"] == "035720"   # NAVER ~ 카카오 (고마진 테크)
    samsung = db.query("SELECT similar_ticker FROM recommendation WHERE base_ticker='005930' ORDER BY rank")
    assert samsung[0]["similar_ticker"] == "000660"  # 삼성 ~ SK하이닉스 (반도체)


def test_peers_endpoint_returns_ratios_side_by_side():
    """비교 기업(Peer) 분석 — 재무 유사도 상위 3곳과 이 종목의 영업이익률·순이익률·부채비율을 나란히."""
    from fastapi.testclient import TestClient
    from app.api import app
    _seed_financials()
    build_recommendations(top_k=3)
    d = TestClient(app).get("/stocks/035420/peers").json()
    assert d["available"] is True
    assert d["base"]["ticker"] == "035420" and d["base"]["name"] == "NAVER"
    assert d["base"]["op_margin"] == 15.0 and d["base"]["net_margin"] == 12.0 and d["base"]["debt_ratio"] == 45
    assert d["base"]["fiscal_year"] == 2025
    assert [p["rank"] for p in d["peers"]] == [1, 2, 3]
    assert d["peers"][0]["ticker"] == "035720" and d["peers"][0]["op_margin"] == 13.0
    assert all(p["ticker"] != "035420" for p in d["peers"])
    assert d["method"] and d["note"]
    assert d["source"] == "sample"                     # 시세 출처 기록 없음(테스트 DB) → 표본 취급


def test_peers_marks_real_financials_as_dart():
    from app.services import recommend
    _seed_financials()
    build_recommendations(top_k=3)
    db.set_meta("market_source", "krx")
    assert recommend.peers("005930")["source"] == "dart"


def test_peers_uses_latest_fiscal_year_and_reports_missing_data():
    from app.services import recommend
    _seed_financials()
    db.execute("INSERT INTO financial_statement(ticker, fiscal_year, report_code, revenue, operating_profit, "
               "net_income, debt_ratio) VALUES('035420', 2024, '11011', 9000000, 900000, 600000, 60)")
    build_recommendations(top_k=3)
    assert recommend.peers("035420")["base"]["fiscal_year"] == 2025      # 최신 회계연도
    gone = recommend.peers("999999")
    assert gone["available"] is False and gone["reason"]
