"""관심종목 + 계정 관리(비밀번호 변경·회원 탈퇴) — 계정에 엮인 데이터 CRUD."""
from fastapi.testclient import TestClient
from app.api import app
from app.collectors import marketgen

client = TestClient(app)


def _auth(u="wl@oos.io", p="pass1234"):
    """회원가입+로그인 → Authorization 헤더."""
    client.post("/auth/register", json={"username": u, "password": p})
    tok = client.post("/auth/login", json={"username": u, "password": p}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_watchlist_add_list_remove():
    marketgen.seed_market(real=False)
    h = _auth()
    # 초기엔 비어있음
    assert client.get("/watchlist", headers=h).json()["count"] == 0
    # 등록
    r = client.post("/watchlist/005930", headers=h).json()
    assert r["ok"] and r["watched"]
    # 중복 등록해도 1개(유니크)
    client.post("/watchlist/005930", headers=h)
    d = client.get("/watchlist", headers=h).json()
    assert d["count"] == 1
    it = d["items"][0]
    assert it["ticker"] == "005930" and it["name"] and it["price"] is not None
    # /me 에도 반영
    assert client.get("/me", headers=h).json()["watchlist"]["count"] == 1
    # 해제
    assert client.delete("/watchlist/005930", headers=h).json()["watched"] is False
    assert client.get("/watchlist", headers=h).json()["count"] == 0


def test_watchlist_requires_login():
    assert client.get("/watchlist").status_code == 401
    assert client.post("/watchlist/005930").status_code == 401


def test_watchlist_unknown_ticker_rejected():
    marketgen.seed_market(real=False)
    h = _auth("wl2@oos.io")
    r = client.post("/watchlist/999999", headers=h)
    assert r.status_code == 404 and "종목 없음" in r.json()["detail"]


def test_change_password_flow():
    marketgen.seed_market(real=False)
    u = "pw@oos.io"
    h = _auth(u)
    # 현재 비번 틀리면 400
    assert client.post("/auth/change-password", headers=h,
                       json={"old_password": "wrong123", "new_password": "newpass9"}).status_code == 400
    other = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]   # 다른 기기
    h2 = {"Authorization": f"Bearer {other}"}
    # 정상 변경 → 이 세션만 남고 다른 기기의 세션은 전부 종료
    assert client.post("/auth/change-password", headers=h,
                       json={"old_password": "pass1234", "new_password": "newpass9"}).json()["ok"]
    assert client.get("/me", headers=h).status_code == 200           # 지금 바꾼 세션은 유지
    r = client.get("/me", headers=h2)
    assert r.status_code == 401 and r.json()["code"] == "session_revoked"   # 다른 기기 세션 종료
    # 새 비번으로 로그인됨
    assert client.post("/auth/login", json={"username": u, "password": "newpass9"}).status_code == 200


def test_delete_account_wipes_data():
    marketgen.seed_market(real=False)
    u = "del@oos.io"
    h = _auth(u)
    client.post("/survey/submit", headers=h, json={"answers": [3, 3, 3, 3, 3]})
    client.post("/watchlist/005930", headers=h)
    client.post("/learn/log", headers=h, json={"kind": "term", "item_key": "PER", "item_name": "PER"})
    # 탈퇴
    assert client.post("/auth/step-up", headers=h, json={"password": "pass1234"}).status_code == 200   # 탈퇴는 항상 비밀번호 재확인
    assert client.delete("/me", headers=h).json()["ok"]
    # 옛 토큰 무효 + 재로그인 불가(계정 삭제)
    assert client.get("/me", headers=h).status_code == 401
    assert client.post("/auth/login", json={"username": u, "password": "pass1234"}).status_code == 401
    # 데이터 실제 삭제 확인
    from app import db
    assert db.query("SELECT 1 FROM survey_result WHERE user_id=:u", {"u": u}) == []
    assert db.query("SELECT 1 FROM watchlist WHERE user_id=:u", {"u": u}) == []
    assert db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": u}) == []
