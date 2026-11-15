"""친구 1:1 채팅 — 서로 친구(요청 수락)일 때만·안 읽은 수·읽음('1')·폴링/페이징·검증·탈퇴 삭제."""
from fastapi.testclient import TestClient

from app import db
from app.api import app

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def _send(h, peer, body):
    return client.post(f"/chat/{peer}/messages", headers=h, json={"body": body})


def _befriend(ha, a, hb, b):
    """a 가 요청 → b 가 수락 = 서로 친구."""
    assert client.post("/friends/" + b, headers=ha).json()["status"] == "pending"
    assert client.post(f"/friends/{a}/accept", headers=hb).json()["status"] == "friends"


def test_send_requires_accepted_friendship():
    ha, a = _auth("ca@oos.io")
    hb, b = _auth("cb@oos.io")
    assert _send(ha, b, "hi").status_code == 400                     # 친구 아님
    client.post("/friends/" + b, headers=ha)                          # 요청만 보낸 상태(대기)
    assert _send(ha, b, "hi").status_code == 400                     # 수락 전에는 불가(스팸 차단)
    assert _send(hb, a, "hi").status_code == 400
    assert client.post(f"/friends/{a}/accept", headers=hb).json()["status"] == "friends"
    m = _send(ha, b, "  안녕  ").json()
    assert m["body"] == "안녕" and m["mine"] and m["read"] is False
    assert _send(hb, a, "답장").status_code == 200                   # 수락 후엔 양쪽 모두 가능
    assert client.get(f"/chat/{a}/messages", headers=hb).json()["can_send"] is True


def test_unfriend_blocks_new_messages_but_keeps_history():
    ha, a = _auth("cu1@oos.io")
    hb, b = _auth("cu2@oos.io")
    _befriend(ha, a, hb, b)
    _send(ha, b, "안녕")
    assert client.delete("/friends/" + a, headers=hb).json()["status"] == "none"   # b 가 친구 끊기
    assert _send(ha, b, "또 안녕").status_code == 400                  # 더는 못 보냄(양쪽 모두)
    assert _send(hb, a, "?").status_code == 400
    t = client.get(f"/chat/{b}/messages", headers=ha).json()
    assert [m["body"] for m in t["items"]] == ["안녕"] and t["can_send"] is False  # 지난 대화는 보임


def test_unread_rooms_and_read_receipt():
    ha, a = _auth("cr1@oos.io")
    hb, b = _auth("cr2@oos.io")
    _befriend(ha, a, hb, b)
    _send(ha, b, "첫 번째")
    m2 = _send(ha, b, "두 번째").json()
    rooms = client.get("/chat/rooms", headers=hb).json()
    assert rooms["unread_total"] == 2
    r = rooms["items"][0]
    assert r["peer"] == a and r["unread"] == 2 and r["last_body"] == "두 번째" and r["last_mine"] is False
    assert client.get(f"/chat/{a}/messages", headers=hb).json()["items"][-1]["body"] == "두 번째"   # 열면 읽음
    assert client.get("/chat/rooms", headers=hb).json()["unread_total"] == 0
    t = client.get(f"/chat/{b}/messages", headers=ha).json()
    assert t["peer_last_read_id"] == m2["id"]                         # 보낸 쪽 '1' 사라짐


def test_polling_after_id_and_paging_before_id():
    ha, a = _auth("cp1@oos.io")
    hb, b = _auth("cp2@oos.io")
    _befriend(ha, a, hb, b)
    ids = [_send(ha, b, f"m{i}").json()["id"] for i in range(5)]
    page = client.get(f"/chat/{b}/messages?limit=3", headers=ha).json()
    assert [m["id"] for m in page["items"]] == ids[2:] and page["has_more"] is True
    older = client.get(f"/chat/{b}/messages?before_id={ids[2]}", headers=ha).json()
    assert [m["id"] for m in older["items"]] == ids[:2] and older["has_more"] is False
    assert client.get(f"/chat/{b}/messages?after_id={ids[-1]}", headers=ha).json()["items"] == []
    new = _send(hb, a, "새 글").json()
    got = client.get(f"/chat/{b}/messages?after_id={ids[-1]}", headers=ha).json()["items"]
    assert [m["id"] for m in got] == [new["id"]] and got[0]["mine"] is False


def test_validation_and_login_required():
    ha, a = _auth("cv1@oos.io")
    hb, b = _auth("cv2@oos.io")
    _befriend(ha, a, hb, b)
    assert _send(ha, b, "   ").status_code == 400                     # 빈 메시지
    assert _send(ha, b, "x" * 1001).status_code == 400                # 길이 초과
    assert _send(ha, a, "me").status_code == 400                      # 자기 자신
    assert _send(ha, "ghost@oos.io", "hi").status_code == 400         # 없는 사용자
    assert client.get("/chat/ghost@oos.io/messages", headers=ha).status_code == 404
    assert client.get("/chat/rooms").status_code == 401


def test_delete_account_wipes_chat():
    ha, a = _auth("cd1@oos.io")
    hb, b = _auth("cd2@oos.io")
    _befriend(ha, a, hb, b)
    _send(ha, b, "bye")
    _send(hb, a, "see you")
    assert client.post("/auth/step-up", headers=ha, json={"password": "pass1234"}).status_code == 200   # 탈퇴는 항상 비밀번호 재확인
    assert client.delete("/me", headers=ha).json()["ok"]
    assert db.query("SELECT 1 FROM chat_message WHERE sender=:u OR receiver=:u", {"u": a}) == []
    assert client.get("/chat/rooms", headers=hb).json()["items"] == []
