"""API 서버 (FastAPI) + 웹 화면 서빙.
DB(창고)에서 읽어 JSON. 모의투자·인증은 서비스 호출. 웹 화면(팀 확정 디자인 web/)은 같은 출처에서 서빙(app/webui.py).
실행:  uvicorn app.api:app --reload
       화면: http://127.0.0.1:8000/   (learn.html · news.html · practice.html · mypage.html)
       문서: http://127.0.0.1:8000/docs
"""
import json
import logging
import math
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, StatementError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.datastructures import MutableHeaders

from app.config import settings
from app import db, webui
from app.services import search as search_svc
from app.services import auth as auth_svc
from app.services import survey as survey_svc
from app.services import monitoring as mon
from app.services import risk as risk_svc
from app.services import soundness as soundness_svc
from app.services import themes as themes_svc
from app.services import practice as practice_svc
from app.services import practice_records as practice_records_svc
from app.services import quiz as quiz_svc
from app.services import avatar as avatar_svc
from app.services import capacity as capacity_svc
from app.services import controls as controls_svc
from app.services import learn as learn_svc
from app.services import market as market_svc
from app.services import detail as detail_svc
from app.services import account as account_svc
from app.services import watchlist as watchlist_svc
from app.services import wallet as wallet_svc
from app.services import attendance as attendance_svc
from app.services import iap as iap_svc
from app.services import transfer as transfer_svc
from app.services import invest as invest_svc
from app.services import vmarket as vmarket_svc
from app.services import yangbong as yangbong_svc
from app.services import toss as toss_svc
from app.services import friends as friends_svc
from app.services import overseas as overseas_svc
from app.services import chat as chat_svc
from app.services import products as products_svc
from app.services import signals as signals_svc
from app.services import news_home as news_home_svc
from app.services import audit as audit_svc
from app.services import ratelimit
from app.security import pep, context as sec_ctx, sessions as sec_sessions, totp as sec_totp
from app.security import rbac as sec_rbac, events as sec_events, rls as sec_rls
from app.security import pdp as sec_pdp, dpop as sec_dpop
from app.security.context import Principal

log = logging.getLogger(__name__)


def _docs_kwargs(enabled):
    """/docs·/redoc·/openapi.json — 외부에 공개할 땐 DOCS_ENABLED=false 로 숨긴다(API 구조 노출 최소화)."""
    return {} if enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}


def _warmup():
    """서버를 켠 직후 첫 방문자가 추천 계산(1~3초, 작은 VM 에선 더)을 기다리지 않게 홈·뉴스 화면 결과를 미리 계산.
    (이후엔 캐시가 만료돼도 가진 값을 먼저 주고 뒤에서 다시 계산한다 — reco_engine._cached)"""
    t0 = time.monotonic()
    for name, fn in (("top5", lambda: signals_svc.today(5, None)), ("review", lambda: signals_svc.review(30, 5)),
                     ("news", news_home_svc.home)):
        try:
            fn()
        except Exception as e:                               # noqa: BLE001 — 예열 실패는 서비스와 무관
            logging.getLogger(__name__).warning("캐시 예열 %s 실패: %s", name, e)
    logging.getLogger(__name__).info("캐시 예열 완료 %.1f초", time.monotonic() - t0)


@asynccontextmanager
async def _lifespan(_app):
    """서버 시작 시 스키마 확인·무손실 마이그레이션(멱등) — 업데이트 후 'python run.py initdb' 를 잊어도
    새 컬럼·표가 없어 /indices·/reco/top5 가 500 이 나는 일이 없게. 실패해도 서버는 뜬다(로그만)."""
    from app.logging_conf import setup_logging
    setup_logging()                                          # 앱 로그 출력(시각·레벨·모듈, KST) + API 키 가리기 + cp949 콘솔 대비
    try:
        applied = db.init_db()
        if applied:
            logging.getLogger(__name__).info("DB 마이그레이션 적용: %s", ", ".join(applied))
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger(__name__).warning("DB 스키마 확인 실패(서버는 계속 실행): %s", e)
    try:                                                     # 관리 역할 '자격' 부트스트랩(활성 아님 — JIT 필요)
        from app.security import pdp, rbac
        rbac.bootstrap()
        logging.getLogger(__name__).info("접근 정책 %s", pdp.info())
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger(__name__).warning("역할 부트스트랩 실패: %s", e)
    try:                                                     # 파일 무결성 — 배포 기준선과 다른 파일로 뜨는지(기준선 없으면 건너뜀)
        from app.security import integrity
        st = integrity.check()
        if st["state"] == "violation":
            logging.getLogger(__name__).error("파일 무결성 위반: %s", st.get("counts"))
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger(__name__).warning("파일 무결성 검사 실패: %s", e)
    if os.getenv("WARMUP_ON_START", "true").lower() in ("1", "true", "yes"):
        threading.Thread(target=_warmup, name="cache-warmup", daemon=True).start()
    try:
        if db.get_meta("market_source") == "krx":           # 실데이터 모드인데 예전 데모 재무·뉴스가 남아 있으면 정리
            from app.collectors import marketgen
            marketgen.purge_demo_artifacts()
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger(__name__).warning("데모 데이터 정리 실패(서버는 계속 실행): %s", e)
    yield


# 기본 거부 — 모든 경로는 정책 동작(pep.guard)을 선언해야 실행된다. 선언이 없으면 실행 전에 403(app/security/pep.py)
_tier_cache: dict = {}


async def _admission(request: Request):
    """대량접속제어(app/services/capacity.py) — 경로의 중요도(policy.json 동작 tier · 경로 예외)를 보고 부하 단계가
    경계면 low, 심각이면 normal 까지 503. DB·스레드를 쓰기 전에(전역 의존성 — 경로 의존성보다 먼저) 돌려보낸다."""
    route = request.scope.get("route")
    if route is None:
        return
    key = id(route)
    tier = _tier_cache.get(key)
    if tier is None:
        tier = sec_pdp.tier(pep.route_action(route), f"{request.method} {getattr(route, 'path', '')}")
        _tier_cache[key] = tier
    try:
        capacity_svc.admit(tier)
    except capacity_svc.Overloaded:
        sec_ctx.current().decision = f"shed:{tier}"        # 접근 로그·지표에서 '돌려보냄'으로 구분
        raise


# 기본 거부 — 모든 경로는 정책 동작(pep.guard)을 선언해야 실행된다. 선언이 없으면 실행 전에 403(app/security/pep.py)
# 그다음 대량접속제어 — 혼잡하면 중요도가 낮은 요청부터 바로 돌려보낸다
app = FastAPI(title="주식 데이터 API", lifespan=_lifespan,
              dependencies=[Depends(pep.default_deny), Depends(_admission)],
              **_docs_kwargs(settings.DOCS_ENABLED))


# CSP(콘텐츠 보안 정책) — 두 가지.
#  · 화면(/, /learn.html …): app/webui.py — 요청마다 새 nonce 를 인라인 <script> 와 헤더에 함께. 인라인 이벤트 핸들러는
#    script-src-attr 'none' 으로 금지(화면 동작은 data-on 해석기 — web/assets/oos/runtime.js), Trusted Types 강제,
#    외부 연결·프레임·폼 전송 없음. 위반은 /csp-report 로 보고 → 보안 이벤트(XSS 시도·깨진 배포를 알아챈다).
#  · 그 밖의 응답(API JSON 등): 문서로 렌더링될 일이 없으므로 같은 출처 외 전부 막는 기본 정책.
DEFAULT_CSP = ("default-src 'self'; script-src 'self'; script-src-attr 'none'; object-src 'none'; "
               "frame-ancestors 'self'; base-uri 'self'; form-action 'self'")
_CSP_EXEMPT = ("/docs", "/redoc")                     # Swagger 화면은 CDN 스크립트를 쓴다(운영에선 DOCS_ENABLED=false)


def _csp_for(path):
    v = (settings.SECURITY_CSP or "").strip()
    if v.lower() == "off" or path.startswith(_CSP_EXEMPT):
        return None
    return v or DEFAULT_CSP


class SecurityHeaders:
    """모든 응답에 기본 보안 헤더 — MIME 추측 금지 · 다른 사이트의 iframe 삽입 금지 · 리퍼러 최소화 ·
    카메라/마이크/위치 권한 차단 · CSP(기본 정책, SECURITY_CSP 로 바꾸거나 'off'). HSTS 는 HTTPS 일 때만(SECURITY_HSTS)."""
    BASE = (("x-content-type-options", "nosniff"), ("x-frame-options", "SAMEORIGIN"),
            ("referrer-policy", "strict-origin-when-cross-origin"),
            ("permissions-policy", "camera=(), microphone=(), geolocation=()"))

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                extra = list(self.BASE)
                if settings.SECURITY_HSTS:
                    extra.append(("strict-transport-security", "max-age=31536000; includeSubDomains"))
                csp = _csp_for(scope.get("path") or "")
                if csp:
                    extra.append(("content-security-policy", csp))
                for k, v in extra:
                    if k not in h:
                        h.append(k, v)
            await send(message)
        await self.app(scope, receive, send_with_headers)


class BodySizeLimit:
    """요청 본문 크기 상한(MAX_BODY_BYTES, 기본 1MB) — 넘으면 413.
    VM 배포(deploy/vm.sh)는 nginx 없이 uvicorn 이 바로 받는다. 예전엔 본문 크기 제한이 없어 로그인 요청 하나에
    200MB 를 실어 보내면 API 프로세스 메모리가 0.1GB → 최대 1.3GB 로 치솟았다(몇 개만 동시에 와도 VM 메모리 고갈 →
    DB·API 강제 종료). Content-Length 가 크면 읽기 전에 거절, 길이 표시 없이(chunked) 오면 읽는 도중 상한에서 끊는다."""
    def __init__(self, app_, max_bytes):
        self.app, self.max = app_, max_bytes

    async def _reject(self, send):
        body = json.dumps({"detail": f"요청 본문이 너무 커요(최대 {self.max // 1000:,}KB)"}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max <= 0:
            await self.app(scope, receive, send)
            return
        for k, v in scope.get("headers") or ():
            if k == b"content-length":
                try:
                    too_big = int(v) > self.max
                except ValueError:
                    too_big = True
                if too_big:
                    await self._reject(send)
                    return
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > self.max:                          # FastAPI 가 그대로 413 응답으로 바꾼다
                    raise HTTPException(413, f"요청 본문이 너무 커요(최대 {self.max // 1000:,}KB)")
            return msg
        await self.app(scope, limited_receive, send)


app.add_middleware(BodySizeLimit, max_bytes=settings.MAX_BODY_BYTES)   # 가장 안쪽 — 413 에도 CORS·보안 헤더가 붙게
# CORS — 기본은 같은 출처만(목록이 비면 미들웨어를 달지 않는다 = 교차 출처 응답 헤더 없음).
# 허용 목록이 있어도 자격 증명(쿠키)은 보내지 않는다(allow_credentials=False) — 교차 출처 UI 는 Bearer 토큰으로.
if settings.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID",
                                      "X-Client", "DPoP"],
                       expose_headers=["Retry-After", "X-Request-ID", "X-Auth-Code"], max_age=600)
from app.http_cache import HttpCache  # noqa: E402
app.add_middleware(HttpCache)                               # 공개 조회 ETag/304 · 개인 응답 no-store(압축 전 본문 기준)
app.add_middleware(GZipMiddleware, minimum_size=2048)      # 용어사전(150KB)·뉴스 홈 등 큰 JSON 압축
app.add_middleware(SecurityHeaders)


class CapacityTracker:
    """동시에 처리 중인 요청 수(워커별) — 부하 단계·대량접속제어의 기준(app/services/capacity.py). SSE 같은 긴 연결은 뺀다."""
    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not capacity_svc.counted(scope.get("path") or ""):
            await self.app(scope, receive, send)
            return
        capacity_svc.enter()
        t0, status = time.perf_counter(), [None]

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            ctx = sec_ctx.current()
            capacity_svc.leave((time.perf_counter() - t0) * 1000.0, status[0],
                               shed=bool(ctx.decision and ctx.decision.startswith("shed:")))


app.add_middleware(CapacityTracker)
app.add_middleware(sec_ctx.ContextMiddleware, on_finish=lambda c: _observe(c))   # 가장 바깥 — 요청 ID·주체·접근 로그


def _observe(ctx):
    """요청 1건이 끝날 때 — 접근 로그(구조화)·지표. 관측 실패가 응답을 막지 않는다(ContextMiddleware 가 예외 흡수)."""
    from app.services import observability
    observability.request_finished(ctx)


@app.exception_handler(pep.PolicyDenied)
def _policy_denied(_request, e):
    """정책 거부 — {"detail": 사용자 안내, "code": 이유 코드}. 이유 코드는 헤더(X-Auth-Code)로도."""
    headers = {"X-Auth-Code": e.code}
    if e.status == 429:
        headers["Retry-After"] = "900"
    elif e.status == 503:                                  # 비상 차단(기능 중지) — 다시 시도는 나중에
        headers["Retry-After"] = "60"
    return JSONResponse(status_code=e.status, content={"detail": e.message, "code": e.code}, headers=headers)


@app.exception_handler(capacity_svc.Overloaded)
def _overloaded(_request, e):
    """대량접속제어 — 혼잡 단계에서 중요도가 낮은 요청을 바로 돌려보낸다(DB·스레드를 쓰기 전)."""
    return JSONResponse(status_code=503, content={"detail": f"지금 접속이 많아요(혼잡 {capacity_svc.LEVELS[e.level]}). "
                                                            "잠시 뒤 다시 시도해 주세요", "code": "overloaded"},
                        headers={"Retry-After": str(e.retry_after), "X-Load-Level": str(e.level)})


