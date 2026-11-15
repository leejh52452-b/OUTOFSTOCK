"""통합 — 마이페이지(/me)에 지갑 생태계 노출 + 회원탈퇴 시 전 데이터 삭제(원자적)."""
from fastapi.testclient import TestClient

from app.api import app
from app import db
from app.collectors import marketgen

client = TestClient(app)


def _auth(u, p="pass1234"):
    client.post("/auth/register", json={"username": u, "password": p})
    tok = client.post("/auth/login", json={"username": u, "password": p}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def test_me_includes_wallet_ecosystem():
    h, u = _auth("me1@oos.io")
    client.post("/attendance/check-in", headers=h)
    d = client.get("/me", headers=h).json()
    for k in ("wallet", "attendance", "invest"):
        assert k in d
    for k in ("moneysim", "game"):                                      # 제거된 기능
        assert k not in d
    # 'avatar' 는 예전 아바타 상점(제거)과 다른 새 기능 — 양봉이 꾸미기 요약(ADR-0022)
    assert d["avatar"] == {"equipped": {}, "worth": 0, "class": {"name": "흙수저", "level": 1}, "owned": 0}
    assert d["wallet"]["balance"] > 0
    assert d["attendance"]["total_days"] == 1


def test_delete_account_wipes_wallet_ecosystem():
    marketgen.seed_market(real=False)
    h, u = _auth("me2@oos.io")
    client.post("/attendance/check-in", headers=h)
    client.post("/iap/purchase", headers=h,
                json={"store": "google", "product_id": "coins_small", "receipt": "del-receipt-1"})
    client.post("/invest/buy", headers=h, json={"ticker": "005930", "quantity": 1})
    # 탈퇴
    assert client.post("/auth/step-up", headers=h, json={"password": "pass1234"}).status_code == 200   # 탈퇴는 항상 비밀번호 재확인
    assert client.delete("/me", headers=h).json()["ok"]
    assert client.get("/me", headers=h).status_code == 401              # 토큰 무효
    for tbl in ("wallet", "wallet_ledger", "attendance", "iap_purchase", "invest_position"):
        assert db.query(f"SELECT 1 FROM {tbl} WHERE username=:u", {"u": u}) == []
