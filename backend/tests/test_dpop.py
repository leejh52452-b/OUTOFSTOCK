"""기기 결속 키(DPoP 방식) — 로그인한 브라우저의 꺼낼 수 없는 키로 서명해야 갱신·돈·계정 동작이 된다(app/security/dpop.py).
핵심 시나리오: 쿠키(접근·갱신 토큰)를 그대로 복사한 '다른 기기'는 키가 없어 세션을 늘리거나 송금할 수 없다."""
import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.security import dpop
from app.services import auth, wallet

PW = "Dpop-pass-2026"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Device:
    """브라우저 WebCrypto 와 같은 형식(ES256, r‖s)으로 증명을 만드는 시험용 기기."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": _b64u(n.x.to_bytes(32, "big")), "y": _b64u(n.y.to_bytes(32, "big"))}

    @property
    def jkt(self):
        return dpop.thumbprint(self.jwk)

    def proof(self, method, path, iat=None, jti=None, header=None):
        h = _b64u(json.dumps(header or {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}).encode())
        p = _b64u(json.dumps({"htm": method, "htu": "https://outofstock.example" + path,
                              "iat": int(time.time()) if iat is None else iat,
                              "jti": jti or secrets.token_hex(16)}).encode())
        r, s = decode_dss_signature(self.key.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{h}.{p}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _web_login(user, device=None):
    c = TestClient(app)
    hdr = {"X-Client": "web"}
    if device:
        hdr["DPoP"] = device.proof("POST", "/auth/login")
    r = c.post("/auth/login", json={"username": user, "password": PW}, headers=hdr)
    assert r.status_code == 200, r.text
    return c


def _csrf(c):
    return {"X-CSRF-Token": c.cookies.get("oos_csrf")}


def _session(user):
    return db.query("SELECT session_id, dpop_jkt, revoked_at, revoke_reason FROM auth_session WHERE username=:u "
                    "ORDER BY created_at DESC LIMIT 1", {"u": user})[0]


def _users(*names):
    for n in names:
        auth.register(n, PW)


def test_login_binds_the_session_to_the_device_key_thumbprint():
    _users("dp-bind")
    dev = Device()
    _web_login("dp-bind", dev)
    assert _session("dp-bind")["dpop_jkt"] == dev.jkt and len(dev.jkt) == 43
    # RFC 9449 예시(6.1절)의 키 → 지문이 표준 문서의 jkt 와 같다(다른 구현과 맞물린다는 증거)
    assert dpop.thumbprint({"kty": "EC", "x": "l8tFrhx-34tV3hRICRDY9zCkDlpBhF42UQUfWVAWBFs",
                            "y": "9VE4jf_Ok_o64zbTTlcuNJajHmt6v9TDVrU0CdvGRDA", "crv": "P-256"}) == \
        "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"


def test_stolen_cookies_on_another_device_cannot_refresh_or_move_money():
    _users("dp-victim", "dp-friend")
    wallet.credit_once("dp-victim", 100_000, "iap", memo="시험 충전")
    dev = Device()
    c = _web_login("dp-victim", dev)
    # 정상: 같은 기기 키로 서명한 송금·갱신
    r = c.post("/transfer", headers={**_csrf(c), "DPoP": dev.proof("POST", "/transfer")}, json={"to": "dp-friend", "amount": 1000})
    assert r.status_code == 200, r.text
    r = c.post("/auth/refresh", headers={**_csrf(c), "X-Client": "web", "DPoP": dev.proof("POST", "/auth/refresh")})
    assert r.status_code == 200, r.text
    # 공격자: 쿠키만 복사(키 없음) → 송금은 401(세션 유지), 갱신은 탈취로 보고 세션 종료
    thief = TestClient(app)
    thief.cookies.update(dict(c.cookies))
    r = thief.post("/transfer", headers=_csrf(thief), json={"to": "dp-friend", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "dpop_required"
    assert thief.get("/wallet").status_code == 200                     # 조회는 접근 토큰 남은 시간(최대 10분)만
    other = Device()                                                     # 공격자 자기 키로 서명해도 지문이 다르다
    r = thief.post("/transfer", headers={**_csrf(thief), "DPoP": other.proof("POST", "/transfer")},
                   json={"to": "dp-friend", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "dpop_mismatch"
    s = _session("dp-victim")
    assert s["revoked_at"] is not None and s["revoke_reason"] == "dpop_mismatch"
    assert db.query("SELECT COUNT(*) AS n FROM wallet_ledger WHERE username='dp-victim' AND reason='transfer_out'")[0]["n"] == 1
    kinds = {r["kind"] for r in db.query("SELECT kind FROM security_event WHERE username='dp-victim'")}
    assert {"dpop_missing", "dpop_mismatch"} <= kinds


def test_refresh_without_the_key_ends_the_session():
    _users("dp-refresh")
    c = _web_login("dp-refresh", Device())
    thief = TestClient(app)
    thief.cookies.update(dict(c.cookies))
    r = thief.post("/auth/refresh", headers={**_csrf(thief), "X-Client": "web"})
    assert r.status_code == 401 and r.json()["code"] == "dpop_mismatch"
    assert _session("dp-refresh")["revoke_reason"] == "dpop_mismatch"
    assert c.get("/me").status_code == 401                              # 진짜 사용자도 다시 로그인(규칙: 그 사용자 전 세션 종료)


def test_proof_is_single_use_and_bound_to_method_path_and_time():
    _users("dp-replay", "dp-r2")
    wallet.credit_once("dp-replay", 100_000, "iap", memo="시험 충전")
    dev = Device()
    c = _web_login("dp-replay", dev)
    p = dev.proof("POST", "/transfer")
    assert c.post("/transfer", headers={**_csrf(c), "DPoP": p}, json={"to": "dp-r2", "amount": 1000}).status_code == 200
    r = c.post("/transfer", headers={**_csrf(c), "DPoP": p}, json={"to": "dp-r2", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "dpop_replay"
    for bad in (dev.proof("GET", "/transfer"),                          # 메서드가 다름
                dev.proof("POST", "/wallet"),                           # 경로가 다름
                dev.proof("POST", "/transfer", iat=int(time.time()) - 3600),   # 오래된 증명
                dev.proof("POST", "/transfer", header={"typ": "dpop+jwt", "alg": "none", "jwk": dev.jwk}),
                dev.proof("POST", "/transfer")[:-4] + "AAAA",           # 서명 변조
                "not.a.proof"):
        r = c.post("/transfer", headers={**_csrf(c), "DPoP": bad}, json={"to": "dp-r2", "amount": 1000})
        assert r.status_code == 401 and r.json()["code"] == "dpop_invalid", bad[:40]
    assert _session("dp-replay")["revoked_at"] is None                  # 깨진 증명은 세션을 끊지 않는다(화면 오류일 수 있음)


def test_unbound_sessions_keep_working_bearer_apps_and_old_browsers():
    _users("dp-app", "dp-app2")
    wallet.credit_once("dp-app", 100_000, "iap", memo="시험 충전")
    tok = TestClient(app).post("/auth/login", json={"username": "dp-app", "password": PW}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert TestClient(app).post("/transfer", headers=h, json={"to": "dp-app2", "amount": 1000}).status_code == 200
    assert _session("dp-app")["dpop_jkt"] is None
    c = _web_login("dp-app2")                                            # 키를 못 만든 브라우저(증명 없이 로그인)
    assert c.post("/transfer", headers=_csrf(c), json={"to": "dp-app", "amount": 500}).status_code == 200


def test_login_with_a_broken_proof_is_refused():
    _users("dp-broken")
    r = TestClient(app).post("/auth/login", json={"username": "dp-broken", "password": PW},
                             headers={"X-Client": "web", "DPoP": "x.y.z"})
    assert r.status_code == 400 and r.json()["code"] == "dpop_invalid"


@pytest.mark.parametrize("path,method", [("/auth/refresh", "POST"), ("/transfer", "POST")])
def test_verify_rejects_proofs_for_another_endpoint(path, method):
    dev = Device()
    got = dpop.verify(dev.proof(method, path), method, path)
    assert got["jkt"] == dev.jkt
    with pytest.raises(dpop.DpopError):
        dpop.verify(dev.proof(method, path), method, "/elsewhere")
