"""운영 관측성 + 파이프라인 실행 이력 — 배치가 언제/어떻게 돌았는지 추적하고 헬스체크로 노출."""
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import monitoring as mon


def test_run_lifecycle_success():
    """실행 시작→종료 기록: 소스별 건수·품질결과·상태가 이력에 남는다."""
    run_id = mon.start_run()
    running = mon.last_run()
    assert running["status"] == "RUNNING" and running["finished_at"] is None

    mon.finish_run(run_id, "SUCCESS",
                   counts={"prices": 6, "dart": 5, "news": 30},
                   quality={"ok": True, "total_issues": 0})
    done = mon.last_run()
    assert done["status"] == "SUCCESS"
    assert done["price_rows"] == 6 and done["dart_rows"] == 5 and done["news_rows"] == 30
    assert done["quality_ok"] == 1 and done["finished_at"] is not None


def test_failed_run_is_recorded():
    """실패한 배치도 원인과 함께 이력에 남는다."""
    run_id = mon.start_run()
    mon.finish_run(run_id, "FAILED", note="DART timeout")
    last = mon.last_run()
    assert last["status"] == "FAILED"
    assert last["note"] == "DART timeout"


def test_quality_fail_flag_recorded():
    run_id = mon.start_run()
    mon.finish_run(run_id, "SUCCESS", quality={"ok": False, "total_issues": 3})
    last = mon.last_run()
    assert last["quality_ok"] == 0 and last["quality_issues"] == 3


def test_recent_runs_newest_first():
    ids = [mon.start_run() for _ in range(3)]
    for rid in ids:
        mon.finish_run(rid, "SUCCESS")
    runs = mon.recent_runs(limit=10)
    assert [r["run_id"] for r in runs] == sorted(ids, reverse=True)


def test_health_reports_freshness():
    from datetime import date
    today = date.today().isoformat()   # 상대 날짜 → 날짜 드리프트 방지(신선도 임계 통과)
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES('005930', :d, 71000, 1000, 1.0)", {"d": today})
    h = mon.health()
    assert h["status"] == "ok" and h["db_ok"] is True
    assert h["latest_price_date"] == today
    assert isinstance(h["data_age_days"], int)


def test_health_no_data_still_ok():
    """데이터가 없어도 DB 연결은 정상 → status ok, 신선도는 None."""
    h = mon.health()
    assert h["db_ok"] is True
    assert h["latest_price_date"] is None and h["data_age_days"] is None


def test_health_and_runs_via_api():
    client = TestClient(app)
    rid = mon.start_run()
    mon.finish_run(rid, "SUCCESS", counts={"prices": 6, "dart": 5, "news": 10})

    h = client.get("/health")
    assert h.status_code == 200 and h.json()["db_ok"] is True
    assert h.json()["last_run"]["status"] == "SUCCESS"
    assert mon.recent_runs()[0]["run_id"] == rid
