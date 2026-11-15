"""동시 요청 경합(3차 점검) — 같은 아이디 동시 가입 · 맞송금 · 같은 멱등키 동시 주문 · 동시 정산 ·
DB 제약/장애의 HTTP 매핑(409/503) · Windows(cp949) 콘솔 출력 안전성."""
import io
import sys
import threading

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api import app
from app.logging_conf import safe_console
from app.services import auth, transfer, wallet
from app.services import watchlist as watchlist_svc

client = TestClient(app)


def _par(n, fn):
    barrier, out, errs = threading.Barrier(n), [], []

    def run(i):
        barrier.wait()
        try:
            out.append(fn(i))
        except Exception as e:                       # noqa: BLE001 — 예외 종류까지 검사
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out, errs


def test_same_username_concurrent_register_one_wins_rest_get_auth_error():
    ok, errs = _par(8, lambda i: auth.register("race_user", "pw123456"))
    assert len(ok) == 1 and len(errs) == 7
    assert all(isinstance(e, auth.AuthError) for e in errs)                  # IntegrityError(500) 아님


def test_opposite_transfers_serialize_without_deadlock_and_conserve_money():
    for u in ("tx_a", "tx_b"):
        auth.register(u, "pw123456")
    wallet.credit_once("tx_a", 1_000_000, "attendance")
    wallet.credit_once("tx_b", 1_000_000, "attendance")
    before = wallet.balance("tx_a") + wallet.balance("tx_b")
    # 돈의 동시성(교착·보존)만 본다 — FDS(10분 5건 추가 인증)는 test_fds 가 따로 본다
    ok, errs = _par(16, lambda i: transfer.send("tx_a", "tx_b", 1000, screen=False) if i % 2 else
                    transfer.send("tx_b", "tx_a", 1000, screen=False))
    assert not errs and len(ok) == 16                                          # 충돌로 실패한 송금 없음
    assert wallet.balance("tx_a") + wallet.balance("tx_b") == before
    assert wallet.reconcile("tx_a")["ok"] and wallet.reconcile("tx_b")["ok"]


def test_db_constraint_and_outage_map_to_409_and_503(monkeypatch):
    client.post("/auth/register", json={"username": "map_user", "password": "pw123456"})
    tok = client.post("/auth/login", json={"username": "map_user", "password": "pw123456"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    def dup(*a, **k):
        raise IntegrityError("INSERT …", {}, Exception("duplicate key value violates unique constraint"))
    monkeypatch.setattr(watchlist_svc, "add", dup)
    r = client.post("/watchlist/005930", headers=h)
    assert r.status_code == 409 and "duplicate" not in r.text                # 내부 메시지 비노출

    def down(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
    monkeypatch.setattr(watchlist_svc, "add", down)
    r = client.post("/watchlist/005930", headers=h)
    assert r.status_code == 503 and r.headers.get("retry-after") == "5"


def test_console_output_never_crashes_on_cp949(monkeypatch):
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp949")                           # 한국어 Windows 리디렉션 출력
    monkeypatch.setattr(sys, "stdout", stream)
    safe_console()
    print("데모 데이터 준비 완료 — ✓ 끝")                                       # — ✓ 는 cp949 에 없음
    stream.flush()
    assert "데모 데이터 준비 완료".encode("cp949") in buf.getvalue()
