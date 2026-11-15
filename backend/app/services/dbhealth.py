"""DB 진단·용량 관리(ADR-0027) — 운영 DBA 가 매일 보는 것을 코드로.

  크기     DB·표·인덱스·TOAST 크기, 하루 1행(db_size_snapshot) → 증가 추세(최근 60일 최소제곱) → 디스크 소진 예측
  위생     죽은 행 비율(자동 VACUUM 지연) · 쓰이지 않는 인덱스 · 앞 열이 겹치는 인덱스 · XID 나이(랩어라운드)
  성능     캐시 적중률 · 임시 파일 · 교착 · 연결 사용률 · 오래 열린 트랜잭션 · 느린 쿼리 상위(pg_stat_statements 가 있으면)
  설정     메모리·WAL 압축·TOAST 압축·느린 쿼리 기록 등 현재 값(vm.sh update 의 자동 튜닝 결과 확인)
  수명주기 보관 기한 지난 행·보관 파일(ilm.status)
기준을 넘으면 경보(같은 항목은 6시간에 한 번 — alerts.notify). 화면: GET /admin/db · 명령: python run.py db-report
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import date, timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)
SNAPSHOT_KEEP_DAYS = 730
SETTINGS_OF_INTEREST = ("server_version", "shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
                        "max_connections", "random_page_cost", "effective_io_concurrency", "wal_compression",
                        "default_toast_compression", "max_wal_size", "checkpoint_completion_target",
                        "shared_preload_libraries", "track_io_timing", "log_min_duration_statement",
                        "log_autovacuum_min_duration", "autovacuum_vacuum_scale_factor", "TimeZone")
# 판정 기준 — (경고, 오류)
DEAD_RATIO = (0.20, 0.50)              # 8MB 넘는 표의 죽은 행 비율
CACHE_HIT = (0.95, 0.90)               # 읽기 10만 블록 넘은 뒤의 캐시 적중률(이 값보다 낮으면)
XID_AGE = (500_000_000, 1_200_000_000)  # 랩어라운드(20억)까지 — 자동 동결 기준이 2억이라 평소엔 그 아래
CONN_USE = (0.80, 0.95)
LONG_TX_MIN = (10, 60)                 # 분 — 오래 열린 트랜잭션은 VACUUM 을 막고 표를 부풀린다
DISK_FREE = (0.15, 0.08)
UNUSED_INDEX_MB = 4
STATS_MIN_DAYS = 7                     # 통계를 모은 지 이만큼 지나야 '안 쓰인 인덱스'를 말한다


def _q(conn, sql, params=None):
    res = conn.execute(text(sql), params or {})
    keys = list(res.keys())
    return [dict(zip(keys, r)) for r in res.fetchall()]


def _owner_engine():
    """통계·크기는 소유자 계정이 더 많이 본다(다른 계정의 쿼리 문장 등) — 있으면 그 계정, 없으면 앱 계정."""
    if settings.MIGRATION_DATABASE_URL:
        return db._make_engine(settings.MIGRATION_DATABASE_URL), True
    return db.get_engine(), False


def collect(light: bool = False) -> dict:
    """현재 상태(읽기만). light=True 면 크기·행 수만(매일 기록용 — 몇 ms)."""
    eng, own = _owner_engine()
    try:
        with eng.connect() as c:
            c.execute(text("SET LOCAL statement_timeout = '15s'"))
            out = {"at": clock.utc().isoformat(timespec="seconds"), "database": c.execute(text("SELECT current_database()")).scalar(),
                   "db_bytes": int(c.execute(text("SELECT pg_database_size(current_database())")).scalar())}
            out["tables"] = _q(c, """
                SELECT n.nspname || '.' || c.relname AS name, pg_relation_size(c.oid) AS heap,
                       pg_indexes_size(c.oid) AS idx, COALESCE(pg_total_relation_size(NULLIF(c.reltoastrelid, 0)), 0) AS toast,
                       pg_total_relation_size(c.oid) AS total,
                       COALESCE(NULLIF(s.n_live_tup, 0), GREATEST(c.reltuples, 0)::bigint) AS live,   -- 통계가 비면(비정상 종료 뒤) 카탈로그 추정
                       COALESCE(s.n_dead_tup, 0) AS dead,
                       s.last_autovacuum, s.last_autoanalyze, COALESCE(s.seq_scan, 0) AS seq_scan, COALESCE(s.idx_scan, 0) AS idx_scan,
                       array_to_string(c.reloptions, ',') AS reloptions
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE n.nspname IN ('public', 'analytics') AND c.relkind IN ('r', 'm')
                ORDER BY pg_total_relation_size(c.oid) DESC""")
            if light:
                return out
            # 앱 연결이 세션에서 바꾸는 값(시간대·TOAST 압축)은 서버 기본값(reset_val)으로 — 화면엔 '서버 설정'을 보인다
            out["settings"] = {r["name"]: r["v"] for r in _q(
                c, "SELECT name, CASE WHEN name IN ('TimeZone', 'default_toast_compression') THEN reset_val "
                   "ELSE current_setting(name) END AS v FROM pg_settings WHERE name = ANY(:n) ORDER BY name",
                {"n": list(SETTINGS_OF_INTEREST)})}
            out["settings"]["pending_restart"] = [r["name"] for r in _q(c, "SELECT name FROM pg_settings WHERE pending_restart")]
            out["indexes"] = _q(c, """
                SELECT n.nspname || '.' || t.relname AS tbl, i.relname AS name, pg_relation_size(i.oid) AS bytes,
                       COALESCE(s.idx_scan, 0) AS scans, x.indisunique AS uniq, x.indisprimary AS pk,
                       x.indexprs IS NOT NULL AS expr, x.indpred IS NOT NULL AS partial,
                       array(SELECT unnest(x.indkey::int2[]))::int[] AS cols, pg_get_indexdef(i.oid) AS def
                FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.oid
                WHERE n.nspname IN ('public', 'analytics') AND t.relkind IN ('r', 'm')""")
            st = _q(c, "SELECT blks_hit, blks_read, temp_files, temp_bytes, deadlocks, "
                       "COALESCE(stats_reset, pg_postmaster_start_time()) AS stats_reset, "
                       "age(datfrozenxid) AS xid_age FROM pg_stat_database d JOIN pg_database b ON b.oid = d.datid "
                       "WHERE d.datname = current_database()")[0]
            out["stats"] = {**st, "stats_reset": st["stats_reset"].isoformat() if st["stats_reset"] else None}
            out["connections"] = {r["state"] or "background": r["n"] for r in _q(
                c, "SELECT state, count(*) AS n FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1")}
            out["max_connections"] = int(c.execute(text("SHOW max_connections")).scalar())
            lt = _q(c, "SELECT EXTRACT(EPOCH FROM (now() - xact_start)) / 60 AS minutes, state, "
                       "left(regexp_replace(query, '\\s+', ' ', 'g'), 80) AS q FROM pg_stat_activity "
                       "WHERE datname = current_database() AND xact_start IS NOT NULL AND pid <> pg_backend_pid() "
                       "ORDER BY xact_start LIMIT 3")
            out["long_tx"] = [{**r, "minutes": round(float(r["minutes"]), 1)} for r in lt]
            out["pg_stat_statements"] = None
            if c.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")).first():
                try:
                    with c.begin_nested():
                        out["pg_stat_statements"] = [
                            {**r, "total_ms": round(float(r["total_ms"]), 1), "mean_ms": round(float(r["mean_ms"]), 2)}
                            for r in _q(c, "SELECT calls, total_exec_time AS total_ms, mean_exec_time AS mean_ms, rows, "
                                           "left(regexp_replace(query, '\\s+', ' ', 'g'), 140) AS q FROM pg_stat_statements "
                                           "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                                           "ORDER BY total_exec_time DESC LIMIT 5")]
                except Exception as e:                    # noqa: BLE001 — 모듈이 미리 적재되지 않았으면(재시작 전) 오류
                    out["pg_stat_statements"] = {"error": type(e).__name__}
            out["owner_view"] = own
            return out
    finally:
        if eng is not db.get_engine():
            eng.dispose()


def _redundant(indexes):
    """같은 표에서 열 목록이 다른 인덱스의 앞부분과 같은 일반 인덱스(유일·식·부분 인덱스 제외)."""
    by_t = {}
    for i in indexes:
        by_t.setdefault(i["tbl"], []).append(i)
    out = []
    for t, xs in by_t.items():
        for a in xs:
            if a["uniq"] or a["pk"] or a["expr"] or a["partial"]:
                continue
            for b in xs:
                if b is a or b["expr"] or b["partial"]:
                    continue
                if len(b["cols"]) >= len(a["cols"]) and b["cols"][:len(a["cols"])] == a["cols"] and \
                        (len(b["cols"]) > len(a["cols"]) or b["uniq"]):
                    out.append({"table": t, "index": a["name"], "covered_by": b["name"], "bytes": a["bytes"]})
                    break
    return out


def history(days: int = 90) -> list[dict]:
    return db.query("SELECT day, db_bytes FROM db_size_snapshot WHERE day >= :d ORDER BY day",
                    {"d": (clock.today() - timedelta(days=days)).isoformat()})


def forecast(hist: list[dict], now_bytes: int, disk_path: str | None = None) -> dict:
    """최근 기록의 최소제곱 기울기(바이트/일)와 디스크가 찰 때까지 남은 일수. 기록이 7일 미만이면 추정하지 않는다."""
    pts = [(date.fromisoformat(str(r["day"])[:10]).toordinal(), int(r["db_bytes"])) for r in hist][-60:]
    out = {"points": len(pts), "bytes_per_day": None, "days_to_full": None}
    try:
        du = shutil.disk_usage(disk_path or settings.DB_DISK_PATH)
        out.update(disk_total=du.total, disk_free=du.free, disk_free_ratio=round(du.free / du.total, 3))
    except OSError:
        du = None
    if len(pts) < 7:
        return out
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    var = sum((x - mx) ** 2 for x, _ in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / var if var else 0.0
    out["bytes_per_day"] = round(slope)
    out["bytes_in_90d"] = max(0, round(slope * 90))
    if du and slope > 0:
        out["days_to_full"] = int(du.free / slope)
    return out


def findings(r: dict, fc: dict | None = None, ilm_status: dict | None = None) -> list[dict]:
    """판정 — [{rule, level(warn|error|info), title, detail}]."""
    out = []

    def add(rule, level, title, detail):
        out.append({"rule": rule, "level": level, "title": title, "detail": detail})

    for t in r.get("tables", []):
        tot = (t["live"] or 0) + (t["dead"] or 0)
        if t["total"] > 8 * 1048576 and tot and t["dead"] / tot >= DEAD_RATIO[0]:
            add(f"dead:{t['name']}", "error" if t["dead"] / tot >= DEAD_RATIO[1] else "warn",
                f"{t['name']} 죽은 행 {t['dead'] / tot:.0%}", "자동 VACUUM 이 따라가지 못한다 — 오래 열린 트랜잭션·VACUUM 기준 확인")
    stats_days = None
    if r.get("stats", {}).get("stats_reset"):
        from datetime import datetime
        stats_days = (clock.utc() - datetime.fromisoformat(r["stats"]["stats_reset"])).days
    for i in r.get("indexes", []):
        if (stats_days is None or stats_days >= STATS_MIN_DAYS) and not (i["uniq"] or i["pk"]) and i["scans"] == 0 \
                and i["bytes"] > UNUSED_INDEX_MB * 1048576:
            add(f"unused_index:{i['name']}", "info", f"쓰이지 않는 인덱스 {i['name']} ({i['bytes'] / 1048576:.0f}MB)",
                "통계를 모은 뒤 한 번도 쓰이지 않았다 — 쓰기·저장 비용만 든다(지우기 전 다른 서버·월간 작업에서 쓰는지 확인)")
    for x in _redundant(r.get("indexes", [])):
        add(f"redundant_index:{x['index']}", "warn", f"겹치는 인덱스 {x['index']}",
            f"{x['covered_by']} 가 같은 앞 열을 덮는다 — 지워도 같은 조회를 처리한다({x['bytes'] / 1048576:.1f}MB)")
    st = r.get("stats") or {}
    reads = (st.get("blks_hit") or 0) + (st.get("blks_read") or 0)
    if reads > 100_000:
        hit = (st.get("blks_hit") or 0) / reads
        if hit < CACHE_HIT[0]:
            add("cache_hit", "error" if hit < CACHE_HIT[1] else "warn", f"캐시 적중률 {hit:.1%}",
                "shared_buffers 가 작거나 큰 표를 통째로 읽는 쿼리가 있다(느린 쿼리 상위 확인)")
    age = int(st.get("xid_age") or 0)
    if age >= XID_AGE[0]:
        add("xid_age", "error" if age >= XID_AGE[1] else "warn", f"트랜잭션 ID 나이 {age:,}",
            "20억에 가까워지면 DB 가 쓰기를 멈춘다 — VACUUM (FREEZE) 와 자동 VACUUM 동작 확인")
    used = sum(r.get("connections", {}).values())
    mx = r.get("max_connections") or 0
    if mx and used / mx >= CONN_USE[0]:
        add("connections", "error" if used / mx >= CONN_USE[1] else "warn", f"연결 {used}/{mx}",
            "연결 풀 크기·워커 수·멈춘 연결 확인")
    for t in r.get("long_tx", []):
        if t["minutes"] >= LONG_TX_MIN[0]:
            add("long_tx", "error" if t["minutes"] >= LONG_TX_MIN[1] else "warn",
                f"{t['minutes']:.0f}분째 열린 트랜잭션", f"{t['state']} · {t['q']}")
            break
    s = r.get("settings") or {}
    if r.get("pg_stat_statements") is None and "settings" in r:
        add("pg_stat_statements", "info", "느린 쿼리 통계(pg_stat_statements) 꺼짐",
            "vm.sh update 가 켠다(DB 재시작 1번) — 그 전엔 쿼리별 소요 시간을 볼 수 없다")
    if s.get("default_toast_compression") and s["default_toast_compression"] != "lz4":
        add("toast_lz4", "info", "TOAST 압축이 pglz", "lz4 가 압축·해제 모두 빠르다(앱 연결은 lz4 로 쓴다 — 서버 기본도 lz4 권장)")
    if s.get("pending_restart"):
        add("pending_restart", "warn", "재시작해야 적용되는 설정 있음", ", ".join(s["pending_restart"]))
    if fc:
        if fc.get("disk_free_ratio") is not None and fc["disk_free_ratio"] < DISK_FREE[0]:
            add("disk_free", "error" if fc["disk_free_ratio"] < DISK_FREE[1] else "warn",
                f"디스크 남은 공간 {fc['disk_free_ratio']:.0%}", "보관 기한 단축·보관 파일 외부 이전·디스크 증설")
        d = fc.get("days_to_full")
        if d is not None and d < settings.DB_DISK_ALERT_DAYS:
            add("disk_forecast", "error" if d < settings.DB_DISK_ALERT_DAYS / 2 else "warn",
                f"이 추세면 {d}일 뒤 디스크가 찬다", f"하루 {fc['bytes_per_day'] / 1048576:.1f}MB 증가(최근 {fc['points']}일)")
    for n, d in ((ilm_status or {}).get("datasets") or {}).items():
        # 보관은 '달 전체'가 기한을 넘었을 때만 하므로 기한 뒤 한 달 남짓은 정상 — 가장 오래된 행이 기한보다 45일 넘게
        # 오래됐으면 월간 작업이 돌지 않은 것
        if d.get("retention_days", 0) > 0 and d.get("oldest") and d.get("cutoff") and \
                d["oldest"] < (date.fromisoformat(d["cutoff"]) - timedelta(days=45)).isoformat():
            add(f"ilm:{n}", "warn", f"{d['title']} 보관 기한 지난 행 {d['rows_past_cutoff']:,}",
                f"가장 오래된 행 {d['oldest']} · 기한 {d['cutoff']} — 월간 보관 작업 확인(python run.py ilm-archive)")
    return out


def report() -> dict:
    """전체 진단 — 수집·예측·판정·수명주기. 화면(/admin/db)·명령(run.py db-report)·진단 프로브가 쓴다."""
    from app.services import ilm
    r = collect()
    fc = forecast(history(), r["db_bytes"])
    try:
        il = ilm.status()
    except Exception as e:                                # noqa: BLE001 — 보관 표가 없는 구버전 DB
        il = {"error": type(e).__name__}
    fs = findings(r, fc, il)
    order = {"error": 0, "warn": 1, "info": 2}
    top = [{k: t[k] for k in ("name", "total", "heap", "idx", "toast", "live", "dead")} for t in r["tables"][:15]]
    return {"at": r["at"], "database": r["database"], "db_bytes": r["db_bytes"], "forecast": fc,
            "findings": sorted(fs, key=lambda f: order.get(f["level"], 3)), "tables": top,
            "indexes_unused": [f for f in fs if f["rule"].startswith("unused_index")],
            "redundant_indexes": _redundant(r["indexes"]), "settings": r["settings"],
            "stats": r["stats"], "connections": r["connections"], "max_connections": r["max_connections"],
            "long_tx": r["long_tx"], "top_queries": r["pg_stat_statements"], "ilm": il,
            "owner_view": r["owner_view"]}


def snapshot(notify: bool = True) -> dict:
    """매일(스케줄러) — 크기 기록(하루 1행, 2년 보관) + 판정 + 경보(warn·error)."""
    from app.services import alerts
    r = collect(light=True)
    detail = [{k: t[k] for k in ("name", "total", "heap", "idx", "toast", "live", "dead")} for t in r["tables"][:25]]
    db.execute("INSERT INTO db_size_snapshot(day, at, db_bytes, detail) VALUES(:d, now(), :b, :j) "
               "ON CONFLICT (day) DO UPDATE SET at = EXCLUDED.at, db_bytes = EXCLUDED.db_bytes, detail = EXCLUDED.detail",
               {"d": clock.today_str(), "b": r["db_bytes"], "j": json.dumps(detail, ensure_ascii=False)})
    db.execute("DELETE FROM db_size_snapshot WHERE day < :d",
               {"d": (clock.today() - timedelta(days=SNAPSHOT_KEEP_DAYS)).isoformat()})
    full = report()
    sent = 0
    if notify:
        for f in full["findings"]:
            if f["level"] in ("warn", "error"):
                alerts.notify(f"db:{f['rule']}", f"DB 진단: {f['title']}", f["detail"], level=f["level"])
                sent += 1
    return {"db_mb": round(r["db_bytes"] / 1048576, 1), "findings": len(full["findings"]), "alerts": sent,
            "bytes_per_day": full["forecast"].get("bytes_per_day"), "days_to_full": full["forecast"].get("days_to_full")}


def _mb(b):
    return f"{(b or 0) / 1048576:,.1f}"


def render_md(rep: dict) -> str:
    """사람이 읽는 보고서(run.py db-report --md)."""
    fc = rep["forecast"]
    lines = [f"# DB 진단 — {rep['database']} · {rep['at'][:16].replace('T', ' ')} UTC", "",
             f"- 크기 **{_mb(rep['db_bytes'])} MB** · 디스크 남은 공간 "
             f"{(fc.get('disk_free_ratio') or 0):.0%} · 증가 {_mb(fc.get('bytes_per_day'))} MB/일"
             + (f" · 이 추세면 **{fc['days_to_full']}일** 뒤 디스크가 찬다" if fc.get("days_to_full") else ""),
             f"- 연결 {sum(rep['connections'].values())}/{rep['max_connections']} · XID 나이 {int(rep['stats'].get('xid_age') or 0):,}"
             f" · 교착 {rep['stats'].get('deadlocks')} · 임시 파일 {rep['stats'].get('temp_files')}", "",
             "## 판정", ""]
    lines += [f"- [{f['level']}] {f['title']} — {f['detail']}" for f in rep["findings"]] or ["- 이상 없음"]
    lines += ["", "## 큰 표", "", "| 표 | 전체 MB | 본문 | 인덱스 | TOAST | 살아 있는 행 | 죽은 행 |", "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [f"| {t['name']} | {_mb(t['total'])} | {_mb(t['heap'])} | {_mb(t['idx'])} | {_mb(t['toast'])} | "
              f"{t['live']:,} | {t['dead']:,} |" for t in rep["tables"]]
    il = rep.get("ilm") or {}
    if il.get("datasets"):
        lines += ["", "## 데이터 수명주기", "", "| 데이터셋 | 보관 일수 | 기한 | 기한 지난 행 | 보관 파일 | 파일 MB |", "|---|---:|---|---:|---:|---:|"]
        lines += [f"| {d['title']} | {d['retention_days']} | {d['cutoff']} | {d['rows_past_cutoff']:,} | {d['files']} | "
                  f"{_mb(d['bytes'])} |" for d in il["datasets"].values()]
    s = rep.get("settings") or {}
    lines += ["", "## 주요 설정", ""] + [f"- {k} = {v}" for k, v in s.items() if k != "pending_restart"]
    if isinstance(rep.get("top_queries"), list):
        lines += ["", "## 느린 쿼리 상위(총 소요)", ""] + [f"- {q['total_ms']:,} ms · {q['calls']}회 · 평균 {q['mean_ms']} ms — `{q['q']}`"
                                                    for q in rep["top_queries"]]
    return "\n".join(lines) + "\n"
