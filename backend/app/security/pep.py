"""정책 집행 지점(PEP) — 모든 API 경로가 여기를 지나야 한다(경로마다 동작 이름을 선언, 없으면 거부).

  자격 꺼내기  Authorization: Bearer(앱·API 클라이언트) 또는 HttpOnly 쿠키(웹). 둘 다 있으면 헤더 우선.
  세션 확인    sessions.verify_access — 만료·유휴·폐기·기기·계정 상태를 매 요청 확인.
               자격이 잘못됐어도 공개 동작이면 '익명'으로 계속(만료된 토큰 때문에 공개 화면이 막히지 않게),
               로그인이 필요한 동작이면 그때 401 + 이유 코드(token_expired → 클라이언트가 갱신 후 재시도).
  게스트       서버가 만든 무작위 쿠키(oos_gid)의 해시가 게스트 ID — 클라이언트가 ID 를 고를 수 없다.
  CSRF         쿠키로 인증된 변경 요청(POST·PUT·PATCH·DELETE)은 X-CSRF-Token(세션별 토큰)과 Origin 을 확인.
  결정         pdp.decide → 거부면 PolicyDenied(상태 코드 + {"detail": 안내, "code": 이유}).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from functools import lru_cache

from fastapi import Depends, Request

from app.config import settings
from app.security import context, dpop, events, pdp, sessions
from app.security.context import Principal

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_GID_RE = re.compile(r"[A-Za-z0-9_\-]{22,64}")


class PolicyDenied(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ── 쿠키 ───────────────────────────────────────────────────
def secure_cookies(ctx: context.RequestContext | None = None) -> bool:
    mode = settings.COOKIE_SECURE
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    ctx = ctx or context.current()
    return ctx.scheme == "https"


def names(secure: bool) -> dict:
    """쿠키 이름 — HTTPS 면 __Host-/__Secure- 접두어(브라우저가 Secure·경로·도메인 규칙을 강제)."""
    if secure:
        return {"at": "__Host-oos_at", "rt": "__Secure-oos_rt", "csrf": "__Host-oos_csrf", "gid": "__Host-oos_gid"}
    return {"at": "oos_at", "rt": "oos_rt", "csrf": "oos_csrf", "gid": "oos_gid"}


def _cookie(name, value, max_age, path="/", http_only=True, secure=False) -> str:
    """max_age=None → 세션 쿠키(수명 없음 — 브라우저를 닫으면 지워진다)."""
    parts = [f"{name}={value}", f"Path={path}"] + ([f"Max-Age={int(max_age)}"] if max_age is not None else []) + ["SameSite=Strict"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def login_cookies(issued: sessions.Issued, ctx: context.RequestContext | None = None) -> list[str]:
    ctx = ctx or context.current()
    sec = secure_cookies(ctx)
    # '로그인 유지'를 고르지 않은 웹 로그인은 세션 쿠키(공용 PC 에서 브라우저를 닫으면 끝) — 서버 만료 규칙은 같다
    n, life = names(sec), (settings.SESSION_ABSOLUTE_SECONDS if getattr(issued, "persistent", True) else None)
    out = [_cookie(n["at"], issued.access, life, secure=sec)]
    if issued.refresh:
        out.append(_cookie(n["rt"], issued.refresh, life, path="/auth", secure=sec))
    if issued.csrf:
        out.append(_cookie(n["csrf"], issued.csrf, life, http_only=False, secure=sec))   # JS 가 읽어 헤더로 보냄
    return out


def clear_cookies(ctx: context.RequestContext | None = None) -> list[str]:
    ctx = ctx or context.current()
    out = []
    for sec in (True, False):
        n = names(sec)
        out += [_cookie(n["at"], "", 0, secure=sec), _cookie(n["rt"], "", 0, path="/auth", secure=sec),
                _cookie(n["csrf"], "", 0, http_only=False, secure=sec)]
    return out


def _from_cookies(request: Request, key: str) -> str | None:
    for sec in (True, False):
        v = request.cookies.get(names(sec)[key])
        if v:
            return v
    return None


# ── 주체 ───────────────────────────────────────────────────
def _guest_name(cookie_value: str) -> str:
    return "guest-" + hashlib.sha256(("gid:" + cookie_value).encode()).hexdigest()[:24]


def resolve(request: Request) -> Principal:
    """요청 주체(요청당 한 번 계산해 저장). 자격 오류는 principal 대신 request.state.auth_error 에 남긴다."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    ctx = context.current()
    token, via = None, None
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token, via = auth[7:].strip(), "bearer"
    else:
        token = _from_cookies(request, "at")
        via = "cookie" if token else None
    principal, err = Principal(), None
    if token:
        try:
            principal = sessions.verify_access(token, ctx, via=via)
        except sessions.SessionError as e:
            err = e
    if not principal.is_user:
        gid = _from_cookies(request, "gid")
        if gid and _GID_RE.fullmatch(gid):
            principal = Principal(kind="guest", name=_guest_name(gid))
    request.state.principal, request.state.auth_error = principal, err
    ctx.principal = principal
    return principal


def _deny(ctx, code: str, status: int | None = None, message: str | None = None):
    ctx.decision = f"deny:{code}"
    d = pdp.Decision(False, code)
    raise PolicyDenied(status or d.status, code, message or d.message)


def _check_csrf(request: Request, principal: Principal, ctx):
    if request.method not in UNSAFE or principal.via != "cookie" or not pdp.csrf_required(ctx.action):
        return
    origin = request.headers.get("origin")
    if origin and not origin_allowed(origin, ctx):
        events.emit("csrf_fail", "medium", username=principal.name, reason="origin", origin=origin[:100])
        _deny(ctx, "csrf_failed", 403, "허용되지 않은 출처의 요청이에요")
    if not sessions.csrf_ok(principal, request.headers.get("x-csrf-token")):
        events.emit("csrf_fail", "medium", username=principal.name, reason="token")
        _deny(ctx, "csrf_failed", 403, "보안 토큰이 맞지 않아요. 새로고침 후 다시 시도해 주세요")


