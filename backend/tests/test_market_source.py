"""시세 적재 출처 보고 — '실데이터인 줄 알았는데 표본' 혼동 방지 + 표본 봉 잔존(가짜 오늘 가격) 방지."""
import json

from fastapi.testclient import TestClient

from app import config, db
from app.api import app
from app.collectors import krx, marketgen

client = TestClient(app)


def _fake_day(today, close=90000):
    def f(bas_dd):
        if bas_dd >= today:                                      # KRX 는 당일 데이터를 다음 영업일에 제공
            return {}
        return {"005930": {"name": "삼성전자", "market": "KOSPI", "open": close, "high": close + 500,
                           "low": close - 500, "close": close, "volume": 1_000_000, "value": 1,
                           "mktcap": 500_000_000_000_000, "shares": 1, "change_rate": 0.5}}
    return f


def test_real_without_key_reports_gen_with_warning(monkeypatch):
    monkeypatch.setattr(config.settings, "KRX_API_KEY", "")
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "auto")
    out = marketgen.seed_market(days=5, real=True)
    assert out["source"] == "gen" and "warning" in out            # 예전엔 'krx' 로 잘못 보고
    assert db.get_meta("market_source") == "gen"
    assert client.get("/market/stocks").json()["status"]["source"] == "gen"


def test_krx_success_purges_stale_generated_today_bar(monkeypatch):
    from datetime import date
    monkeypatch.setattr(marketgen, "_today", lambda: date(2026, 10, 1))   # 평일로 고정(주말·공휴일엔 '오늘 봉'이 없다)
    marketgen.seed_market(real=False)                             # 데모: '오늘' 날짜 표본 봉까지 생김
    today = marketgen._today().isoformat()
    assert db.query("SELECT 1 FROM ohlc WHERE ticker='005930' AND trade_date=:d", {"d": today})
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", _fake_day(today))
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    out = marketgen.seed_market(days=3, real=True)
    assert out["source"] == "krx" and "warning" not in out
    assert not db.query("SELECT 1 FROM ohlc WHERE ticker='005930' AND trade_date=:d", {"d": today})
    assert client.get("/stocks/005930/detail").json()["price"] == 90000   # 가짜 오늘 가격이 아닌 실데이터


def test_krx_failure_keeps_existing_real_data(monkeypatch):
    today = marketgen._today().isoformat()
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", _fake_day(today, close=88000))
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    assert marketgen.seed_market(days=3, real=True)["source"] == "krx"

    def boom(_):
        raise ConnectionError("KRX down")
    monkeypatch.setattr(krx, "daily_stocks", boom)
    out = marketgen.seed_market(days=3, real=True)
    assert out["source"] == "kept" and "warning" in out            # 표본으로 덮어쓰지 않음
    assert db.get_meta("market_source") == "krx"
    assert client.get("/stocks/005930/detail").json()["price"] == 88000


def test_demo_seed_independent_of_hash_randomization():
    """예전엔 hash() 시드 → 프로세스마다(PYTHONHASHSEED) 데모 가격이 달라졌다. 다른 해시 시드 2개로 확인."""
    import os
    import subprocess
    import sys
    import app as app_pkg
    root = os.path.dirname(os.path.dirname(app_pkg.__file__))
    code = ("from app.collectors import marketgen as m;"
            "print(m._seed_of('000660'), [round(x) for x in m.gen_closes(5, 173000, m._seed_of('000660'))])")
    outs = {subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                           env={**os.environ, "PYTHONHASHSEED": s}).stdout.strip() for s in ("1", "2")}
    assert len(outs) == 1 and next(iter(outs))


def test_username_validation_blocks_script_injection():
    for bad in ["x'y", "<b>", "a b", "a", 'q"r']:
        assert client.post("/auth/register", json={"username": bad, "password": "pw123456"}).status_code == 400
    for ok in ["주원", "user_1-2.3", "me@oos.io"]:
        assert client.post("/auth/register", json={"username": ok, "password": "pw123456"}).status_code == 200


