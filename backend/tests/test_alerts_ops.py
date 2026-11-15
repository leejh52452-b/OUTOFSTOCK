"""관제 — 경보 확인(ack)·미확인 오류 경보 상향(15분)·확인까지 걸린 시간(MTTA) · 스케줄러 작업
(app/services/alerts.py · scheduler.py). 금융위(2026.10) 점검의 '24시간 관제 공백'을 작은 서비스 크기로."""
from datetime import timedelta

import scheduler
from app import clock, db
from app.config import settings
from app.security import assessment
from app.services import alerts


def _backdate(key, minutes):
    db.execute("UPDATE alert_log SET created_at=:t WHERE key=:k",
               {"t": (clock.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds"), "k": key})


def test_unacked_error_alert_escalates_once_and_ack_clears_it():
    alerts.notify("job:시세 적재", "예약 작업 실패: 시세 적재", "boom", level="error")
    alerts.notify("quality:kr", "품질 주의", "", level="warn")                    # 주의 수준은 상향하지 않는다
    assert alerts.escalate_unacked() == {}                                   # 아직 15분 안 — 상향 없음
    _backdate("job:시세 적재", 20)
    _backdate("quality:kr", 20)
    assert alerts.escalate_unacked() == {"escalated": ["job:시세 적재"]}
    esc = db.query("SELECT key, level, title FROM alert_log WHERE key LIKE 'escalate:%'")
    assert len(esc) == 1 and esc[0]["level"] == "error" and esc[0]["title"].startswith("[상향] 20분째")
    assert alerts.escalate_unacked() == {}                                   # 같은 경보는 한 번만 상향(상향 알림 자체도 상향 안 함)
    _backdate("escalate:job:시세 적재", 30)
    assert alerts.escalate_unacked() == {}
    aid = db.query("SELECT alert_id FROM alert_log WHERE key='escalate:job:시세 적재'")[0]["alert_id"]
    r = alerts.ack(aid, "ops-a")                                             # 상향 알림에서 확인해도 원래 경보까지 함께
    assert r["key"] == "job:시세 적재" and r["acked"] == 2
    assert not db.query("SELECT 1 FROM alert_log WHERE key LIKE '%job:시세 적재' AND ack_at IS NULL")
    assert db.query("SELECT actor FROM audit_log WHERE action='alert.ack'")[0]["actor"] == "ops-a"
    s = alerts.ack_stats()
    assert s["errors"] == 1 and s["acked"] == 1 and s["unacked_overdue"] == 0 and s["mtta_min"] >= 20
    assert assessment._run_probe("alerts_acked")["status"] == "fail"         # MTTA 20분 > 목표 15분


def test_ack_within_target_keeps_the_probe_green():
    assert assessment._run_probe("alerts_acked")["status"] == "ok"           # 오류 경보 없음
    alerts.notify("job:x", "예약 작업 실패: x", "", level="error")
    _backdate("job:x", 16)
    assert assessment._run_probe("alerts_acked")["status"] == "fail"         # 기한 지난 미확인
    alerts.ack(db.query("SELECT alert_id FROM alert_log WHERE key='job:x'")[0]["alert_id"], "ops-a")
    db.execute("UPDATE alert_log SET ack_at=:t WHERE key='job:x'",
               {"t": (clock.now() - timedelta(minutes=10)).isoformat(timespec="seconds")})   # 6분 만에 확인한 것으로
    s = alerts.ack_stats()
    assert s["mtta_min"] == 6.0 and s["p90_min"] == 6.0
    assert assessment._run_probe("alerts_acked")["status"] == "ok"
    try:
        alerts.ack(999_999, "ops-a")
        raise AssertionError("없는 경보 확인이 통과")
    except LookupError:
        pass


def test_old_alerts_are_not_escalated_after_deploy(monkeypatch):
    alerts.notify("job:old", "예약 작업 실패: 옛날", "", level="error")
    _backdate("job:old", 60 * 30)                                            # 하루 넘은 옛 경보 — 첫 배포 때 폭주하지 않게
    assert alerts.escalate_unacked() == {}
    monkeypatch.setattr(settings, "ALERT_ACK_MINUTES", 0)
    alerts.notify("job:new", "예약 작업 실패: 방금", "", level="error")
    assert alerts.escalate_unacked()["escalated"] == ["job:new"]


def test_scheduler_runs_uptime_watch_and_change_review_jobs():
    jobs = {j.id: j for j in scheduler.build_scheduler().get_jobs()}
    assert {"uptime", "watch", "change_reviews"} <= set(jobs)
    assert jobs["uptime"].trigger.interval == timedelta(minutes=1)
    assert jobs["watch"].trigger.interval == timedelta(minutes=5)
    alerts.notify("job:y", "예약 작업 실패: y", "", level="error")
    _backdate("job:y", 30)
    out = scheduler.job_watch()
    assert out["escalate"] == {"escalated": ["job:y"]} and "deadlines" not in out