@app.exception_handler(PoolTimeoutError)
def _pool_timeout(_request, e):
    """DB 연결 풀이 다 찼다 — 30초 기다리다 500 대신 짧게(DB_POOL_TIMEOUT_S) 기다리고 503(Retry-After)."""
    capacity_svc.note_pool_timeout()
    log.warning("DB 연결 풀 대기 시간 초과(%ss)", settings.DB_POOL_TIMEOUT_S)
    return _json_error(503, "요청이 몰려 잠시 처리하지 못했어요. 잠시 후 다시 시도해 주세요", headers={"Retry-After": "3"})


@app.exception_handler(sec_sessions.SessionError)
def _session_error(_request, e):
    return JSONResponse(status_code=e.status, content={"detail": str(e), "code": e.code},
                        headers={"X-Auth-Code": e.code})


# ── 오류 응답 정리 — 예상 못 한 DB·입력 오류가 500(텍스트) 대신 JSON {"detail": "..."} 로 가게 ──
def _json_error(status, msg, headers=None):
    return JSONResponse(status_code=status, content={"detail": msg}, headers=headers)


@app.exception_handler(wallet_svc.WalletError)
def _wallet_error(_request, e):
    """지갑 규칙 위반(잔액 부족 등)=400, 동시 요청 충돌=409 — 어느 경로에서 나와도 같은 규칙."""
    return _json_error(409 if "충돌" in str(e) else 400, str(e))


@app.exception_handler(IntegrityError)
def _integrity_error(_request, e):
    log.warning("동시 요청 충돌(IntegrityError): %s", type(e.orig).__name__ if e.orig else e)
    return _json_error(409, "요청이 동시에 겹쳤어요. 잠시 후 다시 시도해 주세요")


@app.exception_handler(DataError)
def _data_error(request: Request, e):
    """DB 가 '값 자체'를 거부한 경우(문자열 길이 초과·NUL 문자·정수 범위 초과 등) → 500 이 아니라 422.
    서버 결함이 아니라 잘못된 입력이므로 클라이언트에게 고칠 수 있는 메시지를 준다(원문 SQL 은 노출 안 함)."""
    log.warning("입력값 DB 거부 %s %s: %s", request.method, request.url.path,
                str(getattr(e, "orig", e)).splitlines()[0][:200])
    return _json_error(422, "입력값이 너무 길거나 범위를 벗어났어요(길이·숫자 범위·특수문자).")


@app.exception_handler(OperationalError)
def _operational_error(_request, e):
    code = getattr(e.orig, "sqlstate", None) or getattr(getattr(e.orig, "diag", None), "sqlstate", None)
    if code in ("40P01", "40001", "55P03"):              # 교착·직렬화 실패·잠금 대기 → 다시 시도하면 되는 충돌
        return _json_error(409, "요청이 동시에 겹쳤어요. 잠시 후 다시 시도해 주세요")
    if code == "57014":                                  # statement_timeout — 분석 쿼리가 시간 제한을 넘김
        return _json_error(504, f"분석 쿼리가 시간 제한({settings.ANALYTICS_QUERY_TIMEOUT_MS // 1000}초)을 넘었어요. "
                                "조건을 좁혀 다시 시도해 주세요")
    log.error("DB 일시 장애: %s", type(e.orig).__name__ if e.orig else type(e).__name__)
    return _json_error(503, "데이터베이스가 잠시 응답하지 않아요. 잠시 후 다시 시도해 주세요",
                       headers={"Retry-After": "5"})


@app.exception_handler(UnicodeError)
def _unicode_error(_request, e):
    return _json_error(400, "쓸 수 없는 문자가 들어 있어요")


@app.exception_handler(StatementError)
def _statement_error(_request, e):
    if isinstance(getattr(e, "orig", None), UnicodeError):
        return _json_error(400, "쓸 수 없는 문자가 들어 있어요")
    log.error("DB 문장 오류: %s", type(e).__name__)
    return _json_error(500, "서버 오류가 났어요. 잠시 후 다시 시도해 주세요")


def _safe_json(v):
    """오류 응답에 넣을 값 — NaN·무한대·짝 없는 서로게이트가 있으면 JSON 직렬화가 터져 500 이 됐다."""
    if isinstance(v, bool) or isinstance(v, int) or v is None:
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    return str(v).encode("utf-8", "replace").decode("utf-8")


@app.exception_handler(RequestValidationError)
def _validation_error(_request, e):
    """입력 검증 실패(422) — 원래 입력값(input)은 되돌려주지 않는다(NaN·깨진 문자열이면 응답 자체가 실패)."""
    detail = []
    for err in e.errors():
        item = {"type": err.get("type"), "loc": [_safe_json(x) for x in err.get("loc", ())],
                "msg": _safe_json(err.get("msg", ""))}
        if err.get("ctx"):
            item["ctx"] = {k: _safe_json(v) for k, v in err["ctx"].items()}
        detail.append(item)
    return JSONResponse(status_code=422, content={"detail": detail})


def _bad_text(v):
    """UTF-8 로 저장할 수 없는 문자(짝 없는 서로게이트 \\ud800 등)가 있으면 True."""
    if isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(v, (list, tuple)):
        return any(_bad_text(x) for x in v)
    if isinstance(v, BaseModel):
        return any(_bad_text(x) for x in v.__dict__.values())
    if isinstance(v, dict):
        return any(_bad_text(k) or _bad_text(x) for k, x in v.items())
    return False


class _Req(BaseModel):
    """요청 바디 공통 — 문자열에 저장할 수 없는 문자가 섞이면 422(예전엔 DB·해시 단계에서 500)."""

    @model_validator(mode="after")
    def _utf8_only(self):
        if _bad_text(list(self.__dict__.values())):
            raise ValueError("쓸 수 없는 문자가 들어 있어요")
        return self


_QTY = 1_000_000_000                        # 수량 상한(비현실적인 값으로 금액이 DB 정수 범위를 넘지 않게)


class AuthReq(_Req):
    # 요청 단계에선 '터무니없이 긴 값'만 막고(422), 아이디 2~50자·비밀번호 8~128자 규칙은 서비스가 한국어 안내와 함께 400
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class SurveyAnswerItem(_Req):
    id: str = Field(max_length=10)           # 문항 번호(Q1~Q20)
    answer: int                              # 고른 보기 번호 0~3 (= 점수)


class SurveyReq(_Req):
    # 권장: [{"id":"Q3","answer":2}, ...] 출제된 10문항 / 호환: [2,0,3,...] 보기 번호 10개
    answers: list[int | SurveyAnswerItem] = Field(max_length=40)


class PracticeStartReq(_Req):
    user_id: str = Field("guest", max_length=50)
    scenario_id: str = Field(max_length=40)


class PracticeStepReq(_Req):
    days: int = Field(1, ge=1, le=60)               # 1일 / 5일 이동


class PracticeTradeReq(_Req):
    side: str = Field(max_length=4)                 # BUY / SELL
    quantity: int = Field(le=_QTY)


class LearnLogReq(_Req):
    kind: str = Field(max_length=20)                # term / indicator / technique
    item_key: str = Field("", max_length=60)
    item_name: str = Field("", max_length=100)


class LearnProgressReq(_Req):
    term: str = Field(max_length=100)               # 용어 카드 이름(learn_terms.json 의 name)
    done: bool = True                               # True=이해했어요, False=표시 해제


class LearnSyncReq(_Req):
    terms: list[str] = Field([], max_length=2000)


class QuizSubmitReq(_Req):
    date: str = Field(max_length=10)                 # 화면의 오늘(YYYY-MM-DD) — 그날 문제를 정한다
    answers: list[int] = Field(max_length=10)        # 고른 보기 번호(화면 순서) — 점수는 서버가 매긴다


class AvatarBuyReq(_Req):
    item_id: str = Field(max_length=20)              # 아이템 id 만 — 가격은 서버 카탈로그
    equip: bool = True                               # 사자마자 입기
    price: int | None = Field(None, ge=0, le=10_000_000_000)  # 화면이 보여 준 가격 — 서버 가격과 다르면 409(돈을 빼지 않음)


class AvatarEquipReq(_Req):
    items: dict[str, str | None]                     # {'hat': 'hat-3', 'pet': None(벗기)} — 바꿀 부위만


class PracticeRecordReq(_Req):
    tech: str = Field(max_length=20)                 # golden | rsi | pullback | support
    mode: str = Field("practice", max_length=10)     # practice(예시 차트) | real(실제 차트 — SOXL·국내 대형주)
    traded: bool = False                             # 매수·매도까지 했는지
    ret: float | None = None                         # 수익률(비율 0.05 = +5%) — 범위는 서비스가 확인
    entry_date: str | None = Field(None, max_length=10)


class ChangePwReq(_Req):
    old_password: str = Field(max_length=1024)          # 8~128자 규칙은 서비스가 400 으로 안내
    new_password: str = Field(max_length=1024)


# ── 추가 기능: 가상머니 지갑 생태계 요청 바디 ──
class IapPurchaseReq(_Req):
    store: str = Field(max_length=10)               # google / apple
    product_id: str = Field(max_length=40)
    receipt: str = Field(max_length=20_000)         # 스토어 결제 영수증(구매 토큰) — 저장은 해시로


class TransferReq(_Req):
    to: str = Field(max_length=50)                  # 받는 사람(아이디)
    amount: int = Field(le=1_000_000_000_000)
    memo: str = Field("", max_length=200)

    @field_validator("to", "memo")
    @classmethod
    def _strip(cls, v: str) -> str:                 # 입구에서 한 번 정규화 — 판정·실행·감사가 같은 받는 사람을 본다(결함 22)
        return v.strip()


class InvestBuyReq(_Req):
    ticker: str = Field(max_length=20)
    quantity: int = Field(le=10_000_000)


class InvestSellReq(_Req):
    quantity: int | None = Field(None, le=_QTY)     # 생략 시 전량 매도(0 이하는 거절)


class VMarketJoinReq(_Req):
    season: int = Field(ge=1, le=100_000)           # 화면이 본 시즌 — 서버 시계의 시즌과 다르면 409
    entry: int = Field(ge=1, le=10 ** 13)           # 참가금(원) — 최소·잔액은 서비스가 확인


class VMarketOrderReq(_Req):
    season: int = Field(ge=1, le=100_000)
    stock: int = Field(ge=0, le=4)                  # 종목 A~E
    side: str = Field(max_length=4)                 # buy | sell
    qty: int = Field(ge=1, le=1_000_000_000)
    bar: int = Field(ge=0, le=65)                   # 화면이 본 '공개된 봉 수' — 새 봉이 나왔으면 409(옛 가격 체결 방지)


class ChatSendReq(_Req):
    body: str = Field(max_length=5000)              # 서비스가 1~1000자로 다시 검사(한국어 안내)


class TossPrepareReq(_Req):
    product_id: str = Field(max_length=40)


class TossConfirmReq(_Req):
    paymentKey: str = Field(max_length=200)
    orderId: str = Field(max_length=64)
    amount: int = Field(le=1_000_000_000)


def _iso_date(value, what="as_of"):
    """YYYY-MM-DD 날짜만 받는다(비우면 None = 오늘 기준). 예전엔 검증 없이 문자열로 비교해 '2025-1-5' 가
    '2025-09-30' 보다 '뒤'로 취급돼 그때까지 공시되지 않은 재무·가격이 쓰였다(시점 조회가 막으려던 미래참조).
    '20250305'·'2025-3-5'·'2025.03.05' 같은 다른 표기도 받지 않는다(문서와 같은 한 가지 형식만)."""
    if not value:
        return None
    try:
        if len(value) != 10:
            raise ValueError(value)
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, f"{what} 는 YYYY-MM-DD 형식이어야 해요") from None


def _clamp(v, lo, hi):
    return max(lo, min(int(v), hi))


# ── 웹 화면(팀 확정 디자인 web/) — 같은 출처에서 내보낸다(app/webui.py: 허용 목록 · 요청마다 nonce · CSP) ──
# 화면은 API 가 아니라 API 문서(OpenAPI)에서 뺀다 — GET·HEAD 한 함수가 같은 operationId 를 두 번 만들던 경고도 함께 없어진다
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, dependencies=[pep.actor("public.read")],
               include_in_schema=False)
def web_home():
    return _web_page("index")


@app.api_route("/{page}.html", methods=["GET", "HEAD"], response_class=HTMLResponse,
               dependencies=[pep.actor("public.read")], include_in_schema=False)
def web_page(page: str):
    return _web_page(page)


def _web_page(name):
    try:
        html, headers = webui.render(name)
    except webui.PageNotFound:
        raise HTTPException(404, "없는 화면이에요") from None
    return HTMLResponse(html, headers=headers)


@app.api_route("/app", methods=["GET", "HEAD"], dependencies=[pep.actor("public.read")], include_in_schema=False)
def legacy_app():
    """예전 화면 주소(/app) — 즐겨찾기·문서 링크가 깨지지 않게 새 첫 화면으로(고정 목적지 — 사용자 값으로 이동하지 않음)."""
    from starlette.responses import RedirectResponse
    return RedirectResponse("/", status_code=308)


# ── 대시보드 ──────────────────────────────────────────────


@app.get("/stocks/{ticker}/news", dependencies=[pep.actor("public.read")])
def stock_news(ticker: str, limit: int = 10, offset: int = Query(0, ge=0, le=1_000_000)):
    """종목 관련 뉴스(최신순, 호재/악재 태그) + summary: 최근 7일 호재·악재·중립 개수(종목 상세 '관련 뉴스')."""
    return themes_svc.stock_news(ticker, _clamp(limit, 1, 50), offset)


@app.get("/stocks/{ticker}/peers", dependencies=[pep.actor("public.read")])
def stock_peers(ticker: str):
    """동종(재무 구조 유사) 기업 비교 — 매출 규모·영업이익률·순이익률·부채비율이 가까운 3곳과 나란히(여신 심사의
    비교 기업 분석 방식). 재무 유사도는 매일 23:00 DART 재무 수집 뒤 다시 계산."""
    from app.services import recommend
    return recommend.peers(ticker[:20])


