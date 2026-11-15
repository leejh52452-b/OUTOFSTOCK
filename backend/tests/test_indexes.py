"""코스피·코스닥 지수 — 다중 소스 폴백 · 출처/기준일 기록 · 실데이터를 상수로 덮지 않음(네트워크 없이 검증)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.collectors import indexes, krx

client = TestClient(app)
KST = timezone(timedelta(hours=9))


def _items():
    return {i["code"]: i for i in client.get("/indices").json()["items"]}


def test_demo_indices_are_labeled_sample():
    indexes.refresh(real=False)
    it = _items()
    assert it["KOSPI"]["source"] == "gen" and it["KOSPI"]["status"] == "sample"
    assert len(it["KOSPI"]["history"]) >= 20                       # 미니차트용 이력


def test_parse_kis_output_sign_and_asof():
    o = {"bstp_nmix_prpr": "6,724.34", "bstp_nmix_prdy_vrss": "57.11", "prdy_vrss_sign": "5",
         "bstp_nmix_prdy_ctrt": "0.84", "bstp_nmix_oprc": "6700", "bstp_nmix_hgpr": "6760",
         "bstp_nmix_lwpr": "6690", "acml_vol": "287884178"}
    wed_10am = datetime(2026, 9, 30, 10, 0, tzinfo=KST)
    r = indexes.parse_kis(o, wed_10am)
    assert r["value"] == 6724.34 and r["change"] == -57.11 and r["change_rate"] == -0.84   # 부호코드 5=하락
    assert r["as_of"] == "2026-09-30" and r["source"] == "kis"
    mon_8am = datetime(2026, 9, 28, 8, 0, tzinfo=KST)                                       # 개장 전 → 직전 영업일
    assert indexes.parse_kis({**o, "acml_vol": "0"}, mon_8am)["as_of"] == "2026-09-25"


def test_parse_yahoo_dates_in_exchange_timezone():
    base = int(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc).timestamp())   # 09:00 KST
    j = {"chart": {"result": [{"meta": {"gmtoffset": 32400, "regularMarketPrice": 6750.0,
                                        "regularMarketTime": base + 86400 + 6 * 3600},
                               "timestamp": [base - 86400, base],
                               "indicators": {"quote": [{"open": [6600, 6650], "high": [6650, 6700],
                                                         "low": [6590, 6640], "close": [6640.0, 6690.0]}]}}]}}
    y = indexes.parse_yahoo(j)
    assert [b["trade_date"] for b in y["bars"]] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert y["last"]["close"] == 6750.0 and y["bars"][1]["change"] == 50.0


def test_krx_index_name_variants(monkeypatch):
    fake = {krx.SVC_IDX_KOSPI: [{"IDX_NM": "코스피 200", "CLSPRC_IDX": "900"},
                                {"IDX_NM": "코스피 지수", "CLSPRC_IDX": "6,724.34", "CMPPREVDD_IDX": "6.37",
                                 "FLUC_RT": "0.09"}],
            krx.SVC_IDX_KOSDAQ: [{"IDX_NM": "KOSDAQ", "CLSPRC_IDX": "821.67", "CMPPREVDD_IDX": "5.69",
                                  "FLUC_RT": "0.70"}]}
    monkeypatch.setattr(krx, "_get", lambda svc, d: fake.get(svc, []))
    by = {r["code"]: r for r in krx.index_day("2026-09-17")}
    assert by["KOSPI"]["value"] == 6724.34 and by["KOSDAQ"]["value"] == 821.67


def test_krx_401_raises_auth_error(monkeypatch):
    class R:
        status_code = 401

        def raise_for_status(self):
            raise AssertionError("raise_for_status 전에 KrxAuthError 여야 함")
    monkeypatch.setattr(krx.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(krx.settings, "KRX_API_KEY", "k")
    with pytest.raises(krx.KrxAuthError):
        krx.index_day("2026-09-17")


def test_refresh_priority_and_history(monkeypatch):
    """KIS(실시간) 성공 → 값은 KIS, 미니차트 이력은 야후에서."""
    monkeypatch.setattr(indexes, "fetch_kis", lambda code: {"value": 6800.0, "change": 10.0, "change_rate": 0.15,
                                                            "as_of": "2026-09-30", "source": "kis"})
    bars = [{"trade_date": f"2026-09-{d:02d}", "open": 6000 + d, "high": 6000 + d, "low": 6000 + d,
             "close": 6000.0 + d} for d in range(1, 30)]
    monkeypatch.setattr(indexes, "fetch_yahoo", lambda code, rng="1y": {"bars": indexes._fill_changes(bars),
                                                                         "last": bars[-1]})
    r = indexes.refresh(real=True)
    assert r["KOSPI"]["source"] == "kis" and r["KOSPI"]["value"] == 6800.0
    hist = indexes.history("KOSPI", 60)
    assert len(hist) >= 29 and hist[-1]["close"] == 6800.0         # 오늘 KIS 값이 이력 마지막 봉
    it = _items()
    assert it["KOSPI"]["source"] == "kis" and it["KOSPI"]["as_of"] == "2026-09-30"


def test_krx_denied_falls_back_to_yahoo(monkeypatch):
    def denied(_d):
        raise krx.KrxAuthError("KRX idx/kospi_dd_trd 401")
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "index_day", denied)
    monkeypatch.setattr(indexes, "fetch_yahoo", lambda code, rng="1y": {
        "bars": [{"trade_date": "2026-09-29", "close": 6700.0}, {"trade_date": "2026-09-30", "close": 6724.0,
                                                                  "change": 24.0, "change_rate": 0.36}],
        "last": {"trade_date": "2026-09-30", "close": 6724.0, "change": 24.0, "change_rate": 0.36}})
    r = indexes.refresh(real=True, sources=("krx", "yahoo"))
    assert r["KOSPI"]["source"] == "yahoo" and "401" in r["errors"]["KOSPI"]["krx"]


def test_all_sources_fail_keeps_previous_real_value_never_constant(monkeypatch):
    with db.transaction() as conn:
        indexes.upsert_index(conn, "KOSPI", "코스피", 6724.34, 6.37, 0.09, "2026-09-17", "krx")
    r = indexes.refresh(real=True, sources=("kis", "yahoo", "fdr"))  # conftest 가 외부 호출을 모두 막아 둠
    assert r["KOSPI"]["source"] == "kept"
    row = db.query("SELECT value, source, as_of FROM market_index WHERE code='KOSPI'")[0]
    assert row["value"] == 6724.34 and row["source"] == "krx" and row["as_of"] == "2026-09-17"   # 상수로 안 덮음
    assert r["KOSDAQ"]["source"] == "none"                          # 실데이터가 한 번도 없으면
    assert db.query("SELECT source FROM market_index WHERE code='KOSDAQ'")[0]["source"] == "gen"   # 표본 표시


def test_stale_flag():
    with db.transaction() as conn:
        indexes.upsert_index(conn, "KOSPI", "코스피", 6000.0, 0, 0, "2026-01-02", "krx")
    assert _items()["KOSPI"]["stale"] is True


def test_health_reports_index_lineage_issues():
    db.set_meta("market_source", "krx")
    indexes.refresh(real=True, sources=("kis",))                    # 실패 → 실데이터가 없으니 표본
    from app.services import monitoring
    assert "index_sample" in client.get("/health").json()["issues"]          # 공개: 문제 코드만
    h = monitoring.health()                                                   # 상세(/admin/health)
    assert "index_sample" in h["issues"] and h["indices"]["items"][0]["source"] == "gen"
    assert "indices" not in client.get("/health").json()
    with db.transaction() as conn:
        indexes.upsert_index(conn, "KOSPI", "코스피", 6724.34, 6.37, 0.09, "2026-09-30", "krx")
        indexes.upsert_index(conn, "KOSDAQ", "코스닥", 821.67, 5.69, 0.70, "2026-09-30", "krx")
    db.set_meta("kr_index_status", '{"errors": {"KOSPI": {"krx": "KrxAuthError: KRX idx/kospi_dd_trd 401"}}}')
    h = client.get("/health").json()
    assert "index_sample" not in h["issues"] and "krx_index_denied" in h["issues"]


def test_old_schema_still_serves_and_server_start_migrates(as_owner):
    """업데이트 후 initdb 를 안 돌려도 /indices 가 500 이 나지 않고, 서버 시작(lifespan) 때 자동 마이그레이션."""
    from sqlalchemy import text
    with db.transaction() as conn:
        conn.execute(text("ALTER TABLE market_index DROP COLUMN source"))
        conn.execute(text("ALTER TABLE market_index DROP COLUMN as_of"))
    try:
        db.execute("INSERT INTO market_index(code,name,value,change,change_rate,ts) "
                   "VALUES('KOSPI','코스피',6700,1,0.1,'2026-09-30')")
        it = _items()
        assert it["KOSPI"]["value"] == 6700 and it["KOSPI"]["status"] == "unknown"
        with TestClient(app):                                         # startup → db.init_db()
            pass
        cols = {r["column_name"] for r in db.query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='market_index'")}
        assert {"source", "as_of"} <= cols
    finally:
        db.init_db()


def test_indices_triggers_self_healing_when_missing_or_sample(monkeypatch):
    calls = []
    monkeypatch.setattr(indexes, "maybe_refresh_async", lambda now=None, bootstrap=False: calls.append(bootstrap))
    client.get("/indices")                                            # 행 없음 → 자가 복구 요청
    with db.transaction() as conn:
        indexes.upsert_index(conn, "KOSPI", "코스피", 6724.34, 6.37, 0.09, "2026-09-30", "krx")
        indexes.upsert_index(conn, "KOSDAQ", "코스닥", 821.67, 5.69, 0.70, "2026-09-30", "yahoo")
    client.get("/indices")                                            # 실데이터 있음 → 평소(장중만) 갱신
    assert calls == [True, False]


def test_bootstrap_refresh_runs_in_background_only_in_real_mode(monkeypatch):
    from app.config import settings
    ran = []
    monkeypatch.setattr(indexes, "refresh", lambda **k: ran.append(k))
    monkeypatch.setattr(indexes, "_last_bg", float("-inf"))
    assert indexes.maybe_refresh_async(bootstrap=True) is False       # 데모/테스트(market_source≠krx)
    monkeypatch.setattr(settings, "INDEX_LIVE_REFRESH", True)
    monkeypatch.setattr(settings, "MARKET_SOURCE", "krx")
    db.set_meta("market_source", "krx")
    assert indexes.maybe_refresh_async(bootstrap=True) is True
    import time as _t
    for _ in range(50):
        if ran:
            break
        _t.sleep(0.02)
    assert ran == [{"real": True}]
    assert indexes.maybe_refresh_async(bootstrap=True) is False       # 5분 안엔 다시 안 함
