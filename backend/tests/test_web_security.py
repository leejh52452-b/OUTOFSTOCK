"""웹 화면(web/ — 팀 확정 디자인) 보안 — 요청마다 nonce CSP · 인라인 핸들러 0개(data-on: 화면이 등록한 함수만) ·
신뢰 타입 · 외부 스크립트 0 · CSP 위반 보고 · 쿠키 세션의 로그아웃(접근 토큰 만료 뒤에도 서버에서 끝남) · '로그인 유지'."""
import json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app import db, webui
from app.api import app
from app.config import settings
from app.services import auth

client = TestClient(app)
WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = {p.stem: p.read_text(encoding="utf-8") for p in sorted(WEB.glob("*.html"))}
RUNTIME = (WEB / "assets" / "oos" / "runtime.js").read_text(encoding="utf-8")
SCRIPTS = {p.relative_to(WEB).as_posix(): p.read_text(encoding="utf-8") for p in sorted(WEB.glob("assets/**/*.js"))}
URL = {"index": "/", "learn": "/learn.html", "news": "/news.html", "practice": "/practice.html", "mypage": "/mypage.html",
       "stocks": "/stocks.html", "avatar": "/avatar.html", "invest": "/invest.html"}
PW = "Web-pass-2026"


# ── data-on 문법(화면의 onParse 와 같은 규칙) — 정적 검사용 파이썬 판 ─────────────────
class _P:
    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def peek(self, c):
        self.ws()
        return self.s.startswith(c, self.i)

    def eat(self, c):
        if not self.peek(c):
            raise SyntaxError(f"'{c}' 필요 @{self.i}: {self.s}")
        self.i += len(c)

    def ident(self):
        self.ws()
        m = re.match(r"[A-Za-z_$][\w$]*", self.s[self.i:])
        if not m:
            raise SyntaxError(f"이름 @{self.i}: {self.s}")
        self.i += len(m.group())
        return m.group()

    def items(self, close, item):
        out = []
        if self.peek(close):
            return out
        while True:
            out.append(item())
            if not self.peek(","):
                return out
            self.eat(",")

    def primary(self):
        self.ws()
        c = self.s[self.i:self.i + 1]
        if c in ("'", '"'):
            self.i += 1
            while self.i < len(self.s) and self.s[self.i] != c:
                self.i += 2 if self.s[self.i] == "\\" else 1
            self.eat(c)
            return
        if c == "-" or c.isdigit():
            m = re.match(r"-?\d+(\.\d+)?", self.s[self.i:])
            if not m:
                raise SyntaxError("숫자")
            self.i += len(m.group())
            return
        if c == "{":
            self.eat("{")
            self.items("}", lambda: (self.ident(), self.eat(":"), self.value()))
            self.eat("}")
            return
        name = self.ident()
        if name in ("null", "true", "false", "event"):
            return
        if name == "this":
            if self.peek("."):
                self.eat(".")
                assert self.ident() == "dataset", self.s
                self.eat(".")
                self.ident()
            return
        raise SyntaxError(f"허용되지 않은 값 {name}: {self.s}")

    def value(self):
        self.primary()
        if self.peek("?"):
            self.eat("?")
            self.value()
            self.eat(":")
            self.value()

    def program(self):
        calls, sets = [], []
        self.ws()
        while self.i < len(self.s):
            name = self.ident()
            if name == "event":
                self.eat(".")
                assert self.ident() in ("preventDefault", "stopPropagation"), self.s
                self.eat("(")
                self.eat(")")
            elif self.peek("="):
                self.eat("=")
                self.value()
                sets.append(name)
            else:
                self.eat("(")
                self.items(")", self.value)
                self.eat(")")
                calls.append(name)
            self.ws()
            if self.i < len(self.s):
                self.eat(";")
                self.ws()
        return calls, sets