@app.get("/stocks/{ticker}/risk", dependencies=[pep.actor("public.read")])
def stock_risk(ticker: str):
    """리스크 지표 — 연율화 변동성·역사적 VaR·최대낙폭·기간수익률."""
    return risk_svc.risk_profile(ticker)


@app.get("/stocks/{ticker}/var-backtest", dependencies=[pep.actor("public.read")])
def stock_var_backtest(ticker: str, conf: float = Query(0.95, ge=0.8, le=0.999)):
    """VaR 백테스트 — 초과(breach) 횟수 + Kupiec POF 검정으로 VaR 모델 타당성 평가."""
    return risk_svc.var_backtest(ticker, conf)


@app.get("/stocks/{ticker}/soundness", dependencies=[pep.actor("public.read")])
def stock_soundness(ticker: str, as_of: str | None = None):
    """재무 건전성 스코어 — 수익성·안정성 비율 → 0~100 종합 + 등급.
    as_of=YYYY-MM-DD 지정 시 그 시점에 공시로 알 수 있던 재무로 평가(look-ahead 방지)."""
    return soundness_svc.soundness(ticker, _iso_date(as_of))


# ── 검색 / 뉴스 ───────────────────────────────────────────


@app.get("/search/suggest", dependencies=[pep.actor("public.read")])
def search_suggest(q: str = "", limit: int = 8, kind: str = "all", prefer: str = "kr"):
    """자동완성 — 국내+해외 후보(정확도·시총 순) + 현재가·등락률. 타이핑 조각은 기록하지 않는다.
    한글 조합 중 글자('삼서'→삼성), 초성('ㅅㅅㅈㅈ'), 대소문자·공백 무시, 별칭('네이버'→NAVER) 지원.
    kind=all(국내+해외) | kr | us · prefer=kr(기본, 국내 먼저) | us(해외 먼저 — 해외 화면 검색창)"""
    return search_svc.suggest(q, limit, kind=kind, prefer=prefer)


class SearchLogReq(BaseModel):
    keyword: str = ""
    ticker: str | None = None


@app.post("/search/log", dependencies=[pep.actor("public.telemetry")])
def search_log(req: SearchLogReq, request: Request):
    """사용자가 실제로 고른 검색 1건 기록(엔터·결과 클릭) → 인기 검색 종목 집계.
    인기 순위 조작 방지: IP 당 1분 30건까지만 반영(넘으면 조용히 무시), 존재하는 종목만 집계."""
    key = ratelimit.key_of("searchlog", _client_ip(request))
    if ratelimit.take([(key, 30)], 60):                         # 확인+기록 한 번에(동시에 몰려도 30건까지만)
        return {"ok": False, "reason": "too_many"}
    ticker = req.ticker if (req.ticker and search_svc.known(req.ticker)) else None
    search_svc.log_search(req.keyword, ticker)
    return {"ok": True}


@app.get("/search/popular", dependencies=[pep.actor("public.read")])
def search_popular(limit: int = 8, days: int = 7):
    """인기 검색 종목(최근 days 일, 실제로 고른 종목 기준) — 기록이 부족하면 거래대금 상위로 채움."""
    return search_svc.popular_stocks(max(1, min(limit, 20)), max(1, min(days, 90)))


@app.get("/news", dependencies=[pep.actor("public.read")])
def news(limit: int = 20):
    items = db.query("SELECT title, url, source, published_at, published_ts FROM news "        # 발행 시각 인덱스
                     f"ORDER BY {db.NEWS_TS.format(p='')} DESC, news_id DESC "
                     "LIMIT :n", {"n": max(1, min(limit, 100))})
    return {"items": items}


@app.get("/api/news/home", dependencies=[pep.actor("public.read")])
@app.get("/news/home", dependencies=[pep.actor("public.read")])
def news_home():
    """뉴스 화면(팀 news.html · assets/news-api.js 계약) — 오늘의 이슈 TOP · 핵심 테마 TOP5 ·
    인기 기사 랭킹(주간/월간) · 실시간 뉴스 + AI 뉴스 픽.
    기사 조회수는 어디서도 공개되지 않아 views=null, 순위는 보도량(coverage: 관련 기사·언론사 수) 기준."""
    return {"status": "success", "message": "", "data": news_home_svc.home()}


# ── 인증 (팀원2) ──────────────────────────────────────────
def _client_ip(request: Request):
    """요청 IP — nginx 뒤라면 uvicorn 이 X-Forwarded-For 로 채운다(기본 --proxy-headers, 127.0.0.1 신뢰).
    nginx 는 반드시 `proxy_set_header X-Forwarded-For $remote_addr;` 로 '덮어써야' 한다(deploy/nginx.conf).
    클라이언트가 보낸 X-Forwarded-For 에 덧붙이면($proxy_add_x_forwarded_for) IP 를 속여 시도 제한을 피할 수 있다."""
    return request.client.host if request.client else "unknown"


def _too_many(wait, what):
    raise HTTPException(429, f"{what} 시도가 너무 많아요. {max(1, (wait + 59) // 60)}분 뒤 다시 시도해 주세요",
                        headers={"Retry-After": str(wait)})


class LoginReq(AuthReq):
    otp: str | None = Field(None, max_length=20)          # 2단계 인증을 켠 계정만(앱 6자리 또는 복구 코드)
    remember: bool = False                                # 웹 '로그인 유지' — 거짓이면 세션 쿠키(브라우저를 닫으면 끝)


class RefreshReq(_Req):
    refresh_token: str | None = Field(None, max_length=200)   # API 클라이언트용 — 웹은 HttpOnly 쿠키


class StepUpReq(_Req):
    password: str = Field(max_length=1024)
    otp: str | None = Field(None, max_length=20)


class OtpReq(_Req):
    code: str = Field(max_length=20)


def _is_web(request: Request) -> bool:
    """웹(브라우저) 클라이언트 — 토큰을 본문 대신 HttpOnly 쿠키로 받는다(스크립트가 읽을 수 없게)."""
    return (request.headers.get("x-client") or "").lower() == "web"


def _auth_fail(e):
    """서비스의 AuthError → HTTP 응답(이유 코드 포함)."""
    raise pep.PolicyDenied(getattr(e, "status", 400), getattr(e, "code", "auth_failed"), str(e))


@app.post("/auth/register", dependencies=[pep.actor("auth.login")])
def register(req: AuthReq, request: Request):
    key = ratelimit.key_of("register", _client_ip(request))     # IP 당 1시간 REGISTER_MAX_PER_IP 번
    wait = ratelimit.take([(key, settings.REGISTER_MAX_PER_IP)], 3600)   # 확인+기록을 한 번에(동시 요청도 한도 안)
    if wait:
        _too_many(wait, "회원가입")
    if _is_web(request) and request.headers.get("origin") and not pep.origin_allowed(request.headers["origin"]):
        raise pep.PolicyDenied(403, "csrf_failed", "허용되지 않은 출처의 요청이에요")
    try:
        res = auth_svc.register(req.username, req.password)
    except auth_svc.AuthError as e:
        raise HTTPException(400, str(e))
    with sec_rls.elevated("register_log"):                      # 아직 익명 주체 — 새 계정의 접속 기록을 남긴다
        account_svc.record_access(req.username, "register")     # 접속 로그
    audit_svc.record("auth.register", req.username, ip=_client_ip(request))
    return res


@app.post("/auth/login", dependencies=[pep.actor("auth.login")])
def login(req: LoginReq, request: Request):
    """로그인 — 시도 제한(IP·아이디, 메모리) + 계정 연속 실패 잠금(DB) + 2단계 인증(켠 계정).
    웹(X-Client: web)은 토큰을 HttpOnly·SameSite=Strict 쿠키로, 그 밖의 클라이언트는 본문(Bearer)으로 받는다."""
    ip, win = _client_ip(request), settings.LOGIN_WINDOW_SECONDS
    web = _is_web(request)
    if web and request.headers.get("origin") and not pep.origin_allowed(request.headers["origin"]):
        raise pep.PolicyDenied(403, "csrf_failed", "허용되지 않은 출처의 요청이에요")   # 로그인 CSRF 차단
    k_user, k_ip = ratelimit.key_of("login", ip, req.username), ratelimit.key_of("login-ip", ip)
    # 시도를 먼저 '예약'(확인+기록 원자적) — 실패하면 그대로 실패 1회, 성공하면 되돌린다(동시 요청도 한도 안).
    wait = ratelimit.take([(k_user, settings.LOGIN_MAX_FAILS), (k_ip, settings.LOGIN_MAX_FAILS_PER_IP)], win)
    if wait:
        _too_many(wait, "로그인")
    try:                                                        # 기기 결속 키 — 증명이 있으면 그 키에 세션을 묶는다(dpop.py)
        sec_ctx.current().dpop_jkt = sec_dpop.from_request(request)
    except sec_dpop.DpopError as e:
        ratelimit.undo(k_user)
        ratelimit.undo(k_ip)
        raise pep.PolicyDenied(400, e.code, str(e))
    try:
        res = auth_svc.login(req.username, req.password, otp=req.otp, client="web" if web else "api",
                             persistent=bool(req.remember) or not web)
    except auth_svc.AuthError as e:
        audit_svc.record("auth.login", (req.username or "")[:50], ip=ip, result="fail",
                         detail={"code": e.code})              # 실패도 남긴다(무차별 대입 추적)
        if e.code == "otp_required":
            ratelimit.undo(k_user)                              # 비밀번호는 맞았다 — 실패로 세지 않음
            ratelimit.undo(k_ip)
        _auth_fail(e)
    except Exception:
        ratelimit.undo(k_user)                                  # 서버 오류는 실패로 세지 않는다
        ratelimit.undo(k_ip)
        raise
    ratelimit.reset(k_user)
    ratelimit.undo(k_ip)
    issued = res.pop("_issued")
    ctx = sec_ctx.current()
    with sec_rls.elevated("login_log"):                         # 로그인 직전 주체는 익명 — 그 계정의 접속 기록만 남긴다
        account_svc.record_access(res["username"], "login", ctx.device_label)   # 접속 기록(기기 계열 — 화면 '접속 기록')
    audit_svc.record("auth.login", res["username"], ip=ip, detail={"session": issued.session_id[:8],
                                                                   "mfa": res["mfa"], "client": "web" if web else "api"})
    if web:
        ctx.set_cookies.extend(pep.login_cookies(issued, ctx))
        return {"username": res["username"], "mode": "cookie", "expires_in": res["expires_in"], "mfa": res["mfa"]}
    return {"username": res["username"], "token": res["token"], "refresh_token": res["refresh_token"],
            "token_type": "Bearer", "expires_in": res["expires_in"], "mfa": res["mfa"]}


@app.post("/auth/refresh", dependencies=[pep.actor("auth.refresh")])
def refresh(request: Request, req: RefreshReq | None = None):
    """토큰 갱신 — 갱신 토큰을 새것으로 바꾸고(회전) 새 접근 토큰 발급. 이미 쓴 갱신 토큰이 오면 세션 폐기.
    웹: 갱신 쿠키(경로 /auth 한정) + X-CSRF-Token(=CSRF 쿠키 값) 필요. API: 본문 refresh_token."""
    ctx = sec_ctx.current()
    token = (req.refresh_token if req else None)
    web = not token
    if web:
        token = pep._from_cookies(request, "rt")
        csrf_cookie, csrf_hdr = pep._from_cookies(request, "csrf"), request.headers.get("x-csrf-token")
        if not token or not csrf_cookie or not csrf_hdr or not secrets.compare_digest(csrf_cookie, csrf_hdr):
            raise pep.PolicyDenied(401, "token_invalid", sec_sessions.MESSAGES["token_invalid"])
    try:                                                        # 기기 결속 세션은 같은 키의 증명이 있어야 갱신된다
        ctx.dpop_jkt = sec_dpop.from_request(request)
    except sec_dpop.DpopError as e:                             # 깨진·재사용 증명 — 세션은 두고 이 요청만 거절
        raise pep.PolicyDenied(401, e.code, str(e))
    try:
        issued = sec_sessions.refresh(token, ctx)
    except sec_sessions.SessionError as e:
        if web:
            ctx.set_cookies.extend(pep.clear_cookies(ctx))
        raise pep.PolicyDenied(e.status, e.code, str(e))
    if web:
        ctx.set_cookies.extend(pep.login_cookies(issued, ctx))
        return {"ok": True, "mode": "cookie", "expires_in": issued.expires_in}
    return {"ok": True, "token": issued.access, "refresh_token": issued.refresh, "token_type": "Bearer",
            "expires_in": issued.expires_in}


@app.post("/auth/step-up")
def step_up(req: StepUpReq, p: Principal = pep.actor("auth.session")):
    """추가 인증 — 탈퇴·큰 금액 송금·위험 신호가 있을 때 비밀번호(+OTP)를 한 번 더 확인(유효 5분)."""
    key = ratelimit.key_of("stepup", p.name)
    wait = ratelimit.take([(key, settings.LOGIN_MAX_FAILS)], settings.LOGIN_WINDOW_SECONDS)
    if wait:
        _too_many(wait, "추가 인증")
    try:
        res = auth_svc.step_up(p, req.password, req.otp)
    except auth_svc.AuthError as e:
        audit_svc.record("auth.step_up", p.name, result="fail")
        _auth_fail(e)
    ratelimit.reset(key)
    audit_svc.record("auth.step_up", p.name, detail={"mfa": res["mfa"]})
    return res


@app.get("/auth/sessions")
def my_sessions(p: Principal = pep.actor("auth.session")):
    """내 로그인 세션(기기) 목록 — 기기 계열·IP 대역·마지막 사용 시각. 모르는 기기는 바로 종료할 수 있다."""
    return {"items": sec_sessions.list_for(p.name, p.session_id)}


@app.delete("/auth/sessions/{session_id}")
def end_session(session_id: str, p: Principal = pep.actor("auth.session")):
    mine = {s["session_id"] for s in sec_sessions.list_for(p.name)}
    if session_id not in mine:
        raise HTTPException(404, "세션 없음")
    sec_sessions.revoke(session_id, "user_revoke")
    audit_svc.record("auth.session_revoke", p.name, target=session_id[:8])
    if session_id == p.session_id:
        sec_ctx.current().set_cookies.extend(pep.clear_cookies())
    return {"ok": True}


