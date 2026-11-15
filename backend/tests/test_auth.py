from fastapi.testclient import TestClient
from app.api import app

client = TestClient(app)


def test_register_and_login_returns_token():
    assert client.post("/auth/register", json={"username": "kim", "password": "pw123456"}).status_code == 200
    r = client.post("/auth/login", json={"username": "kim", "password": "pw123456"})
    assert r.status_code == 200 and "token" in r.json()


def test_duplicate_register_rejected():
    client.post("/auth/register", json={"username": "kim", "password": "pw123456"})
    assert client.post("/auth/register", json={"username": "kim", "password": "pw123456"}).status_code == 400


def test_wrong_password_rejected():
    client.post("/auth/register", json={"username": "lee", "password": "correct1"})
    assert client.post("/auth/login", json={"username": "lee", "password": "wrong"}).status_code == 401


def test_frontend_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "OUTOFSTOCK" in r.text          # 웹 화면(팀 확정 디자인 web/)
    assert client.get("/app", follow_redirects=False).headers["location"] == "/"   # 예전 주소는 새 첫 화면으로


def test_password_hash_format_and_transparent_upgrade_of_legacy_hashes(monkeypatch):
    # 반복 횟수를 해시와 함께 저장 → 기준(OWASP 2023: 600,000)을 올려도 예전 해시는 그대로 검증되고,
    # 로그인에 성공하는 순간 새 기준·새 솔트로 다시 저장된다(전원 비밀번호 재설정 없이 이행)
    import secrets as _s
    from app import db
    from app.config import settings
    from app.services import auth
    auth.register("hashfmt@oos.io", "Tr8vel-blue")
    row = db.query("SELECT password_hash FROM app_user WHERE username='hashfmt@oos.io'")[0]
    assert row["password_hash"].startswith(f"pbkdf2_sha256${settings.PASSWORD_HASH_ITERATIONS}$")
    salt = _s.token_hex(16)                                       # 예전 형식(접두어 없음, 100,000회)으로 저장된 사용자
    db.execute("INSERT INTO app_user(username, password_hash, salt, created_at) VALUES('legacy@oos.io', :h, :s, "
               "'2026-09-01T00:00:00')", {"h": auth._hash("oldpass123", salt, auth.LEGACY_ITERATIONS), "s": salt})
    try:
        auth.login("legacy@oos.io", "wrongpass1")
        raise AssertionError("틀린 비밀번호는 실패해야 함")
    except auth.AuthError:
        pass
    assert db.query("SELECT salt FROM app_user WHERE username='legacy@oos.io'")[0]["salt"] == salt   # 실패 땐 그대로
    assert auth.login("legacy@oos.io", "oldpass123")["token"]
    up = db.query("SELECT password_hash, salt FROM app_user WHERE username='legacy@oos.io'")[0]
    assert up["password_hash"].startswith("pbkdf2_sha256$") and up["salt"] != salt
    assert auth.login("legacy@oos.io", "oldpass123")["token"]                 # 상향 뒤에도 같은 비밀번호로
    monkeypatch.setattr(settings, "PASSWORD_HASH_ITERATIONS", settings.PASSWORD_HASH_ITERATIONS * 2)   # 기준 상향
    auth.login("hashfmt@oos.io", "Tr8vel-blue")
    assert db.query("SELECT password_hash FROM app_user WHERE username='hashfmt@oos.io'")[0]["password_hash"] \
        .startswith(f"pbkdf2_sha256${settings.PASSWORD_HASH_ITERATIONS}$")
    assert auth.change_password("hashfmt@oos.io", "Tr8vel-blue", "newpass456")["ok"]
    assert auth.login("hashfmt@oos.io", "newpass456")["token"]


def test_register_enforces_same_password_rule_as_ui_and_change():
    # 예전: 화면·비밀번호 변경은 8자 이상인데 가입 API 는 1자도 받았다(React 등 다른 화면에서 약한 비밀번호 가입 가능)
    assert client.post("/auth/register", json={"username": "short@oos.io", "password": "pw12345"}).status_code == 400
    assert client.post("/auth/register", json={"username": "long@oos.io", "password": "x" * 129}).status_code == 400
    assert client.post("/auth/register", json={"username": "ok@oos.io", "password": "pw123456"}).status_code == 200


def test_usernames_differing_only_in_case_cannot_both_register():
    # 예전: 'Alice' 가 있어도 'alice' 로 가입 가능 → 송금·채팅·친구 요청에서 같은 사람처럼 보이는 계정(사칭)
    assert client.post("/auth/register", json={"username": "Alice", "password": "pw123456"}).status_code == 200
    r = client.post("/auth/register", json={"username": "alice", "password": "pw123456"})
    assert r.status_code == 400 and "대소문자" in r.json()["detail"]
    assert client.post("/auth/register", json={"username": "ALICE", "password": "pw123456"}).status_code == 400
    assert client.post("/auth/login", json={"username": "Alice", "password": "pw123456"}).status_code == 200
