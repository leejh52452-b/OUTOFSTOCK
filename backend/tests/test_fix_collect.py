"""2026-10 전체 검토 — 수집기·배치·CLI 회귀 테스트(외부 API 는 전부 가짜 응답)."""
import json
import logging
import os
import subprocess
import sys
import threading

import app as app_pkg
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.collectors import dart, edgar, overseas_market, overseas_realtime, realtime
from app.logging_conf import redact, safe_error

client = TestClient(app)
ROOT = os.path.dirname(os.path.dirname(app_pkg.__file__))


# ── API 키가 로그·/health 에 새지 않는다 ─────────────────────
def test_keys_are_redacted_in_logs_and_run_notes(monkeypatch, caplog):
    monkeypatch.setattr(settings, "DART_API_KEY", "dartSECRET1234567890")
    url = "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=dartSECRET1234567890"
    with caplog.at_level(logging.WARNING):
        logging.getLogger("t").warning("재시도 1/3: 404 for url: %s", url)
    assert "dartSECRET" not in caplog.text and "crtfc_key=***" in caplog.text
    assert "zzz" not in redact("https://finnhub.io/api/v1/quote?symbol=A&token=zzz999")
    assert "dartSECRET" not in safe_error(RuntimeError(url))
    from app.services import monitoring
    rid = monitoring.start_run()
    monitoring.finish_run(rid, "FAILED", note=f"실패: {url}")
    assert "dartSECRET" not in json.dumps(client.get("/health").json(), ensure_ascii=False)
    db.execute("UPDATE pipeline_run SET note=:n", {"n": "old leak authkey=EXIMKEY123&x=1"})   # 예전 기록
    assert "EXIMKEY" not in monitoring.health()["last_run"]["note"]          # 상세(관리자용)에서도 가려짐
    assert "note" not in (client.get("/health").json()["last_run"] or {})    # 공개 응답엔 내부 문구 자체가 없음


def test_finnhub_key_goes_in_header_not_url(monkeypatch):
    from app.collectors import finnhub
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"c": 1}
    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "fhKEY123456")
    monkeypatch.setattr(finnhub.requests, "get", lambda url, params=None, timeout=None, headers=None:
                        seen.update(url=url, params=params, headers=headers) or R())
    finnhub._get("/quote", {"symbol": "AAPL"})
    assert "token" not in (seen["params"] or {}) and seen["headers"]["X-Finnhub-Token"] == "fhKEY123456"


# ── DART 오류 본문 · EDGAR 연간값 ─────────────────────────
def test_dart_error_body_does_not_abort(monkeypatch):
    class R:
        content = b'{"status":"020","message":"\\uc694\\uccad \\uc81c\\ud55c"}'

        def json(self):
            return {"status": "020", "message": "요청 제한"}
    monkeypatch.setattr(settings, "DART_API_KEY", "k" * 40)
    monkeypatch.setattr(dart, "_get", lambda *a, **k: R())
    out = dart.collect_financials(["005930"])
    assert out["ok"] == 0 and "020" in out["error"]                   # 예전엔 BadZipFile 로 배치 전체 중단


def test_edgar_picks_full_year_not_q4():
    usg = {"Revenues": {"units": {"USD": [
        {"start": "2024-10-01", "end": "2025-09-27", "val": 400, "form": "10-K", "fp": "FY", "fy": 2025,
         "filed": "2025-10-31"},
        {"start": "2025-06-29", "end": "2025-09-27", "val": 100, "form": "10-K", "fp": "FY", "fy": 2025,
         "filed": "2025-10-31"}]}}}
    assert edgar._latest_annual(usg, ["Revenues"])["value"] == 400


