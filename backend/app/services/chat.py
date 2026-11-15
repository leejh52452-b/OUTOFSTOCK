"""친구 1:1 채팅(메신저) — 카카오톡처럼 대화방 목록·말풍선 대화·읽음 표시('1')·안 읽은 수 배지.

  · 전송 조건: 서로 친구일 때만(친구 요청을 상대가 수락한 사이). 모르는 사람의 메시지·스팸 차단.
    친구를 끊으면 지난 대화는 볼 수 있지만 새 메시지는 보낼 수 없다.
  · 읽음: 상대가 대화방을 열면(스레드 조회) 내게 온 메시지 read_at 이 채워지고, 보낸 사람 화면의 '1'이 사라진다.
  · 실시간: 프론트는 열린 대화방만 2초마다 after_id 이후 '새 메시지만' 가져온다(인덱스 조회라 가볍다).
  · 탈퇴 시 그 사용자가 주고받은 메시지는 모두 삭제(개인정보).
"""
from sqlalchemy import text

from app import crypto, db
from app.services import quotes

MAX_LEN = 1000
PAGE = 50


class ChatError(Exception):
    """채팅 규칙 위반(빈 메시지·길이 초과·친구 아님·자기 자신 등)."""


def _exists(u):
    return bool(db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": u}))


def can_chat(a, b):
    """서로 친구(요청 수락 완료)일 때만 대화 가능."""
    from app.services.friends import are_friends
    return are_friends(a, b)


def _aad(sender, receiver):
    """메시지 암호문의 위치 결속 — 보낸 사람·받는 사람(다른 대화로 옮겨 붙이면 복호화 실패)."""
    return f"chat:{sender}:{receiver}"


def _body(r):
    return crypto.decrypt(r["body"], aad=_aad(r["sender"], r["receiver"]))


def _row(r, me):
    return {"id": r["id"], "sender": r["sender"], "receiver": r["receiver"], "body": _body(r),
            "created_at": r["created_at"], "read": r["read_at"] is not None, "mine": r["sender"] == me}


def send(me, peer, body):
    body = (body or "").strip()
    if not body:
        raise ChatError("메시지를 입력하세요")
    if len(body) > MAX_LEN:
        raise ChatError(f"메시지는 {MAX_LEN}자까지 보낼 수 있어요")
    if me == peer:
        raise ChatError("자기 자신에게는 보낼 수 없어요")
    if not _exists(peer):
        raise ChatError("존재하지 않는 사용자입니다")
    if not can_chat(me, peer):
        raise ChatError("서로 친구인 사람에게만 메시지를 보낼 수 있어요 — 친구 요청을 보내고 상대가 수락하면 대화할 수 있어요")
    from app.security import dlp                          # 주민등록번호·카드번호는 저장(암호화) 전에 가린다 — 되돌릴 수 없게
    body, masked = dlp.guard(body, "chat.body", me)
    with db.transaction() as conn:
        # 상대 계정을 잠그고(FOR SHARE) 친구 관계를 트랜잭션 안에서 다시 확인 — 확인 직후 상대가 탈퇴하면 없는 계정 앞으로
        # 메시지가 남아, 같은 아이디로 새로 가입한 사람이 남의 대화를 받아 보던 틈을 막는다
        if not conn.execute(text("SELECT 1 FROM app_user WHERE username=:u FOR SHARE"), {"u": peer}).first():
            raise ChatError("존재하지 않는 사용자입니다")
        if conn.execute(text("SELECT COUNT(*) FROM friendship WHERE (owner=:a AND friend=:b) OR (owner=:b AND friend=:a)"),
                        {"a": me, "b": peer}).scalar() != 2:
            raise ChatError("서로 친구인 사람에게만 메시지를 보낼 수 있어요")
        r = conn.execute(text(
            "INSERT INTO chat_message(sender, receiver, body, created_at) VALUES(:s, :r, :b, :t) "
            "RETURNING id, sender, receiver, body, created_at, read_at"),
            {"s": me, "r": peer, "b": crypto.encrypt(body, aad=_aad(me, peer)), "t": quotes.iso_now()}).mappings().first()
        from app.services import stream
        stream.notify(conn, peer, "chat", {"from": me, "id": r["id"]})      # 받는 사람 화면에 바로(본문은 싣지 않는다)
    out = _row(r, me)
    if masked:
        out["masked"] = masked                            # 화면이 '가려서 보냈어요'를 알릴 수 있게
    return out


def mark_read(me, peer):
    """peer → me 로 온 안 읽은 메시지를 읽음 처리. 처리 건수 반환."""
    with db.transaction() as conn:
        r = conn.execute(text("UPDATE chat_message SET read_at=:t WHERE sender=:p AND receiver=:me "
                              "AND read_at IS NULL"), {"t": quotes.iso_now(), "p": peer, "me": me})
    return r.rowcount or 0


def thread(me, peer, after_id=0, before_id=None, limit=PAGE):
    """me↔peer 대화. after_id>0 → 그 이후 새 메시지(폴링), before_id → 그 이전 과거 메시지(위로 스크롤).
    조회하는 순간 상대가 보낸 메시지는 읽음 처리된다."""
    if not _exists(peer):
        raise ChatError("존재하지 않는 사용자입니다")
    limit = max(1, min(int(limit or PAGE), 200))
    mark_read(me, peer)
    pair = "((sender=:me AND receiver=:p) OR (sender=:p AND receiver=:me))"
    params = {"me": me, "p": peer, "n": limit}
    if after_id:
        params["a"] = int(after_id)
        rows = db.query(f"SELECT * FROM chat_message WHERE {pair} AND id > :a ORDER BY id ASC LIMIT :n", params)
    else:
        extra = ""
        if before_id:
            params["b"] = int(before_id)
            extra = " AND id < :b"
        rows = db.query(f"SELECT * FROM chat_message WHERE {pair}{extra} ORDER BY id DESC LIMIT :n",
                        params)[::-1]
    last_read = db.query("SELECT MAX(id) AS m FROM chat_message WHERE sender=:me AND receiver=:p "
                         "AND read_at IS NOT NULL", {"me": me, "p": peer})
    has_more = False
    if rows and not after_id:
        has_more = bool(db.query(f"SELECT 1 FROM chat_message WHERE {pair} AND id < :f LIMIT 1",
                                 {"me": me, "p": peer, "f": rows[0]["id"]}))
    return {"peer": peer, "items": [_row(r, me) for r in rows],
            "peer_last_read_id": (last_read[0]["m"] if last_read else None) or 0,
            "can_send": can_chat(me, peer), "has_more": has_more}


def rooms(me):
    """대화방 목록 — 상대별 마지막 메시지·시각·안 읽은 수(최신 대화순)."""
    from app.services.friends import _friend_set
    last = db.query(
        "SELECT DISTINCT ON (peer) peer, id, body, created_at, sender, receiver FROM ("
        "  SELECT id, body, created_at, sender, receiver,"
        "         CASE WHEN sender=:me THEN receiver ELSE sender END AS peer"
        "  FROM chat_message WHERE sender=:me OR receiver=:me) m "
        "ORDER BY peer, id DESC", {"me": me})
    unread = {r["sender"]: r["n"] for r in db.query(
        "SELECT sender, COUNT(*) AS n FROM chat_message WHERE receiver=:me AND read_at IS NULL "
        "GROUP BY sender", {"me": me})}
    fset = _friend_set(me)
    items = [{"peer": r["peer"], "last_id": r["id"], "last_body": _body(r), "last_at": r["created_at"],
              "last_mine": r["sender"] == me, "unread": int(unread.get(r["peer"], 0)),
              "is_friend": r["peer"] in fset} for r in last]
    items.sort(key=lambda x: x["last_id"], reverse=True)
    return {"items": items, "unread_total": sum(unread.values())}


def unread_total(me):
    r = db.query("SELECT COUNT(*) AS n FROM chat_message WHERE receiver=:me AND read_at IS NULL", {"me": me})
    return int(r[0]["n"]) if r else 0


def delete_user(conn, username):
    """회원 탈퇴 시 — 주고받은 메시지 전부 삭제(트랜잭션 안에서 호출)."""
    conn.execute(text("DELETE FROM chat_message WHERE sender=:u OR receiver=:u"), {"u": username})
