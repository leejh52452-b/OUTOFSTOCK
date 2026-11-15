"""시도 제한 공유 저장소(PostgreSQL) — 워커·서버가 여럿이어도 한도가 하나(app/services/ratelimit_pg.py)."""
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.services import auth, ratelimit


@pytest.fixture(autouse=True)
def _pg_backend(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "postgres")
    yield


def test_concurrent_takes_admit_exactly_the_limit():
    key = ratelimit.key_of("t", "conc")
    results, barrier = [], threading.Barrier(20)

    def go():
        barrier.wait()
        results.append(ratelimit.take([(key, 5)], 60))
    ts = [threading.Thread(target=go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert results.count(0) == 5 and all(w > 0 for w in results if w)
    assert db.query("SELECT COUNT(*) AS n FROM rate_event WHERE key=:k", {"k": key})[0]["n"] == 5


def test_undo_reset_and_multi_key_atomicity():
    a, b = ratelimit.key_of("a"), ratelimit.key_of("b")
    assert ratelimit.take([(a, 2), (b, 1)], 60) == 0
    assert ratelimit.take([(a, 2), (b, 1)], 60) > 0          # b 가 찼으면 a 도 기록하지 않는다(전부 또는 없음)
    assert db.query("SELECT COUNT(*) AS n FROM rate_event WHERE key=:k", {"k": a})[0]["n"] == 1
    ratelimit.undo(b)
    assert ratelimit.take([(a, 2), (b, 1)], 60) == 0
    ratelimit.reset(a)
    assert db.query("SELECT COUNT(*) AS n FROM rate_event WHERE key=:k", {"k": a})[0]["n"] == 0


def test_login_limit_is_shared_across_workers():
    """워커 두 개(서로 다른 프로세스라고 가정 — 메모리 제한이면 각자 5회씩 10회)가 같은 표를 본다."""
    auth.register("rl-pg", "Rl-pass-2026")
    c1, c2 = TestClient(app), TestClient(app)
    codes = [(c1 if i % 2 else c2).post("/auth/login", json={"username": "rl-pg", "password": "wrong-pass-1"}).status_code
             for i in range(8)]
    assert codes[:settings.LOGIN_MAX_FAILS].count(401) == settings.LOGIN_MAX_FAILS and 429 in codes[settings.LOGIN_MAX_FAILS:]