@app.post("/auth/logout-all")
def logout_all(p: Principal = pep.actor("auth.session")):
    """모든 기기에서 로그아웃(이 세션 포함)."""
    n = sec_sessions.revoke_user(p.name, "logout_all")
    audit_svc.record("auth.logout_all", p.name, detail={"sessions": n})
    sec_ctx.current().set_cookies.extend(pep.clear_cookies())
    return {"ok": True, "sessions": n}


@app.get("/auth/mfa")
def mfa_status(p: Principal = pep.actor("auth.session")):
    return sec_totp.status(p.name)


@app.post("/auth/mfa/setup")
def mfa_setup(p: Principal = pep.actor("auth.mfa")):
    """2단계 인증 등록 시작 — OTP 앱에 등록할 비밀키(otpauth URI). 최근 5분 안 재인증 필요."""
    try:
        res = sec_totp.begin(p.name)
    except sec_totp.MfaError as e:
        raise HTTPException(409, str(e))
    audit_svc.record("auth.mfa_setup", p.name)
    return res


@app.post("/auth/mfa/confirm")
def mfa_confirm(req: OtpReq, p: Principal = pep.actor("auth.mfa")):
    """앱이 보여 주는 6자리로 등록 확인 → 켜짐 + 복구 코드 10개(지금 한 번만 보여 준다)."""
    try:
        codes = sec_totp.confirm(p.name, req.code)
    except sec_totp.MfaError as e:
        sec_events.emit("mfa_fail", "medium", username=p.name, phase="enroll")
        raise HTTPException(400, str(e))
    sec_sessions.step_up(p.session_id, 2)                    # 방금 OTP 를 확인했다 — 이 세션은 AAL2
    audit_svc.record("auth.mfa_enable", p.name)
    return {"enabled": True, "recovery_codes": codes}


@app.post("/auth/mfa/disable")
def mfa_disable(req: OtpReq, p: Principal = pep.actor("auth.mfa")):
    if not sec_totp.verify(p.name, req.code):
        sec_events.emit("mfa_fail", "medium", username=p.name, phase="disable")
        raise HTTPException(400, "인증 코드가 맞지 않아요")
    sec_totp.disable(p.name)
    sec_rbac.deactivate(p)                                    # 관리 권한은 OTP 가 전제 — 함께 끈다
    audit_svc.record("auth.mfa_disable", p.name)
    return {"enabled": False}


# ── 실습 소유권 — 계정 세션은 그 계정만, 게스트 세션은 그 게스트 쿠키만 ──────────────────
def _owner_name(p: Principal) -> str:
    """실습 기록의 주인 — 로그인하면 계정, 아니면 서버가 발급한 게스트 ID(클라이언트가 고를 수 없다)."""
    return p.name if p.kind in ("user", "guest") and p.name else "guest"


def _check_owner(owner, p: Principal, what):
    if owner is None:
        raise HTTPException(404, f"{what} 세션 없음")
    if owner == "guest":
        # 예전 공용 데모 세션(run.py demo 의 seed_demo_progress) — 지금은 방문자마다 게스트 ID 라 화면 어디에서도 쓰지 않는데,
        # 번호만 알면 누구나 읽고·진행하고·매매할 수 있었다(2026-11-02 취약점 자가 점검 A4 'IDOR' 로 발견). 이제 아무에게도 열지 않는다
        sec_events.emit("authz_deny", "low", username=p.name if p.is_user else None, resource=what, owner_kind="shared")
        raise HTTPException(404, f"{what} 세션 없음")
    if owner == p.name:
        return
    if auth_svc.is_guest_id(owner) or p.is_user:
        sec_events.emit("authz_deny", "low", username=p.name if p.is_user else None, resource=what,
                        owner_kind="guest" if auth_svc.is_guest_id(owner) else "user")
        raise HTTPException(403, "본인 세션이 아닙니다")
    raise HTTPException(401, "로그인이 필요합니다")


# ── 투자 성향 설문 (OUTOFSTOCK_투자성향설문.docx) ──────────
@app.get("/survey/questions", dependencies=[pep.actor("public.read")])
def survey_questions(all_items: bool = Query(False, alias="all")):
    """5개 영역 × 4문항 은행에서 영역별 2문항씩 무작위 10문항(보기 0~3점). ?all=true 면 20문항 전체."""
    return survey_svc.questions(all_items=all_items)


@app.post("/survey/submit")
def survey_submit(req: SurveyReq, p: Principal = pep.actor("survey.submit")):
    """채점(0~30점) → 안정형·중립형·공격형 + 성향별 추천 상품 3개. 로그인 상태면 성향 이력에 저장."""
    try:
        result = survey_svc.submit(req.answers)
    except survey_svc.SurveyError as e:
        raise HTTPException(400, str(e))
    account_svc.record_survey(p.name if p.is_user else None, result)   # 로그인 상태면 유저 성향 이력에 기록
    return result


# ── 운영 관측성 ───────────────────────────────────────────
@app.get("/health", dependencies=[pep.actor("public.read")])
def health():
    """헬스체크(공개) — 상태·DB 연결·문제 코드·데이터 기준일·마지막 배치 결과만. 내부 오류 문구·알림 제목·
    품질 게이트 세부는 /admin/health(권한 필요)로 — 공개 응답으로 내부 구조가 새지 않게(정보 노출 최소화).
    DB 에 못 붙으면 503 — 배포 스크립트·도커 헬스체크가 '실패'로 알아채게."""
    h = mon.public_health()
    return h if h["db_ok"] else JSONResponse(status_code=503, content=h)


@app.get("/admin/health")
def admin_health(p: Principal = pep.actor("admin.audit")):
    """헬스체크(상세) — 배치 기록·뉴스 수집·지수 출처·추천 상태·품질 게이트·최근 알림."""
    return mon.health()


# ══ OUTOFSTOCK 리디자인 ═══════════════════════════════════
# ── 홈: 지수 · 성향별 추천상품 · 학습 요약 ────────────────
@app.get("/indices", dependencies=[pep.actor("public.read")])
def indices():
    """시장 지수 — 코스피·코스닥·원/달러·국고채."""
    return market_svc.indices()


@app.get("/reco/products", dependencies=[pep.actor("public.read")])
def reco_products(profile: str = "안정형"):
    """성향별 추천 상품 3개(OUTOFSTOCK_추천상품_선정.docx). profile=안정형|중립형|공격형."""
    return products_svc.for_profile(profile)


@app.get("/yangbong/weekly", dependencies=[pep.actor("public.read")])
def yangbong_weekly():
    """양봉이 주간 추천(팀 알고리즘) — 이번 주 5종목 · 30일이 지난 가장 최근 바구니 결과 · 누적 성적(승률·평균·매주 100만 원씩).
    수익률은 소수(0.05 = +5%). source: seed(팀 백테스트 2022~2026) | batch(우리 배치가 고른 주). 교육용 예시 · 투자 권유 아님."""
    return yangbong_svc.weekly()


@app.get("/reco/top5", dependencies=[pep.actor("public.read")])
def reco_top5(limit: int = 5, profile: str | None = None):
    """오늘의 추천 종목 Top 5 — 멀티팩터 모델(모멘텀·기술적 신호·가치·재무 건전성·뉴스 심리·수급·안정성).
    종목마다 종합점수(0~100)·등급·팩터 기여도·근거·주의·위험 수준. profile=안정형|중립형|공격형 이면 적합성 필터.
    교육용 예시 · 투자 권유 아님."""
    return signals_svc.today(limit, profile)


@app.get("/reco/top5/review", dependencies=[pep.actor("public.read")])
def reco_top5_review(days: int = 30, limit: int = 5):
    """한 달 전 추천 결과 — 그날 보여준 Top 5(스냅샷, 없으면 그날 기준 재계산)를 그날 종가에 샀다면 지금 몇 %인지
    + 같은 기간 유니버스 평균(벤치마크)·초과수익."""
    return signals_svc.review(days, limit)


def _heavy_cached(request: Request, key, fn, ttl=600):
    """무거운 계산(백테스트·모형 비교·모니터링, VM 규모 1회 0.5~2.5초) — 캐시(신선·갱신 중 값)로 답할 수 있으면 바로,
    새로 계산해야 할 때만 IP 당 1분 HEAVY_MISS_PER_MIN 번까지(넘으면 429 + Retry-After).
    파라미터를 바꿔 가며 캐시를 계속 비껴가는 요청이 CPU 를 붙잡고 공용 캐시의 Top 5 를 밀어내지 못하게.
    이미 누가 같은 계산을 하는 중이면 그 결과를 기다릴 뿐이라 세지 않는다(한 IP 뒤의 여러 사용자)."""
    from app.services import reco_engine as R
    if not (R.cache_has(key, ttl) or R.cache_busy(key)):
        k = ratelimit.key_of("heavy", _client_ip(request))
        wait = ratelimit.take([(k, settings.HEAVY_MISS_PER_MIN)], 60)   # 확인+기록 한 번에(동시 요청도 한도 안)
        if wait:
            raise HTTPException(429, f"계산 요청이 많아요. {wait}초 뒤 다시 시도해 주세요",
                                headers={"Retry-After": str(wait)})
    return R._cached(key, fn, ttl=ttl)


@app.get("/reco/top5/backtest", dependencies=[pep.actor("public.read")])
def reco_top5_backtest(request: Request, days: int = 120, horizon: int = 20, step: int | None = None,
                       limit: int = 5, profile: str | None = None):
    """모델 검증(워크포워드 백테스트) — 과거 기준일마다 그날까지의 데이터로만 Top N 을 뽑아 horizon 거래일 뒤 성과 측정.
    벤치마크(적격 종목 동일가중) 대비 초과수익·승률·적중률·IC(점수-수익 순위상관)·누적 곡선. 10분 캐시.
    값은 허용 값 중 가장 가까운 값으로 맞춘다 — days 60/90/120/180/250/400 · horizon·step 5/10/20/40/60 ·
    limit 3/5/10 (실제 쓴 값은 응답 params)."""
    from app.services import reco_engine, catalog
    prof = profile if profile in reco_engine.PROFILES else None
    days, horizon, step, limit = reco_engine.backtest_params(days, horizon, step, limit)
    key = ("bt", days, horizon, step, limit, prof, len(catalog.universe()))
    return _heavy_cached(request, key, lambda: reco_engine.backtest(days, horizon, step, limit, prof))


@app.get("/reco/model", dependencies=[pep.actor("public.read")])
def reco_model():
    """모델 설명서(Model Card) — 팩터 정의·국면별 가중치·적격성 필터·분산 제약·적합성 기준·데이터 출처·한계."""
    from app.services import reco_engine as R
    cur = signals_svc.today(5).get("model") or {}
    return {"version": R.MODEL_VERSION, "purpose": "교육용 종목 학습 추천(투자 권유 아님)",
            "factors": [{"key": k, "label": R.FACTOR_LABEL[k], "desc": R.FACTOR_DESC[k],
                         "weights": {reg: w[k] for reg, w in R.REGIME_WEIGHTS.items()}} for k in R.FACTORS],
            "regimes": {k: R.REGIME_LABEL[k] for k in R.REGIME_WEIGHTS},
            "regime_rule": "60일선 위 종목 비율 60%↑(코스피도 60일선 위)=상승 · 40%↓ 또는 코스피 60일선 아래·20일 −5%↓=하락",
            "eligibility": ["25거래일 이상 시세", "가격 변화·거래가 멈춘 종목 제외(거래정지 의심)",
                            "20일 평균 거래대금 ≥ min(5억원, 유니버스 중앙값의 5%)",
                            "단기 과열 제외(1일 +20%·5일 +35% 이상)", "급락 중 제외(20일 −40% 이하)", "자본잠식 제외"],
            "diversification": {"max_corr": R.MAX_CORR, "max_per_sector": R.MAX_PER_SECTOR,
                                "rule": "이미 고른 종목과 60일 수익률 상관이 높거나 같은 업종 3번째·보통주/우선주 중복이면 건너뜀"},
            "profiles": {k: v["desc"] for k, v in R.PROFILES.items()},
            "standardization": "적격 종목 안에서 5~95% 윈저라이즈 → z-점수(±3 제한), 결측은 0(중립), 데이터 없는 팩터는 제외 후 가중치 재분배",
            "point_in_time": "일봉·시총은 기준일 이하, 재무는 공시일(없으면 결산 후 90일) 이후, 뉴스는 발행일 이후에만 반영",
            "audit": "매일 결과를 reco_snapshot 에 기록(버전·팩터 기여·근거) — /reco/top5/history",
            "current": cur,
            "limitations": ["과거 데이터 기반 통계 모델 — 미래 수익을 보장하지 않음",
                            "뉴스 감성은 제목 사전(lexicon) 기반이라 문맥 오판 가능",
                            "업종 정보가 없는 종목은 상관 제약으로만 분산"]}


@app.get("/reco/model/monitor", dependencies=[pep.actor("public.read")])
def reco_model_monitor(request: Request, ref_days: int = Query(60, ge=20, le=250)):
    """모형 모니터링 — 입력 변수 안정성(PSI: 기준일 vs 최신일)·적격 모집단·국면 변화·IC 추이 → 안정/주의/재검토.
    신용평가 모형의 정기 모니터링(PSI·성능지표) 관행을 추천 모델에 적용. 10분 캐시.
    ref_days 는 20/40/60/120/250 중 가장 가까운 값(응답 ref_days)."""
    from app.services import reco_engine as R
    ref_days = R.monitor_ref_days(ref_days)
    return _heavy_cached(request, ("monitor", ref_days, len(R.catalog.universe())), lambda: R.monitor(ref_days))