def _programs(src):
    """한 화면의 모든 data-on / data-on-submit / data-on-self 값 — 템플릿 보간(${…})은 값 자리 표본으로 바꿔 문법만 본다."""
    out = []
    for m in re.finditer(r'data-on(?:-submit|-self)?="([^"]*)"', src):
        v = re.sub(r"'\$\{[^}]*\}'", "'x'", m.group(1))
        v = re.sub(r"\$\{[^}]*\}", "0", v)
        out.append(v.replace("\\'", "'"))
    return out


def _registered(src):
    """OOS.actions({ … }) 로 등록한 이름 — 단축 속성(이름만) 과 '이름: 함수' 둘 다."""
    names = set()
    for m in re.finditer(r"(?:OOS\.)?actions\(\{([^{}]*)\}\)", src):
        for part in re.split(r",\s*", m.group(1)):
            k = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
            if k:
                names.add(k.group(1))
    return names


RUNTIME_ACTIONS = _registered(RUNTIME)                    # 모든 화면 공용(헤더 로그인/로그아웃)


# ── 정적 검사 ────────────────────────────────────────────────
def test_pages_have_no_inline_event_handlers_or_eval():
    assert set(PAGES) == {"index", "learn", "news", "practice", "mypage", "stocks", "avatar", "invest"}
    for name, src in {**PAGES, **SCRIPTS}.items():
        assert not re.search(r"(?<![\w-])on[a-z]+\s*=\s*[\"'`$]", src), f"인라인 이벤트 핸들러가 남아 있다: {name}"
        assert not re.search(r"(?:href|src|action)\s*=\s*[\"']\s*javascript:", src), name
        assert not re.search(r"\beval\(|new Function\(|setTimeout\(\s*[\"'`]", src), name
        assert "Authorization" not in src and "oos_token" not in src.replace("'oos_token'", ""), name   # 토큰은 HttpOnly 쿠키로만


def test_scripts_are_same_origin_and_runtime_loads_first():
    for name, src in PAGES.items():
        tags = re.findall(r"<script\b[^>]*>", src)
        srcs = [re.search(r'src="([^"]+)"', t).group(1) for t in tags if "src=" in t]
        assert srcs[0] == "assets/oos/runtime.js", name              # 신뢰 타입 정책·data-on 해석기가 다른 스크립트보다 먼저
        assert all(x.startswith("assets/") and "//" not in x for x in srcs), (name, srcs)   # 외부(공급망) 스크립트 0
        assert sum("src=" not in t for t in tags) == 1, name          # 인라인은 화면 코드 하나(서버가 nonce 를 붙인다)


@pytest.mark.parametrize("page", ["index", "learn", "news", "practice", "mypage", "stocks", "avatar", "invest"])
def test_every_declarative_action_parses_and_calls_only_registered_functions(page):
    src = PAGES[page]
    used = set()
    for prog in _programs(src):
        calls, assigned = _P(prog).program()
        assert not assigned, prog                                     # 대입 문법은 쓰지 않는다
        used |= set(calls)
    allow = _registered(src) | RUNTIME_ACTIONS
    assert used <= allow, f"{page}: 등록되지 않은 동작 {used - allow}"
    for name in _registered(src):                                     # 등록한 이름은 그 화면에 실제로 있는 함수
        assert re.search(rf"(?m)^\s*(?:async\s+)?function\s+{re.escape(name)}\s*\(", src), (page, name)


def test_runtime_dispatches_through_a_registry_not_window_lookup():
    assert "createPolicy('default'" in RUNTIME and "require" not in RUNTIME.split("createPolicy")[0][-200:]
    assert "window[" not in RUNTIME                                  # 문자열로 전역 함수를 찾지 않는다(등록한 것만)
    assert "ACTIONS[st.f]" in RUNTIME and "Object.create(null)" in RUNTIME
    assert RUNTIME_ACTIONS == {"headerLogout"}


@pytest.mark.parametrize("bad", ["fetch('/x')", "go(location)", "go('a')+1", "this.innerHTML='x'",
                                 "window.go('x')", "go(`x`)", "event.target.remove()", "openSurvey()=1"])
