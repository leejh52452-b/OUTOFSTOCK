"""인증 — 회원가입·로그인·비밀번호 변경·추가 인증. 세부 보안 장치는 app/security 에 있다.

  비밀번호   정책 검사(흔한 비밀번호·아이디 포함 차단) + PBKDF2-SHA256 600,000회 + 페퍼(security/passwords.py)
  로그인     시도 제한(API 층) + 연속 ACCOUNT_LOCK_THRESHOLD 회 실패 시 계정 일시 잠금(DB — 재시작·다중 워커에도 유지)
             + 2단계 인증(켠 계정은 OTP 필수) → 세션 발급(security/sessions.py: 10분 접근 토큰 + 회전 갱신 토큰)
  사용자 열거 방지  없는 아이디·틀린 비밀번호·잠긴 계정(비밀번호가 틀린 경우)을 같은 문구·같은 시간으로 응답
  비밀번호 변경  현재 비밀번호 확인 → 새 솔트로 재해시 → 이 세션을 뺀 모든 세션 폐기
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import clock, db
from app.config import settings
from app.security import context, events, passwords, sessions, totp

log = logging.getLogger(__name__)

PASSWORD_MIN, PASSWORD_MAX = passwords.MIN_LEN, passwords.MAX_LEN
LEGACY_ITERATIONS = passwords.LEGACY_ITERATIONS
GENERIC_FAIL = "아이디 또는 비밀번호가 올바르지 않습니다"


class AuthError(Exception):
    """인증 실패 — code 로 이유를 구분(화면 안내·클라이언트 분기용), 메시지는 사용자에게 그대로."""
    def __init__(self, message: str, code: str = "auth_failed", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


# ── 예전 이름(테스트·다른 모듈 호환) ─────────────────────────
def _hash(password, salt, iterations=passwords.LEGACY_ITERATIONS):
    return passwords._pbkdf2(passwords._raw(password), salt, iterations)


def _tok(token):
    return sessions.token_hash(token)


# 아이디 허용 문자: 글자(한글 포함)·숫자·_ @ . - (2~50자) — 다른 사람 화면에 표시되는 아이디로 스크립트 주입 차단
_USERNAME_RE = re.compile(r"[\w@.\-]{2,50}")
_INVISIBLE = {"\u115f", "\u1160", "\u200b", "\u2800", "\u3164", "\uffa0"}   # 보이지 않는 글자(빈 이름 사칭 방지) — 소스엔 이스케이프로(숨은 문자 점검)
_GUEST_RE = re.compile(r"guest(-[0-9a-z]{8,40})?")


def is_guest_id(value):
    return bool(value) and bool(_GUEST_RE.fullmatch(str(value)))


def validate_username(username):
    if not username or not _USERNAME_RE.fullmatch(username):
        raise AuthError("아이디는 2~50자의 글자·숫자·_ @ . - 만 사용할 수 있어요")
    if any(ch in _INVISIBLE for ch in username) or not any(ch.isalnum() for ch in username):
        raise AuthError("아이디에는 글자나 숫자가 하나 이상 들어가야 해요(보이지 않는 문자·점만으로는 안 돼요)")
    if username.lower() == "guest" or username.lower().startswith("guest-"):
        raise AuthError("guest 로 시작하는 아이디는 쓸 수 없어요(비로그인 이용자용 예약어)")


def validate_password(password, what="비밀번호", username=None, current=None):
    try:
        passwords.check_policy(password, username=username, current=current, what=what)
    except passwords.PolicyError as e:
        raise AuthError(str(e), "weak_password") from None


# ── 가입 ───────────────────────────────────────────────────
def register(username, password, enforce_policy=True):
    """가입 — enforce_policy=False 는 데모 계정 시드 전용(공개 데모 비밀번호)."""
    if not username or not password:
        raise AuthError("아이디·비밀번호가 필요합니다")
    validate_username(username)
    if enforce_policy:
        validate_password(password, username=username)
    elif not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise AuthError(f"비밀번호는 {PASSWORD_MIN}~{PASSWORD_MAX}자여야 합니다")
    salt = passwords.new_salt()
    pw_hash = passwords.encode(password, salt)               # 해시(수백 ms)는 잠금 밖에서
    try:
        with db.transaction() as conn:
            # 대소문자만 다른 아이디 금지(사칭) — 같은 이름(대소문자 무시)의 가입은 잠금으로 줄 세운다
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "register:" + username.lower()})
            if conn.execute(text("SELECT 1 FROM app_user WHERE lower(username)=lower(:u)"), {"u": username}).first():
                raise AuthError("이미 존재하는 아이디입니다(대소문자만 다른 아이디는 쓸 수 없어요)", "duplicate")
            # password_changed_at 은 '바꾼' 시각만(가입은 변경이 아니다) — 이상거래탐지의 '24시간 안 비밀번호 변경'(계정 탈취 신호)이
            # 모든 새 계정에 켜지던 문제(2026-10-21 E2E 에서 발견: 새 계정 송금 점수가 일괄 +15)
            conn.execute(text("INSERT INTO app_user(username, password_hash, salt, created_at) VALUES(:u, :h, :s, :c)"),
                         {"u": username, "h": pw_hash, "s": salt, "c": clock.iso_now()})
    except IntegrityError:
        raise AuthError("이미 존재하는 아이디입니다", "duplicate") from None
    log.info("회원가입: %s", username)
    return {"username": username}


# ── 로그인 ─────────────────────────────────────────────────
def _find_user(username):
    """정확히 같은 아이디 우선, 없으면 대소문자만 다른 계정이 딱 하나일 때 그것(모바일 자동 대문자)."""
    cols = "username, password_hash, salt, status, failed_count, locked_until"
    rows = db.query(f"SELECT {cols} FROM app_user WHERE username=:u", {"u": username})
    if rows:
        return rows[0]
    rows = db.query(f"SELECT {cols} FROM app_user WHERE lower(username)=lower(:u) LIMIT 2", {"u": username})
    return rows[0] if len(rows) == 1 else None


def _fail(name, reason):
    """실패 1회 — 계정별 연속 실패 수를 올리고 기준에 닿으면 일시 잠금(보안 이벤트·전 세션 종료)."""
    n = db.write_query("UPDATE app_user SET failed_count = failed_count + 1 WHERE username=:u RETURNING failed_count",
                       {"u": name})
    count = n[0]["failed_count"] if n else 0
    events.emit("login_fail", "low", username=name, reason=reason, count=count)
    if count >= settings.ACCOUNT_LOCK_THRESHOLD:
        db.execute("UPDATE app_user SET failed_count = 0 WHERE username=:u", {"u": name})
        events.lock_account(name, f"login_fail x{count}")


def login(username, password, otp=None, client="api", ctx=None, persistent=True):
    """비밀번호(+OTP) 확인 → 세션 발급. 반환: token(접근)·refresh_token·csrf·session_id·expires_in·username."""
    ctx = ctx or context.current()
    if not username or not password or len(password) > PASSWORD_MAX:
        passwords.dummy(password)
        events.emit("login_fail", "low", reason="malformed")
        raise AuthError(GENERIC_FAIL, "invalid_credentials", 401)
    row = _find_user(username)
    if row is None:
        passwords.dummy(password)                          # 없는 아이디도 같은 비용(응답 시간으로 존재 여부가 새지 않게)
        events.emit("login_fail", "low", reason="unknown_user")
        raise AuthError(GENERIC_FAIL, "invalid_credentials", 401)
    ok, upgrade = passwords.verify(password, row["salt"], row["password_hash"])
    passwords.equalize(password, row["password_hash"])
    name, now = row["username"], clock.utc()
    locked = row["status"] == "locked" or (row["locked_until"] is not None and row["locked_until"] > now)
    if row["status"] == "disabled" or locked:
        if not ok:                                         # 비밀번호도 틀렸으면 잠금 사실을 알려 주지 않는다(열거 방지)
            events.emit("login_fail", "low", username=name, reason="locked_and_wrong")
            raise AuthError(GENERIC_FAIL, "invalid_credentials", 401)
        left = int((row["locked_until"] - now).total_seconds() // 60) + 1 if row["locked_until"] else None
        msg = ("이용이 중지된 계정이에요. 관리자에게 문의해 주세요" if row["status"] == "disabled" else
               f"비밀번호를 여러 번 틀려 잠시 잠겼어요. {left}분 뒤 다시 시도해 주세요" if left else
               "계정이 잠겨 있어요. 관리자에게 문의해 주세요")
        raise AuthError(msg, "account_locked", 403)
    if not ok:
        _fail(name, "wrong_password")
        raise AuthError(GENERIC_FAIL, "invalid_credentials", 401)
    aal = 1
    if totp.enabled(name):
        if not otp:
            raise AuthError("2단계 인증 코드(OTP)를 입력해 주세요", "otp_required", 401)
        if not totp.verify(name, otp):
            events.emit("mfa_fail", "medium", username=name, phase="login")
            _fail(name, "wrong_otp")
            raise AuthError("인증 코드가 맞지 않아요", "otp_invalid", 401)
        aal = 2
    expected = row["password_hash"]
    with db.transaction() as conn:
        if upgrade:                                        # 평문을 아는 유일한 순간 — 현재 기준(반복·페퍼)으로 다시 저장
            salt = passwords.new_salt()
            new_h = passwords.encode(password, salt)
            if conn.execute(text("UPDATE app_user SET password_hash=:h, salt=:s WHERE username=:u AND password_hash=:old"),
                            {"h": new_h, "s": salt, "u": name, "old": expected}).rowcount == 1:
                expected = new_h
                log.info("비밀번호 해시 기준 상향: %s", name)
        # 비밀번호를 확인하던 사이에 탈퇴·비밀번호 변경이 있었으면 세션을 만들지 않는다(FOR SHARE 로 줄 세움)
        if not conn.execute(text("SELECT 1 FROM app_user WHERE username=:u AND password_hash=:h FOR SHARE"),
                            {"u": name, "h": expected}).first():
            raise AuthError(GENERIC_FAIL, "invalid_credentials", 401)
        conn.execute(text("UPDATE app_user SET failed_count=0, last_login_at=:n WHERE username=:u"), {"n": now, "u": name})
        issued = sessions.create(conn, name, "web" if client == "web" else "api", aal, ctx, persistent=persistent)
    return {"token": issued.access, "refresh_token": issued.refresh, "csrf": issued.csrf, "username": name,
            "session_id": issued.session_id, "expires_in": issued.expires_in, "mfa": aal >= 2,
            "_issued": issued}


def step_up(principal, password, otp=None):
    """추가 인증 — 비밀번호(+OTP 켠 계정) 재확인. 성공하면 세션의 step_up_at 갱신·위험 신호 해제."""
    rows = db.query("SELECT password_hash, salt FROM app_user WHERE username=:u", {"u": principal.name})
    if not rows:
        raise AuthError("사용자를 찾을 수 없습니다", "not_found", 404)
    ok = bool(password) and len(password) <= PASSWORD_MAX and \
        passwords.verify(password, rows[0]["salt"], rows[0]["password_hash"])[0]
    if not ok:
        events.emit("step_up_fail", "medium", username=principal.name)
        raise AuthError("비밀번호가 올바르지 않습니다", "step_up_failed", 401)
    aal = 1
    if totp.enabled(principal.name):
        if not totp.verify(principal.name, otp):
            events.emit("mfa_fail", "medium", username=principal.name, phase="step_up")
            raise AuthError("인증 코드(OTP)가 맞지 않아요", "otp_invalid", 401)
        aal = 2
    sessions.step_up(principal.session_id, aal)
    return {"ok": True, "valid_for": settings.STEP_UP_MAX_AGE_SECONDS, "mfa": aal >= 2}


def revoke(token):
    """로그아웃(토큰 원문으로) — 그 토큰의 세션 전체(접근·갱신 토큰) 폐기. 다른 기기 로그인은 유지."""
    if not token:
        return
    rows = db.query("SELECT session_id FROM auth_token WHERE token=:t", {"t": _tok(token)})
    if rows:
        sessions.revoke(rows[0]["session_id"], "logout")


def change_password(username, old_password, new_password, keep_session=None):
    """비밀번호 변경 — 현재 비밀번호 확인 후 새 솔트로 재해시. keep_session 을 뺀 모든 세션 폐기."""
    validate_password(new_password, "새 비밀번호", username=username, current=old_password)
    rows = db.query("SELECT password_hash, salt FROM app_user WHERE username=:u", {"u": username})
    if not rows:
        raise AuthError("사용자를 찾을 수 없습니다", "not_found", 404)
    if not old_password or len(old_password) > PASSWORD_MAX or \
            not passwords.verify(old_password, rows[0]["salt"], rows[0]["password_hash"])[0]:
        events.emit("step_up_fail", "medium", username=username, phase="change_password")
        raise AuthError("현재 비밀번호가 올바르지 않습니다", "wrong_password")
    salt = passwords.new_salt()
    new_h = passwords.encode(new_password, salt)
    with db.transaction() as conn:                       # 바꾸기 + 다른 세션 폐기를 한 번에
        r = conn.execute(text("UPDATE app_user SET password_hash=:h, salt=:s, password_changed_at=:n "
                              "WHERE username=:u AND password_hash=:old"),
                         {"h": new_h, "s": salt, "n": clock.utc(), "u": username, "old": rows[0]["password_hash"]})
        if r.rowcount != 1:
            raise AuthError("비밀번호가 방금 바뀌었어요 — 다시 로그인한 뒤 시도해 주세요", "conflict", 409)
        sessions.revoke_user(username, "password_change", keep=keep_session, conn=conn)
    log.info("비밀번호 변경: %s", username)
    return {"ok": True}


def user_for_token(token):
    """접근 토큰 → 사용자명(요청 밖 확인용 — 기기·IP 검사 없이 만료·폐기만). 무효면 None."""
    try:
        return sessions.verify_access(token, None, check_context=False).name
    except sessions.SessionError:
        return None


def unlock(username):
    """관리자 잠금 해제 — 연속 실패 수·잠금 시각 초기화."""
    return db.execute("UPDATE app_user SET locked_until=NULL, failed_count=0, "
                      "status=CASE WHEN status='locked' THEN 'active' ELSE status END WHERE username=:u",
                      {"u": username})


def lock(username, minutes=60):
    return db.execute("UPDATE app_user SET locked_until=:t WHERE username=:u",
                      {"t": clock.utc() + timedelta(minutes=minutes), "u": username})
