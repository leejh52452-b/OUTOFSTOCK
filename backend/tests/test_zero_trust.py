"""제로트러스트 — 신원·세션·기기·정책·관리 권한·자동 대응 검증(app/security)."""
import base64
import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.security import context, events, keys, passwords, pdp, pep, rbac, sessions, totp
from app.security.context import Principal
from app.services import auth, ratelimit

client = TestClient(app)
PW = "Zt-pass-2026"
CHROME_129 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
CHROME_130 = CHROME_129.replace("129.0.0.0", "130.0.0.0")
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"


def _reg(u, pw=PW):
    auth.register(u, pw)


def _login(u, pw=PW, c=None, **body):
    return (c or client).post("/auth/login", json={"username": u, "password": pw, **body})


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _h(u, pw=PW):
    return _bearer(_login(u, pw).json()["token"])


def _web(u, pw=PW, ua=None):
    c = TestClient(app)
    hdr = {"X-Client": "web"}
    if ua:
        c.headers["User-Agent"] = ua
    r = c.post("/auth/login", json={"username": u, "password": pw}, headers=hdr)
    assert r.status_code == 200, r.text
    return c


def _csrf(c):
    return {"X-CSRF-Token": c.cookies.get("oos_csrf")}


def _age_auth(u, minutes=60):
    db.execute(f"UPDATE auth_session SET auth_time = now() - interval '{int(minutes)} minutes', step_up_at = NULL "
               "WHERE username=:u", {"u": u})


# ── 정책(PDP/PEP) ─────────────────────────────────────────
def test_every_api_route_declares_a_known_policy_action():
    acts = pdp.actions()
    missing = [f"{sorted(r.methods)} {r.path}" for r in app.routes
               if isinstance(r, APIRoute) and pep.route_action(r) not in acts]
    assert missing == []


def test_route_without_policy_is_denied_by_default():
    a = FastAPI(dependencies=[Depends(pep.default_deny)])
    a.add_middleware(context.ContextMiddleware)

    @a.get("/forgot-to-guard")
    def _x():
        return {"leak": True}
    a.add_exception_handler(pep.PolicyDenied, lambda r, e: __import__("fastapi").responses.JSONResponse(
        status_code=e.status, content={"code": e.code}))
    r = TestClient(a).get("/forgot-to-guard")
    assert r.status_code == 403 and r.json()["code"] == "unknown_action"


