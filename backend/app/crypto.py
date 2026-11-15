"""민감 값 암호화 저장 — 봉투 암호화(envelope encryption) + 알고리즘 교체 가능 구조(crypto agility).

  enc = encrypt("값", aad="chat:alice:bob")
      → 'enc:v2:a256gcm:<KEK ID>:<base64(포장된 DEK)>:<base64(nonce 12B | 암호문+태그)>'
  decrypt(enc, aad=...) → 원문. 형식이 아니면 그대로(도입 전 평문 행 호환). v1(직접 암호화) 값도 계속 읽는다.

  봉투 암호화
    · 값마다 새 데이터 키(DEK, 32바이트 무작위)로 암호화하고, DEK 는 키 암호화 키(KEK = DATA_ENCRYPTION_KEY)로 포장해
      함께 저장한다. KEK 를 바꿀 때 데이터는 다시 암호화하지 않고 DEK 포장만 새 KEK 로 바꾼다(rewrap — 빠르고 안전).
    · KEK 는 DB 밖(.env·비밀 관리자, 운영 확장 시 HSM/KMS)에만 있다. 포장·풀기(_wrap/_unwrap)가 KMS 연동 지점이다.
  AAD(추가 인증 데이터)  '어느 표·열·누구의 값인지'를 넣는다 — 암호문을 다른 행·열로 옮겨 붙이면 복호화가 실패한다.
                          DEK 포장에도 같은 AAD 를 묶는다.
  알고리즘 표기          a256gcm = AES-256-GCM(현재). 머리말에 알고리즘 이름이 들어가 있어, KCMVP 검증 모듈(ARIA-GCM 등)로
                          바꿀 때 _ALGS 에 구현을 추가하고 새 값부터 새 알고리즘으로 쓰면 된다(옛 값은 그대로 읽힘).
  키 교체                 새 KEK 를 DATA_ENCRYPTION_KEY 에, 예전 KEK 는 DATA_ENCRYPTION_KEYS_OLD 에(읽기만) →
                          python run.py crypto-rotate 가 등록된 암호화 열을 모두 rewrap.
  비밀번호는 여기서 다루지 않는다 — 되돌릴 수 없는 해시(PBKDF2 + 페퍼, app/security/passwords.py)로만 저장.
"""
from __future__ import annotations

import base64
import hashlib
import os

from app.config import settings

PREFIX = "enc:v1:"                                   # 예전 형식(KEK 로 직접 암호화) — 읽기 전용
PREFIX_V2 = "enc:v2:"
DEFAULT_ALG = "a256gcm"


class CryptoError(RuntimeError):
    pass


def _parse_key(b64):
    try:
        k = base64.b64decode(b64.strip(), validate=True)
    except Exception:                                    # noqa: BLE001
        raise CryptoError("DATA_ENCRYPTION_KEY 는 base64 로 인코딩한 32바이트여야 합니다") from None
    if len(k) != 32:
        raise CryptoError("DATA_ENCRYPTION_KEY 는 32바이트(AES-256)여야 합니다")
    return k


def _kid(k):
    return hashlib.sha256(k).hexdigest()[:8]


def _keys():
    """{키ID: KEK} — 현재 키(쓰기·읽기) + 예전 키(읽기만)."""
    out = {}
    for b in [settings.DATA_ENCRYPTION_KEY] + [x for x in settings.DATA_ENCRYPTION_KEYS_OLD.split(",") if x.strip()]:
        if b and b.strip():
            k = _parse_key(b)
            out[_kid(k)] = k
    return out


def enabled():
    return bool(settings.DATA_ENCRYPTION_KEY)


# ── 알고리즘 등록부(crypto agility) ─────────────────────────
def _aesgcm_seal(key: bytes, plain: bytes, aad: bytes | None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)                               # GCM: 같은 키로 nonce 재사용 금지 — 매번 무작위(값마다 새 DEK 라 더 안전)
    return nonce + AESGCM(key).encrypt(nonce, plain, aad)


