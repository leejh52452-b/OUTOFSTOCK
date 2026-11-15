"""요청당 SQL 예산 — N+1·중복 확인이 다시 들어오지 않게(성능 회귀 시험).

2026-11-02 측정에서 고친 것: 송금 한 건이 같은 계정 잠금을 5번·지갑 생성 확인을 4번·계좌번호 조회를 4번 하던 것(41 → 33),
지갑·꾸미기 조회(GET)가 매번 계정 잠금 + INSERT … ON CONFLICT 를 실행하던 것(조회가 쓰기를 함 — 5 → 3, 쓰기 0).
같은 트랜잭션 안의 반복은 트랜잭션 범위 메모(db.tx_cache)로 한 번만 한다. 예산을 넘기면 실행 계획·호출 경로부터 본다."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import db
from app.api import app
from app.services import auth, wallet

client = TestClient(app)
PW = "Qb-pass-2026"
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


@pytest.fixture()
def sql():
    seen = []

    def on(conn, cursor, statement, params, context, executemany):
        seen.append(statement)
    event.listen(db.get_engine(), "before_cursor_execute", on)
    yield seen
    event.remove(db.get_engine(), "before_cursor_execute", on)


@pytest.fixture()
def users():
    for u in ("qb-a", "qb-b"):
        auth.register(u, PW)
    wallet.credit_once("qb-a", 5_000_000, "iap", memo="시험 충전")
    tok = client.post("/auth/login", json={"username": "qb-a", "password": PW}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    client.get("/wallet", headers=h)                       # 지갑을 만든 뒤(첫 조회만 생성)부터 잰다
    return h


@pytest.mark.parametrize("path,budget", [("/wallet", 3), ("/avatar/me", 3), ("/me/badges", 3), ("/me", 18)])
def test_account_reads_stay_within_budget_and_never_write(sql, users, path, budget):
    sql.clear()
    assert client.get(path, headers=users).status_code == 200
    writes = [s.split()[0:3] for s in sql if WRITE.match(s)]
    assert not writes, f"{path} 조회가 쓰기를 했다: {writes}"
    assert len(sql) <= budget, f"{path}: SQL {len(sql)}개 > 예산 {budget}"


@pytest.mark.parametrize("method,path,body,budget", [
    ("POST", "/transfer", {"to": "qb-b", "amount": 1000}, 33),
    ("POST", "/avatar/buy", {"item_id": "hat-1"}, 15),
    ("POST", "/attendance/check-in", None, 16),
])
def test_money_writes_stay_within_budget(sql, users, method, path, body, budget):
    sql.clear()
    r = client.request(method, path, headers=users, json=body)
    assert r.status_code == 200, r.text
    assert len(sql) <= budget, f"{path}: SQL {len(sql)}개 > 예산 {budget}"


def test_transfer_takes_each_account_lock_once(sql, users):
    sql.clear()
    assert client.post("/transfer", headers=users, json={"to": "qb-b", "amount": 1000}).status_code == 200
    holds = [s for s in sql if "FOR KEY SHARE" in s]
    ensures = [s for s in sql if "INSERT INTO wallet(" in s]
    accts = [s for s in sql if s.strip().startswith("SELECT account_no FROM wallet")]
    assert len(holds) == 2 and len(ensures) == 2 and len(accts) == 2      # 두 계정 × 1번씩


def test_tx_cache_does_not_leak_between_transactions():
    with db.transaction() as c1:
        db.tx_cache(c1)["x"] = 1
    with db.transaction() as c2:
        assert "x" not in db.tx_cache(c2)
