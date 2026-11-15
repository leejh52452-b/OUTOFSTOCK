"""장기 실전 모의투자 — 현재가 매수(지갑 차감)·평가손익·매도 정산·부분매도·소유권."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import wallet
from app.collectors import marketgen

client = TestClient(app)


def _auth(u, topup=50_000_000):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    if topup:
        wallet.credit_once(u, topup, "iap", memo="테스트 충전")   # 매수 여력 확보
    return {"Authorization": "Bearer " + tok}, u


def test_buy_creates_position_and_debits_wallet():
    marketgen.seed_market(real=False)
    h, u = _auth("inv1@oos.io")
    b0 = wallet.balance(u)
    d = client.post("/invest/buy", headers=h, json={"ticker": "005930", "quantity": 10}).json()
    assert d["ok"]
    assert wallet.balance(u) == b0 - d["buy_price"] * 10
    pos = client.get("/invest", headers=h).json()
    assert pos["summary"]["open_count"] == 1
    p0 = pos["open"][0]
    assert p0["ticker"] == "005930" and p0["current_price"] > 0 and "pnl" in p0


def test_sell_full_credits_and_closes():
    marketgen.seed_market(real=False)
    h, u = _auth("inv2@oos.io")
    d = client.post("/invest/buy", headers=h, json={"ticker": "005930", "quantity": 5}).json()
    pid = d["position_id"]
    b_before = wallet.balance(u)
    s = client.post(f"/invest/{pid}/sell", headers=h, json={}).json()
    assert s["ok"] and s["sold"] == 5
    assert wallet.balance(u) == b_before + s["proceeds"]
    pos = client.get("/invest", headers=h).json()
    assert pos["summary"]["open_count"] == 0 and pos["summary"]["closed_count"] == 1


def test_partial_sell_reduces_quantity():
    marketgen.seed_market(real=False)
    h, u = _auth("inv3@oos.io")
    pid = client.post("/invest/buy", headers=h,
                      json={"ticker": "000660", "quantity": 10}).json()["position_id"]
    client.post(f"/invest/{pid}/sell", headers=h, json={"quantity": 4})
    pos = client.get("/invest", headers=h).json()
    assert pos["open"][0]["quantity"] == 6


def test_buy_insufficient_and_cross_user_sell():
    marketgen.seed_market(real=False)
    h, u = _auth("inv4@oos.io", topup=0)                        # 환영금(100만)만 보유
    assert client.post("/invest/buy", headers=h,
                       json={"ticker": "005930", "quantity": 100000}).status_code == 400
    pid = client.post("/invest/buy", headers=h,
                      json={"ticker": "005930", "quantity": 1}).json()["position_id"]
    hb, _ = _auth("inv5@oos.io", topup=0)
    assert client.post(f"/invest/{pid}/sell", headers=hb, json={}).status_code == 400
