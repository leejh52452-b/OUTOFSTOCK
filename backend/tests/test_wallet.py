"""가상머니 지갑 코어 — 환영금·입출금·멱등·잔액부족·정합성 대사."""
import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.services import wallet

client = TestClient(app)


def _auth(u="wal@oos.io", p="pass1234"):
    client.post("/auth/register", json={"username": u, "password": p})
    tok = client.post("/auth/login", json={"username": u, "password": p}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def test_wallet_requires_login():
    assert client.get("/wallet").status_code == 401


def test_welcome_bonus_on_first_access():
    h, u = _auth("welcome@oos.io")
    d = client.get("/wallet", headers=h).json()
    assert d["balance"] == 1_000_000
    assert any(l["reason"] == "welcome" for l in d["ledger"])   # 환영금 원장 1건


def test_credit_debit_and_reconcile():
    h, u = _auth("cd@oos.io")
    client.get("/wallet", headers=h)                            # 지갑 생성(환영금)
    wallet.credit_once(u, 500_000, "iap", memo="테스트 충전")
    wallet.debit_once(u, 200_000, "shop_buy", memo="테스트 구매")
    assert wallet.balance(u) == 1_000_000 + 500_000 - 200_000
    rec = wallet.reconcile(u)
    assert rec["ok"] and rec["balance"] == rec["ledger_sum"]    # 잔액 == 원장 합


def test_debit_insufficient_raises():
    h, u = _auth("ins@oos.io")
    client.get("/wallet", headers=h)
    with pytest.raises(wallet.WalletError):
        wallet.debit_once(u, 9_999_999_999, "shop_buy")         # 잔액 초과 출금


def test_credit_idempotent():
    h, u = _auth("idem@oos.io")
    client.get("/wallet", headers=h)
    wallet.credit_once(u, 100_000, "iap", idem_key="k1")
    wallet.credit_once(u, 100_000, "iap", idem_key="k1")        # 같은 키 → 1회만
    assert wallet.balance(u) == 1_000_000 + 100_000