@app.get("/reco/model/challengers", dependencies=[pep.actor("public.read")])
def reco_model_challengers(request: Request, days: int = 180, horizon: int = 20, step: int | None = None,
                           limit: int = 5):
    """모형 검증 — Champion–Challenger: 현재 모형 vs 동일가중·모멘텀 단일·저변동 단일을 같은 워크포워드 기간·규칙으로
    비교(평균 초과수익·승률·IC·누적) + 기간별 초과수익 차이의 쌍체 t값. 10분 캐시. 값 맞춤은 백테스트와 같음."""
    from app.services import reco_engine as R
    days, horizon, step, limit = R.backtest_params(days, horizon, step, limit)
    key = ("cc", days, horizon, step, limit, len(R.catalog.universe()))
    return _heavy_cached(request, key, lambda: R.compare_models(days, horizon, step, limit))


@app.get("/market/stocks", dependencies=[pep.actor("public.read")])
def market_stocks(sort: str = "value", limit: int = 30):
    """종목 순위표 — 현재가/등락률/거래량/거래대금/시총. sort=value|volume|marcap|rise|fall."""
    return market_svc.stock_list(sort, _clamp(limit, 1, 200))


# ── 해외 주식(미국): 국내와 같은 순위·검색·상세·차트 ─────────


@app.get("/overseas/list", dependencies=[pep.actor("public.read")])
def overseas_list(sort: str = "value", limit: int = 50):
    """해외 순위표 — sort=value(거래대금)|volume|marcap|rise|fall|price. 현재가 규칙은 국내와 동일."""
    return overseas_svc.stock_list(sort, _clamp(limit, 1, 200))


@app.get("/overseas/indices", dependencies=[pep.actor("public.read")])
def overseas_indices():
    """미국 3대 지수(S&P500·나스닥·다우) + 원/달러."""
    return overseas_svc.indices()


@app.get("/overseas/{symbol}/detail", dependencies=[pep.actor("public.read")])
def overseas_detail(symbol: str):
    """해외 종목 상세 — 시가/고가/저가/거래량/거래대금/시총/PER/PBR/52주 + EDGAR 재무 + 원화 환산."""
    d = overseas_svc.detail(symbol.upper())
    if not d:
        raise HTTPException(404, f"해외 종목 없음: {symbol}")
    return d


@app.get("/overseas/{symbol}/ohlc", dependencies=[pep.actor("public.read")])
def overseas_ohlc(symbol: str, period: str = "6M", indicators: str = ""):
    """해외 종목 캔들(장 마감 후에도 마지막 캔들=당일) + 지표. period=1M|3M|6M|1Y."""
    return overseas_svc.ohlc(symbol.upper(), period, indicators)


# ── 학습: 용어 · 보조지표 · 매매기법 ──────────────────────


@app.get("/learn/today", dependencies=[pep.actor("public.read")])
def learn_today():
    return learn_svc.today_learn()


@app.post("/learn/log")
def learn_log(req: LearnLogReq, p: Principal = pep.actor("learn.log")):
    """학습 열람 기록 — 로그인 상태면 유저 학습로그에 남긴다."""
    account_svc.record_learning(p.name if p.is_user else None, req.kind, req.item_key, req.item_name)
    return {"ok": True}


@app.get("/learn/dictionary", dependencies=[pep.actor("public.read")])
def learn_dictionary():
    """학습하기(팀 용어 사전 화면) 전체 — 용어 카드(비유·풀이·난이도·주제·별칭·연관·실습·더 알아보기·상세)."""
    return learn_svc.dictionary()


@app.get("/learn/progress")
def learn_progress(user=pep.user("account.read")):
    """'이해했어요' 표시한 용어(계정 저장) — 학습 로그를 이벤트로 복원."""
    return learn_svc.progress(user)


@app.post("/learn/progress")
def learn_progress_set(req: LearnProgressReq, user=pep.user("account.write")):
    try:
        return learn_svc.set_learned(user, req.term, req.done)
    except KeyError:
        raise HTTPException(404, "없는 용어예요")


@app.post("/learn/progress/sync")
def learn_progress_sync(req: LearnSyncReq, user=pep.user("account.write")):
    """로그인 전 브라우저에 표시해 둔 용어를 계정으로 합침."""
    return learn_svc.sync_learned(user, req.terms[:500])


@app.post("/learn/progress/reset")
def learn_progress_reset(user=pep.user("account.write")):
    return learn_svc.reset_learned(user)


# ── Daily Stock Quiz(웹 용어사전) — 서버 채점·하루 한 번 ─────────
@app.get("/learn/quiz")
def learn_quiz(day: str | None = Query(None, alias="date", max_length=10), user=pep.user("account.read")):
    """그날(기본: 한국 날짜 오늘) 퀴즈 기록 + 지금까지 푼 날 수(마이페이지 '퀴즈 완료')."""
    try:
        return quiz_svc.state(user, day)
    except quiz_svc.QuizError as e:
        raise HTTPException(400, str(e))


@app.post("/learn/quiz")
def learn_quiz_submit(req: QuizSubmitReq, user=pep.user("account.write")):
    """다 푼 퀴즈 제출 — 화면은 고른 보기 번호만 보내고, 서버가 같은 문제은행·같은 날짜 공식으로 그날 문제를 다시 만들어
    채점한다(점수를 화면이 정하지 않는다). 같은 날 두 번째 제출은 처음 결과 그대로(duplicate: true).
    정답 보상: 점수 × QUIZ_REWARD_PER_CORRECT 를 같은 트랜잭션에서 지갑에 지급(reward·balance) — 한국 날짜 하루 1번."""
    try:
        return quiz_svc.submit(user, req.date, req.answers)
    except quiz_svc.QuizError as e:
        raise HTTPException(400, str(e))


# ── 실습(과거 사례): 시나리오 목록 + 진행 ─────────────────
@app.get("/practice/scenarios")
def practice_scenarios(p: Principal = pep.actor("practice.use")):
    """과거 사례 실습 목록 + 유저별 상태(미시작/진행중/완료) + 요약 통계.
    비로그인은 서버가 발급한 게스트 쿠키로 구분(다른 방문자와 진행 상태가 섞이지 않게 — user_id 파라미터는 무시)."""
    return practice_svc.list_scenarios(_owner_name(p))


@app.get("/practice/home")
def practice_home(p: Principal = pep.actor("practice.use")):
    """홈 '진행 중인 실습' 배너(없으면 null)."""
    return {"active": practice_svc.home_active(_owner_name(p))}


# 웹 '학습 및 실습하기' 기록 — /practice/{session_id} 보다 먼저 선언(경로 순서: 'records' 가 번호로 해석되지 않게)
@app.get("/practice/records")
def practice_records(limit: int = 20, offset: int = 0, user=pep.user("account.read")):
    """내 실습 기록 — 기법별 집계(summary: 연습·실전 횟수·최고 수익률·매매 완료) + 최근 기록."""
    return practice_records_svc.overview(user, _clamp(limit, 1, 100), _clamp(offset, 0, 1_000_000))


@app.post("/practice/records")
def practice_record_add(req: PracticeRecordReq, user=pep.user("account.write")):
    """실습 한 판 기록 — 수익률은 브라우저 계산값이라 범위·종류만 확인하고 보상과 잇지 않는다(표시용).
    계정당 1시간 PRACTICE_RECORD_MAX_PER_HOUR 건까지."""
    wait = ratelimit.take([(ratelimit.key_of("practice-record", user), settings.PRACTICE_RECORD_MAX_PER_HOUR)], 3600)
    if wait:
        _too_many(wait, "실습 기록 저장")
    try:
        return {"ok": True, "summary": practice_records_svc.add(user, req.tech, req.mode, req.traded, req.ret,
                                                                req.entry_date)}
    except practice_records_svc.RecordError as e:
        raise HTTPException(400, str(e))


@app.post("/practice/start")
def practice_start(req: PracticeStartReq, p: Principal = pep.actor("practice.use")):
    owner = _owner_name(p)                          # 로그인 시 그 계정, 아니면 게스트 쿠키 ID(본문 user_id 는 무시)
    try:
        return practice_svc.start(owner, req.scenario_id)
    except practice_svc.PracticeError as e:
        raise HTTPException(400, str(e))


def _check_practice_owner(session_id: int, p: Principal):
    """실습 세션은 주인만 — 계정 세션은 그 계정 세션으로, 게스트 세션은 그 게스트 쿠키로만(번호 추측으로 남의 실습 조작 차단).
    주인 확인 자체는 RLS 상승 조회(남의 세션이면 RLS 가 숨겨 '없음'이 되므로, 403 과 탐지 이벤트를 위해 주인만 읽는다)."""
    with sec_rls.elevated("ownership_check"):
        owner = practice_svc.owner_of(session_id)
    _check_owner(owner, p, "실습")


@app.get("/practice/{session_id}")
def practice_state(session_id: int, p: Principal = pep.actor("practice.use")):
    _check_practice_owner(session_id, p)
    try:
        return practice_svc.get_state(session_id)
    except practice_svc.PracticeError as e:
        raise HTTPException(404, str(e))


@app.post("/practice/{session_id}/step")
def practice_step(session_id: int, req: PracticeStepReq, p: Principal = pep.actor("practice.use")):
    _check_practice_owner(session_id, p)
    try:
        return practice_svc.step(session_id, req.days)
    except practice_svc.PracticeError as e:
        raise HTTPException(400, str(e))


@app.post("/practice/{session_id}/end")
def practice_end(session_id: int, p: Principal = pep.actor("practice.use")):
    _check_practice_owner(session_id, p)
    try:
        return practice_svc.end(session_id)
    except practice_svc.PracticeError as e:
        raise HTTPException(400, str(e))


@app.post("/practice/{session_id}/trade")
def practice_trade(session_id: int, req: PracticeTradeReq,
                   idempotency_key: str | None = Header(default=None),
                   p: Principal = pep.actor("practice.use")):
    _check_practice_owner(session_id, p)
    try:
        return practice_svc.trade(session_id, req.side, req.quantity, idem_key=idempotency_key)
    except practice_svc.PracticeError as e:
        raise HTTPException(400, str(e))


# ── 종목 상세: 펀더멘털 + 기간별 캔들 ─────────────────────
@app.get("/stocks/{ticker}/detail", dependencies=[pep.actor("public.read")])
def stock_detail(ticker: str):
    """종목 상세 — 시가/고저/PER/PBR/시총/52주 등."""
    d = detail_svc.stock_detail(ticker)
    if not d:
        from app.services import catalog
        if catalog.exists(ticker):                     # 아는 종목인데 시세가 아직 없음(새 VM 첫 수집 전) — '없는 종목'과 구분
            raise HTTPException(404, "아직 시세 데이터가 없어요 — 첫 수집(run.py real) 뒤에 보여요")
        raise HTTPException(404, f"종목 없음: {ticker}")
    return d


@app.get("/stocks/{ticker}/ohlc", dependencies=[pep.actor("public.read")])
def stock_ohlc(ticker: str, period: str = "6M", indicators: str = ""):
    """종목 캔들 시계열(TradingView 용) + 지표 오버레이. period=1M|3M|6M|1Y."""
    return detail_svc.ohlc_series(ticker, period, indicators)


