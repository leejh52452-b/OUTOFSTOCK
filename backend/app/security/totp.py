"""2단계 인증 — TOTP(RFC 6238, 30초·6자리·HMAC-SHA1: Google Authenticator 등 OTP 앱 호환) + 복구 코드.

  · 비밀키는 user_mfa.secret_enc 에 AES-256-GCM 으로 암호화 저장(AAD = 'user_mfa:<아이디>' — 다른 계정 행으로
    옮겨 붙이면 복호화 실패). 등록 확인 전(enabled_at NULL)에는 로그인에 쓰이지 않는다.
  · 재사용 차단: 마지막으로 받은 시간 단계(last_step) 이하의 코드는 거부 — 어깨너머로 본 코드를 30초 안에 다시 못 쓴다.
  · 시계 오차: 앞뒤 1단계(±30초)까지 허용.
  · 복구 코드 10개(한 번씩만) — 휴대폰을 잃어버렸을 때. 해시만 저장, 원문은 등록 순간 한 번만 보여 준다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from sqlalchemy import text

from app import clock, crypto, db

PERIOD, DIGITS, ISSUER = 30, 6, "OUTOFSTOCK"


class MfaError(ValueError):
    pass


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    pad = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32.upper() + pad)


def code_at(secret_b32: str, step: int) -> str:
    mac = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(n).zfill(DIGITS)


_now = time.time                      # 테스트에서 시계를 바꿔 끼운다


def current_step(at: float | None = None) -> int:
    return int((_now() if at is None else at) // PERIOD)


def match(secret_b32: str, code: str | None, last_step: int = 0, window: int = 1, at: float | None = None) -> int | None:
    """맞는 코드면 그 시간 단계, 아니면 None. last_step 이하는 재사용으로 거부."""
    c = (code or "").strip().replace(" ", "")
    if not (c.isdigit() and len(c) == DIGITS):
        return None
    now = current_step(at)
    hit = None
    for step in range(now - window, now + window + 1):
        if hmac.compare_digest(code_at(secret_b32, step), c) and step > last_step:
            hit = step                                    # 끝까지 돌아 비교 횟수를 일정하게(시간차 단서 줄이기)
    return hit


def uri(username: str, secret_b32: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={ISSUER}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


def _aad(username):
    return f"user_mfa:{username}"


def _rc_hash(code: str) -> str:
    return hashlib.sha256(("rc:" + code.replace("-", "").upper()).encode()).hexdigest()


# ── 계정 단위 ───────────────────────────────────────────────
def status(username: str) -> dict:
    rows = db.query("SELECT enabled_at, recovery_hashes FROM user_mfa WHERE username=:u", {"u": username})
    if not rows or rows[0]["enabled_at"] is None:
        return {"enabled": False}
    left = len(json.loads(rows[0]["recovery_hashes"] or "[]"))
    return {"enabled": True, "enabled_at": rows[0]["enabled_at"].isoformat(), "recovery_codes_left": left}


def enabled(username: str) -> bool:
    return bool(db.query("SELECT 1 FROM user_mfa WHERE username=:u AND enabled_at IS NOT NULL", {"u": username}))


def begin(username: str) -> dict:
    """등록 시작 — 새 비밀키(확인 전 상태). 이미 켜져 있으면 거부(끄고 다시)."""
    if enabled(username):
        raise MfaError("이미 2단계 인증이 켜져 있어요")
    secret = new_secret()
    db.execute("INSERT INTO user_mfa(username, secret_enc, enabled_at, last_step, created_at) "
               "VALUES(:u, :s, NULL, 0, :n) ON CONFLICT(username) DO UPDATE SET secret_enc=EXCLUDED.secret_enc, "
               "enabled_at=NULL, last_step=0, recovery_hashes=NULL, created_at=EXCLUDED.created_at",
               {"u": username, "s": crypto.encrypt(secret, aad=_aad(username)), "n": clock.utc()})
    return {"secret": secret, "otpauth_uri": uri(username, secret), "period": PERIOD, "digits": DIGITS}


def confirm(username: str, code: str) -> list[str]:
    """등록 확인 — 앱이 보여 주는 코드가 맞으면 켜고, 복구 코드 10개를 한 번만 돌려준다."""
    rows = db.query("SELECT secret_enc, enabled_at, last_step FROM user_mfa WHERE username=:u", {"u": username})
    if not rows or rows[0]["enabled_at"] is not None:
        raise MfaError("등록을 먼저 시작해 주세요")
    secret = crypto.decrypt(rows[0]["secret_enc"], aad=_aad(username))
    step = match(secret, code, rows[0]["last_step"] or 0)
    if step is None:
        raise MfaError("인증 코드가 맞지 않아요")
    codes = ["-".join(secrets.token_hex(4).upper()[i:i + 4] for i in (0, 4)) for _ in range(10)]
    db.execute("UPDATE user_mfa SET enabled_at=:n, last_step=:st, recovery_hashes=:rh WHERE username=:u",
               {"n": clock.utc(), "st": step, "rh": json.dumps([_rc_hash(c) for c in codes]), "u": username})
    return codes


def verify(username: str, code: str | None) -> bool:
    """로그인·추가 인증 — OTP 또는 복구 코드(한 번 쓰면 사라짐). 원자적으로 last_step 을 올려 동시 재사용도 막는다."""
    if not code:
        return False
    with db.transaction() as conn:
        row = conn.execute(text("SELECT secret_enc, last_step, recovery_hashes FROM user_mfa "
                                "WHERE username=:u AND enabled_at IS NOT NULL FOR UPDATE"),
                           {"u": username}).mappings().first()
        if not row:
            return False
        c = code.strip()
        if len(c.replace("-", "")) == 8 and not c.isdigit():          # 복구 코드
            hashes = json.loads(row["recovery_hashes"] or "[]")
            h = _rc_hash(c)
            if h not in hashes:
                return False
            hashes.remove(h)
            conn.execute(text("UPDATE user_mfa SET recovery_hashes=:rh WHERE username=:u"),
                         {"rh": json.dumps(hashes), "u": username})
            return True
        secret = crypto.decrypt(row["secret_enc"], aad=_aad(username))
        step = match(secret, c, int(row["last_step"] or 0))
        if step is None:
            return False
        conn.execute(text("UPDATE user_mfa SET last_step=:s WHERE username=:u AND last_step < :s"),
                     {"s": step, "u": username})
        return True


def disable(username: str) -> None:
    db.execute("DELETE FROM user_mfa WHERE username=:u", {"u": username})
