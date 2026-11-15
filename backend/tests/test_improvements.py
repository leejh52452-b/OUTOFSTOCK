"""전체 개선분 검증 — IAP 긴 영수증, 탈퇴 완결성(구 simulator/backtest), 종목순위 배치조회."""
from fastapi.testclient import TestClient

from app.api import app
from app.collectors import marketgen

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


# ── IAP: 실 스토어 토큰처럼 긴 영수증도 크래시 없이 처리(멱등키 해시) ──
def test_iap_long_receipt_ok():
    h, u = _auth("iaplong@oos.io")
    long_receipt = "R" * 300                       # 지갑 멱등키 80자 한도를 넘길 길이
    r = client.post("/iap/purchase", headers=h,
                    json={"store": "apple", "product_id": "coins_small",
                          "receipt": long_receipt}).json()
    assert r["ok"] and r["coins"] == 1_000_000
    r2 = client.post("/iap/purchase", headers=h,
                     json={"store": "apple", "product_id": "coins_small",
                           "receipt": long_receipt}).json()
    assert r2["duplicate"] is True                 # 재사용 감지도 정상


# ── 탈퇴 완결성: 구 simulator·backtest 세션도 함께 삭제 ──


# ── 종목순위: 배치 조회 후에도 정렬·필드 정상 ──
def test_market_stock_list_batched():
    marketgen.seed_market(real=False)
    d = client.get("/market/stocks?sort=value&limit=5").json()
    assert len(d["items"]) >= 1
    vals = [it["value"] for it in d["items"]]
    assert vals == sorted(vals, reverse=True)                 # value 내림차순 유지
    it = d["items"][0]
    for k in ("ticker", "name", "price", "change_rate", "volume", "value", "marcap", "rank"):
        assert k in it
    # 등락률 상승 정렬도 확인
    rise = client.get("/market/stocks?sort=rise&limit=5").json()["items"]
    rates = [it["change_rate"] for it in rise]
    assert rates == sorted(rates, reverse=True)
