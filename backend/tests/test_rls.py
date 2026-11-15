"""행 수준 보안(RLS) — 인가 로직에 실수가 있어도 DB 가 남의 행을 내주지 않는다(V010 + app/security/rls.py)."""
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.security import rls
from app.services import auth, watchlist as watchlist_svc

client = TestClient(app)
PW = "Rls-pass-2026"


def _h(u):
    auth.register(u, PW)
    return {"Authorization": "Bearer " + client.post("/auth/login", json={"username": u, "password": PW}).json()["token"]}


def _raw_conn():
    url = db.get_engine().url
    return psycopg.connect(host=url.host, port=url.port, user=url.username, password=url.password, dbname=url.database)


def test_session_without_context_sees_no_personal_rows():
    db.execute("INSERT INTO watchlist(user_id, ticker, created_at) VALUES('rls-a', '005930', '2026-10-01T09:00:00')")   # 시스템 문맥(요청 밖)
    assert len(db.query("SELECT * FROM watchlist")) == 1
    with _raw_conn() as c:                                   # 앱 훅을 거치지 않은 직접 접속(사람이 앱 계정으로 붙은 경우)
        assert c.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM stock").fetchone()[0] > 0          # 공개 표는 그대로


def test_acting_principal_sees_and_writes_only_own_rows():
    for u in ("rls-a", "rls-b"):
        db.execute("INSERT INTO watchlist(user_id, ticker, created_at) VALUES(:u, '005930', '2026-10-01T09:00:00')", {"u": u})
    with rls.acting_as("rls-b"):
        assert [r["user_id"] for r in db.query("SELECT user_id FROM watchlist")] == ["rls-b"]
        assert db.execute("DELETE FROM watchlist WHERE user_id='rls-a'") == 0          # 남의 행은 지울 대상조차 아님
        with pytest.raises(Exception, match="row-level security"):
            db.execute("INSERT INTO watchlist(user_id, ticker, created_at) VALUES('rls-a', '000660', '2026-10-01T09:00:00')")
    assert len(db.query("SELECT * FROM watchlist")) == 2


def test_buggy_authorization_code_still_cannot_leak_other_users_rows(monkeypatch):
    ha, hb = _h("rls-alice"), _h("rls-bob")
    client.post("/watchlist/005930", headers=ha)
    client.post("/watchlist/000660", headers=hb)

    def buggy_items(_user):                                   # 인가 버그 흉내: 사용자 조건을 빠뜨린 조회
        return db.query("SELECT user_id, ticker FROM watchlist ORDER BY id")
    monkeypatch.setattr(watchlist_svc, "items", buggy_items)
    rows = client.get("/watchlist", headers=hb).json()["items"]
    assert rows == [{"user_id": "rls-bob", "ticker": "000660"}]     # 앨리스의 행은 DB 가 돌려주지 않는다


def test_two_party_rows_and_named_elevations():
    ha, hb, _hc = _h("rls-p1"), _h("rls-p2"), _h("rls-p3")
    client.post("/friends/rls-p2", headers=ha)
    client.post("/friends/rls-p1/accept", headers=hb)
    client.post("/chat/rls-p2/messages", headers=ha, json={"body": "안녕"})
    with rls.acting_as("rls-p2"):
        assert len(db.query("SELECT * FROM chat_message")) == 1     # 받는 사람에게 보임
    with rls.acting_as("rls-p3"):
        assert db.query("SELECT * FROM chat_message") == []          # 제3자에게는 없음
        assert db.query("SELECT * FROM friendship") == []
    before = rls.stats().get("transfer", 0)
    client.get("/wallet", headers=ha)
    assert client.post("/transfer", headers=ha, json={"to": "rls-p2", "amount": 1000}).status_code == 200
    assert rls.stats()["transfer"] == before + 1                     # 상승 구간은 이름과 횟수로 남는다
    with rls.acting_as("rls-p2"):
        assert db.query("SELECT amount FROM wallet_ledger WHERE reason='transfer_in'")[0]["amount"] == 1000


def test_two_party_writes_cannot_forge_the_sender():
    """V013 — 보는 것은 양쪽, 새로 쓰는 행은 '보내는 사람 = 나'만. 인가 버그로 남을 보낸 사람으로 적어도 DB 가 거부한다
    (예전 WITH CHECK 는 '어느 한쪽이 나'면 통과 — 2026-10-23 독립 점검)."""
    _h("rls-f1"), _h("rls-f2")
    with rls.acting_as("rls-f1"):
        with pytest.raises(Exception, match="row-level security"):
            db.execute("INSERT INTO chat_message(sender, receiver, body, created_at) "
                       "VALUES('rls-f2', 'rls-f1', 'x', '2026-10-01T09:00:00+09:00')")            # 받는 사람이 나여도 보낸 사람을 위조할 수 없다
        with pytest.raises(Exception, match="row-level security"):
            db.execute("INSERT INTO friend_request(sender, receiver, status, created_at) "
                       "VALUES('rls-f2', 'rls-f1', 'pending', '2026-10-01T09:00:00+09:00')")      # 남이 나에게 보낸 척하는 요청
        db.execute("INSERT INTO friend_request(sender, receiver, status, created_at) "
                   "VALUES('rls-f1', 'rls-f2', 'pending', '2026-10-01T09:00:00+09:00')")          # 내가 보내는 것은 된다
    with rls.acting_as("rls-f2"):                                         # 받는 사람은 보고 상태를 바꿀 수 있다
        assert db.execute("UPDATE friend_request SET status='declined' WHERE sender='rls-f1'") == 1
    pols = {r["policyname"]: r["cmd"] for r in db.query(
        "SELECT policyname, cmd FROM pg_policies WHERE tablename='chat_message'")}
    assert pols == {"rls_party_read": "SELECT", "rls_party_insert": "INSERT",
                    "rls_party_update": "UPDATE", "rls_party_delete": "DELETE"}
    if db.query("SELECT current_user AS u")[0]["u"] == "oos_app":         # 최소 권한 모드(CI least-privilege): 열 단위 권한까지
        with rls.acting_as("rls-f2"), pytest.raises(Exception, match="permission denied"):
            db.execute("UPDATE friend_request SET sender='rls-f9' WHERE sender='rls-f1'")
        with rls.acting_as("rls-f2"), pytest.raises(Exception, match="permission denied"):
            db.execute("UPDATE chat_message SET sender='rls-f9' WHERE receiver='rls-f2'")
