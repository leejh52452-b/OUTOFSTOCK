"""2026-10 전체 검토 — 시세 데이터(표본·실데이터 전환, 상세, 실습, 신호, 입력 검증) 회귀 테스트."""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import config, db
from app.api import app
from app.collectors import krx, marketgen
from app.services import practice, signals, themes
from app.services import quotes as Q

client = TestClient(app)


def _bdays(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _fake_krx(monkeypatch, days_with_data, close=50_000, fail=(), rate=0.5):
    """KRX 가짜 응답 — days_with_data 날짜만 005930 데이터, fail 날짜는 예외."""
    have = {d.isoformat() for d in days_with_data}

    def daily(bas_dd, markets=("STK", "KSQ")):
        if bas_dd in fail:
            raise ConnectionError("KRX timeout")
        if bas_dd not in have:
            return {}
        return {"005930": {"name": "삼성전자", "market": "KOSPI", "open": close, "high": close + 500,
                           "low": close - 500, "close": close, "volume": 1000, "value": 1,
                           "mktcap": 400_000_000_000_000, "shares": 1, "change_rate": rate}}
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", daily)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")


# ── 표본 → 실데이터 전환: 데모 봉이 남지 않는다 ─────────────
def test_demo_bars_do_not_survive_switch_to_real(monkeypatch):
    marketgen.seed_market(real=False)
    for sc in practice.SCENARIOS:
        marketgen.seed_scenario_ohlc(sc, real=False)
    today = marketgen._today()
    real_days = _bdays(today - timedelta(days=1), 5)
    _fake_krx(monkeypatch, real_days)
    out = marketgen.seed_market(days=5, real=True)
    assert out["source"] == "krx"
    rows = db.query("SELECT trade_date FROM ohlc WHERE ticker='005930' AND trade_date >= '2024-01-01' "
                    "ORDER BY trade_date")
    assert [r["trade_date"] for r in rows] == [d.isoformat() for d in real_days]    # 데모 봉 0개
    assert db.query("SELECT COUNT(*) AS n FROM daily_price WHERE ticker='005930'")[0]["n"] == 5
    sc = practice.scenario_meta("samsung-golden")
    assert db.query("SELECT COUNT(*) AS n FROM ohlc WHERE ticker='005930' AND trade_date BETWEEN :a AND :b",
                    {"a": sc["start_date"], "b": sc["end_date"]})[0]["n"] > 50  # 실습 구간은 보존
    assert json.loads(db.get_meta("krx_since"))["005930"] == real_days[0].isoformat()
    assert db.query("SELECT COUNT(*) AS n FROM ranking")[0]["n"] > 0          # 첫 실행부터 랭킹이 참


def test_failed_krx_day_keeps_bars_and_kis_today_bar_survives(monkeypatch):
    today = marketgen._today()
    days = _bdays(today - timedelta(days=1), 6)
    _fake_krx(monkeypatch, days, close=50_000)
    marketgen.seed_market(days=6, real=True)
    kis_today = (today if today.weekday() < 5 else days[-1] + timedelta(days=1)).isoformat()
    db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) "
               "VALUES('005930', :d, 1, 2, 1, 51000, 5) ON CONFLICT DO NOTHING", {"d": kis_today})
    _fake_krx(monkeypatch, days, close=52_000, fail={days[2].isoformat()})
    out = marketgen.seed_market(days=5, real=True)
    assert out["source"] == "krx"
    got = {r["trade_date"]: r["close"] for r in db.query("SELECT trade_date, close FROM ohlc WHERE ticker='005930'")}
    assert got[days[2].isoformat()] == 50_000                           # 실패한 날은 기존 봉 유지(예전엔 삭제)
    assert got[days[-1].isoformat()] == 52_000                          # 받은 날은 새 값
    assert kis_today in got                                             # KRX 최신일 뒤 KIS 봉도 유지


def test_krx_real_run_keeps_real_fx(monkeypatch):
    db.execute("INSERT INTO market_index(code,name,value,change,change_rate,ts) "
               "VALUES('USDKRW','원/달러 환율',1395.5,1.2,0.09,'2026-10-01T09:00:00+09:00')")
    _fake_krx(monkeypatch, _bdays(marketgen._today() - timedelta(days=1), 3))
    marketgen.seed_market(days=3, real=True)
    assert db.query("SELECT value FROM market_index WHERE code='USDKRW'")[0]["value"] == 1395.5


def test_krx_change_rate_and_zero_ohlc(monkeypatch):
    fake = {krx.SVC_STK: [{"ISU_CD": "005930", "ISU_NM": "삼성전자", "MKT_NM": "KOSPI", "TDD_OPNPRC": "0",
                           "TDD_HGPRC": "0", "TDD_LWPRC": "0", "TDD_CLSPRC": "70,000", "ACC_TRDVOL": "0",
                           "ACC_TRDVAL": "0", "MKTCAP": "1", "LIST_SHRS": "1", "FLUC_RT": "-"},
                          {"ISU_CD": "000001", "TDD_CLSPRC": "0"}], krx.SVC_KSQ: []}
    monkeypatch.setattr(krx, "_get", lambda svc, d: fake.get(svc, []))
    r = krx.daily_stocks("2026-09-10")
    assert r["005930"]["low"] == 70000 and r["005930"]["open"] == 70000   # 거래정지일 0 → 종가
    assert r["005930"]["change_rate"] is None and "000001" not in r