# ── 계정 / 마이페이지 ─────────────────────────────────────
@app.get("/events")
async def events_stream(p: Principal = pep.actor("account.read")):
    """실시간 알림(SSE) — 잔액·채팅·친구 요청·세션 종료를 바뀐 순간 밀어 준다(폴링 대체).
    하트비트마다 세션을 다시 확인해 로그아웃·폐기·유휴 만료면 닫는다. 응답은 캐시·버퍼링 금지."""
    from fastapi.responses import StreamingResponse
    from app.services import stream
    sid = p.session_id
    return StreamingResponse(stream.events(p.name, lambda: sec_sessions.alive(sid)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/auth/logout")
def logout(request: Request, p: Principal = pep.actor("auth.refresh")):
    """로그아웃 — 이 세션(접근·갱신 토큰)을 서버에서 즉시 폐기, 웹이면 쿠키도 지운다.
    접근 토큰이 이미 만료됐어도 갱신 쿠키로 세션을 찾아 끝낸다 — 예전엔 만료 뒤 로그아웃하면 화면만 로그아웃되고
    갱신 쿠키가 살아 있어, 공용 PC 에서 다음 사람이 세션을 되살릴 수 있었다."""
    ctx = sec_ctx.current()
    found = (p.session_id, p.name) if p.is_user else sec_sessions.session_of_refresh(pep._from_cookies(request, "rt"))
    if found:
        sid, name = found
        sec_sessions.revoke(sid, "logout")
        with sec_rls.elevated("logout_log"):                    # 만료된 접근 토큰이면 주체가 익명 — 그 계정의 접속 기록만
            account_svc.record_access(name, "logout", ctx.device_label)
        audit_svc.record("auth.logout", name, detail={"via": "access" if p.is_user else "refresh_cookie"})
    ctx.set_cookies.extend(pep.clear_cookies())
    return {"ok": True, "ended": bool(found)}


@app.get("/me")
def me(user=pep.user("account.read")):
    """마이페이지 — 내 프로필·투자성향 + 각 로그 '최근 5건'과 전체 건수(counts)."""
    return account_svc.my_overview(user)


@app.get("/me/survey")
def me_survey(user=pep.user("account.read")):
    """내 최신 투자성향(없으면 null) — 홈 '투자 성향' 카드·추천 상품용 가벼운 조회."""
    return {"survey": account_svc.latest_survey(user)}


@app.get("/me/badges")
def me_badges(user=pep.user("account.read")):
    """내비 배지 — 안 읽은 채팅 수 + 받은 친구 요청 수(한 번에)."""
    return {"chat_unread": chat_svc.unread_total(user), "friend_requests": friends_svc.pending_count(user)}


@app.get("/me/logs/{kind}")
def me_logs(kind: str, limit: int = 20, offset: int = 0, filter: str = "all",
            user=pep.user("account.read")):
    """로그 전용 페이지(더보기) — kind=wallet|practice|learning|access|game|moneysim|survey|watchlist.
    최신순, limit/offset 페이지네이션. 지갑은 filter=all|reward|charge|transfer|sim|invest|shop."""
    try:
        return account_svc.logs(user, kind, limit, offset, filter)
    except account_svc.LogError as e:
        raise HTTPException(404, str(e))


@app.post("/auth/change-password")
def change_password(req: ChangePwReq, p: Principal = pep.actor("account.password")):
    """비밀번호 변경 — 현재 비밀번호 확인 후 변경(기존 토큰 무효화).
    현재 비밀번호를 LOGIN_MAX_FAILS 번 틀리면 잠시 429 — 훔친 토큰으로 비밀번호를 대입해 계정을 빼앗지 못하게."""
    user = p.name
    key = ratelimit.key_of("chpw", user)
    wait = ratelimit.take([(key, settings.LOGIN_MAX_FAILS)], settings.LOGIN_WINDOW_SECONDS)
    if wait:
        _too_many(wait, "비밀번호 변경")
    try:
        res = auth_svc.change_password(user, req.old_password, req.new_password, keep_session=p.session_id)
    except auth_svc.AuthError as e:
        audit_svc.record("auth.change_password", user, result="fail")
        raise HTTPException(400, str(e))
    ratelimit.reset(key)
    account_svc.record_access(user, "change_pw")
    audit_svc.record("auth.change_password", user)
    return res


@app.delete("/me")
def delete_me(user=pep.user("account.delete")):
    """회원 탈퇴 — 계정에 엮인 모든 데이터 삭제(감사 로그에는 '탈퇴했다'는 사실만 남는다). 최근 5분 안 재인증 필요."""
    res = account_svc.delete_account(user)
    audit_svc.record("account.delete", user)
    sec_ctx.current().set_cookies.extend(pep.clear_cookies())    # 세션은 계정과 함께 지워졌다 — 브라우저 쿠키도 정리
    return res


# ── 관심종목(즐겨찾기) ─────────────────────────────────────
@app.get("/watchlist")
def watchlist_list(user=pep.user("account.read")):
    """내 관심종목 + 현재가/등락률(장중 실시간 반영)."""
    items = watchlist_svc.items(user)
    return {"count": len(items), "items": items}


@app.post("/watchlist/{ticker}")
def watchlist_add(ticker: str, user=pep.user("account.write")):
    """관심종목 등록 — 유니버스에 없는 코드는 404(예전엔 200 + ok:false 라 별표가 켜진 것처럼 보였다)."""
    res = watchlist_svc.add(user, ticker)
    if not res.get("ok"):
        raise HTTPException(404, f"종목 없음: {ticker}")
    return res


@app.delete("/watchlist/{ticker}")
def watchlist_remove(ticker: str, user=pep.user("account.write")):
    return watchlist_svc.remove(user, ticker)


# ══ 추가 기능: 가상머니 지갑 생태계 (전부 로그인 필수) ═════════════
# ── 지갑 ──────────────────────────────────────────────────
@app.get("/wallet")
def wallet_overview(user=pep.user("account.read")):
    """내 지갑 — 잔액 + 최근 거래내역(원장)."""
    return wallet_svc.overview(user)


# ── 출석 체크(+가상머니) ──────────────────────────────────
@app.get("/attendance")
def attendance_status(user=pep.user("account.read")):
    """출석 현황 — 오늘 출석 여부·연속 출석·총 출석일."""
    return attendance_svc.status(user)


@app.post("/attendance/check-in")
def attendance_check_in(user=pep.user("account.write")):
    """오늘 출석 — 최초면 가상머니 지급(하루 1회, 멱등)."""
    return attendance_svc.check_in(user)


# ── 양봉이 꾸미기(가상머니 아이템) ─────────────────────────
@app.get("/avatar/catalog", dependencies=[pep.actor("public.read")])
def avatar_catalog():
    """아이템 목록(모자·옷·바지·신발·안경·펫 × 서민~전설 5등급) — 가격은 서버 사본(app/data/avatar_items.json) 기준."""
    return avatar_svc.catalog()


@app.get("/avatar/me")
def avatar_me(user=pep.user("account.read")):
    """내 양봉이 — 산 아이템·부위별 착용·몸값(입은 아이템 가격 합)·수저 등급·잔액."""
    out = avatar_svc.state(user)
    out["balance"] = wallet_svc.balance(user)
    return out


@app.post("/avatar/buy")
def avatar_buy(req: AvatarBuyReq, user=pep.user("avatar.buy")):
    """아이템 구매 — 산 기록과 지갑 차감을 한 트랜잭션에서(이미 가진 아이템은 다시 돈을 받지 않는다·잔액 부족이면 둘 다 취소).
    위험 신호(세션 IP 대역 변경 등)가 있으면 추가 인증. 화면이 보여 준 가격(price)이 서버 가격과 다르면 409."""
    try:
        return avatar_svc.buy(user, req.item_id, req.equip, expect_price=req.price)
    except avatar_svc.AvatarPriceChanged as e:
        raise HTTPException(409, str(e))
    except avatar_svc.AvatarError as e:
        raise HTTPException(400, str(e))


@app.post("/avatar/equip")
def avatar_equip(req: AvatarEquipReq, user=pep.user("account.write")):
    """갈아입기·벗기 — 산 아이템만, 부위마다 하나(한 트랜잭션)."""
    try:
        return avatar_svc.equip(user, req.items)
    except avatar_svc.AvatarError as e:
        raise HTTPException(400, str(e))


# ── 인앱결제 충전 ─────────────────────────────────────────
@app.get("/iap/products", dependencies=[pep.actor("public.read")])
def iap_products():
    """충전 상품(코인 팩) 목록."""
    return iap_svc.products()


@app.post("/iap/purchase")
def iap_purchase(req: IapPurchaseReq, user=pep.user("wallet.charge")):
    """스토어 영수증 검증 후 가상머니 충전(영수증 재사용 방지)."""
    try:
        return iap_svc.purchase(user, req.store, req.product_id, req.receipt)
    except iap_svc.IAPError as e:
        raise HTTPException(400, str(e))


# ── P2P 송금 ──────────────────────────────────────────────
@app.post("/transfer")
def transfer_send(req: TransferReq, request: Request, p: Principal = pep.actor("wallet.transfer"),
                  idempotency_key: str | None = Header(default=None, max_length=200)):
    """친구(다른 계정)에게 가상머니 송금(원자적 출금+입금).
    Idempotency-Key 헤더(선택, 200자 이하): 같은 키로 다시 보내면 송금은 1번만, 처음 결과를 duplicate:true 로
    돌려준다 — 더블클릭·재시도 대비. 같은 키로 받는 사람·금액이 다르면 409.
    금액 기준 추가 인증: 정책(wallet.transfer)의 amount_gte 이상이면 최근 5분 안 재인증이 있어야 한다."""
    pep.check(request, "wallet.transfer", amount=req.amount)
    user = p.name
    try:   # 이상거래탐지는 송금 트랜잭션 안(지갑 잠금·멱등 확인 뒤)에서 — 동시 송금·재시도도 정확히 채점(결함 20·21)
        res = transfer_svc.send(user, req.to, req.amount, req.memo, idem_key=idempotency_key,
                                risk=p.risk, fresh=sec_sessions.fresh(p))
    except transfer_svc.TransferHeld as e:
        v = e.verdict
        if v["decision"] == "block":
            audit_svc.record("wallet.transfer", user, target=req.to, result="blocked",
                             detail={"amount": req.amount, "fds": {"score": v["score"], "rules": v["rules"]}})
            raise pep.PolicyDenied(403, "fds_blocked", "이상 거래가 의심돼 송금을 막았어요. 잠시 후 다시 시도하거나 문의해 주세요")
        raise pep.PolicyDenied(401, "step_up_required", "평소와 다른 송금이라 비밀번호를 한 번 더 확인할게요")
    except transfer_svc.TransferConflict as e:
        raise HTTPException(409, str(e))
    except transfer_svc.TransferError as e:
        audit_svc.record("wallet.transfer", user, target=req.to, detail={"amount": req.amount}, result="fail")
        raise HTTPException(400, str(e))
    if not (isinstance(res, dict) and res.get("duplicate")):
        audit_svc.record("wallet.transfer", user, target=req.to, detail={"amount": req.amount})
    return res


# ── 장기 실전 모의투자(현재가 매수 → 실제 최신가 추적) ────
@app.get("/invest")
def invest_positions(user=pep.user("account.read")):
    """내 장기 투자 — 보유(평가손익)·청산(실현손익)·합계."""
    return invest_svc.positions(user)


@app.get("/invest/risk")
def invest_risk(user=pep.user("account.read")):
    """내 포트폴리오 리스크 · 스트레스 테스트 — 1일 VaR 95%·ES 97.5%(원화)·코스피 베타·시나리오 손실
    (코스피 −5/−10/−20%, 코로나19형, 금융위기형)·과거 최악 구간 재현·집중도(HHI·유효 종목 수·한도)."""
    from app.services import stress as stress_svc
    return stress_svc.invest_risk(user)


@app.post("/invest/buy")
def invest_buy(req: InvestBuyReq, user=pep.user("invest.trade")):
    """현재가로 모의 매수(지갑에서 출금)."""
    try:
        return invest_svc.buy(user, req.ticker, req.quantity)
    except (invest_svc.InvestError, wallet_svc.WalletError) as e:
        raise HTTPException(400, str(e))


@app.post("/invest/{position_id}/sell")
def invest_sell(position_id: int, req: InvestSellReq, user=pep.user("invest.trade")):
    """현재가로 모의 매도(지갑으로 입금). 수량 생략 시 전량."""
    try:
        return invest_svc.sell(user, position_id, req.quantity)
    except invest_svc.InvestError as e:
        raise HTTPException(400, str(e))


# ── 모의투자: 가상 시장 시즌(팀 invest.html — 미래 봉은 서버 밖으로 나가지 않는다) ────
def _vm_error(e: Exception):
    if isinstance(e, vmarket_svc.VMarketNotFound):
        raise HTTPException(404, str(e))
    if isinstance(e, vmarket_svc.VMarketConflict):
        raise HTTPException(409, str(e))
    raise HTTPException(400, str(e))


@app.get("/vmarket/season", dependencies=[pep.actor("public.read")])
def vmarket_season(id: int | None = Query(None, ge=1, le=100_000)):
    """시즌 차트 — 진행 중이면 서버 시계로 '지금까지 공개된 봉'까지만(사전 봉 120 + 공개된 시즌 봉), 끝난 시즌은 전부 + 장세.
    다음 시즌 이후는 404. 봉 = [시가, 고가, 저가, 종가, 거래량]. 같은 봉 수면 같은 응답이라 ETag/304."""
    try:
        return vmarket_svc.season_view(id)
    except vmarket_svc.VMarketError as e:
        _vm_error(e)


@app.get("/vmarket/me")
def vmarket_me(user=pep.user("account.read")):
    """내 모의투자 — 이번 시즌(참가금·현금·종목별 보유·체결)·끝난 시즌 기록·정산 대기 시즌·지갑 잔액(읽기만)."""
    return vmarket_svc.me(user)


@app.post("/vmarket/join")
def vmarket_join(req: VMarketJoinReq, user=pep.user("vmarket.trade")):
    """이번 시즌 참가 — 참가금을 지갑에서 빼 시즌 현금으로(한 트랜잭션, 시즌마다 한 번)."""
    try:
        return vmarket_svc.join(user, req.season, req.entry)
    except vmarket_svc.VMarketError as e:
        _vm_error(e)


@app.post("/vmarket/order")
def vmarket_order(req: VMarketOrderReq, user=pep.user("vmarket.trade"),
                  idempotency_key: str | None = Header(default=None, max_length=200)):
    """매수·매도 — 장중에만, 지금 공개된 마지막 봉 종가로 즉시 체결(체결가는 서버가 정한다). bar 가 서버와 다르면 409.
    Idempotency-Key 가 같은 재전송은 한 번만 체결. 계정당 1분 VMARKET_ORDERS_PER_MIN 건까지(넘으면 429)."""
    wait = ratelimit.take([(ratelimit.key_of("vmarket-order", user), settings.VMARKET_ORDERS_PER_MIN)], 60)
    if wait:
        _too_many(wait, "모의투자 주문")
    try:
        return vmarket_svc.order(user, req.season, req.stock, req.side, req.qty, req.bar, idem_key=idempotency_key)
    except vmarket_svc.VMarketError as e:
        _vm_error(e)


@app.post("/vmarket/settle")
def vmarket_settle(user=pep.user("vmarket.trade")):
    """끝난 내 시즌 정산 — 남은 주식을 마지막 종가로 팔고 최종 평가금액을 지갑으로(같은 시즌은 한 번만).
    매일 00:05 스케줄러도 끝난 시즌을 모두 정산한다 — 화면은 주말에 바로 결과를 보여 주려고 부른다."""
    return vmarket_svc.settle_user(user)


# ── 웹 결제(토스페이먼츠) ─────────────────────────────────
@app.get("/pay/toss/config", dependencies=[pep.actor("public.read")])
def toss_config():
    """프론트 결제창 설정(공개 클라이언트 키·모드). 키 없으면 stub 데모로 동작."""
    return toss_svc.config()


@app.post("/pay/toss/prepare")
def toss_prepare(req: TossPrepareReq, user=pep.user("wallet.charge")):
    """결제 주문 생성 — 프론트가 이 order_id/amount 로 토스 결제창을 연다."""
    try:
        return toss_svc.prepare(user, req.product_id)
    except toss_svc.TossError as e:
        raise HTTPException(400, str(e))


@app.post("/pay/toss/confirm")
def toss_confirm(req: TossConfirmReq, user=pep.user("wallet.charge")):
    """토스 결제 승인 검증 후 가상머니 적립(주문 단위 멱등)."""
    try:
        res = toss_svc.confirm(user, req.paymentKey, req.orderId, req.amount)
    except (toss_svc.TossError, wallet_svc.WalletError) as e:
        audit_svc.record("payment.confirm", user, target=req.orderId, detail={"amount": req.amount}, result="fail")
        raise HTTPException(400, str(e))
    audit_svc.record("payment.confirm", user, target=req.orderId,
                     detail={"amount": req.amount, "duplicate": bool(res.get("duplicate"))})
    return res


# ── 친구: 요청 → 상대 수락 → 친구(양방향) ─────────────────
@app.get("/users")
def users_directory(q: str = "", user=pep.user("account.read")):
    """다른 계정 사용자 목록(나 제외) + 관계(relation: friend|sent|received|none). q 로 아이디 검색."""
    return {"items": friends_svc.directory(user, q)}


@app.get("/friends")
def friends_list(user=pep.user("account.read")):
    """내 친구 목록(서로 수락한 사이만)."""
    return {"items": friends_svc.my_friends(user)}


@app.get("/friends/requests")
def friends_requests(user=pep.user("account.read")):
    """대기 중인 친구 요청 — received(받은 요청: 수락/거절) · sent(보낸 요청: 취소)."""
    return friends_svc.requests(user)


def _friend_call(fn, user, username):
    try:
        return fn(user, username)
    except friends_svc.FriendError as e:
        raise HTTPException(400, str(e))


@app.post("/friends/{username}")
def friends_request(username: str, user=pep.user("account.write")):
    """친구 요청 보내기 → status=pending. 상대가 먼저 요청해 뒀다면 바로 친구(status=friends)."""
    return _friend_call(friends_svc.request, user, username)


@app.post("/friends/{username}/accept")
def friends_accept(username: str, user=pep.user("account.write")):
    """받은 친구 요청 수락 → 서로 친구."""
    return _friend_call(friends_svc.accept, user, username)


@app.post("/friends/{username}/decline")
def friends_decline(username: str, user=pep.user("account.write")):
    """받은 친구 요청 거절."""
    return _friend_call(friends_svc.decline, user, username)


@app.delete("/friends/{username}/request")
def friends_cancel(username: str, user=pep.user("account.write")):
    """내가 보낸 친구 요청 취소."""
    return _friend_call(friends_svc.cancel, user, username)


@app.delete("/friends/{username}")
def friends_remove(username: str, user=pep.user("account.write")):
    """친구 끊기(양쪽 모두에서 삭제)."""
    return friends_svc.remove(user, username)


@app.get("/friends/{username}/profile")
def friends_profile(username: str, user=pep.user("account.read")):
    """친구/사용자 공개 프로필(민감정보 제외)."""
    try:
        return friends_svc.profile(user, username)
    except friends_svc.FriendError as e:
        raise HTTPException(404, str(e))


# ── 친구 1:1 채팅(메신저) ─────────────────────────────────
@app.get("/chat/rooms")
def chat_rooms(user=pep.user("account.read")):
    """대화방 목록 — 상대별 마지막 메시지·시각·안 읽은 수(최신순) + 전체 안 읽은 수."""
    return chat_svc.rooms(user)


@app.get("/chat/{peer}/messages")
def chat_messages(peer: str, after_id: int = Query(0, ge=0, le=2**31 - 1),
                  before_id: int | None = Query(None, ge=0, le=2**31 - 1), limit: int = 50,
                  user=pep.user("account.read")):
    """대화 내용. after_id → 새 메시지만(폴링), before_id → 이전 메시지(위로 스크롤). 조회 시 읽음 처리."""
    try:
        return chat_svc.thread(user, peer, after_id, before_id, _clamp(limit, 1, 200))
    except chat_svc.ChatError as e:
        raise HTTPException(404, str(e))


@app.post("/chat/{peer}/messages")
def chat_send(peer: str, req: ChatSendReq, user=pep.user("account.write")):
    """메시지 전송 — 서로 친구(요청 수락 완료)일 때만. 1~1000자."""
    try:
        return chat_svc.send(user, peer, req.body)
    except chat_svc.ChatError as e:
        raise HTTPException(400, str(e))


# ── 관리(API 전용 — 화면 없음) ───────────────────────────────
#  모든 관리 동작 = 역할 자격 + JIT 활성(시간 제한·사유) + AAL2(OTP 세션) + 감사 기록. 상시 관리자는 없다.
class ElevateReq(_Req):
    role: str = Field(max_length=20)
    reason: str = Field(max_length=200)
    minutes: int | None = Field(None, ge=5, le=240)


class RoleGrantReq(_Req):
    username: str = Field(max_length=50)
    role: str = Field(max_length=20)
    reason: str = Field(max_length=200)


class LockReq(_Req):
    minutes: int = Field(60, ge=1, le=10_080)
    reason: str = Field(max_length=200)


def _rbac_call(fn, *a):
    try:
        return fn(*a)
    except sec_rbac.RbacError as e:
        raise pep.PolicyDenied(e.status, e.code, str(e))


@app.get("/admin/me")
def admin_me(p: Principal = pep.actor("admin.elevate")):
    """내 관리 역할 — 자격(켤 수 있는 역할)·지금 활성인 역할·이 세션의 인증 강도."""
    return {"eligible": sorted(sec_rbac.eligible(p.name)), "active": sorted(p.roles), "aal": p.aal,
            "catalog": sec_rbac.ROLES}


@app.post("/admin/elevate")
def admin_elevate(req: ElevateReq, p: Principal = pep.actor("admin.elevate")):
    """JIT 활성 — 자격·OTP 세션(AAL2)·사유를 확인하고 최대 JIT_MAX_MINUTES 동안만 켠다."""
    return _rbac_call(sec_rbac.activate, p, req.role, req.reason, req.minutes)


@app.post("/admin/elevate/end")
def admin_elevate_end(p: Principal = pep.actor("admin.elevate")):
    return {"ended": _rbac_call(sec_rbac.deactivate, p)}


@app.get("/admin/audit")
def admin_audit(limit: int = 100, actor: str | None = None, action: str | None = None,
                p: Principal = pep.actor("admin.audit")):
    audit_svc.record("admin.audit_read", p.name, detail={"actor": actor, "action": action})
    return {"items": audit_svc.recent(_clamp(limit, 1, 500), actor, action)}


@app.get("/admin/audit/verify")
def admin_audit_verify(p: Principal = pep.actor("admin.audit")):
    """감사 로그 해시 체인 검증 — 중간 행이 바뀌거나 지워졌으면 처음 어긋난 행 번호 + 외부 증인(체인 머리) 대조."""
    return {**audit_svc.verify(), "witness": audit_svc.verify_witnesses()}


@app.get("/admin/security/events")
def admin_security_events(limit: int = 100, kind: str | None = None, severity: str | None = None,
                          p: Principal = pep.actor("admin.audit")):
    return {"items": sec_events.recent(_clamp(limit, 1, 500), kind, severity)}


@app.get("/admin/security/digest")
def admin_security_digest(hours: int = 24, p: Principal = pep.actor("admin.audit")):
    """보안 요약 — 종류·심각도별 이벤트·자동 대응 건수·재검토가 다가온 역할 자격(스케줄러가 매일 알림으로도 보냄)."""
    return sec_events.digest(_clamp(hours, 1, 24 * 30), notify=False)


@app.get("/admin/pii-access")
def admin_pii_access(days: int = 30, p: Principal = pep.actor("admin.audit")):
    """개인정보 처리(열람) 기록 점검 — 최근 days 일 목록 + 운영자별 일 단위 건수(과다 열람 표시).
    안전성 확보조치 기준 제8조②(2025 개정): 점검 주기·방법·사후조치는 내부 관리계획으로 — 이 서비스는 월 1회
    (docs/security/internal_management_plan.md §8)."""
    from app.services import privacy
    audit_svc.record("admin.pii_review", p.name, detail={"days": days})
    return privacy.access_review(_clamp(days, 1, 365))


@app.get("/admin/metrics")
def admin_metrics(fmt: str = "json", p: Principal = pep.actor("admin.audit")):
    from app.services import observability
    if fmt == "prometheus":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(observability.prometheus() + capacity_svc.prometheus())
    from app.security import egress
    from app.services import outbox
    return {**observability.snapshot(), "rls_elevations": sec_rls.stats(), "egress_circuits": egress.status(),
            "outbox": outbox.stats()}


# ── 운영 복원력: 용량·성능 관리 · 서비스 비상 차단 ─────────────────────────────
class ControlReq(_Req):
    reason: str = Field(min_length=5, max_length=200)


def _control_call(fn, *args):
    try:
        return fn(*args)
    except controls_svc.ControlError as e:
        raise pep.PolicyDenied(e.status, e.code, str(e))


@app.get("/admin/capacity")
def admin_capacity(p: Principal = pep.actor("admin.ops")):
    """부하 단계(정상·주의·경계·심각)·지표·기준·최근 성능관리보고서 — 이 워커(pid) 기준."""
    return capacity_svc.status()


@app.get("/admin/db")
def admin_db(p: Principal = pep.actor("admin.ops")):
    """DB 진단(ADR-0027) — 크기·증가 추세·디스크 소진 예측·판정(죽은 행·겹치는/안 쓰는 인덱스·캐시·XID·연결·오래 열린
    트랜잭션)·주요 설정·느린 쿼리 상위·데이터 수명주기(보관 기한·보관 파일). 읽기만 — 보관·정리는 스케줄러·CLI 몫."""
    from app.services import dbhealth
    return dbhealth.report()


@app.get("/admin/controls")
def admin_controls(p: Principal = pep.actor("admin.ops")):
    """서비스 비상 차단 상태 — 기능별 운영/중지·사유 + 대기 중 재개 요청."""
    return controls_svc.state()


@app.post("/admin/controls/{feature}/pause")
def admin_control_pause(feature: str, req: ControlReq, p: Principal = pep.actor("admin.control")):
    """기능 즉시 중지(한 사람) — 사고 때 피해 확산 방지. 다시 열려면 재개 요청 + 다른 관리자 승인."""
    return _control_call(controls_svc.pause, feature, req.reason, p.name)


@app.post("/admin/controls/{feature}/resume")
def admin_control_resume(feature: str, req: ControlReq, p: Principal = pep.actor("admin.control")):
    """재개 요청 — 원인 확인 결과를 사유로. 다른 관리자가 승인해야 열린다(2인 통제)."""
    return _control_call(controls_svc.request_resume, feature, req.reason, p.name)


@app.post("/admin/controls/requests/{request_id}/{decision}")
def admin_control_decide(request_id: int, decision: str, p: Principal = pep.actor("admin.control")):
    if decision not in ("approve", "reject"):
        raise HTTPException(404, "approve 또는 reject")
    return _control_call(controls_svc.decide, request_id, decision == "approve", p.name)


@app.get("/admin/ledger")
def admin_ledger(days: int = 14, p: Principal = pep.actor("admin.audit")):
    """원장 — 현재 시산표(계정별 합, 전체 합 0) + 최근 일 마감 결과 + 마감 해시 사슬 검증."""
    from app.services import ledger
    closes = db.query("SELECT close_date, status, entries, txns, debit, credit, close_hash FROM ledger_close "
                      "ORDER BY close_date DESC LIMIT :n", {"n": _clamp(days, 1, 366)})
    return {"trial_balance": ledger.trial_balance(), "closes": closes, "verify": ledger.verify_closes()}


@app.get("/admin/data-quality")
def admin_data_quality(run: bool = False, p: Principal = pep.actor("admin.audit")):
    """데이터 품질 — 계약 목록·최근 측정 결과·일별 통과율 추세. run=true 면 지금 다시 측정(시스템 문맥으로)."""
    from app.services import contracts
    latest = None
    if run:
        with sec_rls.elevated("dq_measure"):                  # 지갑·원장 계약은 전체 행을 봐야 한다
            latest = contracts.run(record=True)
    else:
        rows = db.query("SELECT dataset, check_name, dimension, value, op, threshold, severity, status, run_at "
                        "FROM dq_result WHERE run_at = (SELECT MAX(run_at) FROM dq_result) ORDER BY dataset, check_name")
        latest = {"results": rows}
    return {"catalog": contracts.catalog(), "latest": latest, "trend": contracts.trend()}


@app.get("/admin/fds")
def admin_fds(days: int = 7, p: Principal = pep.actor("admin.audit")):
    """이상거래탐지 요약 — 결정별 건수·자주 걸린 규칙·임계값(오탐 검토·튜닝)."""
    from app.services import fds
    return fds.review(_clamp(days, 1, 90))


@app.get("/admin/policy")
def admin_policy(p: Principal = pep.actor("admin.audit")):
    """지금 적용 중인 접근 정책(버전·해시·동작 목록) — 어떤 정책으로 판단했는지 추적."""
    from app.security import pdp
    return {**pdp.info(), "actions": pdp.load()["actions"]}


@app.post("/admin/users/{username}/sessions/revoke")
def admin_revoke_sessions(username: str, p: Principal = pep.actor("admin.security")):
    n = sec_sessions.revoke_user(username, f"admin:{p.name}")
    audit_svc.record("admin.sessions_revoke", p.name, target=username, detail={"sessions": n})
    return {"revoked": n}


@app.post("/admin/users/{username}/lock")
def admin_lock(username: str, req: LockReq, p: Principal = pep.actor("admin.security")):
    if username == p.name:
        raise HTTPException(400, "자기 계정은 잠글 수 없어요")
    if not auth_svc.lock(username, req.minutes):
        raise HTTPException(404, "없는 계정이에요")
    n = sec_sessions.revoke_user(username, f"admin_lock:{p.name}")
    audit_svc.record("admin.lock", p.name, target=username, detail={"minutes": req.minutes, "reason": req.reason})
    return {"locked_minutes": req.minutes, "revoked": n}


@app.post("/admin/users/{username}/unlock")
def admin_unlock(username: str, p: Principal = pep.actor("admin.security")):
    if not auth_svc.unlock(username):
        raise HTTPException(404, "없는 계정이에요")
    audit_svc.record("admin.unlock", p.name, target=username)
    return {"ok": True}


@app.get("/admin/users/{username}/pii")
def admin_user_pii(username: str, request: Request, purpose: str = Query("", max_length=200),
                   p: Principal = pep.actor("admin.pii")):
    """개인정보 열람 — 목적(5자 이상)이 있어야 하고, 누가·언제·누구의·무엇을·왜 봤는지 기록한다."""
    pep.check(request, "admin.pii", purpose=purpose)
    from app.services import privacy
    res = privacy.subject_view(username, operator=p.name, purpose=purpose)
    if res is None:
        raise HTTPException(404, "없는 계정이에요")
    return res


@app.get("/admin/roles")
def admin_roles(p: Principal = pep.actor("admin.roles")):
    return sec_rbac.overview()


@app.post("/admin/roles/requests")
def admin_role_request(req: RoleGrantReq, p: Principal = pep.actor("admin.roles")):
    return _rbac_call(sec_rbac.request_grant, p, req.username, req.role, req.reason)


@app.post("/admin/roles/requests/{request_id}/approve")
def admin_role_approve(request_id: int, p: Principal = pep.actor("admin.roles")):
    return _rbac_call(sec_rbac.decide, p, request_id, True)


@app.post("/admin/roles/requests/{request_id}/reject")
def admin_role_reject(request_id: int, p: Principal = pep.actor("admin.roles")):
    return _rbac_call(sec_rbac.decide, p, request_id, False)


@app.delete("/admin/roles/{username}/{role}")
def admin_role_revoke(username: str, role: str, p: Principal = pep.actor("admin.roles")):
    if username == p.name:
        raise HTTPException(400, "자기 역할은 다른 관리자가 회수해야 해요(직무 분리)")
    return {"removed": sec_rbac.revoke_eligibility(p, username, role)}


# ── 자율보안(2026-11): 금융보안 수준진단 · 자산·위험 등록부 · 파일 무결성 · 인사조치 일괄 회수 ─────────────────
class OffboardReq(_Req):
    reason: str = Field(min_length=5, max_length=200)
    disable: bool = True


@app.get("/admin/assessment")
def admin_assessment(p: Principal = pep.actor("admin.assess")):
    """금융보안 수준진단(금융보안원 프레임워크 7개 분야 45개 항목) — 최근 결과·이력·재진단 기한(분기)."""
    from app.security import assessment
    return assessment.overview()


@app.post("/admin/assessment/run")
def admin_assessment_run(p: Principal = pep.actor("admin.assess")):
    """지금 진단 — 근거(문서·코드·시험) 확인 + 운영 점검(프로브). 결과는 추가만 저장되고 다이제스트가 감사 체인에 남는다."""
    from app.security import assessment
    return assessment.run(actor=p.name)


@app.get("/admin/assessment/{assessment_id}")
def admin_assessment_get(assessment_id: int, p: Principal = pep.actor("admin.assess")):
    """지난 진단 1건 — 저장된 결과와 다이제스트를 다시 대조(intact)."""
    from app.security import assessment
    r = assessment.get(assessment_id)
    if r is None:
        raise HTTPException(404, "없는 진단이에요")
    return r


@app.get("/admin/risk")
def admin_risk(p: Principal = pep.actor("admin.assess")):
    """자산·위험 등록부 — 자산 중요도 요약 · 위협별 고유/잔여 위험·처리·재검토 기한 · 공급사 등록부 · 표 분류 차이."""
    from app import clock
    from app.security import assets, riskreg
    inv = assets.inventory()
    return {"register": riskreg.evaluate(today=clock.today()), "assets": inv["summary"], "components": inv["components"],
            "supplier_problems": assets.supplier_problems(), "drift": assets.drift()}


@app.get("/admin/integrity")
def admin_integrity(run: bool = False, p: Principal = pep.actor("admin.ops")):
    """파일 무결성 — 배포 기준선 대비 바뀐·사라진·새 파일(웹셸 의심). run=true 면 지금 다시 검사."""
    from app.security import integrity
    return integrity.status(refresh=run)


@app.post("/admin/users/{username}/offboard")
def admin_offboard(username: str, req: OffboardReq, p: Principal = pep.actor("admin.security")):
    """인사조치(퇴직·전보) 일괄 회수 — 관리 자격·활성 권한·대기 요청·세션 + 계정 중지(전자금융감독규정 제13조 ①11호)."""
    return _rbac_call(sec_rbac.offboard, p, username, req.reason, req.disable)


@app.delete("/admin/security/ip-blocks/{ip_hash}")
def admin_ip_unblock(ip_hash: str, p: Principal = pep.actor("admin.security")):
    """자동 차단된 IP 해제 — 주소 원문은 저장하지 않으므로 보안 이벤트의 가명값(ip_hash)으로 지정한다."""
    if not (16 <= len(ip_hash) <= 64 and all(c in "0123456789abcdef" for c in ip_hash)):
        raise HTTPException(400, "ip_hash 형식이 아니에요")
    return {"unblocked": sec_events.unblock_ip(ip_hash, p.name)}


# ── IT 내부통제(2026-11): 변경관리 · 장애 판정·보고 · 관제(경보 확인·상향) · 외부 관제 연동 ─────────────────
#   금감원 IT 내부통제(배포 전 영향 분석·제3자 검증) · 시행세칙 제7조의4(사고보고) · 금융위 24시간 관제 점검(2026.10)
class ChangeDecisionReq(_Req):
    note: str = Field("", max_length=500)


def _aware(v):
    """시간대 없는 시각은 한국시간으로 본다(관리자가 화면·CLI 에서 적는 시각)."""
    from app import clock
    return v.replace(tzinfo=clock.KST) if isinstance(v, datetime) and v.tzinfo is None else v


class IncidentOpenReq(_Req):
    kind: str = Field(pattern="^(outage|security|data|other)$")
    title: str = Field(min_length=5, max_length=200)
    started_at: datetime | None = None

    @field_validator("started_at")
    @classmethod
    def _tz(cls, v):
        return _aware(v)


class IncidentUpdateReq(_Req):
    cause: str | None = Field(None, max_length=1000)
    action: str | None = Field(None, max_length=1000)
    measures: str | None = Field(None, max_length=1000)
    impact_note: str | None = Field(None, max_length=1000)
    loss_amount: int | None = Field(None, ge=0, le=10**15)
    reputation: bool | None = None
    data_damage: bool | None = None
    reportable: bool | None = None
    reported: str | None = Field(None, pattern="^(최초|중간|종결)$")
    ended_at: datetime | None = None

    @field_validator("ended_at")
    @classmethod
    def _tz(cls, v):
        return _aware(v)


@app.get("/admin/changes")
def admin_changes(p: Principal = pep.actor("admin.ops")):
    """변경관리 — 모드(enforce·record)·배포 금지 시간·승인 대기·사후 검토 기한 초과·최근 변경(사전 영향 분석·자동 점검 결과)."""
    from app.services import changes
    return changes.overview()


@app.post("/admin/changes/{change_id}/{decision}")
def admin_change_decide(change_id: int, decision: str, req: ChangeDecisionReq, p: Principal = pep.actor("admin.change")):
    """approve·reject — 요청자는 자기 변경을 승인할 수 없다(제3자 검증), 돈·인증·스키마·성능 민감 변경은 검증 근거 필수.
    review — 긴급 변경 사후 검토(배포한 사람 ≠ 검토자, 10자 이상)."""
    from app.services import changes
    if decision not in ("approve", "reject", "review"):
        raise HTTPException(404, "approve·reject·review 중 하나")
    try:
        if decision == "review":
            return changes.review(change_id, p.name, req.note)
        return changes.decide(change_id, decision == "approve", p.name, req.note)
    except changes.ChangeError as e:
        raise pep.PolicyDenied(e.status, e.code, str(e))


@app.get("/admin/incidents")
def admin_incidents(p: Principal = pep.actor("admin.ops")):
    """장애·사고 — 진행 중·보고 기한 초과 사건, 최근 사건(중대 판정·원인 후보·기한), 30일 가용률·감시 범위, 판정 기준."""
    from app.services import incidents
    return incidents.overview()


@app.get("/admin/incidents/{incident_id}")
def admin_incident(incident_id: int, fmt: str = "json", stage: str = "최초", p: Principal = pep.actor("admin.ops")):
    """사건 1건 — fmt=md 면 사고보고서 초안(시행세칙 별지 제2호 항목 순서, 변경시점 후보 자동 첨부)."""
    from fastapi.responses import PlainTextResponse
    from app.services import incidents
    if fmt == "md":
        try:
            md = incidents.report_md(incident_id, stage if stage in ("최초", "중간", "종결") else "최초")
        except LookupError:
            raise HTTPException(404, "없는 사건이에요")
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    r = incidents.get(incident_id)
    if r is None:
        raise HTTPException(404, "없는 사건이에요")
    return r


@app.post("/admin/incidents")
def admin_incident_open(req: IncidentOpenReq, p: Principal = pep.actor("admin.incident")):
    """사건 등록(보안 침해·데이터 오류·감시 공백 동안의 장애 등) — 보안 침해는 처음부터 보고 대상(24시간)."""
    from app.services import incidents
    try:
        return incidents.get(incidents.open_manual(req.kind, req.title, p.name, req.started_at))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/admin/incidents/{incident_id}")
def admin_incident_update(incident_id: int, req: IncidentUpdateReq, p: Principal = pep.actor("admin.incident")):
    """원인·처리·대책·평판·손실·보고 대상 + 보고 단계(최초·중간·종결) + 종료 시각 — 모두 감사 기록."""
    from app.services import incidents
    try:
        return incidents.update(incident_id, p.name, **req.model_dump(exclude_none=True))
    except LookupError:
        raise HTTPException(404, "없는 사건이에요")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/admin/alerts")
def admin_alerts(limit: int = 50, p: Principal = pep.actor("admin.ops")):
    """운영 경보 — 최근 경보(확인·상향 여부)와 관제 지표(MTTA·미확인·상향 수)."""
    from app.services import alerts
    return alerts.board(_clamp(limit, 1, 200))


@app.post("/admin/alerts/{alert_id}/ack")
def admin_alert_ack(alert_id: int, p: Principal = pep.actor("admin.incident")):
    """경보 확인 — 같은 사건(key)의 경보와 상향 알림을 함께 확인 처리(누가·언제, 감사 기록)."""
    from app.services import alerts
    try:
        return alerts.ack(alert_id, p.name)
    except LookupError:
        raise HTTPException(404, "없는 경보예요")


@app.get("/admin/security/export")
def admin_security_export(hours: float = 24, fmt: str = "cef", p: Principal = pep.actor("admin.audit")):
    """외부 관제(SIEM) 연동 — 보안 이벤트·감사 기록·운영 경보를 CEF 또는 JSON Lines 로(아이디는 가명, 내보내기도 감사 기록)."""
    from fastapi.responses import PlainTextResponse
    from app.security import siem
    if fmt not in ("cef", "jsonl"):
        raise HTTPException(400, "fmt 는 cef 또는 jsonl")
    r = siem.export(hours, fmt, actor=p.name)
    return PlainTextResponse("\n".join(r["lines"]) + ("\n" if r["lines"] else ""),
                             media_type="text/plain; charset=utf-8" if fmt == "cef" else "application/x-ndjson",
                             headers={"X-Event-Count": str(r["count"])})


# CSP·신뢰 타입 위반 보고 — 브라우저가 보낸다(report-uri). XSS 시도나 깨진 배포(정책에 없는 스크립트)를 서버가 알아챈다.
# 공개 경로라 아무나 보낼 수 있으므로: 본문 상한(20KB) + IP 당 분당 30건 + 정해진 몇 칸만 짧게 잘라 남긴다(쿼리스트링 제외).
_CSP_FIELDS = {   # 예전 형식(report-uri: kebab-case) · Reporting API(camelCase) → 저장 이름
    "document-uri": "document", "documentURL": "document", "violated-directive": "directive",
    "effective-directive": "directive", "effectiveDirective": "directive", "blocked-uri": "blocked",
    "blockedURL": "blocked", "source-file": "source", "sourceFile": "source", "line-number": "line",
    "lineNumber": "line", "script-sample": "sample", "sample": "sample", "disposition": "disposition"}


def _csp_detail(rep: dict) -> dict:
    out = {}
    for k, name in _CSP_FIELDS.items():
        v = rep.get(k)
        if v not in (None, "") and name not in out:
            out[name] = str(v)[:120]
    for name in ("document", "blocked", "source"):            # 주소의 쿼리스트링(결제 반환값 등)은 남기지 않는다
        if name in out:
            out[name] = out[name].split("?")[0]
    return out


@app.post("/csp-report", status_code=204, dependencies=[pep.actor("security.report")])
async def csp_report(request: Request):
    if ratelimit.take([(ratelimit.key_of("csp", _client_ip(request)), 30)], 60):
        return JSONResponse(status_code=204, content=None)          # 넘치면 조용히 버린다(보고 폭주로 DB 를 채우지 않게)
    try:
        body = json.loads((await request.body())[:20000] or b"{}")
    except ValueError:
        raise HTTPException(400, "보고 형식 오류")
    for it in (body if isinstance(body, list) else [body])[:10]:   # Reporting API(배열) · report-uri(단건)
        if not isinstance(it, dict):
            continue
        rep = it.get("csp-report") or it.get("body") or {}
        if isinstance(rep, dict) and rep:
            sec_events.emit("csp_violation", "low", **_csp_detail(rep))
    return JSONResponse(status_code=204, content=None)


# 화면 자산 — web/assets(그림·스크립트·용어 그림) · web/images. 페이지 HTML 은 위 경로(nonce·CSP)로만 나간다
# (자산 폴더에는 .html 이 없고, 있어도 이 마운트로는 CSP nonce 없이 나가 스크립트가 돌지 않는다).
app.mount("/assets", webui.WebStatic(directory=str(webui.WEB_ROOT / "assets"), check_dir=False), name="web-assets")
app.mount("/images", webui.WebStatic(directory=str(webui.WEB_ROOT / "images"), check_dir=False), name="web-images")