# ── KIS 초당 한도(HTTP 500 + EGW00201) 는 재시도 ──────────────
def test_kis_rate_limit_is_detected_and_retried(monkeypatch):
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def json(self):
            return self.body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise realtime.requests.HTTPError(f"{self.status_code} Server Error")

    def get(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return R(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."})
        return R(200, {"rt_cd": "0", "output": [{"stck_bsop_date": "20260930", "stck_clpr": "70000",
                                                 "stck_oprc": "0", "stck_hgpr": "0", "stck_lwpr": "0",
                                                 "acml_vol": "1"}]})
    monkeypatch.setattr(settings, "KIS_APP_KEY", "a")
    monkeypatch.setattr(settings, "KIS_APP_SECRET", "b")
    monkeypatch.setattr(realtime, "get_access_token", lambda: "tok")
    monkeypatch.setattr(realtime.requests, "get", get)
    monkeypatch.setattr(realtime.time, "sleep", lambda s: None)
    out = realtime.refresh_latest_bars(["005930"])
    assert out == {"ok": 1, "fail": 0} and len(calls) == 2


def test_kis_token_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "KIS_APP_KEY", "a")
    monkeypatch.setattr(settings, "KIS_APP_SECRET", "b")

    def boom():
        raise realtime.requests.HTTPError("403 Forbidden")
    monkeypatch.setattr(realtime, "get_access_token", boom)
    assert "error" in realtime.refresh_latest_bars(["005930"])


# ── 해외: 잠깐 실패해도 실데이터 유지 · 폴러 · 프리마켓 ─────────
def test_overseas_transient_failure_keeps_real_bars(monkeypatch):
    bars = [{"trade_date": f"2026-09-{d:02d}", "open": 1, "high": 1, "low": 1, "close": 279.0 + d, "volume": 1}
            for d in range(21, 26)]
    monkeypatch.setattr(overseas_market, "fetch_fdr_bars", lambda s, days=480, finished_only=True: bars)
    monkeypatch.setattr(overseas_market, "fetch_fx", lambda: None)            # 환율 네트워크는 트랜잭션 밖(fetch_fx)
    monkeypatch.setattr(overseas_market, "seed_fx", lambda conn, fetched=None: None)
    monkeypatch.setattr(overseas_market, "snapshot_quotes", lambda syms: 0)
    import app.collectors.edgar as ed
    monkeypatch.setattr(ed, "shares_outstanding", lambda s: 15_000_000_000)
    monkeypatch.setattr("time.sleep", lambda s: None)
    overseas_market.seed_overseas(real=True, symbols=["AAPL"])

    def boom(*a, **k):
        raise ConnectionError("yahoo down")
    monkeypatch.setattr(overseas_market, "fetch_fdr_bars", boom)
    out = overseas_market.seed_overseas(real=True, symbols=["AAPL"])
    assert out["kept"] == 1 and "warning" in out
    row = db.query("SELECT source, shares FROM overseas_stock WHERE symbol='AAPL'")[0]
    assert row["source"] == "fdr" and row["shares"] == 15_000_000_000
    assert db.query("SELECT MAX(close) AS c FROM overseas_ohlc WHERE symbol='AAPL'")[0]["c"] == 304.0


def test_fx_change_uses_previous_business_day(monkeypatch):
    from app.collectors import forex
    monkeypatch.setattr(forex, "usd_krw_quote", lambda max_days=10: {"rate": 1400.0, "prev": 1390.0,
                                                                     "date": "2026-09-30", "prev_date": "2026-09-29"})
    assert forex.usd_krw_pair() == (1400.0, 1390.0)                      # 예전 형식(튜플)도 같은 값
    db.execute("INSERT INTO market_index(code,name,value,change,change_rate,ts) "
               "VALUES('USDKRW','원/달러 환율',1400.0,0,0,'2026-10-01T09:00:00+09:00')")       # 같은 값이 이미 저장돼 있어도
    with db.transaction() as conn:
        assert overseas_market.seed_fx(conn) == "koreaexim"
    r = db.query("SELECT value, change FROM market_index WHERE code='USDKRW'")[0]
    assert r["value"] == 1400.0 and r["change"] == 10.0                  # 예전엔 0.00


def test_finnhub_poller_survives_errors_and_premarket_does_not_flip(monkeypatch):
    book = overseas_realtime._Book()
    book.trade("AAPL", 256.0, 1_790_000_000_000)                        # 오늘 프리마켓 체결(새 거래일)
    sdate_today = book.s["AAPL"]["session_date"]
    st = book.base("AAPL", {"price": 250.0, "prev_close": 249.0, "open": 1, "high": 1, "low": 1},
                   "2000-01-01")                                         # /quote 는 아직 전날 값
    assert st["price"] == 256.0 and st["session_date"] == sdate_today and st["prev_close"] == 250.0
    stop, calls = threading.Event(), []

    def quote(sym):
        calls.append(sym)
        if len(calls) == 1:
            raise RuntimeError("db down")
        stop.set()
        return None
    monkeypatch.setattr(overseas_realtime.finnhub, "quote", quote)
    monkeypatch.setattr(overseas_realtime.time, "sleep", lambda s: None)
    overseas_realtime._poller(stop, overseas_realtime._Book(), ["AAPL", "MSFT"])
    assert calls == ["AAPL", "MSFT"]                                      # 첫 실패 뒤에도 계속 폴링


# ── 배치: 한 단계 실패가 나머지를 막지 않음 ─────────────────
def test_real_pipeline_continues_after_step_failure(monkeypatch):
    import run
    from app.collectors import marketgen, news
    from app.services import practice
    ran = []

    def fail_market(real=False, days=180):
        raise RuntimeError("KRX https://x?crtfc_key=abc123SECRET down")
    monkeypatch.setattr(marketgen, "seed_market", fail_market)
    monkeypatch.setattr(marketgen, "seed_scenario_ohlc", lambda sc, real=False: ran.append("practice") or 0)
    monkeypatch.setattr(dart, "collect_financials", lambda: ran.append("dart") or {"ok": 0})
    monkeypatch.setattr(news, "collect_news", lambda: ran.append("news") or {"total": 0})
    monkeypatch.setattr(overseas_market, "seed_overseas", lambda real=False: ran.append("us") or {"source": "gen"})
    monkeypatch.setattr(realtime, "enabled", lambda: False)
    out = run.real_pipeline("test")
    assert {"practice", "dart", "news", "us"} <= set(ran) and len([r for r in ran if r == "practice"]) == \
        len(practice.SCENARIOS)
    assert out["errors"] and "KRX 시세" in out["errors"][0]
    last = db.query("SELECT status, note FROM pipeline_run ORDER BY run_id DESC LIMIT 1")[0]
    assert last["status"] == "PARTIAL" and "abc123SECRET" not in json.dumps(out, ensure_ascii=False)
    assert "last_pipeline_partial" in client.get("/health").json()["issues"]


# ── CLI ────────────────────────────────────────────────────
def _run(*args, env=None):
    return subprocess.run([sys.executable, "run.py", *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=120)


def test_run_without_args_only_prints_usage():
    r = _run()
    assert r.returncode == 0 and "사용법" in r.stdout and "데모 데이터 준비 완료" not in r.stdout


def test_demo_refuses_to_overwrite_real_data():
    url = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://oos:oos@localhost:5432/outofstock_test")
    db.set_meta("market_source", "krx")
    r = _run("demo", env={"DATABASE_URL": url})
    assert r.returncode == 2 and "--force" in r.stdout
    assert db.get_meta("market_source") == "krx"


def test_realtime_exits_nonzero_when_collector_dies(monkeypatch):
    import run
    monkeypatch.setattr(realtime, "enabled", lambda: True)
    monkeypatch.setattr(realtime, "run_realtime", lambda stop=None: None)   # 스레드가 바로 끝남(죽음)
    monkeypatch.setattr(overseas_realtime, "enabled", lambda: False)
    try:
        run._run_realtime("kr")
        raise AssertionError("종료 코드 0 으로 끝남")
    except SystemExit as e:
        assert e.code == 1


def test_init_db_is_safe_to_run_concurrently():
    errs = []

    def go():
        try:
            db.init_db()
        except Exception as e:                   # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert db.get_meta("migration:20260929-news-title-tail") == "applied"