def test_krx_get_retries_server_errors(monkeypatch):
    import requests
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def json(self):
            return {"OutBlock_1": [{"x": 1}]}
    monkeypatch.setattr(krx.requests, "get", lambda *a, **k: calls.append(1) or R(503 if len(calls) < 3 else 200))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert krx._get(krx.SVC_STK, "2026-09-10") == [{"x": 1}] and len(calls) == 3


# ── 실습 구간: 실패하면 안 건드림 · 이미 받았으면 건너뜀 · 워밍업 ─────
def test_scenario_refetch_is_safe(monkeypatch):
    sc = practice.scenario_meta("samsung-golden")
    real_days = [d for d in (date.fromisoformat(practice.warmup_start(sc)) + timedelta(days=i) for i in range(260))
                 if d.weekday() < 5 and d.isoformat() <= sc["end_date"]]
    _fake_krx(monkeypatch, real_days, close=49_500)
    n = marketgen.seed_scenario_ohlc(sc, real=True)
    assert n == len(real_days) and db.get_meta("scenario_src:samsung-golden") == "krx"
    calls = []
    monkeypatch.setattr(krx, "daily_stocks", lambda *a, **k: calls.append(1) or {})
    assert marketgen.seed_scenario_ohlc(sc, real=True) == n and not calls    # 이미 실데이터 → 호출 0번

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(krx, "daily_stocks", boom)
    marketgen.seed_scenario_ohlc(sc, real=True, force=True)                  # 실패 → 아무것도 안 씀
    closes = {r["close"] for r in db.query("SELECT close FROM ohlc WHERE ticker='005930'")}
    assert closes == {49_500}                                                # 생성 데이터로 안 바뀜


def test_practice_sma60_available_from_first_screen():
    sc = practice.scenario_meta("kakao-ma-fail")
    marketgen.seed_scenario_ohlc(sc, real=False)
    st = practice.start("guest", "kakao-ma-fail")
    sma60 = st["overlays"]["SMA60"]
    assert len(sma60) == len(st["series"]) and sma60[-1] is not None          # 워밍업 봉 덕에 첫 화면부터
    assert st["series"][0]["trade_date"] >= sc["start_date"]                  # 워밍업 봉은 화면에 안 나옴


def test_practice_occ_and_missing_price():
    sc = practice.scenario_meta("hyundai-volume")
    marketgen.seed_scenario_ohlc(sc, real=False)
    st = practice.start("guest", "hyundai-volume")
    sid = st["session_id"]
    practice.trade(sid, "BUY", 10)
    v0 = db.query("SELECT version FROM practice_session WHERE session_id=:s", {"s": sid})[0]["version"]
    db.execute("DELETE FROM ohlc WHERE ticker=:t AND trade_date=:d", {"t": sc["ticker"], "d": st["cursor_date"]})
    done = practice.end(sid)
    assert done["status"] == "done" and done["final_return"] > -5            # 가격 빠진 날도 0원 평가 안 함
    assert db.query("SELECT version FROM practice_session WHERE session_id=:s", {"s": sid})[0]["version"] > v0
    for fn in (lambda: practice.step(sid, 1), lambda: practice.trade(sid, "BUY", 1)):
        try:
            fn()
            raise AssertionError("끝난 실습이 바뀜")
        except practice.PracticeError:
            pass