def test_policy_file_is_validated_fail_closed(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text('{"actions": {"x": {"principals": ["root"]}}}', encoding="utf-8")
    with pytest.raises(pdp.PolicyConfigError):
        pdp.load(bad)
    bad.write_text('{"actions": {"x": {"principals": ["user"], "roles_any": ["god"]}}}', encoding="utf-8")
    with pytest.raises(pdp.PolicyConfigError):
        pdp.load(bad)
    assert "account.read" in pdp.actions()                    # 실제 정책은 그대로


def test_pdp_decision_matrix():
    import time
    now = time.time()
    anon, guest = Principal(), Principal(kind="guest", name="guest-abcdef0123")
    user = Principal(kind="user", name="u", aal=1, auth_time=now - 3600, session_id="s")
    assert pdp.decide("account.read", anon).code == "login_required"
    assert pdp.decide("account.read", guest).code == "login_required"
    assert pdp.decide("practice.use", anon).obligations == ("issue_guest",)
    assert pdp.decide("practice.use", guest).allow
    assert pdp.decide("admin.audit", user).code == "forbidden"
    auditor1 = Principal(kind="user", name="a", aal=1, roles=frozenset({"auditor"}))
    assert pdp.decide("admin.audit", auditor1).code == "mfa_required"
    auditor2 = Principal(kind="user", name="a", aal=2, roles=frozenset({"auditor"}))
    assert pdp.decide("admin.audit", auditor2).allow
    assert pdp.decide("admin.security", auditor2).code == "forbidden"     # 역할마다 권한 분리
    assert pdp.decide("account.delete", user).code == "step_up_required"
    assert pdp.decide("account.delete", user, fresh=True).allow
    assert pdp.decide("wallet.transfer", user, {"amount": 10_000}).allow
    assert pdp.decide("wallet.transfer", user, {"amount": 1_000_000}).code == "step_up_required"
    risky = Principal(kind="user", name="u", aal=1, risk=frozenset({"ip_change"}))
    assert pdp.decide("wallet.transfer", risky).code == "step_up_required"
    assert pdp.decide("admin.pii", Principal(kind="user", name="s", aal=2, roles=frozenset({"security_admin"})),
                      {"purpose": "짧음"}).code == "purpose_required"
    assert pdp.decide("no.such.action", user).code == "unknown_action"


# ── 로그인·쿠키·CSRF ──────────────────────────────────────
def test_bearer_login_returns_rotating_tokens_and_web_login_uses_httponly_cookies():
    _reg("zt-login")
    j = _login("zt-login").json()
    assert j["token"].startswith("oosa_") and j["refresh_token"].startswith("oosr_")
    assert j["token_type"] == "Bearer" and j["expires_in"] == settings.ACCESS_TOKEN_TTL_SECONDS
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": "zt-login", "password": PW}, headers={"X-Client": "web"})
    body = r.json()
    assert body["mode"] == "cookie" and "token" not in body and "refresh_token" not in body   # 스크립트가 못 읽게
    sc = r.headers.get_list("set-cookie")
    at = next(x for x in sc if x.startswith("oos_at="))
    rt = next(x for x in sc if x.startswith("oos_rt="))
    cs = next(x for x in sc if x.startswith("oos_csrf="))
    assert "HttpOnly" in at and "SameSite=Strict" in at
    assert "HttpOnly" in rt and "Path=/auth" in rt                         # 갱신 토큰은 /auth 경로에만 실림
    assert "HttpOnly" not in cs                                            # CSRF 토큰은 JS 가 읽어 헤더로
    assert c.get("/me").json()["user"] == "zt-login"
    assert db.query("SELECT client FROM auth_session WHERE username='zt-login' ORDER BY created_at DESC LIMIT 1")[0][
        "client"] == "web"


def test_cookie_requests_need_csrf_token_and_same_origin():
    _reg("zt-csrf")
    c = _web("zt-csrf")
    r = c.post("/watchlist/005930")
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert c.post("/watchlist/005930", headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.post("/watchlist/005930", headers=_csrf(c)).status_code == 200
    r = c.post("/watchlist/000660", headers={**_csrf(c), "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert c.post("/watchlist/000660", headers={**_csrf(c), "Origin": "http://testserver"}).status_code == 200
    assert client.post("/watchlist/000660", headers=_h("zt-csrf")).status_code == 200   # Bearer 는 CSRF 대상 아님
    assert db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='csrf_fail'")[0]["n"] == 3


def test_login_from_foreign_origin_is_rejected_for_web_clients():
    _reg("zt-lcsrf")
    r = client.post("/auth/login", json={"username": "zt-lcsrf", "password": PW},
                    headers={"X-Client": "web", "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"


def test_same_origin_check_is_port_exact():
    """출처 = 스킴+호스트+포트. 같은 서버의 다른 포트(학생 VM 의 다른 앱 등)는 쿠키(포트 구분 없음)를 공유하므로
    Origin 의 포트까지 맞아야 한다 — 앞단은 Host 를 포트째 넘긴다(nginx $http_host, test_deploy_config)."""
    _reg("zt-port")
    body = {"username": "zt-port", "password": PW}
    web = {"X-Client": "web", "Host": "203.0.113.5:8000"}
    assert client.post("/auth/login", json=body, headers={**web, "Origin": "http://203.0.113.5:8000"}).status_code == 200
    r = client.post("/auth/login", json=body, headers={**web, "Origin": "http://203.0.113.5:3000"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = client.post("/auth/login", json=body, headers={**web, "Host": "203.0.113.5", "Origin": "http://203.0.113.5:8000"})
    assert r.status_code == 403                                   # 포트를 뗀 Host(예전 nginx $host)면 정상 화면도 막혔다


# ── 토큰 만료·갱신 회전·재사용 탐지 ───────────────────────
def test_access_expiry_refresh_rotation_grace_and_reuse_detection():
    _reg("zt-rot")
    j = _login("zt-rot").json()
    db.execute("UPDATE auth_token SET expires_at = now() - interval '1 second' WHERE token=:t",
               {"t": sessions.token_hash(j["token"])})
    r = client.get("/me", headers=_bearer(j["token"]))
    assert r.status_code == 401 and r.json()["code"] == "token_expired" and r.headers["x-auth-code"] == "token_expired"
    r1 = client.post("/auth/refresh", json={"refresh_token": j["refresh_token"]}).json()
    assert r1["token"] != j["token"] and r1["refresh_token"] != j["refresh_token"]
    assert client.get("/me", headers=_bearer(r1["token"])).status_code == 200
    r2 = client.post("/auth/refresh", json={"refresh_token": j["refresh_token"]})     # 다른 탭이 거의 동시에(유예 창)
    assert r2.status_code == 200 and r2.json()["refresh_token"] is None and r2.json()["token"]
    db.execute("UPDATE auth_session SET rotated_at = now() - interval '1 hour' WHERE username='zt-rot'")
    r3 = client.post("/auth/refresh", json={"refresh_token": j["refresh_token"]})     # 유예 밖 재사용 = 탈취 의심
    assert r3.status_code == 401 and r3.json()["code"] == "refresh_reuse"
    assert client.get("/me", headers=_bearer(r1["token"])).json()["code"] == "session_revoked"
    ev = db.query("SELECT severity, response FROM security_event WHERE kind='refresh_reuse'")
    assert ev and ev[0]["severity"] == "critical" and "revoke_user" in ev[0]["response"]


def test_web_refresh_needs_csrf_and_rotates_cookies():
    _reg("zt-wref")
    c = _web("zt-wref")
    old_rt = c.cookies.get("oos_rt")
    assert c.post("/auth/refresh").status_code in (401, 403)           # CSRF 헤더 없음(접근 쿠키가 유효하면 PEP 가 403)
    r = c.post("/auth/refresh", headers=_csrf(c))
    assert r.status_code == 200 and r.json()["mode"] == "cookie"
    assert c.cookies.get("oos_rt") != old_rt and c.get("/me").status_code == 200


def test_idle_and_absolute_session_expiry():
    _reg("zt-idle")
    h = _h("zt-idle")
    db.execute("UPDATE auth_session SET last_seen_at = now() - interval '2 hours' WHERE username='zt-idle'")
    r = client.get("/me", headers=h)
    assert r.status_code == 401 and r.json()["code"] == "session_idle"
    assert db.query("SELECT revoke_reason FROM auth_session WHERE username='zt-idle'")[0]["revoke_reason"] == "idle"
    h2 = _h("zt-idle")
    db.execute("UPDATE auth_session SET expires_at = now() - interval '1 second' "
               "WHERE username='zt-idle' AND revoked_at IS NULL")
    assert client.get("/me", headers=h2).json()["code"] == "session_expired"


# ── 기기 결속·IP 위험 신호 ─────────────────────────────────
def test_token_replayed_from_another_device_revokes_session():
    _reg("zt-dev")
    c = TestClient(app)
    tok = c.post("/auth/login", json={"username": "zt-dev", "password": PW},
                 headers={"User-Agent": CHROME_129}).json()["token"]
    assert c.get("/me", headers={**_bearer(tok), "User-Agent": CHROME_130}).status_code == 200   # 브라우저 업데이트는 그대로
    r = c.get("/me", headers={**_bearer(tok), "User-Agent": FIREFOX})
    assert r.status_code == 401 and r.json()["code"] == "device_mismatch"
    assert c.get("/me", headers={**_bearer(tok), "User-Agent": CHROME_129}).json()["code"] == "session_revoked"
    ev = db.query("SELECT severity, response FROM security_event WHERE kind='device_mismatch'")
    assert ev and ev[0]["severity"] == "high" and "alert" in ev[0]["response"]


def test_ip_change_raises_risk_and_requires_step_up_for_money_moves():
    _reg("zt-ip")
    _reg("zt-ip-to")
    h = _h("zt-ip")
    client.get("/wallet", headers=h)                                    # 지갑(환영금) 생성
    _age_auth("zt-ip")
    assert client.post("/transfer", headers=h, json={"to": "zt-ip-to", "amount": 1000}).status_code == 200
    db.execute("UPDATE auth_session SET last_ip_net='203.0.113.0/24' WHERE username='zt-ip'")
    client.get("/wallet", headers=h)                                    # 다른 대역에서 온 요청 → 위험 신호
    assert "ip_change" in db.query("SELECT risk FROM auth_session WHERE username='zt-ip'")[0]["risk"]
    r = client.post("/transfer", headers=h, json={"to": "zt-ip-to", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    assert client.post("/auth/step-up", headers=h, json={"password": "wrong-pass-1"}).status_code == 401
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).json()["ok"]
    assert client.post("/transfer", headers=h, json={"to": "zt-ip-to", "amount": 1000}).status_code == 200
    assert not db.query("SELECT risk FROM auth_session WHERE username='zt-ip'")[0]["risk"]   # 재인증하면 해제


def test_large_transfer_and_account_deletion_need_recent_authentication():
    _reg("zt-big")
    _reg("zt-big-to")
    h = _h("zt-big")
    client.get("/wallet", headers=h)
    _age_auth("zt-big")
    r = client.post("/transfer", headers=h, json={"to": "zt-big-to", "amount": 1_000_000})
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    r = client.delete("/me", headers=h)
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    assert client.post("/transfer", headers=h, json={"to": "zt-big-to", "amount": 1_000_000}).status_code == 200
    assert client.delete("/me", headers=h).json()["ok"]
    assert not db.query("SELECT 1 FROM auth_session WHERE username='zt-big'")          # 탈퇴 → 세션 행 삭제
    _reg("zt-fresh")
    hf = _h("zt-fresh")
    r = client.delete("/me", headers=hf)                                # 방금 로그인했어도 — 로그인은 '추가 인증'이 아니다
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    assert client.post("/auth/step-up", headers=hf, json={"password": PW}).status_code == 200
    assert client.delete("/me", headers=hf).json()["ok"]


def test_stolen_session_right_after_login_cannot_skip_step_up():
    """로그인 직후 5분 안이라도, 다른 IP 대역(위험 신호)에서 온 송금·충전은 비밀번호 재확인이 먼저(2026-10-23 독립 점검)."""
    _reg("zt-steal")
    _reg("zt-steal-to")
    h = _h("zt-steal")                                                  # 피해자가 방금 로그인
    client.get("/wallet", headers=h)
    db.execute("UPDATE auth_session SET last_ip_net='198.51.100.0/24' WHERE username='zt-steal'")   # 다른 대역에서 쓰임
    client.get("/wallet", headers=h)
    r = client.post("/transfer", headers=h, json={"to": "zt-steal-to", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"


# ── 잠금·세션 관리 ─────────────────────────────────────────
def test_consecutive_failures_lock_account_without_leaking_existence():
    _reg("zt-lock")
    h = _h("zt-lock")
    for _ in range(settings.ACCOUNT_LOCK_THRESHOLD):
        assert _login("zt-lock", "wrong-pass-1").status_code == 401
    ratelimit.clear()                                                  # 메모리 시도 제한과 별개로 DB 잠금 확인
    r = _login("zt-lock", "wrong-pass-1")
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"   # 틀리면 잠금 여부를 말하지 않음
    r = _login("zt-lock")
    assert r.status_code == 403 and r.json()["code"] == "account_locked" and "분 뒤" in r.json()["detail"]
    assert client.get("/me", headers=h).json()["code"] in ("session_revoked", "account_locked")   # 잠금 = 전 세션 종료
    assert db.query("SELECT 1 FROM security_event WHERE kind='account_locked' AND username='zt-lock'")
    auth.unlock("zt-lock")
    assert _login("zt-lock").status_code == 200


def test_session_limit_listing_revocation_and_logout_all():
    _reg("zt-sess")
    toks = [_login("zt-sess").json()["token"] for _ in range(settings.MAX_SESSIONS_PER_USER + 1)]
    assert client.get("/me", headers=_bearer(toks[0])).json()["code"] == "session_revoked"   # 가장 오래된 것 종료
    items = client.get("/auth/sessions", headers=_bearer(toks[-1])).json()["items"]
    assert len(items) == settings.MAX_SESSIONS_PER_USER and sum(i["current"] for i in items) == 1
    assert all("token" not in i and "refresh_hash" not in i for i in items)
    other = next(i["session_id"] for i in items if not i["current"])
    assert client.delete(f"/auth/sessions/{other}", headers=_bearer(toks[-1])).json()["ok"]
    assert client.delete("/auth/sessions/not-mine", headers=_bearer(toks[-1])).status_code == 404
    assert client.post("/auth/logout-all", headers=_bearer(toks[-1])).json()["sessions"] >= 3
    assert client.get("/me", headers=_bearer(toks[-1])).status_code == 401


def test_logout_revokes_server_side_and_clears_cookies():
    _reg("zt-out")
    c = _web("zt-out")
    r = c.post("/auth/logout", headers=_csrf(c))
    assert r.status_code == 200 and any("Max-Age=0" in x for x in r.headers.get_list("set-cookie"))
    assert c.get("/me").status_code == 401
    assert db.query("SELECT revoke_reason FROM auth_session WHERE username='zt-out'")[0]["revoke_reason"] == "logout"


# ── 2단계 인증(TOTP) ──────────────────────────────────────
def _enroll(u, h, clock_box, monkeypatch=None):
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200   # 등록·해제는 항상 재확인
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    codes = client.post("/auth/mfa/confirm", headers=h,
                        json={"code": totp.code_at(s, totp.current_step())}).json()["recovery_codes"]
    return s, codes


def test_totp_enrollment_login_replay_recovery_and_encryption(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    _reg("zt-otp")
    h = _h("zt-otp")
    secret, codes = _enroll("zt-otp", h, clock_box)
    assert len(codes) == 10 and client.get("/auth/mfa", headers=h).json()["enabled"]
    assert db.query("SELECT secret_enc FROM user_mfa WHERE username='zt-otp'")[0]["secret_enc"].startswith("enc:v2:")
    r = _login("zt-otp")
    assert r.status_code == 401 and r.json()["code"] == "otp_required"
    used = totp.code_at(secret, totp.current_step())
    r = _login("zt-otp", otp=used)                                       # 등록에 쓴 코드를 다시(재사용)
    assert r.status_code == 401 and r.json()["code"] == "otp_invalid"
    clock_box[0] += 30
    j = _login("zt-otp", otp=totp.code_at(secret, totp.current_step())).json()
    assert j["mfa"] is True
    assert db.query("SELECT aal FROM auth_session WHERE username='zt-otp' ORDER BY created_at DESC LIMIT 1")[0]["aal"] == 2
    ratelimit.clear()
    assert _login("zt-otp", otp=codes[0]).status_code == 200            # 복구 코드 1회
    assert _login("zt-otp", otp=codes[0]).json()["code"] == "otp_invalid"


def test_mfa_setup_requires_recent_authentication():
    _reg("zt-otp2")
    h = _h("zt-otp2")
    _age_auth("zt-otp2")
    r = client.post("/auth/mfa/setup", headers=h)
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"


# ── 관리 권한: 자격 + JIT + OTP + 2인 승인 ──────────────────
def _admin(u, monkeypatch, clock_box, roles=("auditor",)):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {u})
    _reg(u)
    rbac.bootstrap()
    h = _h(u)
    _enroll(u, h, clock_box)                                             # 확인 시 이 세션 = AAL2
    for role in roles:
        r = client.post("/admin/elevate", headers=h, json={"role": role, "reason": "월간 보안 점검 수행"})
        assert r.status_code == 200, r.text
    return h


def test_jit_elevation_requires_eligibility_mfa_and_reason(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    monkeypatch.setattr(settings, "ADMIN_USERS", {"zt-boss"})
    _reg("zt-boss")
    rbac.bootstrap()
    h = _h("zt-boss")
    assert client.get("/admin/audit", headers=h).json()["code"] == "forbidden"          # 자격만으론 못 쓴다
    r = client.post("/admin/elevate", headers=h, json={"role": "auditor", "reason": "월간 보안 점검"})
    assert r.status_code == 403 and r.json()["code"] == "mfa_required"
    _enroll("zt-boss", h, clock_box)
    assert client.post("/admin/elevate", headers=h, json={"role": "auditor", "reason": "x"}).json()["code"] == \
        "reason_required"
    assert client.post("/admin/elevate", headers=h, json={"role": "auditor", "reason": "월간 보안 점검"}).status_code == 200
    assert client.get("/admin/audit", headers=h).status_code == 200
    assert client.get("/admin/audit/verify", headers=h).json()["ok"] is True
    assert client.get("/admin/users/zt-boss/pii", headers=h, params={"purpose": "본인 확인 테스트"}).json()["code"] \
        == "forbidden"                                                   # auditor 는 개인정보 열람 불가(역할 분리)
    db.execute("UPDATE role_activation SET expires_at = now() - interval '1 second'")
    assert client.get("/admin/audit", headers=h).json()["code"] == "forbidden"          # 시간이 지나면 자동 회수
    _reg("zt-nobody")
    r = client.post("/admin/elevate", headers=_h("zt-nobody"), json={"role": "auditor", "reason": "궁금해서요"})
    assert r.status_code == 403 and r.json()["code"] in ("not_eligible", "mfa_required")


def test_role_grant_needs_two_different_admins(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    h1 = _admin("zt-adm1", monkeypatch, clock_box, roles=("admin",))
    clock_box[0] += 60
    h2 = _admin("zt-adm2", monkeypatch, clock_box, roles=("admin",))
    _reg("zt-staff")
    rid = client.post("/admin/roles/requests", headers=h1,
                      json={"username": "zt-staff", "role": "auditor", "reason": "분기 감사 지원"}).json()["request_id"]
    r = client.post(f"/admin/roles/requests/{rid}/approve", headers=h1)
    assert r.status_code == 403 and r.json()["code"] == "four_eyes"
    assert client.post(f"/admin/roles/requests/{rid}/approve", headers=h2).json()["status"] == "approved"
    assert rbac.eligible("zt-staff") == {"auditor"}
    exp = db.query("SELECT expires_at FROM user_role WHERE username='zt-staff'")[0]["expires_at"]
    assert exp is not None                                               # 재검토 주기(만료)가 있다
    assert client.delete("/admin/roles/zt-adm1/admin", headers=h1).status_code == 400   # 자기 역할 회수 불가


def test_pii_view_needs_purpose_is_logged_and_reviewable(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    h = _admin("zt-sec", monkeypatch, clock_box, roles=("security_admin", "auditor"))
    _reg("zt-subject")
    r = client.get("/admin/users/zt-subject/pii", headers=h)
    assert r.status_code == 400 and r.json()["code"] == "purpose_required"
    r = client.get("/admin/users/zt-subject/pii", headers=h, params={"purpose": "고객 문의 확인(티켓 1234)"})
    assert r.status_code == 200 and r.json()["username"] == "zt-subject"
    log = db.query("SELECT operator, subject, purpose FROM pii_access_log")
    assert log == [{"operator": "zt-sec", "subject": "zt-subject", "purpose": "고객 문의 확인(티켓 1234)"}]
    rev = client.get("/admin/pii-access", headers=h).json()
    assert rev["total"] == 1 and rev["summary"][0]["operator"] == "zt-sec"
    assert db.query("SELECT 1 FROM security_event WHERE kind='pii_access' AND username='zt-sec'")


# ── 탐지·자동 대응 ─────────────────────────────────────────
def test_probing_other_peoples_sessions_auto_revokes_the_session():
    from app.collectors import marketgen
    from app.services import practice
    sc = next(x for x in practice.SCENARIOS if x["scenario_id"] == "samsung-golden")
    marketgen.seed_scenario_ohlc(sc, real=False)
    _reg("zt-victim")
    _reg("zt-prober")
    sid = client.post("/practice/start", headers=_h("zt-victim"), json={"scenario_id": "samsung-golden"}).json()[
        "session_id"]
    hp = _h("zt-prober")
    codes = [client.get(f"/practice/{sid}", headers=hp).status_code for _ in range(20)]
    assert codes.count(403) == 20
    r = client.get("/me", headers=hp)
    assert r.status_code == 401 and r.json()["code"] == "session_revoked"
    assert db.query("SELECT 1 FROM security_event WHERE kind='authz_deny' AND response LIKE '%revoke_session%'")


def test_credential_stuffing_from_one_ip_blocks_login_temporarily():
    for i in range(30):
        assert _login(f"nobody-{i}", "wrong-pass-1").status_code == 401
    r = _login("nobody-x", "wrong-pass-1")
    assert r.status_code == 429 and r.json()["code"] == "ip_blocked"
    assert client.get("/indices").status_code == 200                      # 공개 화면은 막지 않는다(로그인만)


def test_ip_block_applies_even_right_after_boot(monkeypatch):
    """차단 목록 캐시의 '읽은 적 없음'이 0.0 이면 부팅 30초 안(monotonic < 30)엔 목록을 안 읽어 차단이 안 먹었다(결함 19)."""
    from app.security import events
    monkeypatch.setattr(events, "_mono", lambda: 5.0)                     # 부팅 5초 뒤
    events.clear_cache()
    events.block_ip("iph-early", "test", 600)
    assert events.blocked("iph-early") is True
    assert events.blocked("iph-other") is False


# ── 비밀번호 정책·페퍼 ────────────────────────────────────
def test_password_policy():
    for bad in ("short1", "onlyletters", "1234567890", "Password1", "qwer1234!", "aaaa1111", "x" * 129):
        with pytest.raises(passwords.PolicyError):
            passwords.check_policy(bad)
    with pytest.raises(passwords.PolicyError):
        passwords.check_policy("kimchi-2026", username="kimchi@oos.io")
    with pytest.raises(passwords.PolicyError):
        passwords.check_policy("Same-pass-1", current="Same-pass-1")
    passwords.check_policy("Tr8vel-blue", username="kim@oos.io")
    r = client.post("/auth/register", json={"username": "zt-weak", "password": "password1"})
    assert r.status_code == 400 and "흔하게" in r.json()["detail"]


def test_pepper_hash_and_master_key_rotation(monkeypatch):
    k1, k2 = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))
    monkeypatch.setattr(settings, "APP_SECRET_KEY", k1)
    _reg("zt-pep")
    stored = db.query("SELECT password_hash FROM app_user WHERE username='zt-pep'")[0]["password_hash"]
    assert stored.startswith("pbkdf2_sha256p$") and keys.kid_of(base64.b64decode(k1)) in stored
    assert _login("zt-pep").status_code == 200
    monkeypatch.setattr(settings, "APP_SECRET_KEY", "")                  # DB 만 유출(키 없음) → 검증 불가
    assert _login("zt-pep").status_code == 401
    ratelimit.clear()
    monkeypatch.setattr(settings, "APP_SECRET_KEY", k2)                  # 키 교체: 새 키 + 예전 키(읽기만)
    monkeypatch.setattr(settings, "APP_SECRET_KEYS_OLD", k1)
    assert _login("zt-pep").status_code == 200
    up = db.query("SELECT password_hash FROM app_user WHERE username='zt-pep'")[0]["password_hash"]
    assert keys.kid_of(base64.b64decode(k2)) in up                       # 로그인 순간 새 키로 다시 저장
    monkeypatch.setattr(settings, "APP_SECRET_KEYS_OLD", "")
    assert _login("zt-pep").status_code == 200


# ── 요청 ID 로 이어지는 기록 ──────────────────────────────
def test_request_id_links_response_events_and_audit():
    _reg("zt-rid")
    r = _login("zt-rid", "wrong-pass-1")
    rid = r.headers["x-request-id"]
    assert db.query("SELECT 1 FROM security_event WHERE request_id=:r AND kind='login_fail'", {"r": rid})
    r = _login("zt-rid")
    rid = r.headers["x-request-id"]
    row = db.query("SELECT detail FROM audit_log WHERE action='auth.login' AND result='ok' ORDER BY audit_id DESC LIMIT 1")
    assert rid in row[0]["detail"]
    r = client.get("/indices", headers={"X-Request-ID": "upstream-req-12345678"})
    assert r.headers["x-request-id"] == "upstream-req-12345678"          # 앞단(nginx)이 준 ID 를 그대로 잇는다
    assert client.get("/indices", headers={"X-Request-ID": "bad id!"}).headers["x-request-id"] != "bad id!"


def test_ip_hash_uses_purpose_key():
    assert events.ip_hash("203.0.113.7") == keys.hmac_hex("ip", "203.0.113.7")
    assert keys.hmac_hex("ip", "1.2.3.4") != keys.hmac_hex("user", "1.2.3.4")   # 용도별 키 분리


def test_admin_actions_need_management_device_certificate_when_required(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    h = _admin("zt-mtls", monkeypatch, clock_box, roles=("auditor",))
    monkeypatch.setattr(settings, "ADMIN_REQUIRE_CLIENT_CERT", True)
    r = client.get("/admin/audit", headers=h)
    assert r.status_code == 403 and r.json()["code"] == "device_untrusted"
    r = client.get("/admin/audit", headers={**h, "X-Client-Cert-Verified": "FAILED:unable to verify"})
    assert r.status_code == 403
    assert client.get("/admin/audit", headers={**h, "X-Client-Cert-Verified": "SUCCESS"}).status_code == 200
    assert client.get("/me", headers=h).status_code == 200              # 일반 기능은 인증서와 무관