def test_parser_rejects_anything_outside_the_grammar(bad):
    with pytest.raises((SyntaxError, AssertionError)):
        calls, assigned = _P(bad).program()
        assert not assigned and set(calls) <= _registered(PAGES["index"]), calls


# ── 화면 응답 ─────────────────────────────────────────────────
@pytest.mark.parametrize("page", ["index", "learn", "news", "practice", "mypage", "stocks", "avatar", "invest"])
def test_pages_get_a_fresh_nonce_and_strict_csp_each_time(page):
    r1, r2 = client.get(URL[page]), client.get(URL[page])
    assert r1.status_code == 200 and "text/html" in r1.headers["content-type"]
    n1 = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", r1.headers["content-security-policy"]).group(1)
    n2 = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", r2.headers["content-security-policy"]).group(1)
    assert n1 != n2 and len(n1) >= 20
    csp = r1.headers["content-security-policy"]
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert script_src == f"'self' 'nonce-{n1}'"                      # unsafe-inline·unsafe-eval·외부 출처 없음
    for d in ("script-src-attr 'none'", "require-trusted-types-for 'script'", "trusted-types default",
              "connect-src 'self'", "frame-src 'none'", "object-src 'none'", "form-action 'self'", "base-uri 'self'",
              "report-uri /csp-report"):
        assert d in csp, d
    assert "https:" not in csp and "tosspayments" not in csp         # 외부로 나가는 길 없음(결제창 없는 화면)
    tags = re.findall(r"<script\b[^>]*>", r1.text)
    inline = [t for t in tags if "src=" not in t]
    assert inline and all(f'nonce="{n1}"' in t for t in inline)
    assert all("nonce=" not in t for t in tags if "src=" in t)       # 파일 스크립트는 'self' 로만(누가 외부 태그를 넣어도 막힘)
    assert "__OOS_CSP_NONCE__" not in r1.text and r1.headers["cache-control"] == "no-store"


def test_only_served_pages_carry_the_backend_marker():
    """우리 서버가 내보낸 화면에만 표식 — 런타임·뉴스 로더는 표식이 있을 때만 API 를 부른다.
    팀이 파일·Live Server·GitHub Pages 로 열면 표식이 없어 시안 예시 모드(없는 API 를 부르며 깨지지 않음)."""
    mark = '<meta name="oos-server" content="1">'
    for name, raw in PAGES.items():
        assert "oos-server" not in raw, name                         # 파일에 박아 두면 다른 서버에서도 API 를 부른다
        html = client.get(URL[name]).text
        head = re.search(r"<head\b[^>]*>(.*?)</head>", html, re.S | re.I).group(1)
        assert head.count(mark) == 1 and html.count(mark) == 1, name
        assert html.lstrip().lower().startswith("<!doctype html>"), name   # doctype 앞에 들어가면 쿼크 모드
    for text in (RUNTIME, SCRIPTS["assets/news-api.js"]):
        assert 'meta[name="oos-server"]' in text


