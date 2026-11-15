"""기기 결속 키(DPoP 방식 — RFC 9449 의 핵심) — 세션을 '로그인한 브라우저 안의 꺼낼 수 없는 개인키'에 묶는다.

왜     쿠키(접근·갱신 토큰)는 감염 PC·악성 확장 프로그램·백업 파일로 복사될 수 있다. 기기 지문(브라우저·OS 계열)은 흉내 낼 수 있다.
       브라우저가 WebCrypto 로 만든 ECDSA P-256 키는 extractable=false — 그 페이지의 스크립트로도 개인키를 꺼낼 수 없고
       IndexedDB 에 '객체'로만 남는다. 갱신·돈·계정·관리 요청마다 그 키의 서명을 요구하면, 쿠키를 훔쳐 다른 기기에서 써도
       세션을 늘리거나(갱신) 돈을 옮길 수 없다 — 훔친 접근 토큰의 남은 수명(최대 10분) 동안 조회만 된다.
흐름   로그인 요청에 DPoP 헤더(공개키 + 서명) → 공개키 지문(jkt, RFC 7638)을 세션에 저장
       → /auth/refresh 와 policy.json 의 bind:"device" 동작은 같은 키로 새로 서명한 증명(메서드·경로·발급 시각·1회용 ID)을 요구.
       키가 다르거나 증명 없이 갱신하면 탈취로 보고 세션 종료(+ 규칙: 그 사용자 전 세션 종료), 돈 동작에 증명이 없으면 401.
형식   JWS compact. header {"typ":"dpop+jwt","alg":"ES256","jwk":{"kty":"EC","crv":"P-256","x","y"}}
       payload {"htm","htu","iat","jti"} · 서명은 WebCrypto 그대로(r‖s 64바이트, IEEE P1363).
RFC 와 다른 점   htu 는 경로만 비교(nginx 뒤 http↔https·포트가 달라도 어긋나지 않게 — 출처는 CSRF·Origin 검사가 본다).
       서버 nonce 대신 발급 시각 ±5분 + 1회용 ID(DB, 10분 보관)로 재사용을 막는다.
결속 안 된 세션(앱 Bearer·키를 만들 수 없는 오래된 브라우저)은 예전 통제(기기 지문·CSRF·추가 인증)로 동작한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app import clock, db

MAX_LEN = 2048
IAT_WINDOW_S = 300
_JTI_RE = re.compile(r"[A-Za-z0-9_\-]{16,64}")
_B64_RE = re.compile(r"[A-Za-z0-9_\-]+")
_now = time.time                      # 시험에서 바꿔 끼운다

MESSAGES = {
    "dpop_invalid": "기기 확인 서명이 올바르지 않아요. 새로고침 후 다시 시도해 주세요",
    "dpop_required": "이 기기에서 로그인한 세션인지 확인하지 못했어요. 새로고침 후 다시 시도해 주세요",
    "dpop_mismatch": "다른 기기에서 쓰인 세션이라 종료했어요. 다시 로그인해 주세요",
    "dpop_replay": "이미 쓴 확인 서명이에요. 다시 시도해 주세요",
}


class DpopError(Exception):
    def __init__(self, code: str):
        super().__init__(MESSAGES.get(code, code))
        self.code = code


def _b64d(s: str) -> bytes:
    if not s or not _B64_RE.fullmatch(s):
        raise DpopError("dpop_invalid")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def thumbprint(jwk: dict) -> str:
    """RFC 7638 — 필수 멤버만, 사전순, 공백 없이 → SHA-256 → base64url(43자)."""
    canon = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
                       separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


def verify(proof: str, method: str, path: str) -> dict:
    """증명 검사(서명·형식·메서드·경로·시각) → {"jkt", "jti", "iat"}. 1회용 확인(consume)은 따로."""
    if not proof or len(proof) > MAX_LEN or proof.count(".") != 2:
        raise DpopError("dpop_invalid")
    h64, p64, s64 = proof.split(".")
    try:
        header = json.loads(_b64d(h64))
        payload = json.loads(_b64d(p64))
        sig = _b64d(s64)
    except (ValueError, UnicodeDecodeError):
        raise DpopError("dpop_invalid") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise DpopError("dpop_invalid")
    jwk = header.get("jwk")
    if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256" or not isinstance(jwk, dict) \
            or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or "d" in jwk or len(sig) != 64:
        raise DpopError("dpop_invalid")
    try:
        x, y = int.from_bytes(_b64d(jwk["x"]), "big"), int.from_bytes(_b64d(jwk["y"]), "big")
        key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()   # 곡선 위의 점인지도 확인
        key.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                   f"{h64}.{p64}".encode(), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, KeyError, TypeError):
        raise DpopError("dpop_invalid") from None
    htu = str(payload.get("htu") or "")
    htu_path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]*", "", htu).split("?")[0].split("#")[0] or "/"
    iat, jti = payload.get("iat"), str(payload.get("jti") or "")
    if payload.get("htm") != method.upper() or htu_path != path or not isinstance(iat, (int, float)) \
            or abs(_now() - iat) > IAT_WINDOW_S or not _JTI_RE.fullmatch(jti):
        raise DpopError("dpop_invalid")
    return {"jkt": thumbprint(jwk), "jti": jti, "iat": int(iat)}


def consume(jkt: str, jti: str) -> None:
    """1회용 — 같은 (키, 증명 ID)는 한 번만. 두 번째면 dpop_replay."""
    n = db.execute("INSERT INTO dpop_jti(jkt, jti, seen_at) VALUES(:k, :j, :t) ON CONFLICT DO NOTHING",
                   {"k": jkt, "j": jti, "t": clock.utc()})
    if not n:
        raise DpopError("dpop_replay")


def from_request(request, consume_it: bool = True) -> str | None:
    """요청의 DPoP 헤더 → 검증한 키 지문(jkt). 헤더가 없으면 None. 같은 요청에서 두 번 불러도 한 번만 검사·소비."""
    cached = getattr(request.state, "dpop_jkt", False)
    if cached is not False:
        return cached
    proof = request.headers.get("dpop")
    if not proof:
        request.state.dpop_jkt = None
        return None
    got = verify(proof, request.method, request.url.path)
    if consume_it:
        consume(got["jkt"], got["jti"])
    request.state.dpop_jkt = got["jkt"]
    return got["jkt"]
