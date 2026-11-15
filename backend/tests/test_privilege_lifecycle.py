"""권한 생명주기 — 쓰지 않는 관리 자격 자동 회수 · 인사조치 일괄 회수(app/security/rbac.py).
근거: 전자금융감독규정 제13조 ①11호(인사조치 시 지체 없이 계정 삭제·사용 중지) · 금융보안 수준진단 2.3·3.2."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.config import settings
from app.security import assessment, rbac, totp
from app.services import auth

client = TestClient(app)
PW = "Life-pass-2026"


def _grant(user, role, by, days_ago, used_days_ago=None):
    at = clock.utc() - timedelta(days=days_ago)
    db.execute("INSERT INTO user_role(username, role, granted_by, approved_by, granted_at, expires_at, reason) "
               "VALUES(:u, :r, :by, 'appr', :at, :exp, '시험 부여')",
               {"u": user, "r": role, "by": by, "at": at, "exp": clock.utc() + timedelta(days=30)})
    if used_days_ago is not None:
        u = clock.utc() - timedelta(days=used_days_ago)
        db.execute("INSERT INTO role_activation(username, role, session_id, reason, activated_at, expires_at) "
                   "VALUES(:u, :r, 'sess-old', '지난 점검', :a, :e)", {"u": user, "r": role, "a": u, "e": u + timedelta(minutes=30)})


def test_dormant_admin_eligibility_is_revoked_but_bootstrap_is_flagged(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda key, title, msg="", **k: sent.append(key) or {"sent": []})
    for u in ("lc-idle", "lc-busy", "lc-boot", "lc-new"):
        auth.register(u, PW)
    _grant("lc-idle", "auditor", "lc-req", 70)                          # 70일 전 부여 · 한 번도 안 켬 → 회수
    _grant("lc-busy", "auditor", "lc-req", 70, used_days_ago=10)        # 열흘 전에 켬 → 유지
    _grant("lc-boot", "admin", "bootstrap", 70)                         # 비상용(부트스트랩) → 표시만
    _grant("lc-new", "security_admin", "lc-req", 10)                    # 부여한 지 열흘 → 유지
    st = {p["name"]: p for p in [assessment._run_probe("dormant_roles")]}
    assert st["dormant_roles"]["status"] == "fail"                      # 회수 전: 운영 점검이 잡는다
    r = rbac.sweep_dormant()
    assert r["revoked"] == ["lc-idle:auditor"] and r["bootstrap_dormant"] == ["lc-boot:admin"] and r["days"] == 60
    left = {(x["username"], x["role"]) for x in db.query("SELECT username, role FROM user_role")}
    assert left == {("lc-busy", "auditor"), ("lc-boot", "admin"), ("lc-new", "security_admin")}
    assert db.query("SELECT target FROM audit_log WHERE action='rbac.dormant_revoke'")[0]["target"] == "lc-idle:auditor"
    assert sent == ["rbac:dormant"]
    assert assessment._run_probe("dormant_roles")["status"] == "ok"
    ov = {(x["username"], x["role"]): x for x in rbac.overview()["roles"]}
    assert ov[("lc-boot", "admin")]["break_glass"] and ov[("lc-busy", "auditor")]["last_used"]
    assert rbac.sweep_dormant()["revoked"] == []                        # 두 번 돌려도 같은 결과(멱등)


def _bearer(name):
    return {"Authorization": "Bearer " + client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]}


def _security_admin(name, monkeypatch, clock_box):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {name})
    auth.register(name, PW)
    h = _bearer(name)
    rbac.bootstrap()
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    client.post("/auth/mfa/confirm", headers=h, json={"code": totp.code_at(s, totp.current_step())})
    clock_box[0] += 30
    assert client.post("/admin/elevate", headers=h, json={"role": "security_admin", "reason": "인사조치 처리"}).status_code == 200
    return h


def test_offboarding_revokes_everything_in_one_step(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    op = _security_admin("lc-op", monkeypatch, clock_box)
    auth.register("lc-leaver", PW)
    auth.register("lc-third", PW)
    leaver = _bearer("lc-leaver")
    _grant("lc-leaver", "auditor", "lc-req", 5)
    _grant("lc-leaver", "admin", "lc-req", 5, used_days_ago=0)
    db.execute("UPDATE role_activation SET expires_at = now() + interval '20 minutes' WHERE username='lc-leaver'")
    for who, by in (("lc-leaver", "lc-op"), ("lc-third", "lc-leaver")):        # 대상인 요청 · 본인이 올린 요청
        db.execute("INSERT INTO role_request(username, role, reason, requested_by, requested_at, status) "
                   "VALUES(:u, 'auditor', '분기 점검 지원', :by, now(), 'pending')", {"u": who, "by": by})
    assert client.post("/admin/users/lc-leaver/offboard", headers=op, json={"reason": "x"}).status_code == 422
    assert client.post("/admin/users/lc-op/offboard", headers=op, json={"reason": "본인 퇴직 처리 시도"}).json()["code"] == \
        "self_offboard"                                                          # 자기 자신은 다른 관리자가
    r = client.post("/admin/users/lc-leaver/offboard", headers=op, json={"reason": "2026-11-02 퇴직(인사 발령)"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["roles_revoked"] == ["admin", "auditor"] and b["activations_revoked"] == 1 and b["requests_rejected"] == 2
    assert b["sessions_revoked"] >= 1 and b["disabled"] and not b["bootstrap_listed"]
    assert not db.query("SELECT 1 FROM user_role WHERE username='lc-leaver'")
    assert not db.query("SELECT 1 FROM role_activation WHERE username='lc-leaver' AND revoked_at IS NULL")
    assert {x["status"] for x in db.query("SELECT status FROM role_request")} == {"rejected"}
    assert client.get("/wallet", headers=leaver).status_code == 401                    # 쓰던 토큰은 즉시 무효
    login = client.post("/auth/login", json={"username": "lc-leaver", "password": PW})
    assert login.status_code in (401, 403) and "중지" in login.text                    # 계정 중지
    au = db.query("SELECT actor, target FROM audit_log WHERE action='rbac.offboard'")[0]
    assert au == {"actor": "lc-op", "target": "lc-leaver"}
    plain = _bearer("lc-third")
    assert client.post("/admin/users/lc-op/offboard", headers=plain, json={"reason": "권한 없는 시도"}).status_code == 403


def test_disabled_account_is_not_regranted_by_bootstrap(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {"lc-boot2"})
    auth.register("lc-boot2", PW)
    rbac.bootstrap()
    assert rbac.eligible("lc-boot2") == set(rbac.ROLES)
    from app.security.context import Principal
    r = rbac.offboard(Principal(kind="user", name="lc-op2"), "lc-boot2", "전보(보안 업무 제외)")
    assert r["bootstrap_listed"] and "ADMIN_USERS" in r["warning"]
    rbac.bootstrap()                                                     # 재기동해도
    assert rbac.eligible("lc-boot2") == set()                            # 중지된 계정에는 다시 주지 않는다
