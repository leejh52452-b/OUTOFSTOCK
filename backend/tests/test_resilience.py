"""복원력(카오스) — DB 지연·멈춘 트랜잭션·DB 장애에서 빠르게 실패하고 서비스가 계속 응답하는지."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import db
from app.api import app
from app.services import market

client = TestClient(app)


def test_slow_query_hits_statement_timeout_and_returns_504_fast(monkeypatch):
    def slow():
        with db.transaction() as c:
            c.execute(text("SET LOCAL statement_timeout = 200"))
            c.execute(text("SELECT pg_sleep(3)"))
    monkeypatch.setattr(market, "indices", slow)
    t0 = time.time()
    r = client.get("/indices")
    assert r.status_code == 504 and time.time() - t0 < 2                       # 3초를 기다리지 않는다
    assert client.get("/health").status_code in (200, 503)                     # 서버는 계속 응답


def test_stuck_transaction_is_terminated_by_idle_timeout():
    eng = create_engine(db.get_engine().url, future=True)
    try:
        with eng.connect() as c:
            c.execute(text("SET idle_in_transaction_session_timeout = 200"))
            c.execute(text("SELECT 1"))                                         # 트랜잭션 열린 채 멈춤
            time.sleep(0.6)
            with pytest.raises(Exception):
                c.execute(text("SELECT 1"))                                     # DB 가 끊었다(잠금을 쥔 채 남지 않음)
    finally:
        eng.dispose()


def test_db_outage_returns_503_with_retry_after(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Down(Exception):
        sqlstate = "08006"

    def boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Down())
    monkeypatch.setattr(market, "indices", boom)
    r = client.get("/indices")
    assert r.status_code == 503 and r.headers.get("retry-after") == "5" and "detail" in r.json()
