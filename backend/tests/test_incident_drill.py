"""사고 대응 모의훈련 — 신호 → 탐지 → 자동 차단 → 실제 요청 확인 → 사람 절차(2인 재개)로 복구(app/security/drill.py).
훈련 하네스 자체를 시험 DB 에서 돌려, 7개 시나리오가 모두 막히고 되돌아오는지·시간이 기록되는지·운영 알림 채널로는
보내지 않는지 확인한다."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.security import assessment, drill
from app.services import alerts

client = TestClient(app)


def test_drill_contains_and_recovers_every_scenario(monkeypatch):
    pushed = []
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.invalid/oos")
    monkeypatch.setitem(alerts._SENDERS, "webhook", lambda title, body: pushed.append(title))
    rep = drill.run(client)
    assert rep["ok"], [(r["name"], [s for s in r["steps"] if not s["ok"]]) for r in rep["results"]]
    assert [r["name"] for r in rep["results"]] == list(drill.SCENARIOS) and rep["passed"] == 7
    for r in rep["results"]:
        assert r["detect_ms"] is not None and r["contain_ms"] is not None and r["recover_ms"] is not None, r["name"]
        assert r["detect_ms"] <= r["contain_ms"] <= r["recover_ms"], r["name"]
    assert pushed == []                                            # 훈련 경보가 실제 당번 채널로 나가지 않았다
    assert settings.ALERT_WEBHOOK_URL == "https://hooks.example.invalid/oos"      # 끝나면 설정 복원
    titles = [x["title"] for x in db.query("SELECT title FROM alert_log ORDER BY alert_id")]
    assert any("원장 변조" in t for t in titles) and any("코드·화면 파일" in t for t in titles)   # 경보 자체는 기록
    from app.services import controls
    controls.clear_cache()
    assert not any(controls.is_paused(f) for f in drill.MONEY)                 # 훈련이 끝나면 모든 기능이 운영 상태
    drill.record(rep)
    assert assessment._run_probe("drill_recent")["status"] == "ok"
    md = drill.render_md(rep)
    assert all(t in md for t in drill.TITLES.values()) and "7/7 통과" in md
    from app.services import incidents
    inc = incidents.listing(5)
    assert any(i["kind"] == "outage" and i["reportable"] and i["status"] == "resolved" for i in inc)   # 장애 판정·보고 훈련
    assert settings.CHANGE_GATE == "record" and settings.CHANGE_FREEZE == "mon-fri 08:30-16:00"   # 변경관리 훈련 뒤 설정 복원


@pytest.mark.parametrize("target,main,why", [
    ("postgresql+psycopg://u:p@db/outofstock", "postgresql+psycopg://u:p@db/outofstock_dev", "drill 또는 test"),
    ("postgresql+psycopg://u:p@db/outofstock_drill", "postgresql+psycopg://u:p@db/outofstock_drill", "같은 DB"),
])
def test_drill_refuses_targets_that_are_not_drill_databases(target, main, why):
    with pytest.raises(drill.DrillError, match=why):
        drill.guard_target(target, main)
    assert drill.guard_target("postgresql+psycopg://u:p@db/outofstock_drill",
                              "postgresql+psycopg://u:p@db/outofstock") == "outofstock_drill"
