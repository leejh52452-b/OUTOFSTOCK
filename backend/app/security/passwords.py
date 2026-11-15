"""비밀번호 — 정책 검사와 저장 해시.

정책(가입·변경 공통 — NIST SP 800-63B 와 KISA 패스워드 안내서를 함께 반영)
  · 8~128자, 영문과 숫자를 모두 포함(화면 안내 문구 '영문, 숫자를 포함해 8자 이상'과 같은 규칙)
  · 흔히 쓰이는(유출 사전 상위) 비밀번호 차단 — 대소문자·끝 특수문자를 지운 형태로도 비교
  · 아이디를 그대로 포함한 비밀번호 차단, 같은 문자 4번 이상 연속 차단('aaaa1111')
  · 변경 시 현재 비밀번호와 같은 값 금지
  주기적 강제 변경은 하지 않는다(NIST: 유출 징후가 있을 때만 바꾸게 하는 편이 실제로 더 안전).

저장 형식(되돌릴 수 없는 해시만 저장, 솔트는 계정마다 무작위 16바이트)
  pbkdf2_sha256p$600000$<키ID>$<해시>  — 페퍼 적용(HMAC-SHA256(페퍼, 비밀번호) → PBKDF2-HMAC-SHA256)
  pbkdf2_sha256$600000$<해시>          — 페퍼 없음(마스터 키 미설정 시)
  <해시>                                — 2026-10 이전 형식(100,000회)
  로그인에 성공하면 더 강한 현재 기준(반복 횟수·페퍼·현재 키)으로 그 자리에서 다시 저장한다(무중단 이행).
  PBKDF2-HMAC-SHA256 은 KCMVP 검증 대상 알고리즘(SHA-2·HMAC·PBKDF2) 조합이라 금융권 암호 모듈 기준과도 맞는다.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from app.config import settings
from app.security import keys

MIN_LEN, MAX_LEN = 8, 128
LEGACY_ITERATIONS = 100_000
_P_PREFIX = "pbkdf2_sha256p$"
_PREFIX = "pbkdf2_sha256$"
_DUMMY_SALT = "0" * 32

# 흔한 비밀번호(유출 사전 상위권 중 '영문+숫자' 규칙을 통과하는 것) — 정규화(소문자, 끝 특수문자 제거) 후 비교
_COMMON = frozenset("""
password1 password12 password123 password1234 passw0rd p@ssw0rd passwd123 pass1word pa55word pa55w0rd
qwerty1 qwerty12 qwerty123 qwerty1234 qwer1234 qwert123 q1w2e3r4 q1w2e3r4t5 1q2w3e4r 1q2w3e4r5t 1q2w3e 1qaz2wsx
zaq12wsx qazwsx123 qweasd123 qweasdzxc1 asdf1234 asdfgh123 zxcv1234 zxcvbnm1 1234qwer 1234asdf 1234abcd 12qwaszx
abc12345 abcd1234 abcde12345 abc123abc a1b2c3d4 a1234567 a12345678 aa123456 aaa11111 abc1234567 1234567a
12345678a 123456789a 123456abc 123abc123 iloveyou1 iloveyou2 love1234 lovely123 admin123 admin1234 admin12345
administrator1 root1234 test1234 test12345 tester123 guest123 welcome1 welcome123 letmein1 monkey123 dragon123
sunshine1 princess1 football1 baseball1 superman1 batman123 trustno1 master123 shadow123 michael1 jordan23
starwars1 pokemon123 computer1 internet1 samsung123 samsung1 hello123 hello1234 hi123456 korea123 seoul123
qwe123qwe qwe12345 asd12345 zxc12345 sky12345 dream123 happy123 money123 money1234 stock123 stocks123
outofstock1 outofstock123 oos12345 demo1234 demo12345 user1234 user12345 changeme1 secret123 default1
""".split())


class PolicyError(ValueError):
    """비밀번호 정책 위반 — 메시지는 사용자에게 그대로 보여 준다."""


def _normalize(pw: str) -> str:
    return re.sub(r"[^a-z0-9]+$", "", pw.lower())


def check_policy(password: str | None, username: str | None = None, current: str | None = None,
                 what: str = "비밀번호") -> None:
    pw = password or ""
    if not MIN_LEN <= len(pw) <= MAX_LEN:
        raise PolicyError(f"{what}는 {MIN_LEN}~{MAX_LEN}자여야 합니다")
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"[0-9]", pw):
        raise PolicyError(f"{what}는 영문과 숫자를 모두 포함해야 해요")
    if re.search(r"(.)\1{3,}", pw):
        raise PolicyError(f"{what}에 같은 문자를 4번 이상 연속으로 쓸 수 없어요")
    if _normalize(pw) in _COMMON:
        raise PolicyError(f"너무 흔하게 쓰이는 {what}예요 — 추측하기 어려운 조합으로 바꿔 주세요")
    u = (username or "").split("@", 1)[0].lower()
    if len(u) >= 3 and u in pw.lower():
        raise PolicyError(f"{what}에 아이디를 넣을 수 없어요")
    if current is not None and secrets.compare_digest(pw.encode("utf-8", "surrogatepass"),
                                                      current.encode("utf-8", "surrogatepass")):
        raise PolicyError("새 비밀번호가 현재 비밀번호와 같아요")


# ── 해시 ─────────────────────────────────────────────────
def _pbkdf2(secret: bytes, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret, bytes.fromhex(salt), iterations).hex()


def _raw(password: str) -> bytes:
    return password.encode("utf-8", "surrogatepass")


def _peppered(password: str, key: bytes) -> bytes:
    return hmac.new(key, _raw(password), hashlib.sha256).hexdigest().encode()


def new_salt() -> str:
    return secrets.token_hex(16)


def encode(password: str, salt: str) -> str:
    """현재 기준으로 저장 문자열을 만든다(페퍼는 마스터 키가 있을 때만)."""
    it = settings.PASSWORD_HASH_ITERATIONS
    sk = keys.subkey("pepper")
    if sk:
        kid, key = sk
        return f"{_P_PREFIX}{it}${kid}${_pbkdf2(_peppered(password, key), salt, it)}"
    return f"{_PREFIX}{it}${_pbkdf2(_raw(password), salt, it)}"


def iterations_of(stored: str | None) -> int:
    s = stored or ""
    try:
        if s.startswith(_P_PREFIX):
            return int(s[len(_P_PREFIX):].split("$", 1)[0])
        if s.startswith(_PREFIX):
            return int(s[len(_PREFIX):].split("$", 1)[0])
    except ValueError:
        return 0
    return LEGACY_ITERATIONS


def verify(password: str, salt: str, stored: str | None) -> tuple[bool, bool]:
    """(일치, 현재 기준으로 다시 저장해야 하는지). 비교는 상수 시간."""
    s = stored or ""
    cur_kid = (keys.subkey("pepper") or (None,))[0]
    try:
        if s.startswith(_P_PREFIX):
            it_s, kid, h = s[len(_P_PREFIX):].split("$", 2)
            sk = keys.subkey("pepper", kid)
            if sk is None:                               # 그 페퍼 키가 설정에 없다(교체 전 키 누락) — 검증 불가
                dummy(password)
                return False, False
            ok = secrets.compare_digest(_pbkdf2(_peppered(password, sk[1]), salt, int(it_s)), h)
            return ok, ok and (int(it_s) < settings.PASSWORD_HASH_ITERATIONS or kid != cur_kid)
        if s.startswith(_PREFIX):
            it_s, h = s[len(_PREFIX):].split("$", 1)
            ok = secrets.compare_digest(_pbkdf2(_raw(password), salt, int(it_s)), h)
            return ok, ok and (int(it_s) < settings.PASSWORD_HASH_ITERATIONS or cur_kid is not None)
        ok = secrets.compare_digest(_pbkdf2(_raw(password), salt, LEGACY_ITERATIONS), s)
        return ok, ok
    except (ValueError, TypeError):
        return False, False


def dummy(password: str | None, iterations: int | None = None) -> None:
    """없는 아이디·짧은 해시에도 같은 비용을 치른다 — 응답 시간으로 '아이디 존재'가 새지 않게."""
    _pbkdf2(_raw(password or "x"), _DUMMY_SALT, iterations or settings.PASSWORD_HASH_ITERATIONS)


def equalize(password: str, stored: str | None) -> None:
    """예전 기준(반복 수가 적은) 해시는 더 빨리 끝난다 — 모자란 만큼 더 계산해 시간을 맞춘다."""
    short = settings.PASSWORD_HASH_ITERATIONS - iterations_of(stored)
    if short > 0:
        dummy(password, short)
