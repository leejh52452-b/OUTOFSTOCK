"""DB 진단·용량 예측(app/services/dbhealth.py)·서버 설정 권장값(app/services/dbtune.py) — ADR-0027."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.services import dbhealth, dbtune

client = TestClient(app)
MB = 1048576


def test_forecast_fits_the_growth_trend_and_days_to_full(monkeypatch):
    import shutil
    from collections import namedtuple
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(100_000 * MB, 90_000 * MB, 10_000 * MB))
    today = clock.today()
    hist = [{"day": (today - timedelta(days=30 - i)).isoformat(), "db_bytes": 500 * MB + i * 10 * MB} for i in range(31)]
    fc = dbhealth.forecast(hist, hist[-1]["db_bytes"])
    assert abs(fc["bytes_per_day"] - 10 * MB) < 1024                    # 하루 10MB
    assert fc["days_to_full"] == 1000 and fc["disk_free_ratio"] == 0.1
    assert dbhealth.forecast(hist[:5], 0)["bytes_per_day"] is None     # 7일 미만 기록이면 추정하지 않는다


def _rep(**kw):
    base = {"tables": [], "indexes": [], "stats": {"blks_hit": 10, "blks_read": 0, "xid_age": 1000, "stats_reset": None},
            "connections": {"active": 1}, "max_connections": 100, "long_tx": [], "settings": {"default_toast_compression": "lz4"},
            "pg_stat_statements": []}
    base.update(kw)
    return base


def test_findings_cover_bloat_cache_wraparound_connections_and_long_transactions():
    big = {"name": "public.news", "total": 300 * MB, "heap": 200 * MB, "idx": 100 * MB, "toast": 0, "live": 600, "dead": 400}
    fs = dbhealth.findings(_rep(tables=[big], stats={"blks_hit": 85_000, "blks_read": 15_000 + 5, "xid_age": 1_300_000_000,
                                                      "stats_reset": None},
                                connections={"active": 90, "idle": 6}, long_tx=[{"minutes": 75.0, "state": "idle in transaction",
                                                                                "q": "SELECT 1"}]))
    lv = {f["rule"].split(":")[0]: f["level"] for f in fs}
    assert lv["dead"] == "warn"                                         # 죽은 행 40%
    assert lv["cache_hit"] == "error" and lv["xid_age"] == "error"
    assert lv["connections"] == "error" and lv["long_tx"] == "error"
    assert not dbhealth.findings(_rep())                                # 건강하면 판정 없음


def test_redundant_index_detector_matches_the_migration_rule():
    idx = [{"tbl": "public.t", "name": "ix_a", "cols": [1], "uniq": False, "pk": False, "expr": False, "partial": False,
            "bytes": 8 * MB, "scans": 3},
           {"tbl": "public.t", "name": "t_pkey", "cols": [1, 2], "uniq": True, "pk": True, "expr": False, "partial": False,
            "bytes": 9 * MB, "scans": 9},
           {"tbl": "public.t", "name": "ix_expr", "cols": [0], "uniq": False, "pk": False, "expr": True, "partial": False,
            "bytes": 1 * MB, "scans": 0}]
    red = dbhealth._redundant(idx)
    assert [r["index"] for r in red] == ["ix_a"] and red[0]["covered_by"] == "t_pkey"
    live = dbhealth.collect()
    assert not dbhealth._redundant(live["indexes"])                     # 이 스키마에는 겹치는 인덱스가 없다


def test_snapshot_keeps_one_row_per_day_and_report_renders(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.alerts.notify", lambda *a, **k: sent.append(a))
    r1 = dbhealth.snapshot(notify=False)
    r2 = dbhealth.snapshot(notify=False)
    rows = db.query("SELECT day, db_bytes FROM db_size_snapshot")
    assert len(rows) == 1 and rows[0]["day"] == clock.today_str() and r1["db_mb"] > 0 and r2["db_mb"] > 0
    rep = dbhealth.report()
    assert rep["db_bytes"] > 0 and rep["settings"]["TimeZone"] and "ilm" in rep
    md = dbhealth.render_md(rep)
    assert "## 판정" in md and "## 큰 표" in md and "## 데이터 수명주기" in md
    assert not sent


def test_admin_db_needs_an_operations_role():
    assert client.get("/admin/db").status_code in (401, 403)


@pytest.mark.parametrize("ram,pg,sb,wal,toast", [(2048, 14, "400MB", "on", "lz4"), (4096, 16, "816MB", "lz4", "lz4"),
                                                  (16384, 16, "4GB", "lz4", "lz4"), (4096, 13, "816MB", "on", None)])
def test_tuning_scales_with_memory_and_version(ram, pg, sb, wal, toast):
    r = dbtune.recommend(ram, cpus=4, pg_major=pg)
    assert r["shared_buffers"] == sb and r["wal_compression"] == wal
    assert r.get("default_toast_compression") == toast
    assert r["random_page_cost"] == "1.1" and r["jit"] == "off" and r["max_parallel_workers_per_gather"] == "2"
    assert 4 <= int(r["work_mem"].rstrip("MB")) <= 32


def test_tuning_keeps_existing_preload_modules_and_marks_restarts():
    r = dbtune.recommend(4096, preload="auto_explain")
    assert r["shared_preload_libraries"] == "auto_explain,pg_stat_statements"
    assert dbtune.recommend(4096, preload="pg_stat_statements")["shared_preload_libraries"] == "pg_stat_statements"
    lines = dict((x.split("\t")[0], x.split("\t")[2]) for x in dbtune.plan_lines(4096, 2, 16))
    assert lines["shared_buffers"] == "1" and lines["shared_preload_libraries"] == "1" and lines["work_mem"] == "0"


def test_vm_sh_tunes_while_services_are_stopped_and_can_be_disabled():
    from pathlib import Path
    sh = Path(__file__).resolve().parents[1].joinpath("deploy", "vm.sh").read_text(encoding="utf-8")
    post = sh[sh.index("post_update() {"):sh.index("cmd_rollback() {")]
    assert post.index("db_tune") < post.index("start_services")         # 서비스가 멈춘 동안 — 재시작이 필요해도 영향 없음
    tune = sh[sh.index("db_tune() {"):sh.index("wait_pg()")]
    assert "DB_AUTOTUNE" in tune and "ALTER SYSTEM SET" in tune and "postgresql.auto.conf" in tune
    assert "localhost|127.0.0.1" in tune                                 # 다른 서버의 DB 는 건드리지 않는다
    assert 'grep -q "sql/migrations/V018__"' in sh                       # 타입 정리 전 코드로 되돌릴 때 DB 복원 안내
