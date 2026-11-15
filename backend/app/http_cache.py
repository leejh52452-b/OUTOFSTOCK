"""HTTP 캐시 정책 — 공개 데이터는 '조건부 요청(ETag/304)'으로 아끼고, 개인 데이터는 어디에도 저장되지 않게.

  공개 조회(정책 동작 public.read, GET 200 JSON)
      ETag = 응답 본문 SHA-256 앞 16바이트(약한 비교). 브라우저가 If-None-Match 로 같은 값을 보내면 본문 없이 304 —
      용어사전(약 150KB)·뉴스 홈·추천처럼 큰데 자주 안 바뀌는 응답의 전송량이 0 에 가깝게 준다.
      Cache-Control: no-cache — 저장은 하되 쓸 때마다 서버에 확인(오래된 시세를 보여 주지 않는다).
  로그인 사용자·관리 응답
      Cache-Control: no-store, Pragma: no-cache — 공용 PC 의 뒤로 가기·프록시 캐시에 지갑·채팅이 남지 않게(보안).
  이미 Cache-Control 을 정한 응답(정적 파일·SSE)은 건드리지 않는다.
"""
from __future__ import annotations

import hashlib

from app.security import context

MAX_BUFFER = 2_000_000


class HttpCache:
    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "GET":
            await self.app(scope, receive, _private_only(send))
            return
        inm = None
        for k, v in scope.get("headers") or ():
            if k == b"if-none-match":
                inm = v.decode("latin-1")
        state = {"start": None, "body": [], "size": 0, "passthrough": False}

        async def wrapped(message):
            if state["passthrough"]:
                await send(message)
                return
            if message["type"] == "http.response.start":
                ctx = context.current()
                headers = dict((k.lower(), v) for k, v in message.get("headers") or [])
                public = ctx.action == "public.read"                 # 공개 조회는 주체와 무관하게 같은 응답
                if b"cache-control" in headers or message["status"] != 200 or \
                        not headers.get(b"content-type", b"").startswith(b"application/json"):
                    state["passthrough"] = True
                    await send(_with_privacy(message, ctx) if b"cache-control" not in headers else message)
                    return
                if not public:
                    state["passthrough"] = True
                    await send(_with_privacy(message, ctx))
                    return
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["body"].append(message.get("body", b""))
                state["size"] += len(message.get("body", b""))
                if state["size"] > MAX_BUFFER:              # 너무 크면 그대로 흘려보낸다(ETag 없이)
                    state["passthrough"] = True
                    await send(state["start"])
                    for chunk in state["body"][:-1]:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({**message, "body": state["body"][-1]})
                    return
                if message.get("more_body"):
                    return
                body = b"".join(state["body"])
                etag = 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'
                start = state["start"]
                hdrs = [(k, v) for k, v in start.get("headers") or [] if k.lower() != b"content-length"]
                hdrs += [(b"etag", etag.encode()), (b"cache-control", b"no-cache"), (b"vary", b"Authorization, Cookie")]
                if inm and etag in [x.strip() for x in inm.split(",")]:
                    await send({"type": "http.response.start", "status": 304, "headers": hdrs})
                    await send({"type": "http.response.body", "body": b""})
                    return
                hdrs.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": hdrs})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, wrapped)


def _with_privacy(message, ctx):
    """공개 조회가 아닌 응답 중 로그인 사용자·관리·계정 응답 — 저장 금지."""
    if ctx.action != "public.read" and (ctx.principal.kind == "user" or
                                        (ctx.action or "").startswith(("admin.", "account.", "wallet.", "auth."))):
        hdrs = list(message.get("headers") or [])
        names = {k.lower() for k, _v in hdrs}
        if b"cache-control" not in names:
            hdrs += [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")]
        return {**message, "headers": hdrs}
    return message


def _private_only(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            message = _with_privacy(message, context.current())
        await send(message)
    return wrapped
