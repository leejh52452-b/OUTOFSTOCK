"""스케줄러 — 매일 23:00(KST) run.py real · 시각 설정 파싱 · 놓친 실행 보충 판단 · 동시 실행 방지."""
from datetime import datetime, timedelta, timezone

import run
import scheduler
from app import db
from app.services import quotes

KST = timezone(timedelta(hours=9))


def test_parse_hhmm():
    assert scheduler.parse_hhmm("23:00") == (23, 0)
    assert scheduler.parse_hhmm("7:05") == (7, 5)
    assert scheduler.parse_hhmm("25:00") == (23, 0)          # 잘못된 값 → 기본
    assert scheduler.parse_hhmm("abc") == (23, 0)


def test_real_job_runs_daily_at_23_kst():
    s = scheduler.build_scheduler()
    jobs = {j.id: j for j in s.get_jobs()}
    assert {"real", "krx", "kis", "us"} <= set(jobs) and "pipeline" not in jobs   # 레거시 파이프라인 대체
    assert "news" not in jobs                                  # 뉴스는 23:00 real 안에서만(하루 1번)
    now = datetime(2026, 9, 24, 22, 59, tzinfo=KST)          # 목요일 22:59
    nxt = jobs["real"].trigger.get_next_fire_time(None, now)
    assert nxt.astimezone(KST).strftime("%Y-%m-%d %H:%M") == "2026-09-24 23:00"
    sat = datetime(2026, 9, 26, 23, 30, tzinfo=KST)          # 토요일 23:30 → 일요일 23:00(매일)
    assert jobs["real"].trigger.get_next_fire_time(None, sat).astimezone(KST).strftime("%a %H:%M") == "Sun 23:00"
    assert jobs["real"].misfire_grace_time == 3600 and jobs["real"].coalesce
    row = [x for x in scheduler.describe(s, now) if x[0] == "real"][0]
    assert row[2].endswith("23:00") and row[1] == "run.py real 전체 파이프라인"   # 로그에 한글 작업명


def test_needs_catchup():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=KST)            # 고정 시각(실행 시각에 따라 결과가 바뀌지 않게)
    db.set_meta("pipeline_real_finished", None)
    assert scheduler.needs_catchup(now)                       # 기록 없음 → 보충
    db.set_meta("pipeline_real_finished", (now - timedelta(hours=3)).isoformat())
    assert not scheduler.needs_catchup(now)
    db.set_meta("pipeline_real_finished", (now - timedelta(hours=30)).isoformat())
    assert scheduler.needs_catchup(now)


def test_real_pipeline_skips_when_already_running(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "_real_all", lambda: calls.append(1) or {"market": "gen"})
    with db.advisory_lock("real") as got:                     # 다른 프로세스가 실행 중인 상황
        assert got
        r = run.real_pipeline("scheduler")
    assert r["skipped"] and calls == []
    r2 = run.real_pipeline("scheduler")                       # 잠금이 풀리면 정상 실행 + 기록
    assert not r2.get("skipped") and calls == [1]
    assert db.get_meta("pipeline_real_trigger") == "scheduler"
    assert quotes.parse_ts(db.get_meta("pipeline_real_finished")) is not None
    runs = db.query("SELECT status, note FROM pipeline_run ORDER BY run_id DESC LIMIT 1")
    assert runs[0]["status"] == "SUCCESS" and "run.py real" in runs[0]["note"]


def test_real_pipeline_records_failure(monkeypatch):
    def boom():
        raise RuntimeError("KRX down")
    monkeypatch.setattr(run, "_real_all", boom)
    try:
        run.real_pipeline("scheduler")
        raise AssertionError("예외가 전파돼야 함")
    except RuntimeError:
        pass
    assert db.query("SELECT status FROM pipeline_run ORDER BY run_id DESC LIMIT 1")[0]["status"] == "FAILED"
    assert '"ok": false' in db.get_meta("pipeline_real_status")
    with db.advisory_lock("real") as got:                     # 실패해도 잠금은 풀려 있어야 함
        assert got
