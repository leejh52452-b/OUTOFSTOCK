"""친구 — 요청을 보내고 '상대가 수락해야' 친구가 된다(양방향). 다른 계정 탐색·요청/수락/거절/취소·삭제·공개 프로필.

  · 친구      = friendship 에 (a→b),(b→a) 두 행이 모두 있을 때. 수락 시 한 트랜잭션으로 두 행을 함께 만들고,
                삭제(친구 끊기)도 두 행을 함께 지운다 → '한쪽만 친구'인 어정쩡한 상태가 생기지 않는다.
  · 요청      = friend_request(status=pending). 같은 방향 대기 요청은 1건(부분 유니크 인덱스).
                상대가 이미 나에게 요청해 둔 상태에서 내가 요청하면 → 서로 원하는 것이므로 곧바로 수락 처리.
  · 동시성    = 같은 두 사람에 대한 동작은 app_user 두 행을 FOR UPDATE(이름순)로 잠가 직렬화
                (A→B 요청과 B→A 요청/수락이 동시에 와도 결과가 하나로 정해진다, 교착 없음).
  · 관계 표시 = me | friend | sent(내가 보낸 요청 대기) | received(받은 요청 대기) | none
  · 공개 프로필은 '안전 부분집합'만 노출 — 성향·실습 성과·투자 수익률.
    잔액·거래원장·접속로그·관심종목 등 민감정보는 제외(프라이버시).
"""

from sqlalchemy import text

from app import db
from app.services import quotes


class FriendError(Exception):
    """친구 규칙 위반(자기 자신·미존재 사용자·대기 요청 없음 등)."""


def _now():
    return quotes.iso_now()


# ── 조회 ──────────────────────────────────────────────────
def _friend_set(owner):
    """서로 친구인 사용자 집합(두 방향 행이 모두 있는 경우만)."""
    return {r["friend"] for r in db.query(
        "SELECT f.friend FROM friendship f JOIN friendship r "
        "ON r.owner = f.friend AND r.friend = f.owner WHERE f.owner = :u", {"u": owner})}


def are_friends(a, b):
    if not a or not b or a == b:
        return False
    return bool(db.query(
        "SELECT 1 FROM friendship f JOIN friendship r ON r.owner = f.friend AND r.friend = f.owner "
        "WHERE f.owner = :a AND f.friend = :b", {"a": a, "b": b}))


def _pending_maps(me):
    sent = {r["receiver"] for r in db.query(
        "SELECT receiver FROM friend_request WHERE sender=:u AND status='pending'", {"u": me})}
    received = {r["sender"] for r in db.query(
        "SELECT sender FROM friend_request WHERE receiver=:u AND status='pending'", {"u": me})}
    return sent, received


def _relation(me, other, fset, sent, received):
    if other == me:
        return "me"
    if other in fset:
        return "friend"
    if other in sent:
        return "sent"
    if other in received:
        return "received"
    return "none"


def relation(me, other):
    fset = _friend_set(me)
    sent, received = _pending_maps(me)
    return _relation(me, other, fset, sent, received)



