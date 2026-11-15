"""현재가 규칙 회귀 테스트 — '장 마감 후 이상한 가격' 버그 방지.

규칙(quotes.effective): 신선한 틱 → LIVE · 틱 영업일 > 최신 일봉 → 틱 가격(그 날 종가) ·
                        같은 날 봉이 있고 틱이 식었으면 → 봉(공식 종가) · 그 외 → 최신 일봉 종가.
                        시뮬레이션 틱은 신선할 때만.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.api import app
from app.collectors import marketgen
from app.collectors.realtime import save_tick
from app.services import quotes as Q
from app.services import invest, wallet

client = TestClient(app)
KST = Q.KST
LAST = {"trade_date": "2026-09-22", "open": 80000, "high": 81000, "low": 79500, "close": 80500, "volume": 1000}
PREV = {"trade_date": "2026-09-19", "open": 79000, "high": 80000, "low": 78000, "close": 79000, "volume": 900}
NOW = datetime(2026, 9, 23, 17, 30, tzinfo=KST)               # 장 마감 2시간 뒤


def _tick(**kw):
    t = {"price": 82300, "change": 1800, "change_rate": 2.24, "open": 80600, "high": 82500, "low": 80400,
         "volume": 15_000_000, "value": 1_230_000_000_000, "session_date": "2026-09-23", "source": "kis",
         "ts": (NOW - timedelta(hours=2)).isoformat()}
    t.update(kw)
    return t


# ── 순수 로직 ─────────────────────────────────────────────
def test_after_close_shows_todays_close_not_previous_day():
    q = Q.effective(LAST, PREV, _tick(), now=NOW)
    assert q["price"] == 82300 and q["status"] == "close"          # 예전엔 80500(전 거래일)로 되돌아감
    assert q["prev_close"] == 80500 and q["change"] == 1800 and q["change_rate"] == 2.24
    assert q["session_date"] == "2026-09-23" and q["open"] == 80600 and q["volume"] == 15_000_000


def test_fresh_tick_is_live():
    q = Q.effective(LAST, PREV, _tick(ts=(NOW - timedelta(seconds=30)).isoformat()), now=NOW)
    assert q["status"] == "live" and q["live"]


def test_stopped_sim_tick_is_ignored():
    q = Q.effective(LAST, PREV, _tick(source="sim", ts=(NOW - timedelta(minutes=10)).isoformat()), now=NOW)
    assert q["price"] == 80500 and q["status"] == "close" and not q["from_tick"]


def test_running_sim_is_labeled_sim_not_live():
    q = Q.effective(LAST, PREV, _tick(source="sim", ts=(NOW - timedelta(seconds=5)).isoformat()), now=NOW)
    assert q["status"] == "sim" and not q["live"]


def test_older_session_tick_loses_to_newer_bar():
    q = Q.effective(LAST, PREV, _tick(session_date="2026-09-19", ts=(NOW - timedelta(days=4)).isoformat()), now=NOW)
    assert q["price"] == 80500 and q["change"] == 1500                # 최신 봉 vs 그 전 봉


def test_legacy_tick_without_session_or_change():
    q = Q.effective(LAST, PREV, {"price": 82300, "change_rate": 2.24, "volume": 1,
                                 "ts": "2026-09-23T15:30:00+09:00"}, now=NOW)
    assert q["price"] == 82300 and q["session_date"] == "2026-09-23"
    assert abs(q["prev_close"] - 80500) < 5                           # 등락률로 전일가 역산


def test_merge_bar_appends_or_replaces_today_candle():
    q = Q.effective(LAST, PREV, _tick(), now=NOW)
    bars = Q.merge_bar([PREV, LAST], q)
    assert len(bars) == 3 and bars[-1]["trade_date"] == "2026-09-23" and bars[-1]["close"] == 82300
    same = Q.effective(LAST, PREV, _tick(session_date="2026-09-22", price=80900,
                                         ts=(NOW - timedelta(seconds=20)).isoformat()), now=NOW)
    bars2 = Q.merge_bar([PREV, LAST], same)
    assert len(bars2) == 2 and bars2[-1]["close"] == 80900         # 신선한 같은 날 틱 → 그 봉 갱신


def test_stale_same_day_tick_loses_to_official_bar():
    """웹소켓이 15:19 에 끊겨 마지막 틱(105)이 남아도, 15:45 에 들어온 공식 종가 봉(110)이 이긴다."""
    bar = {"trade_date": "2026-09-23", "open": 100, "high": 111, "low": 99, "close": 110, "volume": 5}
    tick = _tick(session_date="2026-09-23", price=105, high=106, change=None, change_rate=None,
                 ts=datetime(2026, 9, 23, 15, 19, tzinfo=KST).isoformat())
    q = Q.effective(bar, PREV, tick, now=NOW)
    assert q["price"] == 110 and not q["from_tick"] and q["high"] == 111
    assert Q.merge_bar([PREV, bar], q)[-1]["close"] == 110          # 차트 마지막 봉도 공식 값 그대로


def test_market_phases_kr_us_and_dst_fallback():
    assert Q.kr_phase(datetime(2026, 9, 23, 8, 0, tzinfo=KST)) == "pre"
    assert Q.kr_phase(datetime(2026, 9, 23, 10, 0, tzinfo=KST)) == "open"
    assert Q.kr_phase(datetime(2026, 9, 23, 16, 0, tzinfo=KST)) == "closed"
    assert Q.kr_phase(datetime(2026, 9, 26, 10, 0, tzinfo=KST)) == "weekend"
    assert Q.us_phase(datetime(2026, 9, 23, 23, 0, tzinfo=KST)) == "open"      # 10:00 EDT
    assert Q.us_phase(datetime(2026, 9, 24, 6, 30, tzinfo=KST)) == "after"     # 17:30 EDT
    assert Q._et_offset(datetime(2026, 1, 15, 15, tzinfo=timezone.utc)).utcoffset(None) == timedelta(hours=-5)
    assert Q._et_offset(datetime(2026, 7, 15, 15, tzinfo=timezone.utc)).utcoffset(None) == timedelta(hours=-4)


# ── DB 통합: 목록·상세·차트·장기투자가 같은 가격 ─────────────
def _next_session_tick(ticker, pct, fresh=False, **extra):
    last = db.query("SELECT trade_date, close FROM ohlc WHERE ticker=:t ORDER BY trade_date DESC LIMIT 1",
                    {"t": ticker})[0]
    nxt = (date.fromisoformat(last["trade_date"]) + timedelta(days=1)).isoformat()
    price = round(last["close"] * (1 + pct / 100))
    save_tick({"ticker": ticker, "price": float(price), "change": float(price - last["close"]),
               "change_rate": pct, "open": last["close"], "high": float(price), "low": last["close"],
               "volume": 1234, "value": 5678, "session_date": nxt, **extra})
    if not fresh:                                                  # 장 마감 후처럼 오래된 수신 시각
        db.execute("UPDATE realtime_price SET ts=:t WHERE ticker=:k",
                   {"t": (Q.now_kst() - timedelta(minutes=30)).isoformat(), "k": ticker})
    return nxt, float(price), last["close"]


def test_list_detail_chart_use_after_close_tick():
    marketgen.seed_market(real=False)
    nxt, price, _ = _next_session_tick("005930", 3.0)
    items = {r["ticker"]: r for r in client.get("/market/stocks?sort=value&limit=50").json()["items"]}
    assert items["005930"]["price"] == price and items["005930"]["status"] == "close"
    d = client.get("/stocks/005930/detail").json()
    assert d["price"] == price and d["change_rate"] == 3.0 and d["status"] == "close"
    assert d["session_date"] == nxt and d["fundamentals"]["volume"] == 1234
    s = client.get("/stocks/005930/ohlc?period=1M").json()["series"]
    assert s[-1]["trade_date"] == nxt and s[-1]["close"] == price


def test_live_change_is_vs_last_close():
    """장중 전일대비는 '직전 종가' 기준이어야 한다(예전: 2거래일 전 종가와 비교하던 버그)."""
    marketgen.seed_market(real=False)
    _nxt, price, last_close = _next_session_tick("005930", 1.0, fresh=True)
    d = client.get("/stocks/005930/detail").json()
    assert d["live"] and d["status"] == "live"
    assert round(d["change"]) == round(price - last_close)


def test_invest_buys_at_effective_price():
    marketgen.seed_market(real=False)
    _nxt, price, _ = _next_session_tick("005930", 2.0)
    u = "qinv@oos.io"
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    wallet.credit_once(u, 50_000_000, "iap", memo="테스트")
    r = invest.buy(u, "005930", 1)
    assert r["buy_price"] == int(price)


def test_market_source_labels_sample_data():
    marketgen.seed_market(real=False)
    st = client.get("/market/stocks").json()["status"]
    assert st["source"] == "gen" and st["phase"] in ("pre", "open", "closed", "weekend", "holiday")   # 신정·설·추석은 holiday


def test_init_db_migrates_old_realtime_table_idempotently(as_owner):
    with db.get_engine().begin() as c:
        c.execute(text("DROP TABLE realtime_price"))
        c.execute(text("CREATE TABLE realtime_price(ticker VARCHAR(20) PRIMARY KEY, price FLOAT, "
                       "change_rate FLOAT, volume BIGINT, ts VARCHAR(20))"))
        c.execute(text("INSERT INTO realtime_price VALUES('005930',71000,0.5,1,'2026-09-22')"))  # 구버전 가짜 틱
        c.execute(text("INSERT INTO realtime_price VALUES('000660',170000,0.5,1,'2026-09-22T10:00:00')"))
    db.init_db()
    db.init_db()                                                   # 두 번 실행해도 안전
    cols = {r["column_name"] for r in db.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name='realtime_price'")}
    assert {"change", "open", "high", "low", "value", "session_date", "source"} <= cols
    assert [r["ticker"] for r in db.query("SELECT ticker FROM realtime_price")] == ["000660"]
    save_tick({"ticker": "000660", "price": 1.0, "change_rate": 0.0, "volume": 1})   # 넓어진 ts(+09:00) 저장 OK


# ── 마감 전에 끊긴 '식은 틱' vs 마감 후 공식 종가 봉 ─────────────────
_TODAY_BAR = {"trade_date": "2026-09-23", "open": 80600, "high": 82500, "low": 80400, "close": 81900, "volume": 16_000_000}


def test_stale_pre_close_tick_loses_to_same_day_official_bar_after_close():
    # 실시간 수집기가 14:00 에 멈춤 → 15:45 공식 종가 봉(81,900)이 들어왔는데도 14:00 틱(82,300)이 '종가'로 보이던 문제
    tick = _tick(ts=datetime(2026, 9, 23, 14, 0, tzinfo=KST).isoformat())
    q = Q.effective(_TODAY_BAR, LAST, tick, now=NOW)
    assert q["price"] == 81900 and not q["from_tick"] and q["status"] == "close"
    assert q["prev_close"] == 80500                                      # 전일 대비도 봉 기준
    q = Q.effective(_TODAY_BAR, LAST, tick, now=datetime(2026, 9, 24, 8, 30, tzinfo=KST))   # 다음 날 개장 전에도
    assert q["price"] == 81900


def test_stale_tick_still_wins_during_session_and_closing_auction_tick_kept():
    tick = _tick(ts=datetime(2026, 9, 23, 14, 0, tzinfo=KST).isoformat())
    q = Q.effective(_TODAY_BAR, LAST, tick, now=datetime(2026, 9, 23, 14, 5, tzinfo=KST))   # 장중: 뜸한 종목의 마지막 체결
    assert q["price"] == 82300 and q["from_tick"]
    auction = _tick(ts=datetime(2026, 9, 23, 15, 30, 12, tzinfo=KST).isoformat())       # 종가 단일가 체결 틱
    assert Q.effective(_TODAY_BAR, LAST, auction, now=NOW)["price"] == 82300


def test_us_stale_tick_before_close_loses_to_official_bar():
    et = Q.to_et().tzinfo
    last = {"trade_date": "2026-09-22", "open": 230, "high": 236, "low": 229, "close": 235.5, "volume": 1}
    prev = {"trade_date": "2026-09-21", "open": 228, "high": 231, "low": 227, "close": 230.0, "volume": 1}
    tick = {"price": 233.0, "session_date": "2026-09-22", "source": "finnhub",
            "ts": datetime(2026, 9, 22, 14, 0, tzinfo=timezone(timedelta(hours=-4))).isoformat()}
    q = Q.effective(last, prev, tick, now=datetime(2026, 9, 23, 7, 30, tzinfo=KST), tz=et)   # 07:30 KST = 18:30 ET
    assert q["price"] == 235.5 and not q["from_tick"]
