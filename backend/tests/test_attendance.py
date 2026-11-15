"""출석 체크 — 하루 1회 보상(멱등) + 연속 출석(streak) 계산."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.api import app
from app.services import attendance, wallet

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_check_in_gives_reward_once_per_day():
    u = "att1@oos.io"
    h = _auth(u)
    b0 = wallet.balance(u)
    r = client.post("/attendance/check-in", headers=h).json()
    assert r["claimed"] is True and r["reward_given"] == 1_000_000
    assert wallet.balance(u) == b0 + 1_000_000
    r2 = client.post("/attendance/check-in", headers=h).json()      # 같은 날 재출석
    assert r2["claimed"] is False
    assert wallet.balance(u) == b0 + 1_000_000                      # 이중 지급 없음


def test_streak_increments_and_resets():
    u = "att2@oos.io"
    _auth(u)
    d0 = date(2026, 1, 1)
    attendance.check_in(u, today=d0.isoformat())
    attendance.check_in(u, today=(d0 + timedelta(days=1)).isoformat())
    s3 = attendance.check_in(u, today=(d0 + timedelta(days=2)).isoformat())
    assert s3["streak"] == 3                                        # 3일 연속
    s_gap = attendance.check_in(u, today=(d0 + timedelta(days=4)).isoformat())
    assert s_gap["streak"] == 1                                     # 하루 걸러 → 리셋


def test_status_shape():
    u = "att3@oos.io"
    h = _auth(u)
    d = client.get("/attendance", headers=h).json()
    assert d["checked_in_today"] is False and d["total_days"] == 0
    assert d["daily_reward"] == 1_000_000
