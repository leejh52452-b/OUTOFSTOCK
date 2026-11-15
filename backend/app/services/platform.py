"""분석 계층(정보계) 서비스 — 적재 현황 · 이름 있는 분석 쿼리(SQL 파일, 분석가 CLI 전용).

안전 원칙(3번 정보보호와 연결)
  · '이름 있는 쿼리'(sql/analytics/*.sql)만 실행한다 — 임의 SQL 을 받지 않는다(화면 분석 API 는 2026-10-20 제거, CLI 만).
  · 읽기 전용 트랜잭션(SET TRANSACTION READ ONLY) + 쿼리 시간 제한(statement_timeout) — 무거운 요청이
    서비스 DB 를 오래 붙잡지 못하게. ANALYTICS_DATABASE_URL(읽기 전용 계정)이 있으면 그 계정으로 접속.
  · 계정계(서비스 API)와 분리 — 화면에서는 호출하지 않고 run.py analytics-query 로만 실행한다.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text

from app import db
from app.config import settings

ROOT = Path(__file__).resolve().parents[2]
QUERY_DIR = ROOT / "sql" / "analytics"
_ro_engine = None


class QueryError(ValueError):
    """잘못된 쿼리 이름·파라미터 — API 는 400 으로 돌려준다."""


def _engine():
    global _ro_engine
    if not settings.ANALYTICS_DATABASE_URL:
        return db.get_engine()
    if _ro_engine is None:
        _ro_engine = create_engine(settings.ANALYTICS_DATABASE_URL, future=True, pool_pre_ping=True,
                                   pool_size=3, max_overflow=2)
    return _ro_engine


@contextmanager
def ro_conn(timeout_ms=None):
    """읽기 전용 + 시간 제한 연결. 시간을 넘기면 PostgreSQL 이 쿼리를 취소한다(QueryCanceled)."""
    ms = int(timeout_ms or settings.ANALYTICS_QUERY_TIMEOUT_MS)
    with _engine().connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        c.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": str(ms)})
        try:
            yield c
        finally:
            c.rollback()


def _rows(c, sql, params=None):
    res = c.execute(text(sql), params or {})
    keys = list(res.keys())
    return keys, [dict(zip(keys, r)) for r in res.fetchall()]


# ── 이름 있는 분석 쿼리 ─────────────────────────────────────
_HEAD = re.compile(r"^--\s*(name|title|description|params|tags):\s*(.*)$")


def _parse(path: Path):
    meta, body = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEAD.match(line)
        if m and not body:
            meta[m.group(1)] = m.group(2).strip()
        else:
            body.append(line)
    params = []
    for spec in filter(None, (s.strip() for s in meta.get("params", "").split(","))):
        name, rest = spec.split(":", 1)
        typ, _, rest = rest.partition("=")
        parts = rest.split(":")
        p = {"name": name, "type": typ, "default": parts[0]}
        if typ == "enum":
            p["options"] = parts[1].split("|")
        else:
            p["min"], p["max"] = float(parts[1]), float(parts[2])
        params.append(p)
    return {"name": meta.get("name") or path.stem, "title": meta.get("title", path.stem),
            "description": meta.get("description", ""), "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            "params": params, "sql": "\n".join(body).strip()}


def queries():
    return {q["name"]: q for q in (_parse(p) for p in sorted(QUERY_DIR.glob("*.sql")))}


def _coerce(p, raw):
    v = p["default"] if raw in (None, "") else raw
    if p["type"] == "enum":
        if v not in p["options"]:
            raise QueryError(f"{p['name']} 는 {', '.join(p['options'])} 중 하나")
        return v
    try:
        x = int(v) if p["type"] == "int" else float(v)
    except (TypeError, ValueError):
        raise QueryError(f"{p['name']} 는 숫자여야 합니다") from None
    if not (p["min"] <= x <= p["max"]):
        raise QueryError(f"{p['name']} 는 {p['min']:g}~{p['max']:g} 사이")
    return x


def run_query(name, args=None):
    """이름 있는 쿼리 실행 → {name, title, params, columns, rows, ms, sql}."""
    q = queries().get(name)
    if not q:
        raise QueryError(f"없는 분석 쿼리: {name}")
    args = args or {}
    params = {p["name"]: _coerce(p, args.get(p["name"])) for p in q["params"]}
    t0 = time.perf_counter()
    with ro_conn() as c:
        cols, rows = _rows(c, q["sql"], params)
    return {"name": name, "title": q["title"], "description": q["description"], "tags": q["tags"],
            "params": params, "columns": cols, "rows": [_jsonable(r) for r in rows],
            "ms": round((time.perf_counter() - t0) * 1000, 1), "sql": q["sql"]}


def _jsonable(r):
    from datetime import date, datetime
    from decimal import Decimal
    out = {}
    for k, v in r.items():
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, (date, datetime)):
            v = v.isoformat()
        out[k] = v
    return out


# ── 적재 현황 ──────────────────────────────────────────────
def status():
    """데이터 플랫폼 현황 — 행 수(통계 추정)·파티션·최신일 커버리지·적재 이력·마이그레이션."""
    from app import migrations
    with ro_conn(10000) as c:
        _, parts = _rows(c, "SELECT c.relname AS name, c.reltuples::bigint AS est_rows, "
                            "pg_total_relation_size(c.oid) AS bytes FROM pg_inherits i "
                            "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'analytics.price_history'::regclass "
                            "ORDER BY c.relname")
        latest = c.execute(text("SELECT analytics.latest_trade_date()")).scalar()
        _, cov = _rows(c, "SELECT tickers, kospi, kosdaq, halted FROM analytics.v_daily_coverage "
                          "WHERE trade_date = :d", {"d": latest}) if latest else ([], [])
        _, loads = _rows(c, "SELECT dataset, status, count(*) AS n, max(finished_at) AS last_at "
                            "FROM analytics.load_log GROUP BY dataset, status ORDER BY dataset, status")
        first = c.execute(text("SELECT min(split_part(part_key, ':', 1)) FROM analytics.load_log "
                               "WHERE dataset = 'krx_daily' AND status = 'ok'")).scalar()
        masters = c.execute(text("SELECT count(*) FROM analytics.security_master")).scalar()
        if sum(max(p["est_rows"], 0) for p in parts) == 0:          # 통계(ANALYZE) 전 — 작은 표면 실제 개수
            n = c.execute(text("SELECT count(*) FROM analytics.price_history")).scalar()
            if parts:
                parts[0]["est_rows"] = n
        snap = c.execute(text("SELECT count(*) FROM analytics.mv_stock_snapshot")).scalar()
    est = sum(max(p["est_rows"], 0) for p in parts)
    return {"latest_trade_date": str(latest)[:10] if latest else None, "first_loaded_date": first,
            "est_rows": est, "bytes": sum(p["bytes"] for p in parts), "securities": masters,
            "snapshot_rows": snap, "partitions": [_jsonable(p) for p in parts],
            "latest_coverage": _jsonable(cov[0]) if cov else None,
            "loads": [_jsonable(x) for x in loads], "target_years": settings.ANALYTICS_YEARS,
            "migrations": migrations.status(), "snapshot_refreshed_at": db.get_meta("analytics_snapshot_at")}
