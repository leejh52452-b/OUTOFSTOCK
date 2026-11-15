"""P2P 송금 — 원자적 이체·잔액부족 롤백·자기송금/미존재 방지·멱등."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import transfer, wallet

client = TestClient(app)


def _reg(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})


def _auth(u):
    _reg(u)
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_transfer_moves_money():
    sender, receiver = "snd@oos.io", "rcv@oos.io"
    h = _auth(sender)
    _reg(receiver)
    wallet.balance(sender); wallet.balance(receiver)         # 지갑 생성
    r = client.post("/transfer", headers=h,
                    json={"to": receiver, "amount": 300_000, "memo": "밥값"}).json()
    assert r["ok"] and r["balance"] == 1_000_000 - 300_000
    assert wallet.balance(receiver) == 1_000_000 + 300_000


def test_insufficient_rolls_back_both_sides():
    sender, receiver = "snd2@oos.io", "rcv2@oos.io"
    h = _auth(sender)
    _reg(receiver)
    r0 = wallet.balance(receiver)
    assert client.post("/auth/step-up", headers=h, json={"password": "pass1234"}).status_code == 200   # 100만↑ 은 재확인 먼저
    resp = client.post("/transfer", headers=h,
                       json={"to": receiver, "amount": 5_000_000})    # 잔액(100만) 초과
    assert resp.status_code == 400
    assert wallet.balance(sender) == 1_000_000                        # 출금 롤백
    assert wallet.balance(receiver) == r0                             # 입금도 롤백(원자성)


def test_self_and_unknown_rejected():
    h = _auth("snd3@oos.io")
    assert client.post("/transfer", headers=h,
                       json={"to": "snd3@oos.io", "amount": 1000}).status_code == 400
    assert client.post("/transfer", headers=h,
                       json={"to": "nobody@oos.io", "amount": 1000}).status_code == 400


def test_transfer_idempotent():
    sender, receiver = "snd4@oos.io", "rcv4@oos.io"
    _auth(sender); _reg(receiver)
    wallet.balance(sender); wallet.balance(receiver)
    transfer.send(sender, receiver, 200_000, idem_key="tx-1")
    transfer.send(sender, receiver, 200_000, idem_key="tx-1")         # 같은 키 → 1회만
    assert wallet.balance(sender) == 1_000_000 - 200_000
    assert wallet.balance(receiver) == 1_000_000 + 200_000


def test_transfer_idempotency_key_header_and_double_click():
    # 예전: /transfer 는 헤더 키를 받지 않아(요청마다 새 키) 더블클릭·재시도 = 두 번 송금
    sender, receiver = "snd5@oos.io", "rcv5@oos.io"
    h = _auth(sender)
    _reg(receiver)
    wallet.balance(sender); wallet.balance(receiver)
    body = {"to": receiver, "amount": 100_000}
    r1 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json=body).json()
    r2 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json=body).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert wallet.balance(sender) == 1_000_000 - 100_000 and wallet.balance(receiver) == 1_000_000 + 100_000
    r3 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json={"to": receiver, "amount": 5})
    assert r3.status_code == 409                                     # 같은 키 · 다른 내용
    assert client.post("/transfer", headers={**h, "Idempotency-Key": "x" * 201}, json=body).status_code == 422


def test_same_client_key_from_two_senders_credits_both():
    # 받는 쪽 원장 키(ti:키)가 보낸 사람과 무관하면 두 번째 입금이 '이미 처리'로 빠져 돈이 사라진다 → 이름공간 분리
    a, b, rcv = "snd6@oos.io", "snd7@oos.io", "rcv6@oos.io"
    ha, hb = _auth(a), _auth(b)
    _reg(rcv)
    wallet.balance(rcv)
    for h in (ha, hb):
        assert client.post("/transfer", headers={**h, "Idempotency-Key": "1"},
                           json={"to": rcv, "amount": 50_000}).json()["duplicate"] is False
    assert wallet.balance(rcv) == 1_000_000 + 100_000
