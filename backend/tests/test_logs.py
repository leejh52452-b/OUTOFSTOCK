"""마이페이지 로그 — /me 는 섹션별 최근 5건 + 전체 건수, /me/logs/{kind} 는 그 로그만 모은 전용 페이지(페이지네이션)."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import account, wallet

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def test_me_shows_five_and_counts_all():
    h, u = _auth("lg1@oos.io")
    for i in range(12):
        account.record_learning(u, "term", f"k{i}", f"용어{i}")
        wallet.credit_once(u, 1000 + i, "iap", memo=f"충전{i}")
    d = client.get("/me", headers=h).json()
    assert d["preview"] == 5
    assert len(d["learning"]["items"]) == 5 and d["learning"]["count"] == 12
    assert d["learning"]["items"][0]["item_name"] == "용어11"          # 최신순
    assert len(d["wallet"]["ledger"]) == 5
    assert d["counts"]["wallet"] == 13                                 # 환영금 1 + 충전 12
    assert d["counts"]["learning"] == 12 and d["counts"]["access"] >= 2   # 가입·로그인


def test_log_page_pagination_and_order():
    h, u = _auth("lg2@oos.io")
    for i in range(23):
        account.record_learning(u, "indicator", f"k{i}", f"지표{i}")
    p1 = client.get("/me/logs/learning?limit=10", headers=h).json()
    assert p1["title"] == "학습 로그" and p1["total"] == 23 and len(p1["items"]) == 10 and p1["has_more"]
    assert p1["items"][0]["item_name"] == "지표22"
    p3 = client.get("/me/logs/learning?limit=10&offset=20", headers=h).json()
    assert [x["item_name"] for x in p3["items"]] == ["지표2", "지표1", "지표0"] and not p3["has_more"]
    ids = [x["id"] for x in p1["items"]] + [x["id"] for x in
                                           client.get("/me/logs/learning?limit=10&offset=10", headers=h).json()["items"]]
    assert len(set(ids)) == 20                                         # 페이지 간 중복 없음


def test_wallet_log_filters():
    h, u = _auth("lg3@oos.io")
    client.post("/attendance/check-in", headers=h)
    wallet.credit_once(u, 5000, "iap", memo="충전")
    all_ = client.get("/me/logs/wallet", headers=h).json()
    assert all_["total"] == 3 and all_["items"][0]["reason_ko"]         # 환영금·출석·충전
    assert {f["key"] for f in all_["filters"]} >= {"all", "reward", "charge", "transfer"}
    reward = client.get("/me/logs/wallet?filter=reward", headers=h).json()
    assert reward["total"] == 2 and {x["reason"] for x in reward["items"]} == {"welcome", "attendance"}
    assert client.get("/me/logs/wallet?filter=charge", headers=h).json()["total"] == 1
    assert client.get("/me/logs/wallet?filter=nope", headers=h).status_code == 404


def test_other_kinds_and_isolation():
    h, u = _auth("lg4@oos.io")
    h2, u2 = _auth("lg5@oos.io")
    account.record_learning(u2, "term", "x", "남의 로그")
    for kind in ("practice", "access", "survey", "watchlist"):
        d = client.get(f"/me/logs/{kind}", headers=h).json()
        assert d["kind"] == kind and "items" in d and d["total"] >= 0
    assert client.get("/me/logs/learning", headers=h).json()["total"] == 0   # 다른 사람 로그는 안 보임
    assert client.get("/me/logs/unknown", headers=h).status_code == 404
    for kind in ("game", "moneysim"):                                  # 제거된 기능의 로그 종류는 없음
        assert client.get(f"/me/logs/{kind}", headers=h).status_code == 404
    assert client.get("/me/logs/learning").status_code == 401
    client.post("/survey/submit", headers=h, json={"answers": [2] * 10})
    s = client.get("/me/logs/survey", headers=h).json()
    assert s["total"] == 1 and s["items"][0]["type"] == "중립형"
