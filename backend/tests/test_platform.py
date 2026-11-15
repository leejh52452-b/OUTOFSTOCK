"""데이터 플랫폼(분석 계층) — 마이그레이션·파티션·대량 적재(COPY)·재개·멱등·KRX 캐시·스냅샷.
2번(전 종목 × 10년) 기능을 외부 호출 없이(가짜 KRX 응답) 숫자로 확인한다."""
from datetime import date

import pytest
from sqlalchemy import text

from app import db, migrations
from app.collectors import krx, krx_history as kh


def _rows(d: date, tickers, base=10000, halted=()):
    """결정적 가짜 하루치 — 종목 i 의 종가 = base + i*100 + (날짜 서수 % 50)*10."""
    out = []
    for i, t in enumerate(tickers):
        c = base + i * 100 + (d.toordinal() % 50) * 10
        h = t in halted
        out.append({"ticker": t, "name": f"종목{t}", "market": "KOSPI", "sect_tp": None,
                    "open": None if h else c - 50, "high": None if h else c + 100, "low": None if h else c - 100,
                    "close": c, "volume": 0 if h else 1000 + i, "value": 0 if h else c * (1000 + i),
                    "mktcap": c * 1_000_000, "shares": 1_000_000, "change_rate": 0.5, "halted": h})
    return out


def _fetch_factory(tickers, empty=(), fail=(), calls=None, halted=()):
    def fetch(bas_dd, mk):
        if calls is not None:
            calls.append((bas_dd, mk))
        d = date.fromisoformat(bas_dd)
        if d in fail:
            raise ConnectionError("KRX down")
        if d in empty or mk == "Q":
            return []
        return _rows(d, tickers, halted=halted)
    return fetch


# ── 마이그레이션 ───────────────────────────────────────────
def test_migrations_applied_with_checksums(as_owner):
    st = migrations.status()
    assert [s["version"] for s in st][:3] == ["001", "002", "003"]
    assert all(s["state"] == "applied" for s in st)


def test_changed_applied_migration_blocks_start(as_owner):
    with db.transaction() as conn:
        conn.execute(text("UPDATE analytics.schema_migrations SET checksum = 'x' WHERE version = '001'"))
    try:
        with pytest.raises(migrations.MigrationError, match="V001"):
            db.init_db()
    finally:
        with db.transaction() as conn:
            conn.execute(text("UPDATE analytics.schema_migrations SET checksum = :c WHERE version = '001'"),
                         {"c": migrations.checksum(migrations.files()[0][2])})
    assert not [a for a in db.init_db() if a.startswith("V")]    # 다시 정상(대기 중인 SQL 마이그레이션 없음)


# ── 적재 ──────────────────────────────────────────────────
def test_load_day_bulk_copy_partition_and_idempotency():
    d = date(2016, 3, 2)
    fetch = _fetch_factory(["000001", "000002", "000003"])
    r = kh.load_day(d, fetch=fetch)
    assert r["ok"] == 1 and r["empty"] == 1 and r["rows"] == 3            # 코스닥은 빈 응답(가짜)
    part = db.query("SELECT DISTINCT tableoid::regclass::text AS p FROM analytics.price_history")[0]["p"]
    assert part == "analytics.price_history_2016"                          # 연도 파티션에 들어감
    again = kh.store_day(d, "K", fetch(d.isoformat(), "K"))
    assert again["rows_upserted"] == 0                                      # 같은 날 재적재 = 바뀐 행 0(멱등)
    changed = fetch(d.isoformat(), "K")
    changed[0]["close"] += 10
    assert kh.store_day(d, "K", changed)["rows_upserted"] == 1             # 정정된 1행만 갱신
    log = db.query("SELECT status, attempts, checksum FROM analytics.load_log WHERE part_key = '2016-03-02:K'")[0]
    assert log["status"] == "ok" and log["attempts"] == 3 and len(log["checksum"]) == 64


def test_halted_rows_are_flagged():
    d = date(2025, 5, 7)
    kh.load_day(d, fetch=_fetch_factory(["000001", "000002"], halted={"000002"}))
    rows = {r["ticker"]: r for r in db.query("SELECT ticker, halted, open FROM analytics.price_history")}
    assert rows["000002"]["halted"] is True and rows["000002"]["open"] is None and rows["000001"]["halted"] is False


def test_backfill_is_resumable_with_budget_and_retries_failures():
    calls = []
    end = date(2025, 1, 31)
    fail = {date(2025, 1, 24)}                                             # (1/27~30 은 설 연휴 — 거래일 아님)
    fetch = _fetch_factory(["000001", "000002"], fail=fail, calls=calls)
    r1 = kh.backfill(years=1, budget=10, end=end, fetch=fetch, refresh=False)
    assert r1["calls"] == 10 and r1["stopped"] and r1["failed"] == 2      # 예산 10회에서 멈춤(그날 두 시장 실패)
    first = set(calls)
    calls.clear()
    fetch2 = _fetch_factory(["000001", "000002"], calls=calls)             # 장애 복구
    kh.backfill(years=1, budget=6, end=end, fetch=fetch2, refresh=False)
    assert ("2025-01-24", "K") in calls                                    # 실패했던 날을 다시 받음
    assert not ({c for c in calls if c[0] != "2025-01-24"} & first)        # 받은 날은 다시 안 부름