def directory(me, q="", limit=50):
    """다른 계정 사용자 목록(나 제외) + 관계(friend/sent/received/none). q 로 아이디 부분검색."""
    fset = _friend_set(me)
    sent, received = _pending_maps(me)
    like = "%" + (q or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.query("SELECT username, created_at FROM app_user WHERE username<>:me "
                    "AND username LIKE :q ESCAPE '\\' ORDER BY username LIMIT :n",
                    {"me": me, "q": like, "n": max(1, min(int(limit or 50), 100))})
    out = []
    for r in rows:
        rel = _relation(me, r["username"], fset, sent, received)
        out.append({"username": r["username"], "created_at": r["created_at"], "relation": rel,
                    "is_friend": rel == "friend"})
    return out


def my_friends(me):
    """내 친구 목록(서로 수락한 사이만)."""
    rows = db.query(
        "SELECT f.friend, GREATEST(f.created_at, r.created_at) AS since FROM friendship f "
        "JOIN friendship r ON r.owner = f.friend AND r.friend = f.owner "
        "WHERE f.owner = :u ORDER BY f.friend", {"u": me})
    return [{"username": r["friend"], "since": r["since"]} for r in rows]


def requests(me):
    """대기 중인 친구 요청 — 받은 요청(수락/거절 대상) · 보낸 요청(취소 가능)."""
    rec = db.query("SELECT id, sender, created_at FROM friend_request WHERE receiver=:u AND status='pending' "
                   "ORDER BY id DESC", {"u": me})
    snt = db.query("SELECT id, receiver, created_at FROM friend_request WHERE sender=:u AND status='pending' "
                   "ORDER BY id DESC", {"u": me})
    return {"received": [{"id": r["id"], "username": r["sender"], "created_at": r["created_at"]} for r in rec],
            "sent": [{"id": r["id"], "username": r["receiver"], "created_at": r["created_at"]} for r in snt]}


def pending_count(me):
    r = db.query("SELECT COUNT(*) AS n FROM friend_request WHERE receiver=:u AND status='pending'", {"u": me})
    return int(r[0]["n"]) if r else 0


# ── 변경(모두 한 트랜잭션 + 두 사용자 행 잠금) ──────────────
def _lock_pair(conn, a, b):
    """두 사용자 행을 이름순으로 FOR UPDATE — 같은 쌍의 친구 동작을 직렬화. 존재하는 사용자 집합 반환."""
    return {r[0] for r in conn.execute(text(
        "SELECT username FROM app_user WHERE username IN (:a, :b) ORDER BY username FOR UPDATE"),
        {"a": a, "b": b})}


def _check(conn, me, other):
    if not other or me == other:
        raise FriendError("자기 자신에게는 친구 요청을 보낼 수 없습니다")
    if other not in _lock_pair(conn, me, other):
        raise FriendError("존재하지 않는 사용자입니다")


def _is_friend_tx(conn, a, b):
    return conn.execute(text(
        "SELECT COUNT(*) FROM friendship WHERE (owner=:a AND friend=:b) OR (owner=:b AND friend=:a)"),
        {"a": a, "b": b}).scalar() == 2


def _make_friends(conn, a, b):
    t = _now()
    for o, f in ((a, b), (b, a)):
        conn.execute(text("INSERT INTO friendship(owner, friend, created_at) VALUES(:o, :f, :t) "
                          "ON CONFLICT(owner, friend) DO NOTHING"), {"o": o, "f": f, "t": t})
    # 두 사람 사이의 남은 대기 요청(양방향)은 모두 수락으로 닫는다
    conn.execute(text("UPDATE friend_request SET status='accepted', responded_at=:t WHERE status='pending' "
                      "AND ((sender=:a AND receiver=:b) OR (sender=:b AND receiver=:a))"),
                 {"a": a, "b": b, "t": t})


def request(me, other):
    """친구 요청 보내기. 이미 친구면 오류, 이미 보낸 요청이 있으면 그대로(멱등),
    상대가 먼저 보낸 요청이 있으면 곧바로 수락 → 친구."""
    with db.transaction() as conn:
        _check(conn, me, other)
        if _is_friend_tx(conn, me, other):
            raise FriendError("이미 친구예요")
        if conn.execute(text("SELECT 1 FROM friend_request WHERE sender=:o AND receiver=:m AND status='pending'"),
                        {"o": other, "m": me}).first():
            _make_friends(conn, me, other)
            return {"ok": True, "friend": other, "status": "friends"}
        conn.execute(text("INSERT INTO friend_request(sender, receiver, status, created_at) "
                          "VALUES(:m, :o, 'pending', :t) ON CONFLICT DO NOTHING"),
                     {"m": me, "o": other, "t": _now()})
        from app.services import stream
        stream.notify(conn, other, "friends", {"type": "request", "from": me})
    return {"ok": True, "friend": other, "status": "pending"}


def add(me, other):
    """(하위호환) 예전 '친구추가' = 이제는 친구 요청."""
    return request(me, other)


def accept(me, other):
    """other 가 보낸 요청을 수락 → 서로 친구."""
    with db.transaction() as conn:
        _check(conn, me, other)
        if not conn.execute(text("SELECT 1 FROM friend_request WHERE sender=:o AND receiver=:m "
                                 "AND status='pending'"), {"o": other, "m": me}).first():
            if _is_friend_tx(conn, me, other):
                return {"ok": True, "friend": other, "status": "friends"}
            raise FriendError("받은 친구 요청이 없어요")
        _make_friends(conn, me, other)
        from app.services import stream
        stream.notify(conn, other, "friends", {"type": "accepted", "by": me})
    return {"ok": True, "friend": other, "status": "friends"}


def _close(me, other, sender, receiver, status, missing):
    with db.transaction() as conn:
        _check(conn, me, other)
        r = conn.execute(text("UPDATE friend_request SET status=:st, responded_at=:t "
                              "WHERE sender=:s AND receiver=:r AND status='pending'"),
                         {"st": status, "t": _now(), "s": sender, "r": receiver})
        if not r.rowcount:
            raise FriendError(missing)
    return {"ok": True, "friend": other, "status": status}


def decline(me, other):
    """받은 요청 거절(상대는 나중에 다시 요청할 수 있다)."""
    return _close(me, other, other, me, "declined", "받은 친구 요청이 없어요")


def cancel(me, other):
    """내가 보낸 요청 취소."""
    return _close(me, other, me, other, "canceled", "보낸 친구 요청이 없어요")


def remove(me, other):
    """친구 끊기 — 두 방향 행을 함께 삭제(대화 기록은 남지만 더 보낼 수는 없다)."""
    with db.transaction() as conn:
        conn.execute(text("DELETE FROM friendship WHERE (owner=:a AND friend=:b) OR (owner=:b AND friend=:a)"),
                     {"a": me, "b": other})
    return {"ok": True, "friend": other, "status": "none"}


def delete_user(conn, username):
    """회원 탈퇴 — 친구 관계·요청을 모두 삭제(트랜잭션 안에서 호출)."""
    conn.execute(text("DELETE FROM friendship WHERE owner=:u OR friend=:u"), {"u": username})
    conn.execute(text("DELETE FROM friend_request WHERE sender=:u OR receiver=:u"), {"u": username})


def profile(me, other):
    """친구/사용자 공개 프로필 — 마이페이지 수준(단, 민감정보 제외) + 나와의 관계."""
    if not db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": other}):
        raise FriendError("존재하지 않는 사용자입니다")
    from app.services import account as account_svc
    from app.security import rls
    with rls.elevated("friend_profile"):                    # 남의 행에서 공개 부분집합(성향·실습 완료 수·수익률)만
        p = account_svc.public_profile(other)
    rel = relation(me, other)
    p["relation"] = rel
    p["is_friend"] = rel == "friend"
    p["is_me"] = (other == me)
    return p
