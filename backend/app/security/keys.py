"""키 관리 — 마스터 키 하나에서 용도별 하위 키를 파생(HKDF-SHA256)하고, 키 ID 로 교체를 지원한다.

  APP_SECRET_KEY        base64 32바이트 마스터 키(★비밀 — .env 또는 비밀 관리자. DB·소스·이미지에 두지 않는다)
  APP_SECRET_KEYS_OLD   교체 전 마스터 키(쉼표 구분) — 검증에만 쓴다(새로 만드는 값은 항상 현재 키)

  subkey("pepper")  비밀번호 페퍼 — 해시 앞에 HMAC 을 한 번 더 씌운다. DB 만 유출되면(키는 DB 밖) 해시를
                    가져가도 사전 대입 자체를 시작할 수 없다(OWASP Password Storage — Peppering).
  subkey("ip")      IP 가명화 — 감사 로그·보안 이벤트에 IP 원문 대신 HMAC 앞 16자.

원칙: 한 키를 여러 용도에 쓰지 않는다(키 분리). 하위 키는 용도 문자열이 다르면 서로 독립이다(HKDF, RFC 5869).
마스터 키가 없으면(개발) 페퍼는 쓰지 않고 IP 가명화는 개발용 고정 키로 대신한다 — 보안 설정 점검(python tools/security_check.py — KEY-1)이
운영 설정에서 이를 '실패'로 표시한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from functools import lru_cache

from app.config import settings

_SALT = b"outofstock/hkdf/v1"
_DEV_MASTER = hashlib.sha256(b"outofstock-dev-only-not-a-secret").digest()   # 키 미설정(개발) 대체 — 운영 금지


class KeyConfigError(RuntimeError):
    """마스터 키 형식 오류."""


def _parse(b64: str) -> bytes:
    try:
        k = base64.b64decode(b64.strip(), validate=True)
    except Exception:                                    # noqa: BLE001
        raise KeyConfigError("APP_SECRET_KEY 는 base64 로 인코딩한 32바이트여야 합니다") from None
    if len(k) != 32:
        raise KeyConfigError("APP_SECRET_KEY 는 32바이트여야 합니다")
    return k


def kid_of(key: bytes) -> str:
    """키 ID — 키 자체가 아니라 키 해시 앞 8자(어느 키로 만든 값인지 구분)."""
    return hashlib.sha256(b"kid:" + key).hexdigest()[:8]


def configured() -> bool:
    return bool((settings.APP_SECRET_KEY or "").strip())


@lru_cache(maxsize=8)
def _masters(cur: str, old: str) -> tuple:
    out = []
    for b in [cur] + [x for x in (old or "").split(",") if x.strip()]:
        if b and b.strip():
            k = _parse(b)
            out.append((kid_of(k), k))
    return tuple(out)


def masters() -> list[tuple[str, bytes]]:
    """[(키ID, 마스터 키)] — 첫 번째가 현재 키. 미설정이면 빈 목록."""
    return list(_masters(settings.APP_SECRET_KEY or "", settings.APP_SECRET_KEYS_OLD or ""))


def hkdf(key: bytes, info: str, length: int = 32) -> bytes:
    """HKDF-SHA256(RFC 5869) — extract 후 expand."""
    prk = hmac.new(_SALT, key, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info.encode() + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def subkey(purpose: str, kid: str | None = None) -> tuple[str, bytes] | None:
    """용도별 하위 키 (키ID, 키). kid 를 주면 그 마스터에서(교체 전 값 검증), 없으면 현재 키.
    마스터 키가 없으면 None(호출한 쪽이 '키 없음' 동작을 정한다)."""
    ms = masters()
    if not ms:
        return None
    for k_id, m in ms:
        if kid is None or k_id == kid:
            return k_id, hkdf(m, f"oos/{purpose}/v1")
    return None


def hmac_hex(purpose: str, msg: str, n: int = 16) -> str:
    """용도별 HMAC 가명값(앞 n 자). 마스터 키가 없으면 개발용 고정 키(운영 금지)."""
    sk = subkey(purpose)
    key = sk[1] if sk else hkdf(_DEV_MASTER, f"oos/{purpose}/dev")
    return hmac.new(key, str(msg).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:n]
