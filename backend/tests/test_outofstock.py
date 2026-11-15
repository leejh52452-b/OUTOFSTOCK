"""OUTOFSTOCK 신규 화면 백엔드 — 지수·추천상품·학습·종목순위·종목상세."""
from fastapi.testclient import TestClient
from app.api import app
from app.collectors import marketgen

client = TestClient(app)


def _seed_market():
    marketgen.seed_market(real=False)


def test_indices():
    _seed_market()
    j = client.get("/indices").json()
    codes = {i["code"] for i in j["items"]}
    assert {"KOSPI", "KOSDAQ", "USDKRW", "BOND3Y"} <= codes


def test_products_by_profile():
    for prof in ["안정형", "중립형", "공격형"]:
        j = client.get("/reco/products?profile=" + prof).json()
        assert j["profile"] == prof and len(j["items"]) == 3
        assert all(p["risk"] in ("저위험", "중위험", "고위험") and p["grade"] for p in j["items"])


def test_market_stocks_sort():
    _seed_market()
    rise = client.get("/market/stocks?sort=rise").json()["items"]
    # 상승률 내림차순
    assert rise[0]["change_rate"] >= rise[-1]["change_rate"]
    val = client.get("/market/stocks?sort=value").json()["items"]
    assert val[0]["value"] >= val[-1]["value"]
    assert "rank" in val[0]


def test_krx_topn_universe(monkeypatch):
    """UNIVERSE_TOP_N 을 따라 시총 상위 N + 실습 종목이 유니버스가 되는지(KRX 모킹)."""
    from app.collectors import krx, marketgen
    from app import config
    from app.services import catalog, market

    def fake_day(_bas):
        out = {f"{i:06d}": {"name": f"종목{i}", "market": "KOSPI", "open": 1000 + i, "high": 1100 + i,
                            "low": 900 + i, "close": 1000 + i, "volume": 1000 * i,
                            "value": (1000 + i) * 1000 * i, "mktcap": (20 - i) * 1_000_000_000_000,
                            "shares": 1000, "change_rate": 0.1} for i in range(1, 11)}
        out["005930"] = {"name": "삼성전자", "market": "KOSPI", "open": 71000, "high": 71500,
                         "low": 70000, "close": 71000, "volume": 9_000_000, "value": 1,
                         "mktcap": 400_000_000_000_000, "shares": 1, "change_rate": 0.5}
        return out

    monkeypatch.setattr(krx, "daily_stocks", fake_day)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(config.settings, "UNIVERSE_TOP_N", 4)
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")

    marketgen.seed_market(days=3, real=True)
    uni = catalog.universe()
    assert "005930" in uni                                     # 실습 종목은 항상 포함
    assert len([t for t in uni if t.startswith("00000")]) <= 4  # 상위 4만
    items = market.stock_list("marcap")["items"]
    assert items[0]["ticker"] == "005930"                      # 시총 최대


def test_detail_per_pbr_from_dart():
    """종목상세 PER/PBR 을 DART 재무(순이익·자본총계)+시총으로 산출하는지."""
    from app import db
    from app.services import detail
    _seed_market()
    db.execute("UPDATE daily_price SET market_cap=:m WHERE ticker='005930'",
               {"m": 400_000_000_000_000})
    db.execute("INSERT INTO financial_statement"
               "(ticker,fiscal_year,report_code,net_income,total_equity) "
               "VALUES('005930',2025,'11011',40000000000000,300000000000000)")
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] == round(f["market_cap"] / 40000000000000, 2)   # 시총/순이익
    assert f["pbr"] == round(f["market_cap"] / 300000000000000, 2)  # 시총/자본총계


def test_stock_detail_and_ohlc():
    _seed_market()
    d = client.get("/stocks/207940/detail").json()
    assert d["name"] == "삼성바이오로직스"
    f = d["fundamentals"]
    assert f["per"] > 0 and f["high_52w"] >= f["low_52w"]
    o = client.get("/stocks/207940/ohlc?period=3M&indicators=SMA20").json()
    assert len(o["series"]) > 0 and "SMA20" in o["overlays"]
    assert all(k in o["series"][0] for k in ("open", "high", "low", "close", "volume"))
