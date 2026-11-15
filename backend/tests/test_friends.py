"""친구 — 요청 → 상대 수락 → 친구(양방향) · 거절/취소/끊기 · 관계 표시 · 공개 프로필 · 선물 · 마이그레이션 · 탈퇴 삭제."""
import threading

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app import db
from app.services import friends

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def _rel(h, other):
    return next(x["relation"] for x in client.get("/users", headers=h).json()["items"] if x["username"] == other)


def _friends(h):
    return [f["username"] for f in client.get("/friends", headers=h).json()["items"]]


def test_request_then_accept_makes_mutual_friends():
    ha, ua = _auth("fa@oos.io")
    hb, ub = _auth("fb@oos.io")
    names = {x["username"] for x in client.get("/users", headers=ha).json()["items"]}
    assert ub in names and ua not in names                        # 나는 제외
    r = client.post("/friends/" + ub, headers=ha).json()
    assert r["ok"] and r["status"] == "pending"                   # 요청만 — 아직 친구 아님
    assert _friends(ha) == [] and _friends(hb) == []
    assert _rel(ha, ub) == "sent" and _rel(hb, ua) == "received"
    assert client.get("/me/badges", headers=hb).json()["friend_requests"] == 1
    reqs = client.get("/friends/requests", headers=hb).json()
    assert [x["username"] for x in reqs["received"]] == [ua] and reqs["sent"] == []
    assert client.post(f"/friends/{ua}/accept", headers=hb).json()["status"] == "friends"
    assert _friends(ha) == [ub] and _friends(hb) == [ua]          # 양쪽 모두 친구
    assert _rel(ha, ub) == "friend" and _rel(hb, ua) == "friend"
    assert client.get("/me/badges", headers=hb).json()["friend_requests"] == 0
    assert client.post("/friends/" + ub, headers=ha).status_code == 400   # 이미 친구


def test_duplicate_request_is_idempotent_and_reverse_request_auto_accepts():
    ha, ua = _auth("fr1@oos.io")
    hb, ub = _auth("fr2@oos.io")
    client.post("/friends/" + ub, headers=ha)
    client.post("/friends/" + ub, headers=ha)                     # 두 번 눌러도 요청 1건
    assert db.query("SELECT COUNT(*) AS n FROM friend_request WHERE status='pending'")[0]["n"] == 1
    r = client.post("/friends/" + ua, headers=hb).json()          # 상대도 나에게 요청 → 곧바로 친구
    assert r["status"] == "friends" and _friends(ha) == [ub]
    assert db.query("SELECT COUNT(*) AS n FROM friend_request WHERE status='pending'")[0]["n"] == 0


def test_decline_and_cancel():
    ha, ua = _auth("fd1@oos.io")
    hb, ub = _auth("fd2@oos.io")
    client.post("/friends/" + ub, headers=ha)
    assert client.post(f"/friends/{ua}/decline", headers=hb).json()["status"] == "declined"
    assert _friends(ha) == [] and _rel(ha, ub) == "none"          # 거절되면 관계 없음 → 다시 요청 가능
    assert client.post(f"/friends/{ua}/decline", headers=hb).status_code == 400   # 받은 요청 없음
    assert client.post("/friends/" + ub, headers=ha).json()["status"] == "pending"
    assert client.delete(f"/friends/{ub}/request", headers=ha).json()["status"] == "canceled"
    assert _rel(hb, ua) == "none"
    assert client.delete(f"/friends/{ub}/request", headers=ha).status_code == 400  # 보낸 요청 없음
    assert client.post(f"/friends/{ua}/accept", headers=hb).status_code == 400     # 취소된 요청은 수락 불가


def test_unfriend_removes_both_sides():
    ha, ua = _auth("fu1@oos.io")
    hb, ub = _auth("fu2@oos.io")
    client.post("/friends/" + ub, headers=ha)
    client.post(f"/friends/{ua}/accept", headers=hb)
    assert client.delete("/friends/" + ub, headers=ha).json()["status"] == "none"
    assert _friends(ha) == [] and _friends(hb) == []
    assert db.query("SELECT 1 FROM friendship") == []


