"""2026-10 전체 검토 — 인증·API 입력 처리 회귀 테스트."""
import threading

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.services import auth as auth_svc
from app.services import ratelimit, account

client = TestClient(app)


def _login(u, pw="pass1234"):
    return client.post("/auth/login", json={"username": u, "password": pw})


def test_signup_rules_match_password_change():
    r = client.post("/auth/register", json={"username": "shortpw", "password": "p"})
    assert r.status_code == 400 and "8자" in r.json()["detail"]
    assert client.post("/auth/register", json={"username": "okpw", "password": "pwrule12"}).status_code == 200
    r = client.post("/auth/register", json={"username": "weakpw", "password": "p" * 8})     # 영문+숫자 규칙·반복 금지
    assert r.status_code == 400 and r.json()["detail"]


def test_username_lookalikes_and_unaddressable_names_rejected():
    assert client.post("/auth/register", json={"username": "Alice1", "password": "pass1234"}).status_code == 200
    r = client.post("/auth/register", json={"username": "ALICE1", "password": "pass1234"})
    assert r.status_code == 400                                        # 대소문자만 다른 사칭 차단
    for bad in ("..", "...", "\u3164\u3164", "_-", "a\u3164"):           # 한글 채움 문자(보이지 않음)
        assert client.post("/auth/register", json={"username": bad, "password": "pass1234"}).status_code == 400
    assert _login("alice1").status_code == 200                         # 로그인은 대소문자 무관
    assert _login("alice1").json()["username"] == "Alice1"


def test_concurrent_duplicate_register_is_400_not_500(monkeypatch):
    real_query = db.query

    def no_precheck(sql, params=None):                                 # 동시 가입 — 사전 확인을 둘 다 통과한 상황
        if "FROM app_user WHERE lower(username)" in sql:
            return []
        return real_query(sql, params)
    auth_svc.register("dup-user", "pass1234")
    monkeypatch.setattr(db, "query", no_precheck)
    r = client.post("/auth/register", json={"username": "dup-user", "password": "pass1234"})
    assert r.status_code == 400 and "이미" in r.json()["detail"]


def test_password_change_blocks_login_that_read_old_hash(monkeypatch):
    """비밀번호를 바꾸는 순간 옛 비밀번호로 로그인하던 요청 — 해시 확인 뒤·토큰 발급 전에 바뀌면 토큰을 안 준다."""
    auth_svc.register("pwrace", "oldpass12")
    tok = _login("pwrace", "oldpass12").json()["token"]
    h = {"Authorization": "Bearer " + tok}
    real_find = auth_svc._find_user

    def find_then_change(u):
        row = real_find(u)                                             # 옛 해시를 읽은 직후
        auth_svc.change_password("pwrace", "oldpass12", "newpass12")   # 주인이 비밀번호 변경
        return row
    monkeypatch.setattr(auth_svc, "_find_user", find_then_change)
    assert _login("pwrace", "oldpass12").status_code == 401
    monkeypatch.setattr(auth_svc, "_find_user", real_find)
    assert client.get("/me", headers=h).status_code == 401            # 기존 토큰도 무효
    assert _login("pwrace", "newpass12").status_code == 200


def test_deleted_account_tokens_are_dead():
    auth_svc.register("gone", "pass1234")
    tok = _login("gone").json()["token"]
    db.execute("DELETE FROM app_user WHERE username='gone'")          # 토큰만 남은 상황
    assert auth_svc.user_for_token(tok) is None


def test_limiter_counts_before_checking_under_concurrency():
    key, n, limit = ratelimit.key_of("t", "x"), 40, 5
    gate, passed = threading.Barrier(n), []

    def go():
        gate.wait()
        if ratelimit.take([(key, limit)], 600) == 0:
            passed.append(1)
    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(passed) == limit                                        # 예전엔 16개가 통과


def test_limiter_keys_are_bounded_and_swept(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit, "_clock", lambda: now[0])
    r = client.post("/auth/login", json={"username": "u" * 1_000_000, "password": "x"})
    assert r.status_code in (413, 422)                                 # 거대한 아이디는 제한 키에 닿기 전에 거절
    r = client.post("/auth/login", json={"username": "u" * 5_000, "password": "x"})
    assert r.status_code == 422                                        # (본문 한도 안이어도 길이 초과는 요청 단계에서)
    for i in range(50):
        _login(f"nobody{i}@oos.io", "wrongpass")
    assert all(len(k) == 32 for k in ratelimit._hits)                  # 키는 고정 길이 해시
    now[0] += 7200
    ratelimit.take([(ratelimit.key_of("any"), 5)], 600)
    assert len(ratelimit._hits) == 1                                   # 만료된 키는 개수와 무관하게 정리


def test_change_password_attempts_are_limited(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILS", 3)
    auth_svc.register("chpw", "pass1234")
    h = {"Authorization": "Bearer " + _login("chpw").json()["token"]}
    body = {"old_password": "wrongpass", "new_password": "newpass12"}
    codes = [client.post("/auth/change-password", headers=h, json=body).status_code for _ in range(4)]
    assert codes == [400, 400, 400, 429]


def test_lone_surrogate_and_oversized_inputs_are_4xx():
    raw = '{"username":"surr1","password":"abc\\ud800defgh"}'
    hdr = {"Content-Type": "application/json"}
    assert client.post("/auth/register", content=raw, headers=hdr).status_code == 422
    assert client.post("/auth/login", content=raw, headers=hdr).status_code == 422
    auth_svc.register("big", "pass1234")
    h = {"Authorization": "Bearer " + _login("big").json()["token"]}
    assert client.post("/learn/log", headers=h, json={"kind": "k" * 21}).status_code == 422
    assert client.post("/learn/log", headers=h, json={"kind": "term", "item_key": "k" * 61}).status_code == 422
    assert client.get("/me/logs/access?offset=9223372036854775808", headers=h).status_code == 200
    assert client.get("/me/logs/access?offset=99999999999", headers=h).json()["items"] == []


def test_cors_is_same_origin_only_by_default():
    r = client.get("/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in r.headers               # 기본: 교차 출처 허용 없음
    r = client.options("/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_health_is_503_when_db_down(monkeypatch):
    from app.services import monitoring

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(monitoring.db, "query", boom)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["db_ok"] is False


def test_products_accept_survey_keys():
    assert client.get("/reco/products?profile=aggressive").json()["profile"] == "공격형"
    assert client.get("/reco/products?profile=neutral").json()["profile"] == "중립형"


def test_timestamps_are_kst_even_when_server_tz_is_utc(monkeypatch):
    import os
    import subprocess
    import sys
    import app as app_pkg
    root = os.path.dirname(os.path.dirname(app_pkg.__file__))
    code = ("from app import clock; from app.services import quotes;"
            "a=clock.stamp(); b=quotes.iso_now()[:19]; print(a[:16]==b[:16])")
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "TZ": "UTC"}).stdout.strip()
    assert out == "True"                                               # 마이페이지 시각 = 채팅 시각(KST)


def test_account_delete_locks_user_first():
    auth_svc.register("del-me", "pass1234")
    tok = _login("del-me").json()["token"]
    assert account.delete_account("del-me")["ok"]
    assert auth_svc.user_for_token(tok) is None and _login("del-me").status_code == 401
