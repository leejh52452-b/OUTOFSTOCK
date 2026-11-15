"""인덱스·파티션 설계 근거 — 같은 데이터·같은 쿼리를 구성별로 EXPLAIN (ANALYZE, BUFFERS) 해서 표로 남긴다.

    .venv/bin/python tools/bench_explain.py                       # 기본: 2,700종목 × 10년(약 660만 행)
    .venv/bin/python tools/bench_explain.py --tickers 300 --years 2   # 빠른 확인
    → docs/perf/explain_report.md (+ .json) 에 저장. 실제 데이터는 건드리지 않는다(별도 스키마 bench 를 만들고 지움).

비교하는 구성
  A heap       : 파티션 없음 + 기본키(ticker, trade_date)만
  B part       : 연도별 범위 파티션 + 기본키만
  C part+btree : B + trade_date B-tree 인덱스
  D part+brin  : B + trade_date BRIN 인덱스(날짜순으로 쌓이는 데이터에 작게 붙는 인덱스)
쿼리(화면·배치가 실제로 하는 일)
  Q1 최신 거래일          SELECT max(trade_date)
  Q2 그날 거래대금 상위    WHERE trade_date = :L ORDER BY value DESC LIMIT 50
  Q3 한 종목 1년 시계열    WHERE ticker = :t AND trade_date > :L - 365
  Q4 거래량 급증(20일 평균 대비) — 윈도 함수
  Q5 52주 신고가 종목      최근 1년 GROUP BY ticker
  Q6 월별 거래대금(3년)    date_trunc 집계 — 파티션 제외 효과
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

OUT_MD = ROOT / "docs" / "perf" / "explain_report.md"

QUERIES = {
    "Q1 최신 거래일": "SELECT max(trade_date) FROM {t}",
    "Q2 그날 거래대금 상위 50": "SELECT ticker, close, value FROM {t} WHERE trade_date = '{L}' ORDER BY value DESC LIMIT 50",
    "Q3 한 종목 1년": "SELECT trade_date, close FROM {t} WHERE ticker = '{tk}' AND trade_date > date '{L}' - 365 "
                    "ORDER BY trade_date",
    "Q4 거래량 급증": "WITH w AS (SELECT ticker, trade_date, volume, avg(volume) OVER (PARTITION BY ticker "
                  "ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS a FROM {t} "
                  "WHERE trade_date > date '{L}' - 40) SELECT ticker, round(volume / a, 2) AS ratio FROM w "
                  "WHERE trade_date = '{L}' AND a > 0 AND volume > 3 * a ORDER BY ratio DESC LIMIT 20",
    "Q5 52주 신고가": "SELECT ticker FROM {t} WHERE trade_date > date '{L}' - 365 GROUP BY ticker "
                  "HAVING max(high) FILTER (WHERE trade_date = '{L}') >= max(high)",
    "Q6 월별 거래대금 3년": "SELECT date_trunc('month', trade_date) AS m, sum(value) FROM {t} "
                       "WHERE trade_date >= date '{L}' - 1095 GROUP BY 1 ORDER BY 1",
}

DDL_COLS = ("ticker varchar(12) NOT NULL, trade_date date NOT NULL, mkt char(1) NOT NULL, open integer, "
            "high integer, low integer, close integer NOT NULL, volume bigint NOT NULL, value bigint NOT NULL, "
            "mktcap bigint, change_rate real, halted boolean NOT NULL DEFAULT false")


def gen_sql(table, n, start, end):
    # 결정적 의사난수(hashtext) — 같은 인자면 같은 데이터. 주말 제외.
    return (f"INSERT INTO {table} SELECT lpad(t::text, 6, '0'), d::date, CASE WHEN t % 3 = 0 THEN 'Q' ELSE 'K' END, "
            "c - 50, c + 100 + (h % 200), c - 100, c, 1000 + (h % 100000), (1000 + (h % 100000))::bigint * c, "
            "c::bigint * 1000000, ((h % 600) - 300) / 100.0, false "
            "FROM (SELECT t, d, abs(hashtext(t::text || d::text)) AS h, "
            "      5000 + (t % 500) * 100 + (abs(hashtext(t::text || d::text)) % 3000) AS c "
            f"      FROM generate_series(1, {n}) t, generate_series(date '{start}', date '{end}', interval '1 day') d "
            "      WHERE extract(isodow FROM d) < 6) s")


def explain(conn, sql, runs=3):
    times, plan = [], None
    for _ in range(runs):
        res = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar()
        p = res[0] if isinstance(res, list) else json.loads(res)[0]
        times.append(p["Execution Time"])
        plan = p
    root = plan["Plan"]
    nodes, parts = [], set()

    def walk(n):
        nodes.append(n["Node Type"] + (f"({n.get('Index Name')})" if n.get("Index Name") else ""))
        if n.get("Relation Name"):
            parts.add(n["Relation Name"])
        for c in n.get("Plans", []):
            walk(c)
    walk(root)
    bufs = root.get("Shared Hit Blocks", 0) + root.get("Shared Read Blocks", 0)
    seen = []
    for x in nodes:
        if x not in seen:
            seen.append(x)
    return {"ms": round(statistics.median(times), 2), "buffers": bufs, "relations": len(parts),
            "plan": " → ".join(seen[:6])}


def size(conn, rel):
    return conn.execute(text(f"SELECT pg_size_pretty(pg_total_relation_size('{rel}'))")).scalar()


def idx_size(conn, like):
    return conn.execute(text("SELECT pg_size_pretty(COALESCE(sum(pg_relation_size(c.oid)), 0)) FROM pg_class c "
                             "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'bench' "
                             "AND c.relkind = 'i' AND c.relname LIKE :p"), {"p": like}).scalar()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=2700)
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="bench 스키마를 지우지 않음")
    ap.add_argument("--url", default=os.getenv("BENCH_DATABASE_URL") or settings.DATABASE_URL)
    a = ap.parse_args()
    end = date(2026, 9, 30)
    start = date(end.year - a.years, 10, 1)
    L = end - timedelta(days=(end.weekday() - 4) if end.weekday() > 4 else 0)
    eng = create_engine(a.url, future=True, isolation_level="AUTOCOMMIT")   # VACUUM 은 트랜잭션 밖에서
    report = {"tickers": a.tickers, "years": a.years, "start": str(start), "end": str(end), "configs": {}}
    t0 = time.perf_counter()
    with eng.connect() as conn:
        conn.execute(text("SET client_min_messages = warning"))
        conn.execute(text("DROP SCHEMA IF EXISTS bench CASCADE"))
        conn.execute(text("CREATE SCHEMA bench"))
        conn.execute(text(f"CREATE TABLE bench.ph_heap ({DDL_COLS}, PRIMARY KEY (ticker, trade_date))"))
        conn.execute(text(f"CREATE TABLE bench.ph_part ({DDL_COLS}, PRIMARY KEY (ticker, trade_date)) "
                          "PARTITION BY RANGE (trade_date)"))
        for y in range(start.year, end.year + 1):
            conn.execute(text(f"CREATE TABLE bench.ph_part_{y} PARTITION OF bench.ph_part "
                              f"FOR VALUES FROM ('{y}-01-01') TO ('{y + 1}-01-01')"))
        conn.commit()
        print(f"데이터 생성 중… {a.tickers}종목 × {a.years}년")
        conn.execute(text(gen_sql("bench.ph_part", a.tickers, start, end)))
        conn.execute(text("INSERT INTO bench.ph_heap SELECT * FROM bench.ph_part"))
        conn.commit()
        conn.execute(text("VACUUM (ANALYZE) bench.ph_heap"))
        conn.execute(text("VACUUM (ANALYZE) bench.ph_part"))
        rows = conn.execute(text("SELECT count(*) FROM bench.ph_heap")).scalar()
        report["rows"] = rows
        report["gen_seconds"] = round(time.perf_counter() - t0, 1)
        print(f"  {rows:,}행 생성 ({report['gen_seconds']}초)")

        def run(name, table, extra=None):
            res = {}
            for qn, q in QUERIES.items():
                res[qn] = explain(conn, q.format(t=table, L=L, tk=f"{min(a.tickers, 1234):06d}"), a.runs)
                print(f"  [{name}] {qn}: {res[qn]['ms']}ms  {res[qn]['plan']}")
            report["configs"][name] = {"queries": res, **(extra or {})}

        run("A heap(파티션 없음)", "bench.ph_heap", {"table_size": size(conn, "bench.ph_heap")})
        run("B 연도 파티션", "bench.ph_part")
        conn.execute(text("CREATE INDEX ix_bench_part_date ON bench.ph_part (trade_date)"))
        conn.execute(text("ANALYZE bench.ph_part"))
        conn.commit()
        run("C 파티션+B-tree(trade_date)", "bench.ph_part", {"index_size": idx_size(conn, "ph_part%trade_date%")})
        conn.execute(text("DROP INDEX bench.ix_bench_part_date"))
        conn.execute(text("CREATE INDEX ix_bench_part_brin ON bench.ph_part USING brin (trade_date) "
                          "WITH (pages_per_range = 32)"))
        conn.execute(text("ANALYZE bench.ph_part"))
        conn.commit()
        run("D 파티션+BRIN(trade_date)", "bench.ph_part", {"index_size": idx_size(conn, "ph_part%trade_date%")})
        if not a.keep:
            conn.execute(text("DROP SCHEMA bench CASCADE"))
            conn.commit()
        report["pg_version"] = conn.execute(text("SHOW server_version")).scalar()
    report["total_seconds"] = round(time.perf_counter() - t0, 1)
    write(report)
    print(f"\n저장: {OUT_MD}")


def write(r):
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.with_suffix(".json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    names = list(r["configs"])
    lines = [f"# 인덱스·파티션 실측(EXPLAIN ANALYZE) — {r['rows']:,}행",
             "",
             f"- 데이터: 합성 {r['tickers']:,}종목 × {r['years']}년({r['start']}~{r['end']}, 평일), PostgreSQL {r['pg_version']}",
             f"- 각 쿼리 {3}회 실행의 중앙값(ms). 버퍼 = 읽은 8KB 블록 수(캐시 적중 포함). '표' = 실제로 읽은 표·파티션 수.",
             "- 재현: `.venv/bin/python tools/bench_explain.py` (환경마다 숫자는 다르다 — 구성 간 **비율**을 본다)",
             "", "| 쿼리 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for q in QUERIES:
        cells = []
        for n in names:
            x = r["configs"][n]["queries"][q]
            cells.append(f"{x['ms']:,}ms · 버퍼 {x['buffers']:,} · 표 {x['relations']}")
        lines.append(f"| {q} | " + " | ".join(cells) + " |")
    lines += ["", "## 실행 계획 요약", ""]
    for n in names:
        lines.append(f"**{n}**" + (f" — 표 크기 {r['configs'][n].get('table_size')}" if r['configs'][n].get('table_size') else "")
                     + (f" — 인덱스 크기 {r['configs'][n].get('index_size')}" if r['configs'][n].get('index_size') else ""))
        for q in QUERIES:
            lines.append(f"- {q}: {r['configs'][n]['queries'][q]['plan']}")
        lines.append("")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
