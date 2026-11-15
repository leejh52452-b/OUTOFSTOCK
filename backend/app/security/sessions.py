"""세션 — 로그인 1회 = 세션 1개. 매 요청마다 세션을 다시 확인한다(한 번 통과했다고 믿지 않는다).

  접근 토큰  'oosa_…' 10분. DB 에는 SHA-256 해시만. 세션에 묶여 세션을 폐기하면 즉시 무효.
  갱신 토큰  'oosr_…' 쓸 때마다 새 토큰으로 바뀐다(회전). 이미 쓴 토큰이 다시 오면 → 누군가 훔쳐 쓴 것으로 보고
             세션 전체 폐기 + 보안 이벤트(OAuth 2.0 Security BCP 의 회전·재사용 탐지). 탭 두 개가 거의 동시에
             갱신하는 정상 상황은 REFRESH_GRACE_SECONDS(30초) 동안 직전 토큰을 허용해 구분한다.
  유휴 만료  SESSION_IDLE_SECONDS(30분) 동안 요청이 없으면 만료. 절대 만료 SESSION_ABSOLUTE_SECONDS(12시간).
  기기 결속  로그인한 기기 지문(브라우저·OS 계열)과 다른 곳에서 같은 토큰이 오면 세션 폐기(토큰 탈취 대응).
             IP 대역이 바뀌면 폐기 대신 '위험 신호'(ip_change) — 민감 거래 때 추가 인증을 요구한다.
  CSRF 토큰  세션마다 하나, 해시만 저장(동기화 토큰 패턴). 쿠키로 인증된 변경 요청은 헤더로 같은 값을 보내야 한다.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings
from app.security.context import Principal, RequestContext

log = logging.getLogger(__name__)

ACCESS_PREFIX, REFRESH_PREFIX = "oosa_", "oosr_"
TOUCH_EVERY = 60                       # 초 — last_seen_at 갱신 간격(요청마다 쓰지 않게)

MESSAGES = {
    "token_invalid": "로그인이 필요합니다",
    "token_expired": "로그인 확인 시간이 지났어요",          # 클라이언트는 /auth/refresh 후 다시 시도
    "session_expired": "로그인 유지 시간이 지나 다시 로그인해 주세요",
    "session_idle": "오랫동안 사용하지 않아 로그아웃됐어요. 다시 로그인해 주세요",
    "session_revoked": "로그아웃되었거나 보안상 세션이 종료됐어요. 다시 로그인해 주세요",
    "device_mismatch": "로그인한 기기와 다른 환경에서 사용돼 보안상 세션을 종료했어요",
    "account_locked": "계정이 잠겨 있어요. 잠시 후 다시 시도하거나 관리자에게 문의해 주세요",
    "refresh_reuse": "보안 위험이 감지돼 세션을 종료했어요. 다시 로그인해 주세요",
    "dpop_mismatch": "로그인한 기기의 확인 서명이 없어 보안상 세션을 종료했어요. 다시 로그인해 주세요",
}


class SessionError(Exception):
    """세션 검증 실패. revoke_reason 이 있으면 호출한 쪽이 그 세션을 폐기해야 한다(트랜잭션 경계 때문에 여기서 안 함)."""
    def __init__(self, code: str, status: int = 401, revoke_reason: str | None = None, event: tuple | None = None):
        super().__init__(MESSAGES.get(code, code))
        self.code, self.status, self.revoke_reason, self.event = code, status, revoke_reason, event


@dataclass
class Issued:
    session_id: str
    access: str
    refresh: str | None
    csrf: str | None
    expires_in: int
    session_expires_at: object = None
    persistent: bool = True                               # 웹 쿠키에 수명을 적을지('로그인 유지') — 갱신 때도 그대로


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8", "surrogatepass")).hexdigest()


def _new(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _epoch(dt):
    return dt.timestamp() if dt is not None else None


def _event(kind, severity, username=None, session_id=None, **detail):
    from app.security import events                       # 순환 import 회피(지연)
    events.emit(kind, severity, username=username, session_id=session_id, **detail)


def _issue_access(conn, session_id: str, username: str, now) -> str:
    access = _new(ACCESS_PREFIX)
    conn.execute(text("INSERT INTO auth_token(token, username, created_at, session_id, expires_at) "
                      "VALUES(:t, :u, :c, :s, :e)"),
                 {"t": token_hash(access), "u": username, "c": clock.iso_now(), "s": session_id,
                  "e": now + timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS)})
    return access


def create(conn, username: str, client: str, aal: int, ctx: RequestContext, persistent: bool = True) -> Issued:
    """로그인 성공 직후(같은 트랜잭션) — 세션·접근 토큰·갱신 토큰·CSRF 토큰 발급. 활성 세션이 많으면 오래된 것부터 종료.
    persistent=False(웹 '로그인 유지' 해제): 쿠키를 세션 쿠키로 — 브라우저를 닫으면 지워진다. 서버 만료(유휴·절대)는 같다."""
    now = clock.utc()
    active = [r[0] for r in conn.execute(text(
        "SELECT session_id FROM auth_session WHERE username=:u AND revoked_at IS NULL AND expires_at > :n "
        "ORDER BY last_seen_at DESC"), {"u": username, "n": now})]
    for old in active[max(0, settings.MAX_SESSIONS_PER_USER - 1):]:
        revoke(old, "session_limit", conn=conn)
    sid = secrets.token_hex(16)
    refresh, csrf = _new(REFRESH_PREFIX), secrets.token_urlsafe(24)
    expires = now + timedelta(seconds=settings.SESSION_ABSOLUTE_SECONDS)
    conn.execute(text(
        "INSERT INTO auth_session(session_id, username, client, created_at, auth_time, aal, last_seen_at, "
        "expires_at, device_hash, device, ip_net, last_ip_net, refresh_hash, refresh_count, csrf_hash, persistent, "
        "dpop_jkt) VALUES(:s, :u, :c, :n, :n, :aal, :n, :e, :dh, :dl, :ip, :ip, :rh, 0, :ch, :p, :jkt)"),
        {"s": sid, "u": username, "c": client, "n": now, "aal": aal, "e": expires, "dh": ctx.device_hash,
         "dl": ctx.device_label, "ip": ctx.ip_net, "rh": token_hash(refresh), "ch": token_hash(csrf),
         "p": bool(persistent), "jkt": getattr(ctx, "dpop_jkt", None)})   # 기기 결속 키(로그인 요청의 DPoP 증명)
    access = _issue_access(conn, sid, username, now)
    return Issued(sid, access, refresh, csrf, settings.ACCESS_TOKEN_TTL_SECONDS, expires, bool(persistent))


_VERIFY_SQL = (
    "SELECT s.session_id, s.username, s.client, s.aal, s.auth_time, s.step_up_at, s.last_seen_at, s.expires_at, "
    "s.device_hash, s.last_ip_net, s.risk, s.csrf_hash, s.revoked_at, s.dpop_jkt, t.expires_at AS token_expires, "
    "u.status, u.locked_until, "
    "(SELECT array_agg(a.role) FROM role_activation a WHERE a.session_id = s.session_id "
    "   AND a.revoked_at IS NULL AND a.expires_at > now()) AS roles "
    "FROM auth_token t JOIN auth_session s ON s.session_id = t.session_id "
    "JOIN app_user u ON u.username = s.username WHERE t.token = :h")


def _check_session(row, ctx: RequestContext | None, now, check_device: bool):
    """세션 행 공통 검사 — 폐기·절대 만료·유휴·기기·계정 상태. 문제가 있으면 SessionError(폐기 사유 포함)."""
    if row["revoked_at"] is not None:
        raise SessionError("session_revoked")
    if now >= row["expires_at"]:
        raise SessionError("session_expired", revoke_reason="expired")
    if now - row["last_seen_at"] > timedelta(seconds=settings.SESSION_IDLE_SECONDS):
        raise SessionError("session_idle", revoke_reason="idle")
    if check_device and ctx is not None and ctx.device_hash and row["device_hash"] \
            and row["device_hash"] != ctx.device_hash:
        raise SessionError("device_mismatch", revoke_reason="device_mismatch",
                           event=("device_mismatch", "high", {"device": ctx.device_label}))
    if row["status"] == "disabled":
        raise SessionError("account_locked", 403, revoke_reason="account_disabled")
    if row["status"] == "locked" or (row["locked_until"] is not None and row["locked_until"] > now):
        raise SessionError("account_locked", 403)


def _check_binding(row, ctx: RequestContext | None):
    """기기 결속 세션 — 갱신에는 같은 기기 키의 증명이 있어야 한다(app/security/dpop.py). 없거나 다른 키면 탈취로 보고 종료."""
    want = row.get("dpop_jkt") if hasattr(row, "get") else None
    if want and getattr(ctx, "dpop_jkt", None) != want:
        raise SessionError("dpop_mismatch", revoke_reason="dpop_mismatch",
                           event=("dpop_mismatch", "high", {"had_proof": bool(getattr(ctx, "dpop_jkt", None))}))


def _settle(err: SessionError, row, conn=None):
    """검증 실패의 뒤처리 — 폐기 사유가 있으면 세션 폐기, 이벤트가 있으면 기록."""
    if err.revoke_reason:
        revoke(row["session_id"], err.revoke_reason, conn=conn)
    if err.event:
        kind, sev, detail = err.event
        _event(kind, sev, username=row["username"], session_id=row["session_id"], **detail)


def _note_ip(row, ctx: RequestContext, now):
    """IP 대역 변경 = 위험 신호(세션은 유지). 같은 세션에서 대역이 바뀐 사실을 이벤트로 남긴다."""
    risk = set(filter(None, (row["risk"] or "").split(",")))
    if ctx.ip_net and row["last_ip_net"] and ctx.ip_net != row["last_ip_net"]:
        risk.add("ip_change")
        db.execute("UPDATE auth_session SET last_ip_net=:ip, risk=:r, last_seen_at=:n WHERE session_id=:s",
                   {"ip": ctx.ip_net, "r": ",".join(sorted(risk)), "n": now, "s": row["session_id"]})
        _event("ip_change", "low", username=row["username"], session_id=row["session_id"])
    elif (now - row["last_seen_at"]).total_seconds() >= TOUCH_EVERY:
        db.execute("UPDATE auth_session SET last_seen_at=:n WHERE session_id=:s", {"n": now, "s": row["session_id"]})
    return frozenset(risk)


def verify_access(token: str | None, ctx: RequestContext | None = None, via: str = "bearer",
                  check_context: bool = True) -> Principal:
    """접근 토큰 → 주체. check_context=False 는 요청 밖(서비스·테스트에서 직접 확인)용 — 기기·IP·활동 갱신 생략."""
    if not token or len(token) > 200:
        raise SessionError("token_invalid")
    rows = db.query(_VERIFY_SQL, {"h": token_hash(token)})
    if not rows:
        raise SessionError("token_invalid")
    row, now = rows[0], clock.utc()
    try:
        _check_session(row, ctx, now, check_device=check_context)
    except SessionError as e:
        _settle(e, row)
        raise
    if row["token_expires"] is None or now >= row["token_expires"]:
        raise SessionError("token_expired")
    if check_context and ctx is not None and ctx.device_hash and not row["device_hash"]:
        # 요청 밖(CLI·배치)에서 만든 세션 — 처음 쓰인 기기에 결속(TOFU). 이후엔 그 기기에서만.
        db.execute("UPDATE auth_session SET device_hash=:d, device=:l WHERE session_id=:s AND device_hash=''",
                   {"d": ctx.device_hash, "l": ctx.device_label, "s": row["session_id"]})
    risk = _note_ip(row, ctx, now) if (check_context and ctx is not None) else \
        frozenset(filter(None, (row["risk"] or "").split(",")))
    return Principal(kind="user", name=row["username"], session_id=row["session_id"], via=via, aal=int(row["aal"]),
                     auth_time=_epoch(row["auth_time"]), step_up_at=_epoch(row["step_up_at"]),
                     roles=frozenset(row["roles"] or ()), risk=risk, csrf_hash=row["csrf_hash"], jkt=row["dpop_jkt"])


def session_of_refresh(refresh_token: str | None) -> tuple[str, str] | None:
    """로그아웃용 — 갱신 토큰(현재·직전)이 가리키는 살아 있는 세션의 (session_id, username). 검증·회전은 하지 않는다.
    접근 토큰(10분)이 만료된 뒤 누른 로그아웃도 세션을 서버에서 끝낼 수 있게(갱신 쿠키는 /auth 경로에만 실린다)."""
    if not refresh_token or len(refresh_token) > 200 or not refresh_token.startswith(REFRESH_PREFIX):
        return None
    h = token_hash(refresh_token)
    rows = db.query("SELECT session_id, username FROM auth_session WHERE (refresh_hash=:h OR refresh_prev_hash=:h) "
                    "AND revoked_at IS NULL", {"h": h})
    return (rows[0]["session_id"], rows[0]["username"]) if rows else None


def refresh(refresh_token: str | None, ctx: RequestContext) -> Issued:
    """갱신 — 현재 갱신 토큰이면 회전(새 갱신+접근 토큰), 직전 토큰이면 유예 창 안에서만 접근 토큰만,
    이미 쓴 토큰이면 재사용 탐지 → 세션 폐기."""
    if not refresh_token or len(refresh_token) > 200 or not refresh_token.startswith(REFRESH_PREFIX):
        raise SessionError("token_invalid")
    h, now = token_hash(refresh_token), clock.utc()
    cols = ("s.session_id, s.username, s.revoked_at, s.expires_at, s.last_seen_at, s.device_hash, s.rotated_at, "
            "s.persistent, s.dpop_jkt, u.status, u.locked_until")
    frm = "FROM auth_session s JOIN app_user u ON u.username = s.username"
    failure: tuple | None = None                      # (SessionError, row) — 트랜잭션을 마친 뒤 처리
    issued: Issued | None = None
    used = prev = None
    with db.transaction() as conn:
        row = conn.execute(text(f"SELECT {cols} {frm} WHERE s.refresh_hash=:h FOR UPDATE OF s"),
                           {"h": h}).mappings().first()
        if row:
            try:
                _check_session(row, ctx, now, check_device=True)
                _check_binding(row, ctx)
            except SessionError as e:
                if e.revoke_reason:
                    revoke(row["session_id"], e.revoke_reason, conn=conn)
                failure = (e, row)
            else:
                new_refresh = _new(REFRESH_PREFIX)
                conn.execute(text(
                    "UPDATE auth_session SET refresh_prev_hash=refresh_hash, refresh_hash=:nh, rotated_at=:n, "
                    "refresh_count=refresh_count+1, last_seen_at=:n, last_ip_net=COALESCE(:ip, last_ip_net) "
                    "WHERE session_id=:s"), {"nh": token_hash(new_refresh), "n": now, "s": row["session_id"],
                                              "ip": ctx.ip_net or None})
                conn.execute(text("INSERT INTO auth_refresh_used(token_hash, session_id, used_at) "
                                  "VALUES(:h, :s, :n) ON CONFLICT DO NOTHING"), {"h": h, "s": row["session_id"], "n": now})
                conn.execute(text("DELETE FROM auth_token WHERE session_id=:s AND expires_at < :n"),
                             {"s": row["session_id"], "n": now})
                access = _issue_access(conn, row["session_id"], row["username"], now)
                issued = Issued(row["session_id"], access, new_refresh, None, settings.ACCESS_TOKEN_TTL_SECONDS,
                                row["expires_at"], bool(row["persistent"]))
        else:
            prev = conn.execute(text(f"SELECT {cols} {frm} WHERE s.refresh_prev_hash=:h FOR UPDATE OF s"),
                                {"h": h}).mappings().first()
            if prev and prev["revoked_at"] is None and prev["rotated_at"] is not None and \
                    (now - prev["rotated_at"]).total_seconds() <= settings.REFRESH_GRACE_SECONDS:
                try:
                    _check_session(prev, ctx, now, check_device=True)
                    _check_binding(prev, ctx)
                except SessionError as e:
                    if e.revoke_reason:
                        revoke(prev["session_id"], e.revoke_reason, conn=conn)
                    failure = (e, prev)
                else:                                     # 다른 탭이 방금 회전 — 접근 토큰만(갱신 쿠키는 그 탭이 이미 바꿈)
                    access = _issue_access(conn, prev["session_id"], prev["username"], now)
                    issued = Issued(prev["session_id"], access, None, None, settings.ACCESS_TOKEN_TTL_SECONDS,
                                    prev["expires_at"], bool(prev["persistent"]))
            else:
                used = conn.execute(text("SELECT session_id FROM auth_refresh_used WHERE token_hash=:h"),
                                    {"h": h}).first()
    if issued:
        return issued
    if failure:
        err, r = failure
        if err.event:
            kind, sev, detail = err.event
            _event(kind, sev, username=r["username"], session_id=r["session_id"], **detail)
        raise err
    if used or prev:                                      # 이미 쓴 갱신 토큰 — 탈취 후 재사용으로 본다
        sid = used[0] if used else prev["session_id"]
        owner = db.query("SELECT username FROM auth_session WHERE session_id=:s", {"s": sid})
        revoke(sid, "refresh_reuse")
        _event("refresh_reuse", "critical", username=owner[0]["username"] if owner else None, session_id=sid)
        raise SessionError("refresh_reuse")
    raise SessionError("token_invalid")


def step_up(session_id: str, aal: int | None = None) -> None:
    """추가 인증 성공 — 시각 기록, 위험 신호 해제(본인임을 다시 증명했으므로). OTP 를 거쳤으면 AAL 상향."""
    db.execute("UPDATE auth_session SET step_up_at=:n, risk=NULL, aal=GREATEST(aal, :a) WHERE session_id=:s",
               {"n": clock.utc(), "s": session_id, "a": aal or 1})


def fresh(principal: Principal, max_age: int | None = None) -> bool:
    """최근(max_age 초 안)에 '추가 인증'(비밀번호 재확인)을 했는가. 로그인 자체는 세지 않는다.
    예전엔 로그인 시각도 쳐서, 로그인 직후 5분 동안은 탈취된 세션(다른 IP 대역 — 위험 신호)으로도 탈퇴·고액 송금·
    이상거래 '추가 인증' 등급이 재확인 없이 통과했다(2026-10-23 독립 점검). 금융권 관행(이체·해지마다 추가 인증)과도 맞춘다."""
    max_age = settings.STEP_UP_MAX_AGE_SECONDS if max_age is None else max_age
    t = principal.step_up_at or 0
    return t > 0 and clock.utc().timestamp() - t <= max_age


def revoke(session_id: str | None, reason: str, conn=None) -> None:
    """세션 폐기 — 갱신 토큰 무효, JIT 권한 회수. 접근 토큰 행은 만료(최대 10분)까지 남겨 두어, 그 토큰이 다시 오면
    '폐기된 세션'(session_revoked)이라고 정확히 알려 준다(정리는 purge). conn 을 주면 그 트랜잭션 안에서."""
    if not session_id:
        return
    now = clock.utc()
    stmts = [
        ("UPDATE auth_session SET revoked_at=:n, revoke_reason=:r, refresh_hash=NULL "
         "WHERE session_id=:s AND revoked_at IS NULL", {"n": now, "r": reason[:40], "s": session_id}),
        # 그 사용자의 열린 화면(SSE)에 알림 — 커밋되면 다른 탭도 바로 로그인 화면으로(세션 ID 앞부분만)
        ("SELECT pg_notify('oos_events', json_build_object('u', username, 'k', 'session', "
         "'d', json_build_object('reason', CAST(:r AS text), 'sid', left(session_id, 8)))::text) "
         "FROM auth_session WHERE session_id=:s", {"r": reason[:40], "s": session_id}),
        ("UPDATE role_activation SET revoked_at=:n WHERE session_id=:s AND revoked_at IS NULL",
         {"n": now, "s": session_id}),
    ]
    if conn is not None:
        for sql, p in stmts:
            conn.execute(text(sql), p)
    else:
        with db.transaction() as c:
            for sql, p in stmts:
                c.execute(text(sql), p)


def revoke_user(username: str, reason: str, keep: str | None = None, conn=None) -> int:
    """사용자의 모든 세션 폐기(keep 세션만 남김) — 비밀번호 변경·계정 잠금·관리자 조치."""
    q = "SELECT session_id FROM auth_session WHERE username=:u AND revoked_at IS NULL"
    rows = (conn.execute(text(q), {"u": username}).fetchall() if conn is not None
            else [(r["session_id"],) for r in db.query(q, {"u": username})])
    n = 0
    for (sid,) in rows:
        if sid != keep:
            revoke(sid, reason, conn=conn)
            n += 1
    return n


def list_for(username: str, current: str | None = None) -> list[dict]:
    """내 세션 목록(활성만) — 기기·대략 위치(IP 대역)·시각. 토큰·해시는 내보내지 않는다."""
    rows = db.query("SELECT session_id, client, device, ip_net, last_ip_net, created_at, last_seen_at, expires_at, aal "
                    "FROM auth_session WHERE username=:u AND revoked_at IS NULL AND expires_at > now() "
                    "ORDER BY last_seen_at DESC", {"u": username})
    return [{"session_id": r["session_id"], "client": r["client"], "device": r["device"],
             "ip_net": r["last_ip_net"] or r["ip_net"], "created_at": r["created_at"].isoformat(),
             "last_seen_at": r["last_seen_at"].isoformat(), "expires_at": r["expires_at"].isoformat(),
             "mfa": int(r["aal"]) >= 2, "current": r["session_id"] == current} for r in rows]


def alive(session_id: str | None) -> bool:
    """세션이 아직 유효한가(폐기·절대 만료·유휴) — 오래 열린 연결(SSE)을 주기적으로 다시 확인할 때. 활동 시각은 갱신하지 않는다."""
    if not session_id:
        return False
    rows = db.query("SELECT revoked_at, expires_at, last_seen_at FROM auth_session WHERE session_id=:s", {"s": session_id})
    if not rows or rows[0]["revoked_at"] is not None:
        return False
    now = clock.utc()
    r = rows[0]
    return now < r["expires_at"] and (now - r["last_seen_at"]).total_seconds() <= settings.SESSION_IDLE_SECONDS


def csrf_ok(principal: Principal, header_value: str | None) -> bool:
    if not principal.csrf_hash or not header_value or len(header_value) > 200:
        return False
    return secrets.compare_digest(token_hash(header_value), principal.csrf_hash)


def purge(days: int = 7) -> dict:
    """정리 배치 — 만료·폐기 후 days 일 지난 세션, 만료된 접근 토큰, 오래된 '사용한 갱신 토큰' 기록."""
    now = clock.utc()
    cut = now - timedelta(days=days)
    with db.transaction() as conn:
        t = conn.execute(text("DELETE FROM auth_token WHERE expires_at < :n OR session_id IS NULL"), {"n": now}).rowcount
        u = conn.execute(text("DELETE FROM auth_refresh_used WHERE used_at < :c"),
                         {"c": now - timedelta(seconds=settings.SESSION_ABSOLUTE_SECONDS)}).rowcount
        s = conn.execute(text("DELETE FROM auth_session WHERE (revoked_at IS NOT NULL AND revoked_at < :c) "
                              "OR expires_at < :c"), {"c": cut}).rowcount
    return {"tokens": t, "refresh_used": u, "sessions": s}
