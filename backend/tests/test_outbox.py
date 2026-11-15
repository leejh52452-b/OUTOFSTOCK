"""트랜잭셔널 아웃박스 — 커밋된 사건만, 최소 1회, 실패는 백오프·dead letter(app/services/outbox.py)."""
import json
import threading

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import auth, outbox

client = TestClient(app)


def _h(u):
    auth.register(u, "Ob-pass-2026")
    tok = client.post("/auth/login", json={"username": u, "password": "Ob-pass-2026"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_event_is_written_only_when_the_transfer_commits():
    h = _h("ob-a")
    _h("ob-b")
    client.get("/wallet", headers=h)
    assert client.post("/transfer", headers=h, json={"to": "ob-b", "amount": 5000}).status_code == 200
    assert client.post("/transfer", headers=h, json={"to": "ob-b", "amount": 99_999_999}).status_code in (400, 401)
    rows = db.query("SELECT topic, payload FROM outbox")
    assert len(rows) == 1 and rows[0]["topic"] == "transfer.completed"      # 실패한 송금은 사건도 없다
    p = json.loads(rows[0]["payload"])
    assert p["amount"] == 5000 and p["from_acct"].startswith("W") and "ob-a" not in rows[0]["payload"]


def test_dispatch_delivers_once_and_retries_failures(monkeypatch):
    with db.transaction() as conn:
        outbox.publish(conn, "transfer.completed", {"ref": "r1", "amount": 20_000_000, "from_acct": "W1", "to_acct": "W2"})
        outbox.publish(conn, "unknown.topic", {"x": 1})
    r = outbox.dispatch()
    assert r == {"done": 1, "retried": 1, "dead": 0}
    assert db.query("SELECT COUNT(*) AS n FROM alert_log WHERE title LIKE '%고액 송금%'")[0]["n"] == 1
    assert outbox.dispatch() == {"done": 0, "retried": 0, "dead": 0}          # 백오프 중 — 아직 다시 안 함
    for _ in range(outbox.MAX_ATTEMPTS):
        db.execute("UPDATE outbox SET available_at = now() WHERE status='pending'")
        outbox.dispatch()
    assert db.query("SELECT status FROM outbox WHERE topic='unknown.topic'")[0]["status"] == "dead"


def test_concurrent_relays_do_not_double_deliver(monkeypatch):
    seen = []
    monkeypatch.setitem(outbox.HANDLERS, "t.count", lambda p: seen.append(p["i"]))
    with db.transaction() as conn:
        for i in range(50):
            outbox.publish(conn, "t.count", {"i": i})
    ts = [threading.Thread(target=outbox.dispatch, kwargs={"limit": 10}) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    outbox.dispatch(limit=100)
    assert sorted(seen) == list(range(50))                                    # 50건, 중복 없음


# ── 처리기 실패·트랜잭션 밖 실행·리스(결함 26) ─────────────
def test_channel_failure_is_retried_not_marked_done(monkeypatch):
    """알림 채널이 전부 실패하면 사건은 '완료'가 아니라 재시도 — 예전엔 notify 가 실패를 삼켜 그대로 done(유실)."""
    from app.config import settings
    from app.services import alerts

    def down(*_a, **_k):
        raise ConnectionError("webhook down")
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.invalid/x")
    monkeypatch.setitem(alerts._SENDERS, "webhook", down)                                  # 채널 장애(네트워크 없이)
    with db.transaction() as conn:
        outbox.publish(conn, "transfer.completed", {"ref": "r-fail", "amount": 20_000_000, "from_acct": "W1", "to_acct": "W2"})
    assert outbox.dispatch() == {"done": 0, "retried": 1, "dead": 0}
    row = db.query("SELECT status, attempts, last_error FROM outbox")[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "DeliveryFailed" in row["last_error"]
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "")                                   # 채널 복구(로그로 처리)
    db.execute("UPDATE outbox SET available_at = now()")
    assert outbox.dispatch() == {"done": 1, "retried": 0, "dead": 0}


def test_handler_runs_outside_the_claiming_transaction(monkeypatch):
    """처리기(외부 전송)가 도는 동안 사건 행을 잠그고 있지 않다 — 예전엔 꺼낸 트랜잭션 안에서 돌아, 전송이 길어지면
    유휴 트랜잭션 제한에 끊겨 묶음 전체가 롤백되고 이미 보낸 알림을 다시 보냈다."""
    from sqlalchemy import text
    seen = []

    def probe(p):
        with db.get_engine().connect() as c:           # 다른 연결에서 같은 행을 기다리지 않고 잠가 본다
            c.execute(text("SET lock_timeout = '200ms'"))
            seen.append(c.execute(text("SELECT id FROM outbox WHERE id=:i FOR UPDATE NOWAIT"), {"i": p["id"]}).scalar())
            c.rollback()
    monkeypatch.setitem(outbox.HANDLERS, "t.probe", probe)
    with db.transaction() as conn:
        outbox.publish(conn, "t.probe", {"id": 1})
    assert outbox.dispatch() == {"done": 1, "retried": 0, "dead": 0} and seen == [1]


def test_lease_expiry_lets_another_relay_finish_a_crashed_claim():
    """중계기가 빌린 뒤 죽으면(처리 전) 리스가 끝난 뒤 다시 잡힌다 — 시도 횟수는 이미 올라가 있다."""
    with db.transaction() as conn:
        outbox.publish(conn, "transfer.completed", {"ref": "r-crash", "amount": 1, "from_acct": "W1", "to_acct": "W2"})
    assert len(outbox._claim(10)) == 1                                      # 빌리고 '죽음'
    assert outbox.dispatch() == {"done": 0, "retried": 0, "dead": 0}       # 리스 중 — 아무도 못 잡는다
    db.execute("UPDATE outbox SET available_at = now() - interval '1 second'")   # 리스 만료
    assert outbox.dispatch() == {"done": 1, "retried": 0, "dead": 0}
    assert db.query("SELECT attempts FROM outbox")[0]["attempts"] == 2
