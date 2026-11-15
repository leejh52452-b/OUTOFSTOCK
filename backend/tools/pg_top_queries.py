"""느린 쿼리 상위 — pg_stat_statements 기준(총 소요·평균·호출 수). 부하 시험 뒤에 돌려 병목을 찾는다.

    python tools/pg_top_queries.py [--limit 15] [--reset] [--md out.md]
전제: postgresql.conf 의 shared_preload_libraries = 'pg_stat_statements' + CREATE EXTENSION pg_stat_statements
(docker-compose 운영 오버레이는 둘 다 켜 둔다). 쿼리 문장은 값이 $1 처럼 지워진 형태라 개인정보가 담기지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402


def top(limit=15):
    eng = create_engine(settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL, future=True)
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, "
            "round(max_exec_time::numeric, 1) AS max_ms, rows, "
            "round(100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0), 1) AS hit_pct, "
            "regexp_replace(left(query, 160), '\\s+', ' ', 'g') AS q "
            "FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "ORDER BY total_exec_time DESC LIMIT :n"), {"n": limit}).mappings().all()
    eng.dispose()
    return [dict(r) for r in rows]


def reset():
    """통계 초기화 — 기본은 슈퍼유저만 된다. 권한이 없으면 부여 방법을 알려 주고 끝낸다(운영 계정에 슈퍼유저를 주지 않는다)."""
    from sqlalchemy.exc import ProgrammingError
    eng = create_engine(settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL, future=True)
    try:
        with eng.begin() as c:
            c.execute(text("SELECT pg_stat_statements_reset()"))
    except ProgrammingError as e:
        if "permission denied" not in str(e):
            raise
        sys.exit("권한 없음 — 슈퍼유저로 한 번: GRANT EXECUTE ON FUNCTION pg_stat_statements_reset(oid, oid, bigint) "
                 f"TO {eng.url.username};")
    finally:
        eng.dispose()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--md")
    a = ap.parse_args()
    if a.reset:
        reset()
        print("초기화")
        return
    rows = top(a.limit)
    lines = ["| 호출 | 총 ms | 평균 ms | 최대 ms | 행 | 캐시 적중 % | 쿼리 |", "|---:|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        q = r["q"].replace("|", "\\|")
        lines.append(f"| {r['calls']} | {r['total_ms']} | {r['mean_ms']} | {r['max_ms']} | {r['rows']} | "
                     f"{r['hit_pct']} | `{q}` |")
    out = "\n".join(lines)
    print(out)
    if a.md:
        Path(a.md).write_text(out + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
