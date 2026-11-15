"""인앱결제 충전 — 상품 목록·영수증 검증·재사용(replay) 방지·오류 처리."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import wallet

client = TestClient(app)


def _auth(u="iap@oos.io"):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def test_products_list():
    d = client.get("/iap/products").json()
    ids = {p["product_id"] for p in d["items"]}
    assert "coins_small" in ids and all("coins" in p for p in d["items"])


def test_purchase_credits_wallet():
    h, u = _auth("iapbuy@oos.io")
    b0 = wallet.balance(u)
    r = client.post("/iap/purchase", headers=h,
                    json={"store": "google", "product_id": "coins_small",
                          "receipt": "receipt-abc-123"}).json()
    assert r["ok"] and r["coins"] == 1_000_000 and r["duplicate"] is False
    assert wallet.balance(u) == b0 + 1_000_000


def test_receipt_replay_no_double_credit():
    h, u = _auth("iaprep@oos.io")
    body = {"store": "apple", "product_id": "coins_medium", "receipt": "same-receipt-xyz"}
    r1 = client.post("/iap/purchase", headers=h, json=body).json()
    b1 = wallet.balance(u)
    r2 = client.post("/iap/purchase", headers=h, json=body).json()   # 같은 영수증 재사용
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert wallet.balance(u) == b1                                   # 재지급 없음


def test_bad_store_and_product_rejected():
    h, u = _auth("iapbad@oos.io")
    assert client.post("/iap/purchase", headers=h,
                       json={"store": "naver", "product_id": "coins_small",
                             "receipt": "r-12345678"}).status_code == 400
    assert client.post("/iap/purchase", headers=h,
                       json={"store": "google", "product_id": "coins_nope",
                             "receipt": "r-12345678"}).status_code == 400