def test_backfill_stops_after_consecutive_failures():
    fetch = _fetch_factory(["000001"], fail={date(2025, 1, d) for d in range(1, 32)})
    r = kh.backfill(years=1, budget=1000, end=date(2025, 1, 31), fetch=fetch, refresh=False)
    assert "연속" in r["stopped"] and r["calls"] == kh.MAX_CONSECUTIVE_FAILS * 2


def test_master_keeps_latest_name_when_backfilling_older_days():
    rows_new = _rows(date(2025, 6, 2), ["000001"])
    rows_new[0]["name"] = "새이름"
    kh.store_day(date(2025, 6, 2), "K", rows_new)
    rows_old = _rows(date(2020, 6, 2), ["000001"])
    rows_old[0]["name"] = "옛이름"
    kh.store_day(date(2020, 6, 2), "K", rows_old)
    m = db.query("SELECT name, first_seen, last_seen FROM analytics.security_master")[0]
    assert m["name"] == "새이름" and str(m["first_seen"]) == "2020-06-02" and str(m["last_seen"]) == "2025-06-02"


# ── KRX 호출 캐시 ──────────────────────────────────────────
def test_daily_stocks_served_from_history_without_calling_krx(monkeypatch):
    d = date(2024, 11, 5)
    kh.load_day(d, fetch=_fetch_factory(["005930", "000660"], halted={"000660"}))

    def no_call(*a, **k):
        raise AssertionError("KRX 를 부르면 안 됨")
    monkeypatch.setattr(krx, "_get", no_call)
    day = krx.daily_stocks(d.isoformat())
    assert set(day) == {"005930", "000660"}
    assert day["000660"]["open"] == day["000660"]["close"]                # 정지 봉은 종가로 채운 같은 모양
    assert krx.daily_stocks(d.isoformat(), markets=("KOSPI",)).keys() == day.keys()


def test_recent_or_unloaded_days_still_call_krx(monkeypatch):
    called = []
    monkeypatch.setattr(krx, "_get", lambda svc, dd: called.append(dd) or [])
    krx.daily_stocks("2024-11-06")                                         # 적재 안 된 날
    assert called


# ── 스냅샷(골라보기·산업의 입력) ─────────────────────────────
def test_snapshot_returns_streak_and_sector_performance():
    tickers = ["000001", "000002", "000003"]
    days = list(reversed(kh.trading_days(date(2025, 1, 1), date(2025, 3, 31))))
    for i, d in enumerate(days):
        rows = _rows(d, tickers)
        for j, r in enumerate(rows):                       # 000001 은 매일 1% 상승, 나머지는 보합
            r["close"] = int(10000 * (1.01 ** i)) if j == 0 else 20000
            r["change_rate"] = 1.0 if j == 0 else 0.0
            r["high"] = r["close"]
        kh.store_day(d, "K", rows)
    db.execute("UPDATE analytics.security_master SET sector = '반도체' WHERE ticker IN ('000001','000002')")
    db.execute("UPDATE analytics.security_master SET sector = '은행' WHERE ticker = '000003'")
    snap = kh.refresh_snapshot()
    assert snap["ok"] and snap["rows"] == 3
    s = {r["ticker"]: r for r in db.query("SELECT * FROM analytics.mv_stock_snapshot")}
    assert float(s["000001"]["ret_1d"]) == pytest.approx(1.0, abs=0.05)
    assert s["000001"]["up_streak"] == len(days) - 1 or s["000001"]["up_streak"] >= 20
    assert s["000001"]["new_high_52w"] is True and s["000002"]["new_high_52w"] is False
    perf = {r["sector"]: r for r in db.query("SELECT * FROM analytics.sector_performance('1m')")}
    assert perf["반도체"]["members"] == 2 and float(perf["반도체"]["ret"]) > 0
    assert float(perf["은행"]["ret"]) == 0


# ── 이름 있는 분석 쿼리(ADR-0004 — 화면 API 를 지운 뒤에도 CLI 가 같은 경계를 쓴다) ──────────
def test_named_queries_only_accept_declared_params_and_run_read_only():
    from app.services import platform
    qs = platform.queries()
    assert {"volume_spike", "drawdown_1y", "sector_rotation"} <= set(qs)
    for bad in ({"min_ratio": "1;DROP TABLE x"}, {"min_ratio": "0"}, {"min_ratio": "nan"}, {"limit": "1e9"}):
        with pytest.raises(platform.QueryError):
            platform.run_query("volume_spike", bad)                     # 형식·범위 밖은 SQL 에 닿기 전에 거부
    with pytest.raises(platform.QueryError):
        platform.run_query("../../etc/passwd")                          # 이름은 등록된 파일만
    with platform.ro_conn() as c:                                       # 읽기 전용 트랜잭션 — 쓰기는 DB 가 거부
        with pytest.raises(Exception) as e:
            c.execute(text("CREATE TEMP TABLE oos_ro_probe(x int)"))
        assert "read-only" in str(e.value).lower() or "ReadOnlySqlTransaction" in type(e.value.orig).__name__
