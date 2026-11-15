"""용량·성능 관리 — 임계치 4단계 · 우선순위 대량접속제어 · 성능관리보고서 · 연결 풀 빠른 실패(app/services/capacity.py).
근거: 금감원 「금융IT 안정성 강화를 위한 가이드라인」(2023.11) 성능 관리 — 정상·주의·경계·심각, 경계·심각 보고, 대량접속제어."""
import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app import db
from app.api import app
from app.config import settings
from app.security import pdp, pep
from app.services import auth, capacity, market

client = TestClient(app)
MAX = settings.CAPACITY_MAX_INFLIGHT


@pytest.fixture(autouse=True)
def _clean():
    capacity.reset()
    yield
    capacity.reset()


def _tier(route, method):
    return pdp.tier(pep.route_action(route), f"{method} {route.path}")


def test_every_route_has_a_tier_and_money_auth_admin_are_critical():
    tiers = {}
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in r.methods:
                tiers[f"{m} {r.path}"] = _tier(r, m)
    assert set(tiers.values()) <= {"critical", "normal", "low"}
    for key in ("POST /auth/login", "POST /auth/refresh", "POST /transfer", "POST /avatar/buy", "POST /invest/buy",
                "DELETE /me", "GET /health", "GET /admin/capacity", "POST /admin/controls/{feature}/pause"):
        assert tiers[key] == "critical", key
    for key in ("GET /market/stocks", "GET /api/news/home", "GET /search/suggest", "GET /indices"):
        assert tiers[key] == "low", key                                 # 공개 시세·뉴스 — 혼잡하면 먼저 양보
    for key in ("GET /", "GET /{page}.html", "GET /me", "GET /wallet"):
        assert tiers[key] == "normal", key


def test_levels_follow_the_four_stage_thresholds():
    m = {"inflight": 0, "pool_in_use": 0, "pool_capacity": 30, "p95_ms": 0, "requests_1m": 0, "error5_ratio": 0}
    for n, want in ((0, 0), (int(MAX * .6), 1), (int(MAX * .8), 2), (MAX, 3)):
        assert capacity.capacity_level({**m, "inflight": n}) == want, n
    assert capacity.capacity_level({**m, "pool_in_use": 29}) == 3          # DB 연결이 바닥나도 심각
    slow = {**m, "requests_1m": 100, "p95_ms": 1200}
    assert capacity.capacity_level(slow) == 0 and capacity.level_now(slow) == 2   # 지연은 보고 단계에만(입장엔 안 씀)
    assert capacity.level_now({**m, "requests_1m": 100, "error5_ratio": 0.25}) == 3


def test_alert_level_sheds_low_tier_first_and_keeps_core_transactions():
    auth.register("cap-user", "Cap-pass-2026")
    tok = client.post("/auth/login", json={"username": "cap-user", "password": "Cap-pass-2026"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    capacity.set_inflight(int(MAX * .8))                                 # 경계 — low 만 돌려보냄
    r = client.get("/market/stocks")
    assert r.status_code == 503 and r.json()["code"] == "overloaded" and r.headers["Retry-After"] == "2"
    assert "혼잡 경계" in r.json()["detail"] and r.headers["X-Load-Level"] == "2"
    assert client.get("/me", headers=h).status_code == 200              # normal 은 아직 받는다
    capacity.set_inflight(MAX - 1)                                       # 심각 — normal 도 돌려보냄
    assert client.get("/me", headers=h).status_code == 503
    assert client.get("/health").status_code == 200                      # 감시 경로는 끝까지
    r = client.post("/auth/login", json={"username": "cap-user", "password": "Cap-pass-2026"})
    assert r.status_code == 200                                          # 핵심 거래(로그인)는 받는다
    shed = capacity.metrics()["shed"]
    assert shed["low"] == 1 and shed["normal"] == 1 and shed["critical"] == 0


def test_admission_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "CAPACITY_ADMISSION", False)
    capacity.set_inflight(MAX)
    assert client.get("/market/stocks").status_code == 200


def test_sse_stream_is_not_counted_as_in_flight_load():
    assert not capacity.counted("/events") and capacity.counted("/transfer")


def test_alert_level_writes_a_performance_report_and_alerts(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda key, title, msg="", level="error", dedup_hours=None:
                        sent.append((key, title, msg, level)) or {"sent": []})
    capacity.set_inflight(int(MAX * .85))
    rep = capacity.report(capacity.level_now(), 0)
    assert rep and rep["level"] == "경계" and rep["metrics"]["inflight"] == int(MAX * .85)
    assert any("워커 한도" in a for a in rep["advice"])
    row = db.query("SELECT level, detail FROM capacity_report WHERE id=:i", {"i": rep["id"]})[0]
    assert row["level"] == 2 and json.loads(row["detail"])["from"] == "정상"
    assert sent and sent[0][0] == "capacity:2" and sent[0][3] == "warn"


def test_transition_reports_only_when_rising_into_alert_and_not_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(capacity, "report", lambda lv, prev=0: calls.append((lv, prev)))
    monkeypatch.setattr(capacity.threading, "Thread",
                        lambda target, args, daemon, name: type("T", (), {"start": lambda self: target(*args)})())
    for lv in (1, 2, 1, 2, 3, 0):
        capacity._transition(lv)
    assert calls == [(2, 1), (3, 2)]                                     # 경계는 10분 안에 한 번만, 심각은 따로


def test_pool_exhaustion_fails_fast_with_503(monkeypatch):
    def boom():
        raise PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached")
    monkeypatch.setattr(market, "indices", boom)
    r = client.get("/indices")
    assert r.status_code == 503 and r.headers["Retry-After"] == "3"
    assert capacity.metrics()["pool_timeouts"] == 1
    assert db.get_engine().pool._timeout == settings.DB_POOL_TIMEOUT_S <= 10   # 기본 30초 대기가 아니다


def test_prometheus_exposes_capacity_gauges():
    text = capacity.prometheus()
    for k in ("oos_capacity_level", "oos_inflight_requests", "oos_db_pool_in_use", 'oos_shed_total{tier="low"}'):
        assert k in text