def _aesgcm_open(key: bytes, blob: bytes, aad: bytes | None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


_ALGS = {"a256gcm": (_aesgcm_seal, _aesgcm_open)}       # 예: "aria256gcm": (KCMVP 모듈 seal, open) 를 추가


def algorithms() -> list[str]:
    return sorted(_ALGS)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _wrap(kek: bytes, dek: bytes, aad: str) -> bytes:
    """DEK 포장 — KMS/HSM 을 쓰면 이 함수가 KMS Encrypt 호출이 된다."""
    return _aesgcm_seal(kek, dek, ("dek:" + aad).encode("utf-8"))


def _unwrap(kek: bytes, wrapped: bytes, aad: str) -> bytes:
    return _aesgcm_open(kek, wrapped, ("dek:" + aad).encode("utf-8"))


def encrypt(plain, aad="", alg=DEFAULT_ALG):
    if plain is None or plain == "" or not enabled():
        return plain
    if alg not in _ALGS:
        raise CryptoError(f"지원하지 않는 알고리즘: {alg}")
    kek = _parse_key(settings.DATA_ENCRYPTION_KEY)
    dek = os.urandom(32)
    seal, _open = _ALGS[alg]
    body = seal(dek, str(plain).encode("utf-8"), aad.encode("utf-8") or None)
    return f"{PREFIX_V2}{alg}:{_kid(kek)}:{_b64(_wrap(kek, dek, aad))}:{_b64(body)}"


def _parts_v2(value):
    try:
        alg, kid, wrapped, body = value[len(PREFIX_V2):].split(":", 3)
        return alg, kid, _unb64(wrapped), _unb64(body)
    except Exception:                                    # noqa: BLE001
        raise CryptoError("암호문 형식 오류") from None


def decrypt(value, aad=""):
    if not isinstance(value, str) or not value.startswith(("enc:v1:", PREFIX_V2)):
        return value                                     # 평문(암호화 도입 전 행·키 없음) — 그대로
    from cryptography.exceptions import InvalidTag
    if value.startswith(PREFIX_V2):
        alg, kid, wrapped, body = _parts_v2(value)
        kek = _keys().get(kid)
        if kek is None:
            raise CryptoError("이 값을 잠근 키가 설정에 없습니다(DATA_ENCRYPTION_KEYS_OLD 확인)")
        if alg not in _ALGS:
            raise CryptoError(f"지원하지 않는 알고리즘: {alg}")
        try:
            dek = _unwrap(kek, wrapped, aad)
            return _ALGS[alg][1](dek, body, aad.encode("utf-8") or None).decode("utf-8")
        except InvalidTag:
            raise CryptoError("복호화 실패 — 값이 바뀌었거나 다른 위치(AAD)의 암호문입니다") from None
    try:                                                 # v1: KEK 로 직접 암호화한 예전 값
        kid, blob = value[len(PREFIX):].split(":", 1)
        raw = base64.b64decode(blob)
    except Exception:                                    # noqa: BLE001
        raise CryptoError("암호문 형식 오류") from None
    k = _keys().get(kid)
    if k is None:
        raise CryptoError("이 값을 잠근 키가 설정에 없습니다(DATA_ENCRYPTION_KEYS_OLD 확인)")
    try:
        return _aesgcm_open(k, raw, aad.encode("utf-8") or None).decode("utf-8")
    except InvalidTag:
        raise CryptoError("복호화 실패 — 값이 바뀌었거나 다른 위치(AAD)의 암호문입니다") from None


def needs_rotation(value):
    """현재 KEK·현재 형식이 아닌 값인가(키 교체·형식 이행 후 다시 저장할 대상)."""
    if not isinstance(value, str) or not enabled():
        return False
    cur = _kid(_parse_key(settings.DATA_ENCRYPTION_KEY))
    if value.startswith(PREFIX):
        return True                                      # v1 → v2 이행 대상
    if value.startswith(PREFIX_V2):
        return _parts_v2(value)[1] != cur
    return False


def rewrap(value, aad=""):
    """KEK 교체 — v2 는 DEK 포장만 새 KEK 로(데이터 암호문 그대로), v1 은 v2 로 다시 암호화. 평문은 그대로."""
    if not needs_rotation(value):
        return value
    if value.startswith(PREFIX):
        return encrypt(decrypt(value, aad), aad)
    from cryptography.exceptions import InvalidTag
    alg, kid, wrapped, body = _parts_v2(value)
    old = _keys().get(kid)
    if old is None:
        raise CryptoError("이 값을 잠근 키가 설정에 없습니다(DATA_ENCRYPTION_KEYS_OLD 확인)")
    try:
        dek = _unwrap(old, wrapped, aad)
    except InvalidTag:
        raise CryptoError("복호화 실패 — 값이 바뀌었거나 다른 위치(AAD)의 암호문입니다") from None
    kek = _parse_key(settings.DATA_ENCRYPTION_KEY)
    return f"{PREFIX_V2}{alg}:{_kid(kek)}:{_b64(_wrap(kek, dek, aad))}:{_b64(body)}"


# ── 등록된 암호화 열 ─────────────────────────────────────────────
# (표, 기본키, 열, 행 → AAD, 행을 읽을 때 함께 필요한 열) — 키 교체(rotate_all)와 키 점검(verify_all)이 같은 규칙을 쓴다.
ENCRYPTED_COLUMNS = (
    ("app_meta", "key", "value", lambda r: f"app_meta:{r['key']}", ()),
    ("payment_order", "order_id", "payment_key", lambda r: f"payment_order:{r['order_id']}", ()),
    ("user_mfa", "username", "secret_enc", lambda r: f"user_mfa:{r['username']}", ()),
    ("chat_message", "id", "body", lambda r: f"chat:{r['sender']}:{r['receiver']}", ("sender", "receiver")),
)


def _encrypted_rows(table, pk, col, extra):
    from app import db
    cols = ", ".join((pk, col) + tuple(extra))
    return db.query(f"SELECT {cols} FROM {table} WHERE {col} LIKE 'enc:%'")


def rotate_all() -> dict:
    """등록된 암호화 열을 훑어 현재 KEK·v2 가 아닌 값을 rewrap(python run.py crypto-rotate)."""
    from sqlalchemy import text

    from app import db
    out = {}
    for table, pk, col, aad_of, extra in ENCRYPTED_COLUMNS:
        n = 0
        rows = _encrypted_rows(table, pk, col, extra)
        with db.transaction() as conn:
            for r in rows:
                new = rewrap(r[col], aad_of(r))
                if new != r[col]:
                    conn.execute(text(f"UPDATE {table} SET {col}=:v WHERE {pk}=:k AND {col}=:old"),
                                 {"v": new, "k": r[pk], "old": r[col]})
                    n += 1
        out[f"{table}.{col}"] = {"scanned": len(rows), "rewrapped": n}
    return out


def verify_all() -> dict:
    """키 점검(읽기만) — 등록된 암호화 열의 값이 지금 키 링(현재 + 예전 KEK)으로 전부 풀리는가.
    복구 훈련에서 '데이터는 살렸는데 키가 없어 못 여는' 사고(키 분실·교체 누락)를 미리 찾는다.
    반환 {ok, columns{표.열: {scanned, ok, failed, missing_kids[]}}} — 값·평문은 결과에 담지 않는다."""
    out, all_ok = {}, True
    for table, pk, col, aad_of, extra in ENCRYPTED_COLUMNS:
        rows = _encrypted_rows(table, pk, col, extra)
        ok, failed, missing = 0, 0, set()
        for r in rows:
            try:
                decrypt(r[col], aad_of(r))
                ok += 1
            except CryptoError as e:
                failed += 1
                if "키가 설정에 없습니다" in str(e) and r[col].startswith(PREFIX_V2):
                    missing.add(_parts_v2(r[col])[1])
        all_ok &= failed == 0
        out[f"{table}.{col}"] = {"scanned": len(rows), "ok": ok, "failed": failed, "missing_kids": sorted(missing)}
    return {"ok": all_ok, "columns": out}
