"""역할 기반 접근 통제(RBAC) — '상시 관리자'를 두지 않는다.

  자격(user_role)   이 역할을 '켤 수 있는' 사람. 부트스트랩(ADMIN_USERS) 외에는 2인 승인으로만 생기고,
                    ROLE_GRANT_DAYS(90일)마다 만료 — 계속 필요하면 다시 승인받는다(주기적 권한 재검토).
  활성(role_activation)  실제로 권한을 쓰는 상태. 세션에 묶이고(로그아웃·세션 폐기 = 회수) JIT_MAX_MINUTES(30분)
                    안에서만 유효. 켤 때 사유를 적고, 2단계 인증(AAL2)을 마친 세션이어야 한다.
  2인 승인(role_request)  자격 부여는 요청자 ≠ 승인자 ≠ 대상자. 자기 자신에게 권한을 줄 수 없다(직무 분리).
  생명주기(2026-11)     ROLE_DORMANT_DAYS(60일) 동안 한 번도 켜지 않은 자격은 매일 자동 회수(필요 없는 권한 — 금융보안 수준진단 3.2).
                    인사조치(퇴직·전보)는 offboard 한 번으로 자격·활성·대기 요청·세션을 회수하고 계정을 중지한다
                    (전자금융감독규정 제13조 ①11호 '인사조치 시 지체 없이 계정 삭제·사용 중지').
                    부트스트랩(ADMIN_USERS) 자격은 비상용으로 자동 회수하지 않고 표시만 — 중지된 계정에는 다시 주지 않는다.
모든 변경은 감사 로그(해시 체인)에 남는다.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings
from app.security.context import Principal

ROLES = {
    "auditor": "감사 기록·보안 이벤트·개인정보 처리 기록 열람(읽기 전용)",
    "security_admin": "세션 강제 종료·계정 잠금/해제·개인정보 열람(사유 기록)",
    "admin": "역할 부여 요청·승인(본인 관련 요청은 승인 불가)",
}


class RbacError(Exception):
    def __init__(self, message: str, status: int = 403, code: str = "forbidden"):
        super().__init__(message)
        self.status, self.code = status, code


def _audit(action, actor, target=None, detail=None):
    from app.services import audit
    audit.record(action, actor, target=target, detail=detail)


def eligible(username: str) -> set[str]:
    rows = db.query("SELECT role FROM user_role WHERE username=:u AND (expires_at IS NULL OR expires_at > now())",
                    {"u": username})
    return {r["role"] for r in rows}


def bootstrap() -> int:
    """ADMIN_USERS(설정)에 적힌 계정에 모든 관리 역할 '자격'을 준다(활성은 아님). 없는 계정은 건너뜀."""
    if not settings.ADMIN_USERS:
        return 0
    n = 0
    users = db.query("SELECT username FROM app_user WHERE lower(username) = ANY(:us) AND status <> 'disabled'",   # 인사조치로
                     {"us": sorted(settings.ADMIN_USERS)})                                                        # 중지된 계정 제외
    for r in users:
        for role in ROLES:
            n += db.execute("INSERT INTO user_role(username, role, granted_by, granted_at, reason) "
                            "VALUES(:u, :r, 'bootstrap', :n, 'ADMIN_USERS 설정') ON CONFLICT DO NOTHING",
                            {"u": r["username"], "r": role, "n": clock.utc()}) or 0
    return n


def activate(principal: Principal, role: str, reason: str, minutes: int | None = None) -> dict:
    """JIT 활성 — 자격 확인 · AAL2(OTP) 확인 · 사유 필수 · 시간 제한."""
    if not principal.is_user or not principal.session_id:
        raise RbacError("로그인이 필요합니다", 401, "login_required")
    if role not in ROLES:
        raise RbacError("알 수 없는 역할이에요", 400, "bad_role")
    if role not in eligible(principal.name):
        raise RbacError("이 역할의 자격이 없어요", 403, "not_eligible")
    if principal.aal < 2:
        raise RbacError("관리 권한은 2단계 인증(OTP)으로 로그인·추가 인증한 세션에서만 켤 수 있어요", 403, "mfa_required")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise RbacError("권한을 켜는 사유를 5자 이상 적어 주세요", 400, "reason_required")
    mins = max(5, min(int(minutes or settings.JIT_MAX_MINUTES), settings.JIT_MAX_MINUTES))
    now = clock.utc()
    with db.transaction() as conn:
        conn.execute(text("UPDATE role_activation SET revoked_at=:n WHERE session_id=:s AND role=:r AND revoked_at IS NULL"),
                     {"n": now, "s": principal.session_id, "r": role})
        conn.execute(text("INSERT INTO role_activation(username, role, session_id, reason, activated_at, expires_at) "
                          "VALUES(:u, :r, :s, :why, :n, :e)"),
                     {"u": principal.name, "r": role, "s": principal.session_id, "why": reason[:200], "n": now,
                      "e": now + timedelta(minutes=mins)})
    _audit("rbac.activate", principal.name, role, {"minutes": mins, "reason": reason[:200]})
    return {"role": role, "expires_at": (now + timedelta(minutes=mins)).isoformat(), "minutes": mins}


def deactivate(principal: Principal, role: str | None = None) -> int:
    q = "UPDATE role_activation SET revoked_at=:n WHERE session_id=:s AND revoked_at IS NULL"
    p = {"n": clock.utc(), "s": principal.session_id}
    if role:
        q += " AND role=:r"
        p["r"] = role
    n = db.execute(q, p) or 0
    _audit("rbac.deactivate", principal.name, role or "*")
    return n


def request_grant(requester: Principal, username: str, role: str, reason: str) -> dict:
    if role not in ROLES:
        raise RbacError("알 수 없는 역할이에요", 400, "bad_role")
    if not db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": username}):
        raise RbacError("없는 계정이에요", 404, "not_found")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise RbacError("요청 사유를 5자 이상 적어 주세요", 400, "reason_required")
    rid = db.write_query("INSERT INTO role_request(username, role, reason, requested_by, requested_at, status) "
                   "VALUES(:u, :r, :why, :by, :n, 'pending') RETURNING id",
                   {"u": username, "r": role, "why": reason[:200], "by": requester.name, "n": clock.utc()})[0]["id"]
    _audit("rbac.request", requester.name, f"{username}:{role}", {"request_id": rid})
    return {"request_id": rid, "status": "pending"}


def decide(approver: Principal, request_id: int, approve: bool) -> dict:
    """승인/반려 — 승인자는 요청자·대상자와 달라야 한다(직무 분리). 승인되면 ROLE_GRANT_DAYS 동안 자격."""
    now = clock.utc()
    with db.transaction() as conn:
        req = conn.execute(text("SELECT * FROM role_request WHERE id=:i FOR UPDATE"), {"i": request_id}).mappings().first()
        if not req or req["status"] != "pending":
            raise RbacError("대기 중인 요청이 아니에요", 404, "not_found")
        if approver.name in (req["requested_by"], req["username"]):
            raise RbacError("본인이 요청했거나 본인이 대상인 요청은 승인할 수 없어요(2인 승인)", 403, "four_eyes")
        if now - req["requested_at"] > timedelta(days=7):
            conn.execute(text("UPDATE role_request SET status='expired', decided_at=:n WHERE id=:i"), {"n": now, "i": request_id})
            raise RbacError("요청이 만료됐어요(7일)", 409, "expired")
        status = "approved" if approve else "rejected"
        conn.execute(text("UPDATE role_request SET status=:st, decided_by=:by, decided_at=:n WHERE id=:i"),
                     {"st": status, "by": approver.name, "n": now, "i": request_id})
        if approve:
            conn.execute(text(
                "INSERT INTO user_role(username, role, granted_by, approved_by, granted_at, expires_at, reason) "
                "VALUES(:u, :r, :by, :ap, :n, :e, :why) ON CONFLICT(username, role) DO UPDATE SET "
                "granted_by=EXCLUDED.granted_by, approved_by=EXCLUDED.approved_by, granted_at=EXCLUDED.granted_at, "
                "expires_at=EXCLUDED.expires_at, reason=EXCLUDED.reason"),
                {"u": req["username"], "r": req["role"], "by": req["requested_by"], "ap": approver.name, "n": now,
                 "e": now + timedelta(days=settings.ROLE_GRANT_DAYS), "why": req["reason"]})
    _audit(f"rbac.{status}", approver.name, f"{req['username']}:{req['role']}", {"request_id": request_id})
    return {"request_id": request_id, "status": status}


def revoke_eligibility(operator: Principal, username: str, role: str) -> int:
    with db.transaction() as conn:
        n = conn.execute(text("DELETE FROM user_role WHERE username=:u AND role=:r"), {"u": username, "r": role}).rowcount
        conn.execute(text("UPDATE role_activation SET revoked_at=:n WHERE username=:u AND role=:r AND revoked_at IS NULL"),
                     {"n": clock.utc(), "u": username, "r": role})
    _audit("rbac.revoke", operator.name, f"{username}:{role}")
    return n or 0


def overview() -> dict:
    """자격 목록(만료 임박 표시) + 대기 중 요청 — 주기적 권한 재검토용."""
    now = clock.utc()
    soon = now + timedelta(days=14)
    idle_cut = now - timedelta(days=settings.ROLE_DORMANT_DAYS)
    roles = db.query("SELECT r.username, r.role, r.granted_by, r.approved_by, r.granted_at, r.expires_at, "
                     "(SELECT MAX(a.activated_at) FROM role_activation a WHERE a.username = r.username AND a.role = r.role) "
                     "AS last_used FROM user_role r ORDER BY r.username, r.role")
    for r in roles:
        ref = max(x for x in (r["granted_at"], r["last_used"]) if x is not None)
        r["recertify_soon"] = bool(r["expires_at"] and r["expires_at"] < soon)
        r["break_glass"] = r["granted_by"] == "bootstrap"
        r["idle_days"] = (now - ref).days
        r["dormant_soon"] = (not r["break_glass"]) and ref < idle_cut + timedelta(days=14)
        r["granted_at"] = r["granted_at"].isoformat()
        r["expires_at"] = r["expires_at"].isoformat() if r["expires_at"] else None
        r["last_used"] = r["last_used"].isoformat() if r["last_used"] else None
    pending = db.query("SELECT id, username, role, reason, requested_by, requested_at FROM role_request "
                       "WHERE status='pending' ORDER BY id")
    for p in pending:
        p["requested_at"] = p["requested_at"].isoformat()
    return {"roles": roles, "pending": pending, "catalog": ROLES, "dormant_days": settings.ROLE_DORMANT_DAYS}


# ── 권한 생명주기(2026-11) — 쓰지 않는 자격 회수 · 인사조치 일괄 회수 ─────────────────────────────
_DORMANT_SQL = ("SELECT r.username, r.role, r.granted_by, r.granted_at, (SELECT MAX(a.activated_at) FROM role_activation a "
                "WHERE a.username = r.username AND a.role = r.role) AS last_used FROM user_role r ORDER BY r.username, r.role")


def dormant(days: int | None = None, now=None) -> dict:
    """쓰지 않는 관리 자격 — 부여(또는 마지막 활성) 뒤 days 일 동안 한 번도 켜지 않은 자격."""
    days = int(days or settings.ROLE_DORMANT_DAYS)
    now = now or clock.utc()
    cut = now - timedelta(days=days)
    rows = db.query(_DORMANT_SQL)
    out = []
    for r in rows:
        ref = max(x for x in (r["granted_at"], r["last_used"]) if x is not None)
        if ref < cut:
            out.append({"username": r["username"], "role": r["role"], "idle_days": (now - ref).days,
                        "last_used": r["last_used"].isoformat() if r["last_used"] else None,
                        "bootstrap": r["granted_by"] == "bootstrap"})
    return {"days": days, "roles": len(rows), "bootstrap": sum(1 for r in rows if r["granted_by"] == "bootstrap"),
            "dormant": out}


def sweep_dormant(now=None) -> dict:
    """매일(보안 일일 작업) — 부트스트랩이 아닌 휴면 자격을 회수(감사·경보). 부트스트랩은 비상용이라 표시만.
    지우는 순간에 다시 확인한다(한 문장) — 방금 켠 자격을 지우지 않게."""
    days = int(settings.ROLE_DORMANT_DAYS)
    now = now or clock.utc()
    cut = now - timedelta(days=days)
    with db.transaction() as conn:
        gone = conn.execute(text(
            "DELETE FROM user_role r WHERE r.granted_by <> 'bootstrap' AND r.granted_at < :cut AND NOT EXISTS ("
            "SELECT 1 FROM role_activation a WHERE a.username = r.username AND a.role = r.role AND a.activated_at >= :cut) "
            "RETURNING r.username, r.role"), {"cut": cut}).mappings().all()
    for g in gone:
        _audit("rbac.dormant_revoke", "system", f"{g['username']}:{g['role']}", {"idle_days_min": days})
    flagged = [f"{r['username']}:{r['role']}" for r in dormant(days, now)["dormant"] if r["bootstrap"]]
    if gone:
        from app.services import alerts
        alerts.notify("rbac:dormant", "[권한] 쓰지 않는 관리 자격 회수",
                      f"{days}일 동안 한 번도 켜지 않은 자격 {len(gone)}건을 회수했습니다 — 다시 필요하면 2인 승인으로 재부여.",
                      level="warn")
    return {"revoked": [f"{g['username']}:{g['role']}" for g in gone], "bootstrap_dormant": flagged, "days": days}


def offboard(operator: Principal, username: str, reason: str, disable: bool = True) -> dict:
    """인사조치(퇴직·전보·장기 휴직) — 관리 자격·활성 권한·대기 중 요청(대상이거나 요청자인 것)을 한 트랜잭션에서 회수하고,
    모든 세션을 끝내고, 계정을 중지한다(disable). 한 사람이 즉시 — 권한을 줄이는 쪽은 빨라야 한다(되살리기는 2인 승인)."""
    from app.security import sessions
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise RbacError("인사조치 사유를 5자 이상 적어 주세요", 400, "reason_required")
    if username == operator.name:
        raise RbacError("자기 계정은 다른 관리자가 회수해야 해요(직무 분리)", 400, "self_offboard")
    if not db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": username}):
        raise RbacError("없는 계정이에요", 404, "not_found")
    now = clock.utc()
    with db.transaction() as conn:
        roles = [r for (r,) in conn.execute(text("DELETE FROM user_role WHERE username=:u RETURNING role"), {"u": username})]
        acts = conn.execute(text("UPDATE role_activation SET revoked_at=:n WHERE username=:u AND revoked_at IS NULL"),
                            {"n": now, "u": username}).rowcount
        reqs = conn.execute(text("UPDATE role_request SET status='rejected', decided_by=:by, decided_at=:n "
                                 "WHERE status='pending' AND (username=:u OR requested_by=:u)"),
                            {"by": operator.name, "n": now, "u": username}).rowcount
        if disable:
            conn.execute(text("UPDATE app_user SET status='disabled' WHERE username=:u"), {"u": username})
    n_sess = sessions.revoke_user(username, f"offboard:{operator.name}")
    listed = username.lower() in {u.lower() for u in settings.ADMIN_USERS}
    _audit("rbac.offboard", operator.name, username,
           {"roles": sorted(roles), "activations": acts, "pending_rejected": reqs, "sessions": n_sess, "disabled": disable,
            "reason": reason[:200]})
    out = {"username": username, "roles_revoked": sorted(roles), "activations_revoked": acts, "requests_rejected": reqs,
           "sessions_revoked": n_sess, "disabled": disable, "bootstrap_listed": listed}
    if listed:
        out["warning"] = ("설정(ADMIN_USERS)에 남아 있는 계정입니다 — 설정에서도 빼세요"
                          + ("" if disable else "(계정을 중지하지 않으면 다음 기동 때 자격이 다시 생깁니다)"))
    return out
