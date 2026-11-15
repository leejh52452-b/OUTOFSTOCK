"""해외 주식(미국) — 국내처럼 순위·검색·상세·차트·관심종목 + 장외 가격 규칙.
네트워크 없이: 표본 데이터(오프라인) + 실데이터 파서/적재는 픽스처·목(mock)으로 검증."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.collectors import edgar, finnhub, overseas_market, overseas_realtime
from app.services import overseas
from app.services import quotes as Q

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


# ── 화면(표본 데이터) ─────────────────────────────────────
def test_lazy_sample_seed_and_all_sort_keys():
    d = client.get("/overseas/list?sort=value&limit=100").json()
    assert d["source"] == "gen" and len(d["items"]) == len(overseas.US_STOCKS)
    for key, field, desc in [("value", "value", True), ("volume", "volume", True),
                             ("marcap", "market_cap", True), ("rise", "change_rate", True),
                             ("fall", "change_rate", False)]:
        xs = [r[field] for r in client.get(f"/overseas/list?sort={key}&limit=100").json()["items"]]
        assert xs == sorted(xs, reverse=desc), key
    r = d["items"][0]
    assert r["rank"] == 1 and r["name_ko"] and r["exchange"] in ("NASDAQ", "NYSE")
    assert r["price_krw"] > r["price"] and d["status"]["phase"] in ("pre", "open", "after", "closed", "weekend")


def test_search_korean_english_symbol():
    from app.services import overseas as ov
    assert ov.search("애플")["items"][0]["symbol"] == "AAPL"
    assert ov.search("nvidia")["items"][0]["symbol"] == "NVDA"
    assert ov.search("tsla")["items"][0]["symbol"] == "TSLA"
    assert ov.search("")["items"] == []


def test_detail_has_domestic_style_fields():
    d = client.get("/overseas/aapl/detail").json()
    f = d["fundamentals"]
    for k in ("open", "high", "low", "volume", "value", "market_cap", "per", "pbr", "high_52w",
              "low_52w", "prev_close", "revenue", "net_income", "assets", "equity"):
        assert k in f, k
    assert d["symbol"] == "AAPL" and d["name_ko"] == "애플" and d["financials_source"] == "gen"
    assert f["per"] and f["pbr"] and f["high_52w"] >= f["low_52w"]
    assert client.get("/overseas/ZZZZ/detail").status_code == 404


def test_ohlc_indicators_and_last_candle_matches_price():
    price = client.get("/overseas/NVDA/detail").json()["price"]
    o = client.get("/overseas/NVDA/ohlc?period=3M&indicators=SMA20,RSI14").json()
    assert len(o["series"]) == 63 and set(o["overlays"]) == {"SMA20", "RSI14"}
    assert abs(o["series"][-1]["close"] - price) < 1e-6


def test_after_hours_quote_newer_session_is_used():
    """Finnhub 시세가 최신 봉보다 뒤 거래일(장 마감 후 수신) → 그 가격이 '직전 종가', 차트 마지막 캔들도 반영."""
    overseas.ensure_data()
    last = db.query("SELECT trade_date, close FROM overseas_ohlc WHERE symbol='AAPL' "
                    "ORDER BY trade_date DESC LIMIT 1")[0]
    nxt = (date.fromisoformat(last["trade_date"]) + timedelta(days=1)).isoformat()
    overseas_market.save_quote("AAPL", {"price": 300.0, "change": 300.0 - last["close"], "change_rate": 1.5,
                                        "prev_close": last["close"], "open": 290.0, "high": 301.0,
                                        "low": 289.0, "t": None}, session_date=nxt)
    db.execute("UPDATE overseas_quote SET ts=:t WHERE symbol='AAPL'",
               {"t": (Q.now_kst() - timedelta(hours=3)).isoformat()})
    d = client.get("/overseas/AAPL/detail").json()
    assert d["price"] == 300.0 and d["status"] == "close" and d["session_date"] == nxt
    s = client.get("/overseas/AAPL/ohlc?period=1M").json()["series"]
    assert s[-1]["trade_date"] == nxt and s[-1]["close"] == 300.0


def test_indices_differ_and_forex():
    items = {i["code"]: i for i in client.get("/overseas/indices").json()["items"]}
    assert {"SPX", "NASDAQ", "DOW"} <= set(items)
    assert len({items[c]["change_rate"] for c in ("SPX", "NASDAQ", "DOW")}) >= 2


def test_watchlist_supports_us_symbols():
    h = _auth("uswl@oos.io")
    assert client.post("/watchlist/AAPL", headers=h).json()["ok"]
    assert client.post("/watchlist/NOPE", headers=h).status_code == 404        # 없는 심볼은 404
    items = client.get("/watchlist", headers=h).json()["items"]
    assert items[0]["ticker"] == "AAPL" and items[0]["overseas"] and items[0]["currency"] == "USD"


# ── 실데이터 파서(픽스처) ──────────────────────────────────
def test_fdr_frame_to_bars_skips_nan():
    idx = pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23"])
    df = pd.DataFrame({"Open": [1.0, 2.0, float("nan")], "High": [1.5, 2.5, 3.5], "Low": [0.5, 1.5, 2.5],
                       "Close": [1.2, float("nan"), 3.1], "Volume": [100, 200, float("nan")]}, index=idx)
    bars = overseas_market.frame_to_bars(df)
    assert [b["trade_date"] for b in bars] == ["2026-09-21", "2026-09-23"]
    assert bars[1]["open"] == 3.1 and bars[1]["volume"] == 0


def test_finnhub_parse_quote():
    q = finnhub.parse_quote({"c": 255.3, "d": 1.2, "dp": 0.47, "h": 256, "l": 250, "o": 251,
                             "pc": 254.1, "t": 1790000000})
    assert q["price"] == 255.3 and q["prev_close"] == 254.1 and q["high"] == 256 and q["t"] == 1790000000
    assert finnhub.parse_quote({"c": 0}) is None


def test_edgar_parsers():
    facts = {"entityName": "Apple Inc.", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"val": 1, "form": "10-K", "fp": "FY", "fy": 2024, "end": "2024-09-28"},
            {"val": 2, "form": "10-K", "fp": "FY", "fy": 2025, "end": "2025-09-27"},
            {"val": 9, "form": "10-Q", "fp": "Q1", "fy": 2026, "end": "2025-12-27"}]}},
        "NetIncomeLoss": {"units": {"USD": [{"val": 3, "form": "10-K", "fp": "FY", "fy": 2025, "end": "2025-09-27"}]}},
        "StockholdersEquity": {"units": {"USD": [{"val": 5, "form": "10-K", "fp": "FY", "fy": 2025,
                                                  "end": "2025-09-27"}]}}}}}
    f = edgar.parse_financials(facts)
    assert f["revenue"] == 2 and f["net_income"] == 3 and f["equity"] == 5 and f["fiscal_year"] == 2025
    shares = {"units": {"shares": [{"val": 999, "end": "2025-06-30", "accn": "a"},
                                   {"val": 60, "end": "2025-10-17", "accn": "b"},
                                   {"val": 40, "end": "2025-10-17", "accn": "b"}]}}
    assert edgar.parse_shares(shares) == 100                    # 최신 기준일, 클래스별 합산


def test_ws_message_parse():
    raw = ('{"type":"trade","data":[{"s":"AAPL","p":255.5,"t":1790000000000,"v":10},'
           '{"s":"NVDA","p":180.1,"t":1790000000001,"v":5}]}')
    assert [x[0] for x in overseas_realtime.parse_ws_message(raw)] == ["AAPL", "NVDA"]
    assert overseas_realtime.parse_ws_message('{"type":"ping"}') == []
    assert overseas_realtime.parse_ws_message("not json") == []


# ── 실데이터 적재(목) — 종목별 폴백·출처 표기 ───────────────
def test_seed_real_with_per_symbol_fallback(monkeypatch):
    def fake_fdr(sym, days=480, finished_only=True):
        if sym == "AAPL":
            return [{"trade_date": "2026-09-21", "open": 250, "high": 251, "low": 249, "close": 250.0, "volume": 10},
                    {"trade_date": "2026-09-22", "open": 251, "high": 256, "low": 250, "close": 255.0, "volume": 20}]
        if sym in ("US500", "IXIC", "DJI", "USD/KRW"):
            return [{"trade_date": "2026-09-21", "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 0},
                    {"trade_date": "2026-09-22", "open": 1, "high": 1, "low": 1, "close": 101.0, "volume": 0}]
        raise ConnectionError("blocked")
    monkeypatch.setattr(overseas_market, "fetch_fdr_bars", fake_fdr)
    monkeypatch.setattr(edgar, "shares_outstanding", lambda s: 15_000_000_000 if s == "AAPL" else None)
    monkeypatch.setattr(edgar, "financials", lambda s: {"entity": "Apple", "currency": "USD", "revenue": 400,
                                                         "net_income": 100, "assets": 900, "equity": 80,
                                                         "fiscal_year": 2025, "period_end": "2025-09-27"})
    monkeypatch.setattr(overseas_market.time, "sleep", lambda *_: None)
    monkeypatch.setattr(settings, "OVERSEAS_SOURCE", "auto")
    out = overseas_market.seed_overseas(real=True)
    assert out["real"] == 1 and out["sample"] == len(overseas.US_STOCKS) - 1
    assert out["source"] == "mixed" and "warning" in out and out["indices"] == "fdr" and out["fx"] == "fdr"
    d = client.get("/overseas/AAPL/detail").json()
    assert d["price"] == 255.0 and d["source"] == "fdr" and d["financials_source"] == "edgar"
    assert d["fundamentals"]["market_cap"] == int(255.0 * 15_000_000_000)
    assert db.query("SELECT COUNT(*) AS n FROM overseas_ohlc WHERE symbol='AAPL'")[0]["n"] == 2   # 표본 봉 제거·교체


def test_sim_quotes_tagged_and_shown_as_sim():
    overseas_realtime.run_sim(interval=0, max_steps=1)
    assert {r["source"] for r in db.query("SELECT source FROM overseas_quote")} == {"sim"}
    assert client.get("/overseas/AAPL/detail").json()["status"] == "sim"


def test_book_keeps_fresher_trade_over_poll(monkeypatch):
    saved = []
    monkeypatch.setattr(overseas_realtime, "save_quote",
                        lambda s, q, source="finnhub", session_date=None, **_k: saved.append((s, q["price"], session_date)))
    b = overseas_realtime._Book()
    b.write("AAPL", b.base("AAPL", {"price": 250.0, "prev_close": 248.0, "open": 249.0,
                                    "high": 251.0, "low": 247.0}, "2026-09-23"))
    ts = int(datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 11:00 EDT
    b.trade("AAPL", 252.5, ts)
    st = b.base("AAPL", {"price": 251.0, "prev_close": 248.0, "open": 249.0, "high": 251.5, "low": 247.0},
                "2026-09-23")                                   # 폴링이 더 오래된 값을 가져와도
    assert st["price"] == 252.5 and st["high"] == 252.5       # 체결가 유지, 고가는 max(폴링, 체결) 병합
    b.trade("AAPL", 253.0, ts + 1000)
    assert b.flush() == 1 and saved[-1] == ("AAPL", 253.0, "2026-09-23")


def test_unfinished_us_bar_dropped_before_close():
    """한국시간 23:00(뉴욕 오전 10시)에 받은 '오늘' 봉은 진행 중 → 저장하지 않는다. 장 마감 후엔 유지."""
    from datetime import datetime, timezone
    bars = [{"trade_date": "2026-09-23", "close": 1.0}, {"trade_date": "2026-09-24", "close": 2.0}]
    during = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)      # 뉴욕 10:00(EDT) 장중
    after = datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)       # 뉴욕 17:00 장 마감 후
    assert [b["trade_date"] for b in overseas_market.drop_unfinished(bars, during)] == ["2026-09-23"]
    assert len(overseas_market.drop_unfinished(bars, after)) == 2
    old = [{"trade_date": "2026-09-23", "close": 1.0}]                  # 오늘 봉이 없으면 그대로
    assert overseas_market.drop_unfinished(old, during) == old