def test_krx_failure_on_one_day_keeps_that_days_existing_bar(monkeypatch):
    # 예전: 수집 중 하루 호출이 실패하면 '수집 구간 삭제'에 그날 기존 봉까지 지워져 차트·수익률에 구멍
    import requests
    from datetime import date, timedelta
    today = marketgen._today()
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    monkeypatch.setattr(krx, "daily_stocks", _fake_day(today.isoformat(), close=88000))
    assert marketgen.seed_market(days=6, real=True)["source"] == "krx"
    dates = [r["trade_date"] for r in db.query("SELECT trade_date FROM ohlc WHERE ticker='005930' ORDER BY trade_date")]
    victim = dates[-4]                                           # 최근에서 네 번째 거래일(초반 2회 이후 호출)
    ok = _fake_day(today.isoformat(), close=89000)

    def flaky(bas_dd):
        if bas_dd == victim:
            raise requests.ConnectionError("reset by peer")
        return ok(bas_dd)
    monkeypatch.setattr(krx, "daily_stocks", flaky)
    assert marketgen.seed_market(days=6, real=True)["source"] == "krx"
    after = {r["trade_date"]: r["close"] for r in db.query("SELECT trade_date, close FROM ohlc WHERE ticker='005930'")}
    assert after.get(victim) == 88000                            # 실패한 날은 기존 봉 유지(삭제 안 됨)
    assert after[dates[-1]] == 89000                             # 받은 날은 새 값으로 교체
    assert date.fromisoformat(victim) < today - timedelta(days=1)


def test_krx_load_of_top150_universe_commits(monkeypatch):
    """실데이터 유니버스(시총 상위 150) 적재가 커밋된다 — 종목별 KRX 시작일(krx_since) JSON 이 약 3.6KB.
    예전: app_meta.value 가 VARCHAR(2000) 라 'value too long' 으로 KRX 적재 전체가 매번 롤백 → 국내 시세가
    KIS 최근 8일 봉뿐이라 추천 Top 5 가 '데이터 부족(25거래일 미만)'으로 비었다(10-10 학교 VM).
    기존 테스트의 가짜 KRX 는 1종목이라 드러나지 않았다."""
    from datetime import date
    monkeypatch.setattr(marketgen, "_today", lambda: date(2026, 10, 9))
    codes = [f"{100000 + i * 37:06d}" for i in range(160)]

    def day(bas_dd):
        if bas_dd >= "2026-10-09":
            return {}
        return {c: {"name": f"종목{i:03d}", "market": "KOSDAQ GLOBAL" if i % 9 == 0 else "KOSPI",
                    "open": 10000, "high": 10100, "low": 9900, "close": 10000 + i, "volume": 1000 + i,
                    "value": 1, "mktcap": (200 - i) * 10 ** 12, "shares": 1, "change_rate": 0.1}
                for i, c in enumerate(codes)}
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", day)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    out = marketgen.seed_market(days=30, real=True)
    assert out["source"] == "krx" and db.get_meta("market_source") == "krx"
    raw = db.get_meta("krx_since")
    assert len(raw) > 2000 and len(json.loads(raw)) >= 150
    assert db.query("SELECT COUNT(*) AS n FROM stock")[0]["n"] >= 150
    assert db.query("SELECT COUNT(DISTINCT trade_date) AS n FROM ohlc WHERE ticker=:t", {"t": codes[0]})[0]["n"] == 30


def test_old_varchar_meta_column_is_widened_on_initdb(as_owner):
    """이미 설치된 DB(app_meta.value VARCHAR(2000))도 initdb·서버 시작 때 TEXT 로 넓혀진다(데이터 그대로)."""
    db.execute("ALTER TABLE app_meta ALTER COLUMN value TYPE VARCHAR(2000)")
    db.set_meta("keep_me", "그대로")
    applied = db.init_db()
    assert "app_meta.value→text" in applied
    typ = db.query("SELECT data_type FROM information_schema.columns WHERE table_name='app_meta' "
                   "AND column_name='value'")[0]["data_type"]
    assert typ == "text" and db.get_meta("keep_me") == "그대로"
    db.set_meta("big", "x" * 5000)
    assert len(db.get_meta("big")) == 5000
    assert "app_meta.value→text" not in db.init_db()                    # 두 번째부터는 할 일 없음(멱등)

