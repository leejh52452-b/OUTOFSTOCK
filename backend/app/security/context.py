"""요청 문맥 — 요청 한 건의 '누가·어디서·무엇을'을 한곳에 모은다(contextvars).

ContextMiddleware(순수 ASGI)가 요청 시작 때 문맥을 만들고, 정책 집행(pep)이 주체(principal)를 채우고,
로그·보안 이벤트·감사 기록이 같은 요청 ID 로 묶인다. 응답 헤더 X-Request-ID 로 사용자 문의 ↔ 서버 로그를 잇는다.

  · 요청 ID: 앞단(nginx $request_id)이 준 값이 형식에 맞으면 그대로, 아니면 새로 만든다.
  · 동기 엔드포인트는 스레드풀에서 돌지만 contextvars 는 복사되어 넘어가고, 문맥 객체는 같은 것을 가리키므로
    엔드포인트·의존성에서 채운 값이 미들웨어의 마무리 단계(접근 로그)까지 보인다.
"""
from __future__ import annotations

import contextvars
import re
import time
import uuid
from dataclasses import dataclass, field

from app.security import device

_RID_RE = re.compile(r"[A-Za-z0-9\-]{8,64}")


@dataclass
class Principal:
    """요청 주체. kind: anonymous(식별 없음) · guest(서버 발급 게스트 쿠키) · user(로그인 세션)."""
    kind: str = "anonymous"
    name: str | None = None                   # user: 아이디 · guest: 'guest-<해시>' · anonymous: None
    session_id: str | None = None
    via: str | None = None                    # cookie | bearer
    aal: int = 0                              # 인증 강도 — 1: 비밀번호, 2: 비밀번호 + OTP
    auth_time: float | None = None            # 비밀번호 확인 시각(epoch 초)
    step_up_at: float | None = None           # 마지막 추가 인증 시각(epoch 초)
    roles: frozenset = frozenset()            # 지금 활성(JIT)된 역할
    risk: frozenset = frozenset()             # 위험 신호 — ip_change 등
    csrf_hash: str | None = None
    jkt: str | None = None                    # 세션에 결속된 기기 키 지문(DPoP) — 없으면 결속 안 된 세션

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


@dataclass
class RequestContext:
    request_id: str
    method: str = ""
    path: str = ""
    ip: str = ""
    ip_net: str = ""
    ua: str = ""
    device_hash: str = ""
    device_label: str = ""
    origin: str | None = None
    host: str = ""
    scheme: str = "http"
    principal: Principal = field(default_factory=Principal)
    route: str | None = None                  # 경로 템플릿(/practice/{session_id}) — 로그 집계용
    action: str | None = None                 # 정책 동작 이름(pep 가 채움)
    decision: str | None = None               # allow / deny:<code>
    status: int | None = None
    t0: float = field(default_factory=time.perf_counter)
    set_cookies: list = field(default_factory=list)   # 정책 집행 중 내려보낼 쿠키(게스트 식별 등)
    dpop_jkt: str | None = None               # 이 요청의 기기 키 증명을 검증한 결과(키 지문) — 로그인·갱신이 쓴다


_ctx: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar("oos_request_ctx", default=None)


def current() -> RequestContext:
    """지금 요청의 문맥 — 요청 밖(CLI·배치·테스트에서 서비스 직접 호출)이면 빈 문맥."""
    c = _ctx.get()
    if c is None:
        c = RequestContext(request_id="local-" + uuid.uuid4().hex[:12])
    return c


def request_id() -> str | None:
    c = _ctx.get()
    return c.request_id if c else None


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


def build(scope) -> RequestContext:
    rid = _header(scope, b"x-request-id") or ""
    if not _RID_RE.fullmatch(rid):
        rid = uuid.uuid4().hex
    ip = (scope.get("client") or ("", 0))[0] or ""
    ua = _header(scope, b"user-agent") or ""
    return RequestContext(
        request_id=rid, method=scope.get("method", ""), path=scope.get("path", ""), ip=ip,
        ip_net=device.ip_net(ip), ua=ua[:512], device_hash=device.fingerprint(ua), device_label=device.label(ua),
        origin=_header(scope, b"origin"), host=(_header(scope, b"host") or "")[:200],
        scheme=scope.get("scheme", "http"))


class ContextMiddleware:
    """가장 바깥 미들웨어 — 문맥 생성, 응답에 X-Request-ID, 끝나면 관측 훅(접근 로그·지표) 호출."""

    def __init__(self, app_, on_finish=None):
        self.app = app_
        self.on_finish = on_finish

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ctx = build(scope)
        token = _ctx.set(ctx)

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                ctx.status = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", ctx.request_id.encode()))
                for c in ctx.set_cookies:
                    headers.append((b"set-cookie", c.encode("latin-1")))
                message["headers"] = headers
            await send(message)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            route = scope.get("route")
            ctx.route = getattr(route, "path", None) or ctx.route
            if self.on_finish:
                try:
                    self.on_finish(ctx)
                except Exception:                    # noqa: BLE001 — 관측 실패가 응답을 망치지 않게
                    pass
            _ctx.reset(token)
