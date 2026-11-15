"""HTTP 캐시 정책 — 공개 조회 ETag/304, 개인 응답 no-store(app/http_cache.py)."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import auth

client = TestClient(app)


def test_public_reads_get_etag_and_304_on_revalidation():
    r = client.get("/learn/dictionary")
    assert r.status_code == 200 and r.headers["etag"].startswith('W/"') and r.headers["cache-control"] == "no-cache"
    r2 = client.get("/learn/dictionary", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and r2.content == b""                       # 본문 0바이트
    r3 = client.get("/learn/dictionary", headers={"If-None-Match": 'W/"stale"'})
    assert r3.status_code == 200 and len(r3.content) > 1000


def test_personal_responses_are_never_stored():
    auth.register("hc-user", "Hc-pass-2026")
    tok = client.post("/auth/login", json={"username": "hc-user", "password": "Hc-pass-2026"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    for path in ("/me", "/wallet", "/watchlist"):
        r = client.get(path, headers=h)
        assert r.headers["cache-control"] == "no-store" and "etag" not in r.headers, path
    r = client.post("/auth/login", json={"username": "hc-user", "password": "Hc-pass-2026"})
    assert r.headers["cache-control"] == "no-store"                          # 토큰이 담긴 응답
    r = client.get("/indices", headers=h)                                     # 로그인해도 공개 데이터는 ETag
    assert "etag" in r.headers
