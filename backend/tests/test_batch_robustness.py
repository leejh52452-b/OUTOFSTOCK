"""배치·수집기 견고성(7차 점검) — 일시 장애가 데이터를 지우거나 덮지 않게, 배치가 멈추거나 통째로 건너뛰지 않게.
각 테스트는 고치기 전 코드에서 실패한다(외부 네트워크 호출 없음 — HTTP 계층을 가짜로 바꿔 재현)."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import text

from app import config, db
from app.collectors import krx, marketgen

KST = timezone(timedelta(hours=9))


def _bar(close, chg=0.5, vol=1_000_000):
    return {"name": "삼성전자", "market": "KOSPI", "open": close, "high": close + 500, "low": close - 500,
            "close": close, "volume": vol, "value": 1, "mktcap": 500_000_000_000_000, "shares": 1,
            "change_rate": chg}


def _krx_on(monkeypatch, day_fn):
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", day_fn)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")


# ── KRX 클라이언트 ─────────────────────────────────────────
def test_krx_never_retries_over_plain_http_with_the_key(monkeypatch):
    # 예전: 인증서 오류면 http:// 로 다시 보내 AUTH_KEY 헤더가 평문으로 나갔다
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        raise requests.exceptions.SSLError("certificate verify failed")
    monkeypatch.setattr(krx.requests, "get", fake_get)
    monkeypatch.setattr(config.settings, "KRX_API_KEY", "secret-key")
    with pytest.raises(RuntimeError, match="평문"):
        krx._get(krx.SVC_STK, "2026-09-30")
    assert urls and all(u.startswith("https://") for u in urls)


def test_krx_response_without_outblock_is_an_error_not_a_holiday(monkeypatch):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"respCode": "429", "respMsg": "limit"}             # 오류 본문(키 없음)
    monkeypatch.setattr(krx.requests, "get", lambda url, **kw: R())
    with pytest.raises(RuntimeError, match="응답 형식"):
        krx._get(krx.SVC_STK, "2026-09-30")


# ── 국내 일봉 적재 ─────────────────────────────────────────
def test_krx_outage_mid_run_stops_quickly_and_keeps_what_it_got(monkeypatch):
    # 예전: 첫 이틀 뒤 KRX 가 죽으면 최대 400번(번마다 재시도 포함) 끝까지 호출 — 몇 시간 동안 트랜잭션 유지
    today = marketgen._today()
    calls = []

    def day(bas_dd):
        calls.append(bas_dd)
        if bas_dd >= today.isoformat():
            return {}
        if len(calls) <= 3:
            return {"005930": _bar(90000)}
        raise ConnectionError("KRX down")
    _krx_on(monkeypatch, day)
    out = marketgen.seed_market(days=120, real=True)
    assert out["source"] == "krx"
    assert len(calls) <= 3 + marketgen.KRX_MAX_CONSECUTIVE_FAILS + 1
    assert db.query("SELECT COUNT(*) AS n FROM ohlc WHERE ticker='005930'")[0]["n"] >= 2


def test_existing_real_db_keeps_todays_kis_bar_when_krx_has_no_today(monkeypatch):
    # 23:00: KRX 는 오늘 데이터가 없다(D+1). 예전엔 KIS 가 15:45 에 넣은 오늘 종가 봉까지 지웠다
    today = marketgen._today()
    _krx_on(monkeypatch, lambda bas_dd: {} if bas_dd >= today.isoformat() else {"005930": _bar(90000)})
    marketgen.seed_market(days=5, real=True)                                    # 실데이터 DB 가 됨
    db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES('005930',:d,91000,92000,"
               "90500,91500,5) ON CONFLICT(ticker,trade_date) DO UPDATE SET close=91500", {"d": today.isoformat()})
    marketgen.seed_market(days=5, real=True)
    row = db.query("SELECT close FROM ohlc WHERE ticker='005930' AND trade_date=:d", {"d": today.isoformat()})
    assert row and row[0]["close"] == 91500


def test_daily_change_rate_comes_from_krx_not_unadjusted_closes(monkeypatch):
    # 액면분할일: 종가는 1/5 로 줄지만 KRX 공식 등락률은 +2.0%. 예전엔 종가로 다시 계산해 −79.6%
    today = marketgen._today()
    days = []
    d = today - timedelta(days=1)
    while len(days) < 2:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    newest, older = days

    def day(bas_dd):
        if bas_dd == newest:
            return {"005930": _bar(20400, chg=2.0)}
        if bas_dd == older:
            return {"005930": _bar(100000, chg=0.1)}
        return {}
    _krx_on(monkeypatch, day)
    marketgen.seed_market(days=2, real=True)
    r = db.query("SELECT change_rate FROM daily_price WHERE ticker='005930' AND trade_date=:d", {"d": newest})
    assert r and r[0]["change_rate"] == 2.0


def test_rankings_see_tickers_loaded_in_the_same_transaction(monkeypatch):
    # 새 DB 첫 적재: 예전엔 유니버스를 다른 연결로 읽어 방금 넣은(커밋 전) 종목이 랭킹에서 빠졌다
    db.execute("DELETE FROM stock")
    today = marketgen._today()

    def day(bas_dd):
        if bas_dd >= today.isoformat():
            return {}
        return {"950999": {**_bar(400000), "name": "새상장"}, "005930": _bar(90000)}   # 데모 목록에 없는 종목
    _krx_on(monkeypatch, day)
    marketgen.seed_market(days=3, real=True)
    tickers = {r["ticker"] for r in db.query("SELECT ticker FROM ranking WHERE ranking_type='marcap'")}
    assert {"950999", "005930"} <= tickers


def test_concurrent_market_loads_run_one_after_another(monkeypatch):
    # 08:40 정기 실행과 수동 fetch-market·보충 실행이 겹치면 예전엔 둘이 동시에 KRX 를 부르며(한도 2배) 같은 행을
    # 고쳤다(랭킹 PK 충돌 시 한쪽 롤백). 이제 한 적재가 끝난 뒤 다음 적재가 시작된다.
    today = marketgen._today()
    log = []

    def day(bas_dd):
        log.append(threading.current_thread().name)
        time.sleep(0.05)
        return {} if bas_dd >= today.isoformat() else {"005930": _bar(90000)}
    _krx_on(monkeypatch, day)
    errors, outs = [], []

    def run():
        try:
            outs.append(marketgen.seed_market(days=3, real=True)["source"])
        except Exception as e:                                  # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=run, name=f"load{i}") for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert errors == [] and outs == ["krx", "krx"]
    switches = sum(1 for a, b in zip(log, log[1:]) if a != b)
    assert switches == 1                                        # 호출이 섞이지 않음(한쪽이 끝난 뒤 다른 쪽)


# ── 실습 시나리오 구간 ──────────────────────────────────────
def _scenario():
    from app.services import practice
    return next(sc for sc in practice.SCENARIOS if sc["ticker"] == "005930")


def _scenario_day(sc, close=56000):
    def day(bas_dd, markets=None):
        if not (sc["start_date"] <= bas_dd <= sc["end_date"]):
            return {}
        return {} if bas_dd.endswith("-15") else {"005930": _bar(close)}   # 매달 15일은 휴장일로 가정
    return day


def test_scenario_loaded_from_krx_is_not_refetched_every_night(monkeypatch):
    # 예전: 매일 23:00 마다 실습 구간 전체(약 1,200회)를 다시 호출
    sc = _scenario()
    calls = []
    inner = _scenario_day(sc)
    _krx_on(monkeypatch, lambda bas_dd, markets=None: calls.append(bas_dd) or inner(bas_dd, markets))
    n1 = marketgen.seed_scenario_ohlc(sc, real=True)
    first = len(calls)
    n2 = marketgen.seed_scenario_ohlc(sc, real=True)
    assert n1 == n2 and first > 0 and len(calls) == first                      # 두 번째는 호출 0회
    marketgen.seed_scenario_ohlc(sc, real=True, force=True)
    assert len(calls) == first * 2                                              # --force 로만 다시


def test_scenario_krx_failure_keeps_real_bars_instead_of_generated_prices(monkeypatch):
    sc = _scenario()
    _krx_on(monkeypatch, _scenario_day(sc, close=56000))
    marketgen.seed_scenario_ohlc(sc, real=True)
    db.set_meta(f"scenario_src:{sc['scenario_id']}", "")                        # 다시 받게(예: --force)

    def boom(bas_dd, markets=None):
        raise ConnectionError("KRX 5xx")
    monkeypatch.setattr(krx, "daily_stocks", boom)
    marketgen.seed_scenario_ohlc(sc, real=True)
    closes = {r["close"] for r in db.query("SELECT close FROM ohlc WHERE ticker='005930' AND trade_date "
                                           "BETWEEN :s AND :e", {"s": sc["start_date"], "e": sc["end_date"]})}
    assert closes == {56000}                                                    # 예전: 생성(가짜) 가격으로 덮임


def test_scenario_krx_load_removes_old_generated_holiday_bars(monkeypatch):
    sc = _scenario()
    marketgen.seed_scenario_ohlc(sc, real=False)                                # 데모: 평일 공휴일에도 봉 생성
    holiday = next(r["trade_date"] for r in db.query(
        "SELECT trade_date FROM ohlc WHERE ticker='005930' AND trade_date BETWEEN :s AND :e "
        "AND EXTRACT(DAY FROM trade_date) = 15 ORDER BY trade_date", {"s": sc["start_date"], "e": sc["end_date"]}))
    _krx_on(monkeypatch, _scenario_day(sc))
    marketgen.seed_scenario_ohlc(sc, real=True)
    assert not db.query("SELECT 1 FROM ohlc WHERE ticker='005930' AND trade_date=:d", {"d": holiday})


# ── 23:00 파이프라인 ───────────────────────────────────────
def test_one_failing_step_does_not_skip_the_rest_and_is_recorded_partial(monkeypatch):
    import run
    from app.collectors import dart, news, overseas_market, realtime
    from app.services import monitoring
    done = []
    monkeypatch.setattr(marketgen, "seed_market", lambda real: done.append("market") or {"source": "krx"})
    monkeypatch.setattr(realtime, "enabled", lambda: True)

    def token_fail():
        raise requests.HTTPError("403 EGW00133 토큰 발급 1분 1회")
    monkeypatch.setattr(realtime, "refresh_latest_bars", token_fail)
    monkeypatch.setattr(marketgen, "seed_scenario_ohlc", lambda sc, real: done.append("practice") or 1)

    def dart_fail():
        raise RuntimeError("DART corpCode 응답 오류 status=020")
    monkeypatch.setattr(dart, "collect_financials", dart_fail)
    monkeypatch.setattr(news, "collect_news", lambda: done.append("news") or {"total": 3})
    monkeypatch.setattr(overseas_market, "seed_overseas", lambda real: done.append("overseas") or {"source": "fdr"})
    monkeypatch.setattr(run, "_publish_reco", lambda: done.append("reco") or {"all": 5})
    out = run.real_pipeline("test")
    assert {"market", "practice", "news", "overseas", "reco"} <= set(done)       # 예전: KIS 실패에서 전부 중단
    # 시세 단계가 '성공'했는데 실제로는 일봉 0건 → 적재 검증이 잡아낸다(INC-2026-10-10-01 재발 방지)
    assert [f.split(":")[0] for f in out["failed"]] == ["적재 검증", "KIS 오늘 종가", "DART 재무"]
    last = monitoring.last_run()
    assert last["status"] == "PARTIAL" and "KIS" in last["note"] and "DART" in last["note"]
    assert "last_pipeline_partial" in monitoring.health()["issues"]


def test_stale_running_rows_are_closed_when_no_run_holds_the_lock():
    from app.services import monitoring
    rid = monitoring.start_run()                                               # 재시작으로 죽은 실행
    assert monitoring.close_stale_runs() == 1
    assert db.query("SELECT status FROM pipeline_run WHERE run_id=:r", {"r": rid})[0]["status"] == "FAILED"
    rid2 = monitoring.start_run()
    with db.advisory_lock("real") as got:                                      # 다른 프로세스가 실제로 실행 중이면
        assert got
        assert monitoring.close_stale_runs() == 0                              # 건드리지 않음
    assert db.query("SELECT status FROM pipeline_run WHERE run_id=:r", {"r": rid2})[0]["status"] == "RUNNING"


def test_catchup_when_latest_scheduled_run_was_missed_or_killed(monkeypatch):
    import scheduler
    monkeypatch.setattr(config.settings, "REAL_PIPELINE_TIME", "23:00")
    monkeypatch.setattr(config.settings, "REAL_PIPELINE_DAYS", "mon-sun")
    db.set_meta("pipeline_real_finished", datetime(2026, 10, 1, 23, 41, tzinfo=KST).isoformat())
    # 다음 날 23:10 재시작(23:00 실행이 죽음) — 예전 기준(26시간)으론 보충 안 함 → 약 48시간 공백
    assert scheduler.needs_catchup(datetime(2026, 10, 2, 23, 10, tzinfo=KST))
    assert scheduler.needs_catchup(datetime(2026, 10, 3, 1, 40, tzinfo=KST))
    assert not scheduler.needs_catchup(datetime(2026, 10, 2, 22, 50, tzinfo=KST))   # 오늘 23:00 은 아직
    monkeypatch.setattr(config.settings, "REAL_PIPELINE_DAYS", "mon-fri")
    db.set_meta("pipeline_real_finished", datetime(2026, 10, 2, 23, 30, tzinfo=KST).isoformat())   # 금
    assert not scheduler.needs_catchup(datetime(2026, 10, 4, 10, 0, tzinfo=KST))   # 일요일 — 정기 실행 없음


# ── 해외 ───────────────────────────────────────────────────
def _us_bars(close, n=30):
    start = datetime(2026, 8, 1)
    return [{"trade_date": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "open": close, "high": close,
             "low": close, "close": close + i * 0.0, "volume": 10} for i in range(n)]


def test_overseas_transient_failure_keeps_real_history_and_indices(monkeypatch):
    from app.collectors import edgar, forex, overseas_market as om
    monkeypatch.setattr(edgar, "shares_outstanding", lambda s: None)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    monkeypatch.setattr(forex, "usd_krw_quote", lambda *a, **k: None)
    monkeypatch.setattr(om, "fetch_fdr_bars", lambda s, days=None, finished_only=True: _us_bars(300.0))
    om.seed_overseas(real=True, symbols=["AAPL"])
    spx = db.query("SELECT value, source FROM market_index WHERE code='SPX'")[0]
    assert spx["source"] == "fdr" and spx["value"] == 300.0

    def boom(*a, **k):
        raise TimeoutError("야후 응답 없음")
    monkeypatch.setattr(om, "fetch_fdr_bars", boom)
    out = om.seed_overseas(real=True, symbols=["AAPL"])
    bars = db.query("SELECT close FROM overseas_ohlc WHERE symbol='AAPL'")
    assert len(bars) == 30 and {b["close"] for b in bars} == {300.0}          # 예전: 표본 320봉으로 교체
    assert db.query("SELECT source FROM overseas_stock WHERE symbol='AAPL'")[0]["source"] == "fdr"
    assert db.query("SELECT value FROM market_index WHERE code='SPX'")[0]["value"] == 300.0   # 예전: 6800 상수
    assert out["kept"] == 1 and out["sample"] == 0


def test_us_index_source_is_real_when_fresh_and_kept_values_are_all_real(monkeypatch):
    """지수 3개 중 1개만 이번에 실패(기존 실데이터 유지) — 셋 다 실데이터인데 'mixed'로 기록돼 화면에 '표본' 배지가 붙었다."""
    from app.collectors import overseas_market as om
    monkeypatch.setattr(om, "fetch_fdr_bars", lambda s, days=None, finished_only=True: _us_bars(500.0))
    with db.transaction() as conn:
        assert om.seed_indices(conn, True) == "fdr"
    first = next(iter(om.US_INDEX))
    fetched = {code: (None if code == first else _us_bars(510.0)) for code in om.US_INDEX}
    with db.transaction() as conn:
        assert om.seed_indices(conn, True, fetched) == "fdr"
    assert db.query("SELECT value FROM market_index WHERE code=:c", {"c": first})[0]["value"] == 500.0
    with db.transaction() as conn:                                  # 처음부터 실패한 지수는 표본 → mixed
        conn.execute(text("DELETE FROM market_index WHERE code=:c"), {"c": first})
        assert om.seed_indices(conn, True, fetched) == "mixed"


def test_fx_change_is_against_previous_business_day_and_source_is_recorded(monkeypatch):
    from app.collectors import forex, overseas_market as om
    monkeypatch.setattr(forex, "usd_krw_quote", lambda *a, **k: {"rate": 1390.8, "date": "2026-10-01",
                                                                  "prev": 1380.0, "prev_date": "2026-09-30"})
    db.execute("INSERT INTO market_index(code,name,value,change,change_rate,ts,source) "
               "VALUES('USDKRW','원/달러',1390.8,0,0,'2026-10-01T09:00:00+09:00','gen') ON CONFLICT(code) DO UPDATE SET value=1390.8, source='gen'")
    with db.transaction() as conn:
        assert om.seed_fx(conn) == "koreaexim"
    r = db.query("SELECT value, change, source, as_of FROM market_index WHERE code='USDKRW'")[0]
    assert r["change"] == 10.8 and r["source"] == "koreaexim" and r["as_of"] == "2026-10-01"   # 예전: 0.0·'gen'


def test_exim_quote_uses_two_latest_publication_days(monkeypatch):
    from app.collectors import forex
    monkeypatch.setattr(config.settings, "EXIM_API_KEY", "k")
    monkeypatch.setattr(forex.clock, "today", lambda: datetime(2026, 10, 5).date())   # 월(휴일 가정)

    def get(day):
        return {"20261002": [{"cur_unit": "USD", "deal_bas_r": "1,390.8"}],
                "20261001": [{"cur_unit": "USD", "deal_bas_r": "1,380.0"}]}.get(day, [])
    monkeypatch.setattr(forex, "_get", get)
    q = forex.usd_krw_quote()
    assert q == {"rate": 1390.8, "date": "2026-10-02", "prev": 1380.0, "prev_date": "2026-10-01"}


def test_call_with_timeout_gives_up_on_a_hung_call():
    from app.retry import call_with_timeout
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        call_with_timeout(time.sleep, 5, timeout=0.3)
    assert time.monotonic() - t0 < 2
    assert call_with_timeout(lambda x: x * 2, 21, timeout=2) == 42


def test_edgar_mapping_failure_is_not_retried_for_every_symbol(monkeypatch):
    from app.collectors import edgar
    calls = []

    def fail(url):
        calls.append(url)
        raise requests.ConnectionError("SEC timeout")
    monkeypatch.setattr(edgar, "_get_json", fail)
    monkeypatch.setattr(edgar, "_cik_cache", None)
    monkeypatch.setattr(edgar, "_cik_failed_at", None)
    assert [edgar.ticker_to_cik(s) for s in ("AAPL", "MSFT", "NVDA")] == [None, None, None]
    assert len(calls) == 1                                                     # 예전: 종목마다 다시(×3 재시도)


# ── DART ───────────────────────────────────────────────────
def test_dart_corpcode_error_body_is_reported_clearly(monkeypatch):
    from app.collectors import dart

    class R:
        content = b'<?xml version="1.0"?><result><status>020</status><message>' \
                  b'\xec\x9a\x94\xec\xb2\xad \xec\xa0\x9c\xed\x95\x9c</message></result>'
    monkeypatch.setattr(dart, "_get", lambda url, params: R())
    with pytest.raises(RuntimeError, match="status=020"):                     # 예전: BadZipFile
        dart.get_corp_code_map("k")


def test_dart_stops_after_consecutive_connection_failures(monkeypatch):
    from app.collectors import dart
    monkeypatch.setattr(config.settings, "DART_API_KEY", "k")
    monkeypatch.setattr(dart, "get_corp_code_map", lambda k: {t: "c" + t for t in ("A1", "A2", "A3", "A4", "A5", "A6", "A7")})
    calls = []

    def fail(k, cc):
        calls.append(cc)
        raise requests.ConnectionError("DART down")
    monkeypatch.setattr(dart, "fetch_financials", fail)
    out = dart.collect_financials(["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
    assert len(calls) == dart.DART_MAX_CONSECUTIVE_FAILS and "stopped" in out


# ── 실시간 수집기 ──────────────────────────────────────────
def test_kis_token_cache_is_per_app_key_and_renewed_on_token_errors(monkeypatch):
    from app.collectors import realtime as rt
    monkeypatch.setattr(config.settings, "KIS_APP_KEY", "key-A")
    monkeypatch.setattr(config.settings, "KIS_APP_SECRET", "secret")
    issued = []

    class R:
        def __init__(self, tok):
            self.tok = tok

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": self.tok, "expires_in": 86400}
    monkeypatch.setattr(rt.requests, "post", lambda url, **kw: issued.append(kw["json"]["appkey"]) or R(f"tok{len(issued)}"))
    assert rt.get_access_token() == "tok1" and rt.get_access_token() == "tok1"   # 캐시
    monkeypatch.setattr(config.settings, "KIS_APP_KEY", "key-B")                 # 키 교체
    assert rt.get_access_token() == "tok2"                                      # 예전: 옛 토큰(tok1) 계속 사용
    seen = []

    def bars(ticker, token):
        seen.append(token)
        if token == "tok2":
            raise RuntimeError("KIS EGW00123: 기간이 만료된 token 입니다")
        return []
    monkeypatch.setattr(rt, "fetch_daily_bars", bars)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    out = rt.refresh_latest_bars(["005930"])
    assert seen == ["tok2", "tok3"] and out["ok"] == 1                         # 토큰 오류 → 새로 받아 1번 재시도


def test_websocket_backoff_grows_when_server_closes_right_after_connect(monkeypatch):
    from app.collectors import realtime as rt
    monkeypatch.setattr(config.settings, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config.settings, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(rt, "_refresher", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_index_poller", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_ws_window", lambda now=None: True)
    monkeypatch.setattr(rt, "get_approval_key", lambda: "APPROVAL")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    class WS:
        def send(self, m):
            pass

        def recv(self):
            return ""                                                          # 접속 직후 끊김

        def close(self):
            pass
    monkeypatch.setattr(rt.websocket, "create_connection", lambda *a, **k: WS())
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 5

        def wait(self, s=None):
            waits.append(s)

        def set(self):
            pass
    rt.run_realtime(tickers=["005930"], stop=Stop())
    assert waits == [1, 2, 4, 8, 16]                                           # 예전: [1, 1, 1, 1, 1]


def test_finnhub_poller_survives_a_db_error(monkeypatch):
    from app.collectors import finnhub, overseas_realtime as orl
    monkeypatch.setattr(finnhub, "quote", lambda s: {"price": 1.0, "t": None})
    monkeypatch.setattr(orl.time, "sleep", lambda s: None)
    writes = []

    class Book:
        def base(self, s, q, sdate):
            return q

        def write(self, s, st):
            writes.append(s)
            if len(writes) == 1:
                raise RuntimeError("server closed the connection unexpectedly")

    class Stop:
        def __init__(self):
            self.n = 0

        def is_set(self):
            return len(writes) >= 3

        def wait(self, s=None):
            self.n += 1
    orl._poller(Stop(), Book(), ["AAPL", "MSFT", "NVDA"])
    assert writes == ["AAPL", "MSFT", "NVDA"]                                  # 예전: 첫 오류에서 스레드 종료


def test_realtime_runner_restarts_only_the_dead_collector(monkeypatch):
    """시작하자마자 죽은 수집기(예: DB 가 아직 안 뜸)만 잠시 뒤 다시 띄우고, 다른 수집기는 그대로 둔다.
    예전: 죽은 수집기는 영영 멈춘 채(프로세스는 살아 있고 종료 코드 0) — tmux·systemd 어느 쪽도 다시 띄우지 않았다."""
    import run
    from app.collectors import realtime, overseas_realtime
    monkeypatch.setattr(realtime, "enabled", lambda: True)
    monkeypatch.setattr(overseas_realtime, "enabled", lambda: True)
    monkeypatch.setattr(run, "RT_RESTART_BASE", 0.05)
    calls, alive = {"kr": 0, "us": 0}, threading.Event()
    quit_ev = threading.Event()

    def kr(stop=None):
        calls["kr"] += 1
        if calls["kr"] == 1:
            raise RuntimeError("DB 아직 준비 안 됨")
        alive.set()
        stop.wait(10)

    def us(stop=None):
        calls["us"] += 1
        stop.wait(10)
    monkeypatch.setattr(realtime, "run_realtime", kr)
    monkeypatch.setattr(overseas_realtime, "run_realtime", us)
    t = threading.Thread(target=run._run_realtime, args=("all", quit_ev), daemon=True)
    t.start()
    assert alive.wait(5)                                   # 죽은 국내 수집기가 다시 떠서 돌고 있음
    quit_ev.set()
    t.join(10)
    assert not t.is_alive()
    assert calls == {"kr": 2, "us": 1}                     # 해외 수집기는 재시작되지 않음


def test_kis_index_on_the_day_after_a_holiday_keeps_the_last_session_date(monkeypatch):
    # 10/5(월) 대체공휴일 다음 날 08:40 — KIS 는 직전 세션(10/2 금) 값을 준다. 예전엔 '직전 평일'=10/5 로 봉 생성
    from app.collectors import indexes
    from app.services import quotes
    now = datetime(2026, 10, 6, 8, 40, tzinfo=KST)
    monkeypatch.setattr(quotes, "now_kst", lambda: now)
    db.execute("INSERT INTO market_index(code,name,value,change,change_rate,ts,source,as_of) VALUES('KOSPI','코스피',"
               "3512.4,21.1,0.6,'2026-10-01T09:00:00+09:00','kis','2026-10-02') ON CONFLICT(code) DO UPDATE SET value=3512.4, change=21.1, "
               "as_of='2026-10-02', source='kis'")
    kis = {"value": 3512.4, "change": 21.1, "change_rate": 0.6, "open": 3500.0, "high": 3520.0, "low": 3490.0,
           "as_of": "2026-10-05", "source": "kis", "live": False}
    monkeypatch.setattr(indexes, "fetch_kis", lambda code, now=None: dict(kis))
    indexes.refresh(real=True, sources=("kis",), history=False)
    assert db.query("SELECT as_of FROM market_index WHERE code='KOSPI'")[0]["as_of"] == "2026-10-02"
    assert not db.query("SELECT 1 FROM index_daily WHERE code='KOSPI' AND trade_date='2026-10-05'")