def test_practice_concurrent_end_and_step_are_serialized():
    sc = practice.scenario_meta("naver-rsi")
    marketgen.seed_scenario_ohlc(sc, real=False)
    sid = practice.start("guest", "naver-rsi")["session_id"]
    errs = []

    def run(fn):
        try:
            fn()
        except practice.PracticeError as e:
            errs.append(e)
    ts = [threading.Thread(target=run, args=(f,)) for f in (lambda: practice.end(sid), lambda: practice.step(sid, 5))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    row = db.query("SELECT status, cursor_date, final_return FROM practice_session WHERE session_id=:s", {"s": sid})[0]
    assert row["status"] == "done" and row["final_return"] is not None


# ── 종목 상세: 52주·1Y 에 실습 구간(몇 년 전) 봉이 섞이지 않음 ─────
def test_detail_52w_ignores_old_scenario_bars(monkeypatch):
    sc = practice.scenario_meta("samsung-golden")
    marketgen.seed_scenario_ohlc(sc, real=False)
    db.execute("UPDATE ohlc SET high = 999999 WHERE ticker='005930'")        # 옛 구간에 터무니없는 고가
    days = _bdays(marketgen._today() - timedelta(days=1), 30)
    _fake_krx(monkeypatch, days, close=60_000)
    marketgen.seed_market(days=30, real=True)
    db.execute("UPDATE ohlc SET low = 0 WHERE ticker='005930' AND trade_date=:d", {"d": days[3].isoformat()})
    d = client.get("/stocks/005930/detail").json()["fundamentals"]
    assert d["high_52w"] == 60_500 and d["low_52w"] == 59_500                # 0원 저가도 제외
    s = client.get("/stocks/005930/ohlc?period=1Y").json()["series"]
    assert s[0]["trade_date"] >= (marketgen._today() - timedelta(days=520)).isoformat()


# ── 신호: 오래된 봉으로 추천하지 않음 · 0 값 ─────────────────
def test_signals_skip_stale_and_gapped_series():
    today = date(2026, 9, 30)
    fresh = [{"trade_date": (today - timedelta(days=i)).isoformat(), "open": 100, "high": 101, "low": 99,
              "close": 100 + (i % 3), "volume": 10} for i in range(40)][::-1]
    assert signals._usable(fresh, today.isoformat()) == fresh
    assert signals._usable(fresh[:-10], today.isoformat()) == []               # 마지막 봉이 열흘 전 → 제외
    old = [{**b, "trade_date": (date(2023, 1, 1) + timedelta(days=i)).isoformat()} for i, b in enumerate(fresh[:20])]
    assert signals._usable(old + fresh, today.isoformat()) == fresh           # 몇 년 전 구간은 잘라냄
    bars = [{**b, "low": 0} for b in fresh]
    assert "주요 이평선 지지" not in [t for t, _ in signals.evaluate(bars)]   # 0 저가로 가짜 '지지' 신호 없음


# ── 입력 검증 ─────────────────────────────────────────────
def test_pit_rejects_ambiguous_dates():
    assert client.get("/stocks/005930/soundness?as_of=2025-03-05").status_code == 200
    for bad in ("20250305", "2025-3-5", "2025.03.05"):
        assert client.get(f"/stocks/005930/soundness?as_of={bad}").status_code == 422


def test_risk_portfolio_whatif_inputs():
    for i, d in enumerate(_bdays(date(2026, 9, 30), 40)):
        for t in ("005930", "000660"):
            db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume) VALUES(:t,:d,:c,1)",
                       {"t": t, "d": d.isoformat(), "c": 100.0 if t == "005930" else 100.0 + (i % 5)})
    for conf in ("0", "1.2", "1"):
        assert client.get(f"/stocks/005930/var-backtest?conf={conf}").status_code == 422


def test_market_status_holiday_and_stale_tick_rules():
    from datetime import datetime
    assert Q.kr_phase(datetime(2026, 9, 25, 10, 0, tzinfo=Q.KST)) == "holiday"    # 추석
    assert Q.kr_phase(datetime(2026, 9, 23, 10, 0, tzinfo=Q.KST)) == "open"
    assert Q.us_phase(datetime(2026, 11, 26, 23, 30, tzinfo=Q.KST)) == "holiday"   # 추수감사절(뉴욕 오전)


def test_official_change_rate_used_on_split_day():
    last = {"trade_date": "2026-09-23", "open": 20000, "high": 20500, "low": 19800, "close": 20000,
            "volume": 1, "dp_rate": 1.0}
    prev = {"trade_date": "2026-09-22", "open": 99000, "high": 99000, "low": 99000, "close": 99000, "volume": 1}
    q = Q.effective(last, prev, None)
    assert q["change_rate"] == 1.0                                              # 예전엔 -79.8% 로 '폭락'


# ── 뉴스 감성 · AI 픽 ─────────────────────────────────────
def test_sentiment_question_headlines_and_symmetry():
    from app.services.sentiment import _lexicon_v2 as f
    assert f("코스피 3000 고점인가").label == "NEU"
    assert f("반도체 겨울 다시 오는 것인가").label == "NEU"
    assert f("삼성전자 5% 상승").label == "POS" and f("삼성전자 5% 하락").label == "NEG"
    assert f("신약 품목 인가 획득").label == "POS"


def test_today_picks_never_contradict():
    db.execute("INSERT INTO news(news_id, title, url, published_at) VALUES(1, 'a', 'u1', '2026-09-30')")
    db.execute("INSERT INTO news_stock(news_id, ticker) VALUES(1, '005930')")
    db.execute("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) VALUES(1,'POS',1,'x','2026-10-01T09:00:00')")
    p = themes.today_picks()
    assert p["recommend"]["ticker"] == "005930" and p["avoid"] is None          # 한 종목이 추천·비추천 동시 X
    db.execute("INSERT INTO news(news_id, title, url, published_at) VALUES(2, 'b', 'u2', '2026-09-30')")
    db.execute("INSERT INTO news_stock(news_id, ticker) VALUES(2, '000660')")
    db.execute("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) VALUES(2,'POS',0.5,'x','2026-10-01T09:00:00')")
    assert themes.today_picks()["avoid"] is None                                 # 모두 호재면 비추천 없음
