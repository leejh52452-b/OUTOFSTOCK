"""분석 계층 적재기 — KRX 전 종목 일별 시세 → analytics.price_history (10년 백필 + 매일 증분).

설계(면접 포인트)
  · 적재 단위 = (거래일, 시장). KRX 는 하루치 전 종목을 한 번에 준다 → 10년 ≈ 2,450거래일 × 2시장 ≈ 4,900번 호출.
  · 재개 가능(resumable): 단위마다 analytics.load_log 에 결과(ok·empty·failed)를 남기고, 다음 실행은
    ok/empty 가 아닌 단위만 다시 받는다. 하루 호출 예산(budget)을 넘기면 멈추고 다음 날 이어서.
  · 대량 적재: 하루치(약 2,700행)를 COPY 로 임시 표에 넣고 INSERT … ON CONFLICT 로 합친다 —
    행마다 INSERT 하는 것보다 왕복이 적고, 같은 날을 다시 받아도 결과가 같다(멱등). 바뀐 행만 갱신(IS DISTINCT FROM).
  · 체크섬: 받은 행 내용의 SHA-256 을 남겨 같은 날 재적재 때 '원천 데이터가 바뀌었는지'를 안다(정정 감지).
  · 한 단위 = 한 트랜잭션: 중간에 실패하면 그 날·그 시장만 롤백되고 load_log 에 failed 로 남는다.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)

DATASET = "krx_daily"
MARKETS = ("K", "Q")
MAX_CONSECUTIVE_FAILS = 5                 # KRX 가 연속으로 실패하면(장애·한도) 그 회차는 멈춘다
_COLS = ("ticker", "trade_date", "mkt", "open", "high", "low", "close", "volume", "value", "mktcap",
         "change_rate", "halted")


def enabled():
    from app.collectors import krx
    return krx.enabled()


# ── 거래일 달력 ────────────────────────────────────────────
def trading_days(start: date, end: date):
    """start~end(포함)의 국내 거래일(주말·휴장일 제외) — 최신일부터(내림차순)."""
    from app.services import quotes
    out, d = [], end
    while d >= start:
        if d.weekday() < 5 and not quotes.is_holiday("kr", d):
            out.append(d)
        d -= timedelta(days=1)
    return out


def last_complete_day(today: date | None = None) -> date:
    """KRX 가 데이터를 주는 가장 최근 거래일(D+1 제공 — 오늘 18시 전이면 그 전 거래일)."""
    from app.services.quality import last_kr_trading_day
    now = clock.now()
    today = today or now.date()
    return last_kr_trading_day(today + timedelta(days=1)) if now.hour >= 18 and today == now.date() \
        else last_kr_trading_day(today)


# ── 적재 이력 ──────────────────────────────────────────────
def load_states(dataset=DATASET):
    try:
        return {r["part_key"]: r["status"] for r in db.query(
            "SELECT part_key, status FROM analytics.load_log WHERE dataset = :ds", {"ds": dataset})}
    except Exception:                                     # noqa: BLE001
        return {}


def record_load(conn, part_key, status, *, dataset=DATASET, rows_in=None, rows_upserted=None,
                checksum=None, source="krx", message=None):
    conn.execute(text(
        "INSERT INTO analytics.load_log(dataset, part_key, status, rows_in, rows_upserted, checksum, source, message,"
        " attempts, started_at, finished_at) VALUES(:ds, :k, :st, :ri, :ru, :cs, :src, :msg, 1, now(), now()) "
        "ON CONFLICT (dataset, part_key) DO UPDATE SET status = EXCLUDED.status, rows_in = EXCLUDED.rows_in, "
        " rows_upserted = EXCLUDED.rows_upserted, checksum = EXCLUDED.checksum, source = EXCLUDED.source, "
        " message = EXCLUDED.message, attempts = analytics.load_log.attempts + 1, "
        " started_at = EXCLUDED.started_at, finished_at = now()"),
        {"ds": dataset, "k": part_key, "st": status, "ri": rows_in, "ru": rows_upserted, "cs": checksum,
         "src": source, "msg": (message or None) and str(message)[:500]})


def _checksum(rows):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x["ticker"]):
        h.update(("|".join(str(r.get(k)) for k in ("ticker", "open", "high", "low", "close", "volume", "value",
                                                     "mktcap", "change_rate")) + "\n").encode())
    return h.hexdigest()


# ── 쓰기 ──────────────────────────────────────────────────
def upsert_master(conn, rows, d: date):
    """종목 마스터 갱신 — 더 최근 날짜의 정보만 이름·시장·소속부를 덮는다(백필은 과거로 가므로)."""
    conn.execute(text(
        "INSERT INTO analytics.security_master(ticker, name, market, sect_tp, listed_shares, first_seen, last_seen) "
        "VALUES(:ticker, :name, :market, :sect_tp, :shares, :d, :d) "
        "ON CONFLICT (ticker) DO UPDATE SET "
        " name = CASE WHEN EXCLUDED.last_seen >= analytics.security_master.last_seen THEN EXCLUDED.name "
        "             ELSE analytics.security_master.name END, "
        " market = CASE WHEN EXCLUDED.last_seen >= analytics.security_master.last_seen THEN EXCLUDED.market "
        "               ELSE analytics.security_master.market END, "
        " sect_tp = CASE WHEN EXCLUDED.last_seen >= analytics.security_master.last_seen THEN EXCLUDED.sect_tp "
        "                ELSE analytics.security_master.sect_tp END, "
        " listed_shares = CASE WHEN EXCLUDED.last_seen >= analytics.security_master.last_seen "
        "                      THEN COALESCE(EXCLUDED.listed_shares, analytics.security_master.listed_shares) "
        "                      ELSE analytics.security_master.listed_shares END, "
        " first_seen = LEAST(analytics.security_master.first_seen, EXCLUDED.first_seen), "
        " last_seen = GREATEST(analytics.security_master.last_seen, EXCLUDED.last_seen), updated_at = now()"),
        [{**r, "d": d} for r in rows])


def copy_upsert(conn, rows, d: date, mk: str) -> int:
    """하루·한 시장 행들 → COPY(임시 표) → INSERT … ON CONFLICT. 반환: 새로 들어가거나 바뀐 행 수."""
    conn.execute(text("SELECT analytics.ensure_price_partition(:y)"), {"y": d.year})
    raw = conn.connection.driver_connection
    with raw.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _ph_stage (LIKE analytics.price_history) ON COMMIT DROP")
        cur.execute("TRUNCATE _ph_stage")
        with cur.copy(f"COPY _ph_stage ({', '.join(_COLS)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row((r["ticker"], d, mk, r["open"], r["high"], r["low"], r["close"], r["volume"],
                              r["value"], r["mktcap"], r["change_rate"], r["halted"]))
        cur.execute(
            f"INSERT INTO analytics.price_history ({', '.join(_COLS)}) SELECT {', '.join(_COLS)} FROM _ph_stage "
            "ON CONFLICT (ticker, trade_date) DO UPDATE SET mkt = EXCLUDED.mkt, open = EXCLUDED.open, "
            "high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume, "
            "value = EXCLUDED.value, mktcap = EXCLUDED.mktcap, change_rate = EXCLUDED.change_rate, "
            "halted = EXCLUDED.halted "
            "WHERE (analytics.price_history.open, analytics.price_history.high, analytics.price_history.low, "
            "analytics.price_history.close, analytics.price_history.volume, analytics.price_history.value, "
            "analytics.price_history.mktcap, analytics.price_history.change_rate, analytics.price_history.halted) "
            "IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume, "
            "EXCLUDED.value, EXCLUDED.mktcap, EXCLUDED.change_rate, EXCLUDED.halted)")
        return cur.rowcount or 0


def store_day(d: date, mk: str, rows, source="krx"):
    """한 단위 적재(한 트랜잭션). 반환 {status, rows_in, rows_upserted}."""
    key = f"{d.isoformat()}:{mk}"
    with db.transaction() as conn:
        if not rows:
            record_load(conn, key, "empty", rows_in=0, rows_upserted=0, source=source,
                        message="빈 응답(휴장일이거나 아직 제공 전)")
            return {"status": "empty", "rows_in": 0, "rows_upserted": 0}
        upsert_master(conn, rows, d)
        n = copy_upsert(conn, rows, d, mk)
        record_load(conn, key, "ok", rows_in=len(rows), rows_upserted=n, checksum=_checksum(rows), source=source)
    return {"status": "ok", "rows_in": len(rows), "rows_upserted": n}


def load_day(d: date, markets=MARKETS, fetch=None):
    """KRX 에서 하루치(시장별)를 받아 적재. 실패한 시장은 failed 로 남기고 나머지는 계속.
    반환 {calls, ok, empty, failed, rows}."""
    from app.collectors import krx
    from app.logging_conf import safe_error
    fetch = fetch or krx.daily_raw
    out = {"calls": 0, "ok": 0, "empty": 0, "failed": 0, "rows": 0, "errors": []}
    for mk in markets:
        out["calls"] += 1
        try:
            rows = fetch(d.isoformat(), mk)
        except Exception as e:                               # noqa: BLE001 — 그 단위만 실패로 남긴다
            msg = safe_error(e, 300)
            with db.transaction() as conn:
                record_load(conn, f"{d.isoformat()}:{mk}", "failed", message=msg)
            out["failed"] += 1
            out["errors"].append(f"{d.isoformat()}:{mk} {msg}")
            continue
        r = store_day(d, mk, rows)
        out[r["status"]] += 1
        out["rows"] += r["rows_in"]
    return out


def backfill(years=None, budget=None, end: date | None = None, fetch=None, refresh=True):
    """최근 years 년을 최신일부터 거꾸로 채운다(이미 ok/empty 인 단위는 건너뜀). 호출 예산 budget 에서 멈춘다.
    반환 {calls, days, rows, failed, remaining, stopped}."""
    years = years or settings.ANALYTICS_YEARS
    budget = settings.ANALYTICS_BACKFILL_BUDGET if budget is None else budget
    end = end or last_complete_day()
    start = date(end.year - years, end.month, min(end.day, 28))
    states = load_states()
    todo = [(d, [mk for mk in MARKETS if states.get(f"{d.isoformat()}:{mk}") not in ("ok", "empty")])
            for d in trading_days(start, end)]
    todo = [(d, mks) for d, mks in todo if mks]
    res = {"calls": 0, "days": 0, "rows": 0, "failed": 0, "remaining": 0, "stopped": None, "errors": [],
           "min_date": None}
    streak = 0
    for i, (d, mks) in enumerate(todo):
        if res["calls"] + len(mks) > budget:
            res["stopped"] = f"호출 예산 {budget}회 소진 — 다음 실행 때 이어서"
            res["remaining"] = len(todo) - i
            break
        r = load_day(d, mks, fetch=fetch)
        res["calls"] += r["calls"]
        res["rows"] += r["rows"]
        res["failed"] += r["failed"]
        res["errors"] += r["errors"][:3]
        res["days"] += 1
        if r["rows"]:
            res["min_date"] = d if res["min_date"] is None else min(res["min_date"], d)
        streak = streak + 1 if r["failed"] == len(mks) else 0
        if streak >= MAX_CONSECUTIVE_FAILS:
            res["stopped"] = f"KRX 연속 {streak}일 실패 — 이번 회차 중단"
            res["remaining"] = len(todo) - i - 1
            break
    res["errors"] = res["errors"][:5]
    if refresh and res["rows"]:
        res["snapshot"] = refresh_snapshot(adj_since=res["min_date"])
    return res


def incremental(days_back=10, fetch=None, end: date | None = None):
    """최근 days_back 거래일(달력 2배 범위) 중 빠진 단위만 받는다(매일 배치). 이미 받은 날은 호출하지 않는다."""
    end = end or last_complete_day()
    return _range(end - timedelta(days=days_back * 2), end, fetch)


def _range(start, end, fetch=None):
    states = load_states()
    res = {"calls": 0, "days": 0, "rows": 0, "failed": 0, "min_date": None}
    for d in trading_days(start, end):
        mks = [mk for mk in MARKETS if states.get(f"{d.isoformat()}:{mk}") not in ("ok", "empty")]
        if not mks:
            continue
        r = load_day(d, mks, fetch=fetch)
        for k in ("calls", "rows", "failed"):
            res[k] += r[k]
        res["days"] += 1
        if r["rows"]:
            res["min_date"] = d if res["min_date"] is None else min(res["min_date"], d)
    if res["rows"]:
        res["snapshot"] = refresh_snapshot(adj_since=res["min_date"])
    return res


def refresh_price_adj(since: date | None = None, full=False):
    """권리 이벤트(액면분할·무상증자·감자) 다시 찾기 — V007 analytics.refresh_price_adj.
    since 이후 거래일만(새로 받은 가장 이른 날). 한 번도 전체를 훑지 않았으면(업그레이드 직후) 전체를 한 번 훑는다.
    반환 찾은 이벤트 수(실패면 None — 수정주가가 조금 늦게 반영될 뿐 스냅샷은 계속)."""
    try:
        full = full or not db.get_meta("price_adj_full_at")
        with db.transaction() as conn:
            n = conn.execute(text("SELECT analytics.refresh_price_adj(CAST(:s AS date))"),
                             {"s": None if full else since}).scalar()
            if full:
                db.set_meta("price_adj_full_at", clock.iso_now(), conn)
        return n
    except Exception as e:                                   # noqa: BLE001
        log.warning("권리 이벤트(수정주가) 갱신 실패: %s", type(e).__name__)
        return None


def refresh_snapshot(adj_since: date | None = None, full_adj=False):
    """머티리얼라이즈드 뷰 새로 고침 — CONCURRENTLY 라 화면 읽기를 막지 않는다(처음엔 일반 REFRESH).
    새 시세를 받았으면(adj_since) 먼저 권리 이벤트를 다시 찾는다 — 스냅샷의 기간 수익률·52주 신고가가 수정주가 기준."""
    import time
    t0 = time.perf_counter()
    events = refresh_price_adj(adj_since, full_adj) if (adj_since or full_adj) else None
    try:
        with db.transaction() as conn:
            # 소유자 권한 함수(V005) — DB 권한을 분리해 앱 계정에 DDL 권한이 없어도 새로 고칠 수 있다
            n = conn.execute(text("SELECT analytics.refresh_snapshot(true)")).scalar()
            db.set_meta("analytics_snapshot_at", clock.iso_now(), conn)
    except Exception as e:                                   # noqa: BLE001
        log.warning("종목 스냅샷 새로 고침 실패: %s", type(e).__name__)
        return {"ok": False, "error": type(e).__name__}
    out = {"ok": True, "rows": n, "ms": int((time.perf_counter() - t0) * 1000)}
    if events is not None:
        out["adj_events"] = events
    return out


# ── 표본(오프라인 데모) ─────────────────────────────────────
def seed_from_app_tables():
    """KRX 키가 없을 때(데모) — 화면용 표(ohlc·stock·financial_statement)를 분석 계층으로 복사해
    골라보기·산업 화면이 비지 않게 한다. 출처는 gen(표본)으로 표시."""
    with db.transaction() as conn:
        conn.execute(text(
            "INSERT INTO analytics.security_master(ticker, name, market, sector, first_seen, last_seen) "
            "SELECT s.ticker, COALESCE(s.name, s.ticker), COALESCE(NULLIF(s.market, ''), 'KOSPI'), s.sector, "
            "       MIN(o.trade_date)::date, MAX(o.trade_date)::date "
            "FROM stock s JOIN ohlc o ON o.ticker = s.ticker GROUP BY s.ticker, s.name, s.market, s.sector "
            "ON CONFLICT (ticker) DO UPDATE SET name = EXCLUDED.name, sector = COALESCE(analytics.security_master.sector, EXCLUDED.sector), "
            " first_seen = LEAST(analytics.security_master.first_seen, EXCLUDED.first_seen), "
            " last_seen = GREATEST(analytics.security_master.last_seen, EXCLUDED.last_seen)"))
        years = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT EXTRACT(YEAR FROM trade_date)::int FROM ohlc")).all()]
        for y in years:
            conn.execute(text("SELECT analytics.ensure_price_partition(:y)"), {"y": y})
        n = conn.execute(text(
            "INSERT INTO analytics.price_history(ticker, trade_date, mkt, open, high, low, close, volume, value, "
            " mktcap, change_rate, halted) "
            "SELECT o.ticker, o.trade_date::date, CASE WHEN m.market = 'KOSDAQ' THEN 'Q' ELSE 'K' END, "
            " round(o.open)::int, round(o.high)::int, round(o.low)::int, round(o.close)::int, COALESCE(o.volume, 0), "
            " COALESCE(round(o.close * o.volume)::bigint, 0), d.market_cap, d.change_rate, false "
            "FROM ohlc o JOIN analytics.security_master m ON m.ticker = o.ticker "
            "LEFT JOIN daily_price d ON d.ticker = o.ticker AND d.trade_date = o.trade_date "
            "WHERE o.close > 0 ON CONFLICT (ticker, trade_date) DO NOTHING")).rowcount or 0
        conn.execute(text(
            "INSERT INTO analytics.fin_annual(ticker, fiscal_year, fs_basis, revenue, operating_profit, net_income, "
            " total_assets, total_liabilities, total_equity, current_assets, current_liabilities, retained_earnings, "
            " disclosed_at, source) "
            "SELECT f.ticker, f.fiscal_year, COALESCE(f.fs_basis, 'CFS'), f.revenue, f.operating_profit, f.net_income, "
            " f.total_assets, f.total_liabilities, f.total_equity, f.current_assets, f.current_liabilities, "
            " f.retained_earnings, f.disclosed_at::date, 'gen' FROM financial_statement f "
            "JOIN analytics.security_master m ON m.ticker = f.ticker ON CONFLICT (ticker, fiscal_year) DO NOTHING"))
    snap = refresh_snapshot(full_adj=True)
    return {"rows": n, "snapshot": snap, "source": "gen"}
