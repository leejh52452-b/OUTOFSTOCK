"""인증→인가 강화 — 토큰 만료, Bearer 가드, 세션 소유권(본인 자원만).
AUTH_ENFORCED 로 보호를 켜고 끈다(개발/데모는 off, 운영은 on)."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import auth as auth_svc

client = TestClient(app)


def _token(username="alice", password="pw123456"):
    client.post("/auth/register", json={"username": username, "password": password})
    return client.post("/auth/login", json={"username": username, "password": password}).json()["token"]


def test_expired_token_is_rejected():
    """TTL 지난 토큰은 만료 처리(None)."""
    old = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    db.execute("INSERT INTO auth_token(token, username, created_at) VALUES(:t,'kim',:c)",
               {"t": auth_svc._tok("tok-old"), "c": old})       # 저장은 해시로
    assert auth_svc.user_for_token("tok-old") is None          # 24h TTL 초과
    fresh = _token("kim2")
    assert auth_svc.user_for_token(fresh) == "kim2"            # 방금 발급 → 유효