def test_add_self_and_unknown_rejected():
    h, u = _auth("fc@oos.io")
    assert client.post("/friends/" + u, headers=h).status_code == 400        # 자기 자신
    assert client.post("/friends/nobody@x.io", headers=h).status_code == 400  # 미존재
    assert client.post("/friends/nobody@x.io/accept", headers=h).status_code == 400
    assert client.get("/friends/requests").status_code == 401                 # 로그인 필요


def test_concurrent_cross_requests_end_as_single_friendship():
    """A→B, B→A 요청이 동시에 와도 결과는 '친구 1쌍'(행 2개) + 대기 요청 0건."""
    _, ua = _auth("fx1@oos.io")
    _, ub = _auth("fx2@oos.io")
    errs = []

    def go(a, b):
        try:
            friends.request(a, b)
        except Exception as e:                                    # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go, args=(ua, ub)), threading.Thread(target=go, args=(ub, ua))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert friends.are_friends(ua, ub)
    assert db.query("SELECT COUNT(*) AS n FROM friendship")[0]["n"] == 2
    assert db.query("SELECT COUNT(*) AS n FROM friend_request WHERE status='pending'")[0]["n"] == 0


def test_legacy_one_way_rows_migrate_to_pending_requests():
    """구버전(단방향 친구추가) 데이터 → 서로 추가한 사이는 친구 유지, 한쪽만은 '대기 요청'으로."""
    _, ua = _auth("fm1@oos.io")
    _, ub = _auth("fm2@oos.io")
    _, uc = _auth("fm3@oos.io")
    with db.transaction() as conn:
        for o, f in ((ua, ub), (ub, ua), (ua, uc)):               # a↔b 상호, a→c 단방향
            conn.execute(text("INSERT INTO friendship(owner, friend, created_at) VALUES(:o,:f,'2026-01-01')"),
                         {"o": o, "f": f})
    applied = db.init_db()
    assert any("friend_request" in a for a in applied)
    assert friends.are_friends(ua, ub) and not friends.are_friends(ua, uc)
    assert db.query("SELECT sender, receiver FROM friend_request WHERE status='pending'") == \
        [{"sender": ua, "receiver": uc}]
    assert not any("friend_request" in a for a in db.init_db())   # 두 번째 실행은 할 일 없음(멱등)
    friends.accept(uc, ua)                                        # c 가 수락하면 친구
    assert friends.are_friends(ua, uc)


def test_public_profile_excludes_sensitive():
    ha, ua = _auth("fp1@oos.io")
    hb, ub = _auth("fp2@oos.io")
    p = client.get("/friends/" + ub + "/profile", headers=ha).json()
    assert p["username"] == ub and "invest" in p and "survey_type" in p
    assert "avatar" not in p and "game" not in p                       # 제거된 기능 필드 없음
    assert p["relation"] == "none" and p["is_friend"] is False
    for k in ("wallet", "balance", "ledger", "access", "watchlist"):   # 민감정보 비공개
        assert k not in p


def test_delete_account_wipes_friendship_and_requests():
    ha, ua = _auth("fz1@oos.io")
    hb, ub = _auth("fz2@oos.io")
    hc, uc = _auth("fz3@oos.io")
    client.post("/friends/" + ub, headers=ha)
    client.post(f"/friends/{ua}/accept", headers=hb)            # a↔b 친구
    client.post("/friends/" + ua, headers=hc)                   # c → a 대기 요청
    assert client.post("/auth/step-up", headers=ha, json={"password": "pass1234"}).status_code == 200   # 탈퇴는 항상 비밀번호 재확인
    assert client.delete("/me", headers=ha).json()["ok"]
    assert db.query("SELECT 1 FROM friendship WHERE owner=:u OR friend=:u", {"u": ua}) == []
    assert db.query("SELECT 1 FROM friend_request WHERE sender=:u OR receiver=:u", {"u": ua}) == []
    assert client.get("/friends/requests", headers=hc).json()["sent"] == []