def test_csp_can_be_switched_off_or_overridden(monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_CSP", "off")
    assert "content-security-policy" not in client.get("/").headers
    monkeypatch.setattr(settings, "SECURITY_CSP", "default-src 'self'; script-src 'nonce-{nonce}'")
    r = client.get("/learn.html")
    n = re.search(r"'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert n != "{nonce}" and f'nonce="{n}"' in r.text


def test_only_listed_pages_are_served_and_old_screen_moved():
    for bad in ("/nope.html", "/README.html", "/assets.html", "/static/index.html", "/static/assets/logo.png",
                "/assets/%2e%2e/app/config.py", "/assets/..%2fapp%2fconfig.py", "/images/%2e%2e/web/index.html"):
        assert client.get(bad).status_code == 404, bad
    r = client.get("/app", follow_redirects=False)                    # 예전 화면 주소 → 새 첫 화면(고정 목적지)
    assert r.status_code == 308 and r.headers["location"] == "/"
    assert client.head("/").status_code == 200


def test_assets_cache_policy():
    r = client.get("/assets/logo.png")                                # 그림은 하루
    assert r.status_code == 200 and "max-age=86400" in r.headers["cache-control"]
    r = client.get("/assets/oos/runtime.js")                          # 스크립트는 매번 확인(배포 즉시 새 코드 — 304 로 가볍게)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and "javascript" in r.headers["content-type"]
    assert client.get("/assets/oos/runtime.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.get("/images/friend.png").status_code == 200


def test_asset_etag_follows_content_even_when_install_resets_mtime(tmp_path):
    """설치본(tar)은 재현 가능하게 모든 파일 시각을 0 으로 푼다 — 시각·크기로 만든 ETag 면 글자 수가 같은 수정
    (색 코드·숫자)을 브라우저가 304 로 놓쳐 옛 스크립트를 계속 쓴다. 내용 ETag + Last-Modified 없음."""
    f = tmp_path / "a.js"
    f.write_text("var x = 1;")
    os.utime(f, (0, 0))
    c = TestClient(Starlette(routes=[Mount("/s", app=webui.WebStatic(directory=str(tmp_path)))]))
    r1 = c.get("/s/a.js")
    e1 = r1.headers["etag"]
    assert r1.status_code == 200 and "last-modified" not in r1.headers
    assert c.get("/s/a.js", headers={"If-None-Match": e1}).status_code == 304
    time.sleep(0.02)                                                  # 커널 시각 단위를 넘겨 ctime 이 갈리게
    f.write_text("var x = 2;")                                        # 같은 크기 · 같은 파일 · 시각 다시 0
    os.utime(f, (0, 0))
    r2 = c.get("/s/a.js", headers={"If-None-Match": e1, "If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"})
    assert r2.status_code == 200 and r2.text == "var x = 2;" and r2.headers["etag"] != e1
    assert c.get("/s/a.js", headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"}).status_code == 200


def test_api_responses_carry_a_script_free_default_csp():
    csp = client.get("/health").headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp and "script-src-attr 'none'" in csp


# ── CSP 위반 보고 ─────────────────────────────────────────────
def test_csp_reports_become_security_events_without_query_strings():
    legacy = {"csp-report": {"document-uri": "https://oos.example/app?paymentKey=secret", "violated-directive": "script-src-elem",
                             "blocked-uri": "https://evil.example/x.js?c=1", "script-sample": "alert(1)"}}
    assert client.post("/csp-report", content=json.dumps(legacy),
                       headers={"Content-Type": "application/csp-report"}).status_code == 204
    modern = [{"type": "csp-violation", "body": {"documentURL": "https://oos.example/app", "effectiveDirective": "script-src-attr",
                                                 "blockedURL": "inline", "lineNumber": 12}}]
    assert client.post("/csp-report", content=json.dumps(modern),
                       headers={"Content-Type": "application/reports+json"}).status_code == 204
    rows = db.query("SELECT detail FROM security_event WHERE kind='csp_violation' ORDER BY id")
    d1, d2 = (json.loads(r["detail"]) for r in rows)
    assert d1["document"] == "https://oos.example/app" and d1["blocked"] == "https://evil.example/x.js"
    assert "secret" not in json.dumps(d1) and d1["directive"] == "script-src-elem" and d1["sample"] == "alert(1)"
    assert d2["directive"] == "script-src-attr" and d2["line"] == "12"
    assert client.post("/csp-report", content=b"not json").status_code == 400


def test_csp_report_flood_is_capped_per_ip():
    body = json.dumps({"csp-report": {"violated-directive": "script-src"}})
    codes = {client.post("/csp-report", content=body).status_code for _ in range(40)}
    assert codes == {204}
    assert db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='csp_violation'")[0]["n"] == 30


# ── 쿠키 세션 로그아웃 ─────────────────────────────────────────
def _web(u):
    auth.register(u, PW)
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": u, "password": PW}, headers={"X-Client": "web"}).status_code == 200
    return c


def test_logout_after_access_token_expiry_still_ends_the_session_on_the_server():
    c = _web("web-lo")
    sid = db.query("SELECT session_id FROM auth_session WHERE username='web-lo'")[0]["session_id"]
    db.execute("UPDATE auth_token SET expires_at = now() - interval '1 minute' WHERE session_id=:s", {"s": sid})
    assert c.get("/me").json()["code"] == "token_expired"                  # 접근 토큰 만료 상태
    rt = c.cookies.get("oos_rt", path="/auth")
    r = c.post("/auth/logout", headers={"X-CSRF-Token": c.cookies.get("oos_csrf")})
    assert r.status_code == 200 and r.json()["ended"] is True
    assert db.query("SELECT revoked_at FROM auth_session WHERE session_id=:s", {"s": sid})[0]["revoked_at"]
    again = TestClient(app)
    again.cookies.set("oos_rt", rt, path="/auth")
    again.cookies.set("oos_csrf", "x")
    assert again.post("/auth/refresh", headers={"X-CSRF-Token": "x"}).status_code == 401   # 갱신 쿠키로 되살릴 수 없다
    assert db.query("SELECT event FROM access_log WHERE user_id='web-lo' ORDER BY log_id DESC LIMIT 1")[0]["event"] == "logout"


def test_logout_without_any_session_is_harmless():
    r = TestClient(app).post("/auth/logout")
    assert r.status_code == 200 and r.json()["ended"] is False


def test_logged_in_browser_csp_reports_need_no_csrf_but_other_writes_do():
    c = _web("web-rep")
    body = json.dumps({"csp-report": {"violated-directive": "script-src-attr"}})
    assert c.post("/csp-report", content=body).status_code == 204          # 브라우저 보고엔 헤더를 붙일 수 없다
    assert c.post("/search/log", json={"keyword": "삼성", "ticker": "005930"}).status_code == 403   # 그 밖의 변경은 CSRF 필수
    ok = c.post("/search/log", json={"keyword": "삼성", "ticker": "005930"}, headers={"X-CSRF-Token": c.cookies.get("oos_csrf")})
    assert ok.status_code == 200


# ── '로그인 유지' — 고르지 않으면 세션 쿠키(브라우저를 닫으면 끝), 고르면 수명 있는 쿠키. 갱신해도 그대로 ───────────
def _cookie_lifetimes(resp):
    return {h.split("=", 1)[0]: ("Max-Age=" in h) for h in resp.headers.get_list("set-cookie")}


@pytest.mark.parametrize("remember", [False, True])
def test_remember_me_controls_cookie_lifetime_and_survives_refresh(remember):
    u = f"web-keep-{int(remember)}"
    auth.register(u, PW)
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": u, "password": PW, "remember": remember}, headers={"X-Client": "web"})
    assert r.status_code == 200
    life = _cookie_lifetimes(r)
    assert set(life) == {"oos_at", "oos_rt", "oos_csrf"} and set(life.values()) == {remember}
    assert db.query("SELECT persistent FROM auth_session WHERE username=:u", {"u": u})[0]["persistent"] is remember
    rr = c.post("/auth/refresh", headers={"X-Client": "web", "X-CSRF-Token": c.cookies.get("oos_csrf")})
    assert rr.status_code == 200 and set(_cookie_lifetimes(rr).values()) == {remember}
    # 로그아웃은 언제나 즉시 지운다(Max-Age=0)
    lo = c.post("/auth/logout", headers={"X-CSRF-Token": c.cookies.get("oos_csrf")})
    assert all("Max-Age=0" in h for h in lo.headers.get_list("set-cookie"))


def test_api_clients_are_unaffected_by_remember_me():
    auth.register("api-keep", PW)
    r = client.post("/auth/login", json={"username": "api-keep", "password": PW})
    assert r.status_code == 200 and r.json()["token"] and not r.headers.get_list("set-cookie")
    assert db.query("SELECT persistent FROM auth_session WHERE username='api-keep'")[0]["persistent"] is True
