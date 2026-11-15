"""보안 설정 — 보안 헤더 · /docs 숨김 · 로그인/가입 시도 제한 · 토큰 해시 저장 · 로그아웃 무효화 ·
비로그인 요청의 이름 사칭 차단 · 계정 세션 보호 · 레거시 백테스트 소유권."""
from fastapi.testclient import TestClient

from app import db
from app.api import app, _docs_kwargs
from app.config import settings
from app.services import auth as auth_svc
from app.services import ratelimit

client = TestClient(app)


def _auth(u, pw="pass1234"):
    client.post("/auth/register", json={"username": u, "password": pw})
    tok = client.post("/auth/login", json={"username": u, "password": pw}).json()["token"]
    return {"Authorization": "Bearer " + tok}, tok


# ── 헤더 · 문서 ───────────────────────────────────────────
def test_security_headers_on_every_response(monkeypatch):
    for path in ("/health", "/", "/nope-404"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "SAMEORIGIN"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin" and "camera=()" in h["permissions-policy"]
        assert "strict-transport-security" not in h
        csp = h["content-security-policy"]                         # 기본 CSP(3번 정보보호) — 외부 스크립트·연결 차단
        assert "default-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert "content-security-policy" not in client.get("/docs").headers      # Swagger(CDN)는 예외
    monkeypatch.setattr(settings, "SECURITY_CSP", "off")
    assert "content-security-policy" not in client.get("/").headers
    monkeypatch.setattr(settings, "SECURITY_HSTS", True)
    monkeypatch.setattr(settings, "SECURITY_CSP", "frame-ancestors 'none'")
    h = client.get("/health").headers
    assert h["strict-transport-security"].startswith("max-age=") and h["content-security-policy"] == "frame-ancestors 'none'"


def test_docs_can_be_hidden():
    assert _docs_kwargs(True) == {}
    assert _docs_kwargs(False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    from fastapi import FastAPI
    hidden = TestClient(FastAPI(**_docs_kwargs(False)))
    assert hidden.get("/docs").status_code == 404 and hidden.get("/openapi.json").status_code == 404


# ── 시도 제한 ─────────────────────────────────────────────
def test_login_locks_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILS", 3)
    _auth("lockme@oos.io")
    bad = {"username": "lockme@oos.io", "password": "wrong-pass"}
    assert [client.post("/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
    r = client.post("/auth/login", json=bad)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and "분 뒤" in r.json()["detail"]
    ok = client.post("/auth/login", json={"username": "lockme@oos.io", "password": "pass1234"})
    assert ok.status_code == 429                                  # 잠긴 동안은 맞는 비번도 기다려야 함
    assert client.post("/auth/login", json={"username": "other@oos.io", "password": "x"}).status_code == 401


def test_login_window_expires_and_success_resets(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILS", 3)
    now = [1000.0]
    monkeypatch.setattr(ratelimit, "_clock", lambda: now[0])
    _auth("win@oos.io")
    bad = {"username": "win@oos.io", "password": "nope"}
    for _ in range(3):
        client.post("/auth/login", json=bad)
    assert client.post("/auth/login", json=bad).status_code == 429
    now[0] += settings.LOGIN_WINDOW_SECONDS + 1                    # 창이 지나면 풀린다
    assert client.post("/auth/login", json={"username": "win@oos.io", "password": "pass1234"}).status_code == 200
    for _ in range(2):                                            # 성공하면 그 아이디 기록은 초기화
        client.post("/auth/login", json=bad)
    assert client.post("/auth/login", json=bad).status_code == 401


def test_login_spray_across_usernames_limited_per_ip(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILS_PER_IP", 4)
    codes = [client.post("/auth/login", json={"username": f"u{i}@oos.io", "password": "x"}).status_code
             for i in range(5)]
    assert codes == [401, 401, 401, 401, 429]


def test_register_limited_per_ip(monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_MAX_PER_IP", 3)
    codes = [client.post("/auth/register", json={"username": f"r{i}@oos.io", "password": "pass1234"}).status_code
             for i in range(4)]
    assert codes == [200, 200, 200, 429]


# ── 토큰 ──────────────────────────────────────────────────
def test_token_stored_as_hash_and_logout_revokes():
    h, tok = _auth("tok@oos.io")
    stored = [r["token"] for r in db.query("SELECT token FROM auth_token WHERE username='tok@oos.io'")]
    assert tok not in stored and auth_svc._tok(tok) in stored     # 원문은 DB 에 없다
    h2, tok2 = _auth("tok@oos.io")                                 # 두 번째 기기
    assert client.get("/me", headers=h).status_code == 200
    assert client.post("/auth/logout", headers=h).json()["ok"]
    assert client.get("/me", headers=h).status_code == 401        # 로그아웃한 토큰은 즉시 무효
    assert client.get("/me", headers=h2).status_code == 200       # 다른 기기 로그인은 유지


def test_guest_names_reserved():
    for name in ("guest", "Guest-1234abcd", "guest-anything"):
        assert client.post("/auth/register", json={"username": name, "password": "pass1234"}).status_code == 400


# ── 비로그인 사칭 · 세션 보호 ─────────────────────────────
def test_anonymous_cannot_act_as_account():
    h, _ = _auth("victim@oos.io")
    from app.services import practice
    from app.collectors import marketgen
    sc = next(x for x in practice.SCENARIOS if x["scenario_id"] == "samsung-golden")
    marketgen.seed_scenario_ohlc(sc, real=False)
    mine = client.post("/practice/start", headers=h, json={"scenario_id": "samsung-golden"}).json()["session_id"]
    assert practice.owner_of(mine) == "victim@oos.io"
    assert client.get("/practice/home?user_id=victim@oos.io").json()["active"] is None   # 남의 진행 상태를 못 엿봄
    assert client.get("/practice/home", headers=h).json()["active"]                      # 본인은 보임
    r = client.post("/practice/start", json={"user_id": "victim@oos.io", "scenario_id": "samsung-golden"})
    owner = practice.owner_of(r.json()["session_id"])
    assert r.status_code == 200 and auth_svc.is_guest_id(owner) and owner != "victim@oos.io"   # 본문 user_id 무시
    assert client.post(f"/practice/{mine}/step", json={"days": 1}).status_code == 401
    assert client.post(f"/practice/{mine}/step", headers=h, json={"days": 1}).status_code == 200


def test_account_session_needs_its_token():
    h, _ = _auth("own@oos.io")
    from app.services import practice
    from app.collectors import marketgen
    sc = next(x for x in practice.SCENARIOS if x["scenario_id"] == "samsung-golden")
    marketgen.seed_scenario_ohlc(sc, real=False)
    sid = client.post("/practice/start", headers=h, json={"scenario_id": "samsung-golden"}).json()["session_id"]
    assert client.get(f"/practice/{sid}").status_code == 401       # 비로그인은 남의 계정 세션에 못 들어감
    assert client.post(f"/practice/{sid}/step", json={"days": 1}).status_code == 401
    assert client.get(f"/practice/{sid}", headers=h).status_code == 200


def test_browser_guest_ids_are_separate():
    a = "guest-aaaaaaaa11"
    assert auth_svc.is_guest_id(a) and not auth_svc.is_guest_id("guest-x") and not auth_svc.is_guest_id("kim")
    from app.services import practice
    from app.collectors import marketgen
    sc = next(x for x in practice.SCENARIOS if x["scenario_id"] == "samsung-golden")
    marketgen.seed_scenario_ohlc(sc, real=False)
    ca, cb = TestClient(app), TestClient(app)                       # 서로 다른 브라우저(쿠키 저장소)
    sid = ca.post("/practice/start", json={"user_id": a, "scenario_id": "samsung-golden"}).json()["session_id"]
    owner = practice.owner_of(sid)
    assert auth_svc.is_guest_id(owner) and owner != a               # 클라이언트가 고른 ID 가 아니라 서버 발급 쿠키 ID
    assert ca.get(f"/practice/{sid}").status_code == 200            # 그 게스트(같은 쿠키)는 이어서 진행
    assert cb.get(f"/practice/{sid}").status_code == 403            # 다른 게스트는 번호를 알아도 못 연다
    assert cb.post(f"/practice/{sid}/step", json={"days": 1}).status_code == 403
    assert cb.get(f"/practice/home?user_id={owner}").json()["active"] is None   # user_id 로 엿보기 불가
    assert ca.get("/practice/home").json()["active"]


def test_list_limits_are_capped():
    for i in range(3):
        db.execute("INSERT INTO news(title, url) VALUES(:t, :u)", {"t": f"n{i}", "u": f"https://ex.com/{i}"})
    r = client.get("/news?limit=1000000")
    assert r.status_code == 200 and len(r.json()["items"]) == 3
    assert client.get("/search/popular?limit=999999").status_code == 200
    assert client.get("/search/popular?limit=-5&days=-1").status_code == 200


# ── 비밀값 가리기(3차 점검) — 키를 URL 쿼리로 보내는 API(DART·Finnhub·수출입은행) 예외 문자열 ──
def test_api_keys_never_leak_to_logs_or_public_health(monkeypatch, caplog):
    import logging

    import requests

    from app.config import settings
    from app.redact import install_log_redaction, redact
    from app.services import monitoring
    monkeypatch.setattr(settings, "DART_API_KEY", "dartSECRET123456")
    err = requests.HTTPError("404 Client Error: Not Found for url: https://opendart.fss.or.kr/api/"
                             "fnlttSinglAcnt.json?crtfc_key=dartSECRET123456&corp_code=00126380")
    assert "dartSECRET123456" not in redact(str(err)) and "crtfc_key=***" in redact(str(err))
    assert redact("wss://ws.finnhub.io?token=abcDEF987654") == "wss://ws.finnhub.io?token=***"
    install_log_redaction()
    with caplog.at_level(logging.WARNING):
        logging.getLogger("app.retry").warning("재시도 1/3 [_get]: %s", err)
        try:
            raise err
        except requests.HTTPError:
            logging.getLogger("app.collectors.dart").exception("재무 실패")
    assert "dartSECRET123456" not in caplog.text
    run_id = monitoring.start_run()                                   # 예전 방식으로 저장된 메모(키 포함)도
    monitoring.finish_run(run_id, "FAILED", note=f"run.py real 실패: {err}")
    body = client.get("/health").text
    assert "dartSECRET123456" not in body and "crtfc_key" not in body   # 공개 /health 엔 내부 오류 문구 자체가 없다
    import json as _json
    detail = _json.dumps(monitoring.health(), ensure_ascii=False)      # 관리자 상세(/admin/health)에서도 가려진다
    assert "dartSECRET123456" not in detail and "crtfc_key=***" in detail


def test_no_static_page_renders_external_text_as_html():
    # 예전 /static/legacy.html 은 뉴스 제목(외부 RSS)을 이스케이프 없이 innerHTML 로 그려 저장형 XSS
    # (같은 출처의 /app 로그인 토큰 탈취 가능) → 이동 페이지로 대체. 정적 폴더의 다른 페이지도 같은 패턴 금지.
    import re
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    assert not (static / "legacy.html").exists()                       # 옛 대시보드는 삭제(7차)
    assert client.get("/static/legacy.html").status_code == 404
    for page in static.glob("*.html"):
        src = page.read_text(encoding="utf-8")
        # 뉴스 제목·링크를 그대로 꽂는 템플릿(${n.title} / href="${n.url}") 금지 — esc()/safeUrl() 을 거쳐야 함
        assert not re.search(r"\$\{\s*n\.title\s*\}|href=\"\$\{\s*n\.url\s*\}\"", src), page.name