def origin_allowed(origin: str, ctx=None) -> bool:
    ctx = ctx or context.current()
    o = origin.rstrip("/")
    return o in settings.TRUSTED_ORIGINS or o == f"{ctx.scheme}://{ctx.host}"


def enforce(request: Request, action: str, attrs: dict | None = None) -> Principal:
    ctx = context.current()
    ctx.action = action
    principal = resolve(request)
    fresh = principal.is_user and sessions.fresh(principal)
    d = pdp.decide(action, principal, attrs, fresh=fresh)
    if not d.allow:
        err = getattr(request.state, "auth_error", None)
        if d.code == "login_required" and err is not None:        # 자격은 있었지만 무효 — 이유를 그대로 알려 준다
            ctx.decision = f"deny:{err.code}"
            raise PolicyDenied(err.status, err.code, str(err))
        if d.code in ("forbidden", "mfa_required"):
            events.emit("authz_deny", "low", username=principal.name, action=action, code=d.code)
        _deny(ctx, d.code)
    if "issue_guest" in d.obligations:
        gid = secrets.token_urlsafe(24)
        sec = secure_cookies(ctx)
        ctx.set_cookies.append(_cookie(names(sec)["gid"], gid, 30 * 86400, secure=sec))
        principal = Principal(kind="guest", name=_guest_name(gid))
        request.state.principal = ctx.principal = principal
    if action.startswith("admin.") and settings.ADMIN_REQUIRE_CLIENT_CERT and \
            request.headers.get("x-client-cert-verified") != "SUCCESS":
        events.emit("device_untrusted", "medium", username=principal.name, action=action)
        _deny(ctx, "device_untrusted", 403, "관리 기능은 등록된 관리 기기(인증서)에서만 쓸 수 있어요")
    if action in ("auth.login",) and events.blocked(events.ip_hash(ctx.ip)):
        _deny(ctx, "ip_blocked", 429, "잠시 후 다시 시도해 주세요(비정상 시도 감지)")
    _check_csrf(request, principal, ctx)
    if principal.is_user and principal.jkt and pdp.device_bound(action):
        _check_device_key(request, principal, ctx, action)
    feature = pdp.control_of(action)
    if feature:
        from app.services import controls
        if controls.is_paused(feature):                   # 비상 차단 — 그 기능만 멈추고 나머지는 그대로
            _deny(ctx, "service_paused", 503, controls.message(feature))
    ctx.decision = "allow"
    return principal


def _check_device_key(request: Request, principal: Principal, ctx, action: str):
    """기기 결속 세션의 돈·계정·관리 동작 — 로그인한 브라우저의 키로 서명한 1회용 증명이 있어야 한다(dpop.py).
    다른 키면 쿠키 탈취로 보고 세션 종료(규칙: 그 사용자 전 세션 종료), 없으면 401(종료하지 않음 — 화면 오류일 수 있다)."""
    try:
        got = dpop.from_request(request)
    except dpop.DpopError as e:
        events.emit("dpop_fail", "medium", username=principal.name, code=e.code, action=action)
        _deny(ctx, e.code, 401, str(e))
    if got is None:
        events.emit("dpop_missing", "medium", username=principal.name, action=action)
        _deny(ctx, "dpop_required", 401, dpop.MESSAGES["dpop_required"])
    if got != principal.jkt:
        sessions.revoke(principal.session_id, "dpop_mismatch")
        events.emit("dpop_mismatch", "high", username=principal.name, session_id=principal.session_id, action=action)
        _deny(ctx, "dpop_mismatch", 401, dpop.MESSAGES["dpop_mismatch"])


# ── FastAPI 의존성 ─────────────────────────────────────────
@lru_cache(maxsize=None)
def guard(action: str):
    """경로에 붙이는 의존성 — 통과하면 Principal. 같은 동작 이름은 같은 함수(캐시)라 경로 검사에서 찾기 쉽다."""
    if action not in pdp.actions():
        raise pdp.PolicyConfigError(f"정책에 없는 동작: {action}")

    def dep(request: Request) -> Principal:
        return enforce(request, action)
    dep.__oos_action__ = action
    dep.__name__ = f"guard_{action.replace('.', '_')}"
    return dep


def actor(action: str):
    return Depends(guard(action))


def user(action: str):
    """로그인 사용자 이름(str)을 돌려주는 의존성 — 예전 require_login 과 같은 자리에 쓴다."""
    g = guard(action)

    def dep(p: Principal = Depends(g)) -> str:
        return p.name
    dep.__oos_action__ = action
    return Depends(dep)


def check(request: Request, action: str, **attrs) -> Principal:
    """본문을 읽은 뒤 속성(금액·목적 등)까지 넣어 다시 판단 — 송금 금액 기준 추가 인증 등."""
    return enforce(request, action, attrs)


def route_action(route) -> str | None:
    """경로에 선언된 동작 이름(의존성 트리에서 찾음) — 기본 거부·정책 커버리지 검사용."""
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        d = stack.pop()
        a = getattr(d.call, "__oos_action__", None)
        if a:
            return a
        stack.extend(d.dependencies or [])
    return None


async def default_deny(request: Request):
    """앱 전역 의존성 — 동작을 선언하지 않은 경로는 실행 전에 거부(새 경로를 만들고 정책을 빠뜨린 경우)."""
    route = request.scope.get("route")
    if route is not None and route_action(route) is None:
        raise PolicyDenied(403, "unknown_action", pdp.MESSAGES["unknown_action"])
