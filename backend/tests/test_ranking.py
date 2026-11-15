"""랭킹(거래대금·거래량·시총·상승·하락 상위) — 시세 적재(marketgen) 때 같은 트랜잭션에서 만든다.
(예전 레거시 FDR 배치의 ranking.py 는 삭제 — 실데이터·데모 모두 marketgen 이 랭킹을 만든다)"""
from app import db
from app.collectors import marketgen


def _seed():
    # (ticker, 어제 종가, 오늘 종가, 오늘 거래량, 시총)
    rows = [("005930", 70000, 71000, 15_200_000, 400_000_000),
            ("000660", 180000, 178000, 8_400_000, 120_000_000),
            ("035420", 190000, 195000, 1_100_000, 30_000_000),
            ("035720", 52500, 52000, 3_900_000, 22_000_000),
            ("005380", 239000, 240000, 900_000, 50_000_000),
            ("051910", 391000, 380000, 650_000, 26_000_000)]
    for tk, prev, close, vol, mc in rows:
        for d, c in (("2026-09-02", prev), ("2026-09-03", close)):
            db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES(:t,:d,:c,:c,:c,:c,:v)",
                       {"t": tk, "d": d, "c": c, "v": vol})
        db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
                   "VALUES(:t,'2026-09-03',:c,:v,:r,:m)",
                   {"t": tk, "c": close, "v": vol, "m": mc, "r": round((close / prev - 1) * 100, 2)})
    with db.transaction() as conn:
        marketgen._build_rankings(conn)


def _top(kind):
    return [r["ticker"] for r in db.query("SELECT ticker FROM ranking WHERE ranking_type=:k ORDER BY rank",
                                          {"k": kind})]


def test_volume_and_marcap_order():
    _seed()
    assert _top("volume")[:2] == ["005930", "000660"]
    assert _top("marcap")[:2] == ["005930", "000660"]


def test_rise_and_fall_order():
    _seed()
    assert _top("rise")[0] == "035420"          # +2.6%
    assert _top("fall")[0] == "051910"          # −2.8%


def test_split_day_uses_official_change_rate_not_close_ratio():
    """액면분할(1:5)일 — 종가는 −80% 지만 KRX 공식 등락률은 +1.2%. 종가로 계산하면 '하락 1위'로 잘못 올라온다."""
    _seed()
    db.execute("INSERT INTO stock(ticker,name,market) VALUES('950999','분할회사','KOSPI')")
    for d, c in (("2026-09-02", 100000), ("2026-09-03", 20240)):
        db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES('950999',:d,:c,:c,:c,:c,1000)",
                   {"d": d, "c": c})
    db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
               "VALUES('950999','2026-09-03',20240,1000,1.2,10000000)")
    with db.transaction() as conn:
        marketgen._build_rankings(conn)
    assert _top("fall")[0] == "051910"
    v = db.query("SELECT metric_value FROM ranking WHERE ranking_type='rise' AND ticker='950999'")
    assert v and abs(float(v[0]["metric_value"]) - 1.2) < 1e-9
