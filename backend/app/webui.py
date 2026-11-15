"""웹 화면 서빙 — 팀 확정 디자인(web/ 폴더: index·learn·news·practice·mypage·invest + 연동 때 더한 stocks·avatar)을 같은 출처에서 내보낸다.

보안 설계
  · 페이지는 허용 목록(PAGES)만 — 경로 조각을 파일 이름으로 쓰지 않는다(경로 조작·숨은 파일 노출 없음).
  · 요청마다 새 nonce(16바이트) → 인라인 <script> 에만 붙이고 CSP 헤더에 같은 값. src 가 있는 스크립트는
    nonce 없이 'self' 로만 허용되므로, 누가 외부 스크립트 태그를 넣어도(공급망·실수) 브라우저가 막고 보고한다.
  · CSP: 인라인 이벤트 핸들러 금지(script-src-attr 'none') · Trusted Types 강제(정책 이름 default 하나만) ·
    외부로의 연결·프레임·폼 전송 없음(connect-src/frame-src/form-action) · 위반은 /csp-report 로.
    화면 템플릿에 인라인 style 속성이 많아 style-src 'unsafe-inline' 은 남긴다(스크립트 실행 경로가 아님 — 한계로 문서화).
  · 내보내는 페이지 <head> 에 표식 <meta name="oos-server"> 를 넣는다 — 화면 런타임은 이 표식이 있을 때만 API 를 부른다
    (팀이 파일·Live Server·GitHub Pages 로 열면 표식이 없어 시안 예시 모드 — 없는 API 를 부르며 깨지지 않게).
  · HTML 은 nonce 가 매번 달라 저장하지 않는다(no-store). 그림·스크립트 파일은 별도 마운트(WebStatic)가 캐시 정책을 정한다.
  · 바뀐 파일 알아보기는 '수정 시각'이 아니라 내용으로 — 설치본(tar)은 재현 가능하게 모든 파일 시각을 0 으로 풀어서,
    시각·크기로 만든 ETag 는 글자 수가 같은 수정(색 코드·숫자)에서 그대로다(브라우저가 304 로 옛 스크립트를 계속 씀).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import FileResponse
from starlette.staticfiles import NotModifiedResponse

from app.config import settings

WEB_ROOT = Path(os.getenv("WEB_ROOT") or Path(__file__).resolve().parents[1] / "web")
PAGES = ("index", "learn", "news", "practice", "mypage", "stocks", "avatar", "invest")

PAGE_CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
            "connect-src 'self'; media-src 'self'; worker-src 'none'; frame-src 'none'; frame-ancestors 'self'; "
            "base-uri 'self'; form-action 'self'; object-src 'none'; manifest-src 'self'; "
            "require-trusted-types-for 'script'; trusted-types default; report-uri /csp-report")

# 인라인 스크립트 시작 태그(src 속성이 없는 것)만 — nonce 를 붙일 자리
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)(?=[\s>])", re.IGNORECASE)
_NONCE_MARK = "__OOS_CSP_NONCE__"
_HEAD = re.compile(r"<head(?=[\s>])[^>]*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)
_CHARSET = re.compile(r"\s*<meta\s[^>]*\bcharset\s*=[^>]*>", re.IGNORECASE)
SERVED_MARK = '<meta name="oos-server" content="1">'
_cache: dict[str, tuple[tuple, str]] = {}
_etags: dict[str, tuple[tuple, str]] = {}


class PageNotFound(LookupError):
    pass


def page_csp(nonce: str) -> str | None:
    """화면 CSP — SECURITY_CSP=off 면 없음, 직접 지정했으면 그 정책에 nonce 만 채운다."""
    v = (settings.SECURITY_CSP or "").strip()
    if v.lower() == "off":
        return None
    if v:
        return v.replace("{nonce}", nonce)
    return PAGE_CSP.format(nonce=nonce)


def _stat_key(st: os.stat_result) -> tuple:
    """파일 판 식별 — ctime 은 tar·cp 가 되돌릴 수 없어(내용·속성이 바뀌면 커널이 갱신) 시각 0 설치본에서도 바뀐다."""
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


def _content_etag(path: str, st: os.stat_result) -> str:
    """내용 SHA-256 ETag — 파일 판(_stat_key)이 같으면 다시 읽지 않는다."""
    key = _stat_key(st)
    hit = _etags.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    tag = f'"{h.hexdigest()[:32]}"'
    _etags[path] = (key, tag)
    return tag


def _template(name: str) -> str:
    """페이지 파일 → nonce 자리표시를 넣은 틀(파일이 바뀌면 다시 읽는다 — 배포 뒤 재시작 없이 새 화면)."""
    if name not in PAGES:
        raise PageNotFound(name)
    path = WEB_ROOT / f"{name}.html"
    try:
        key = _stat_key(path.stat())
    except FileNotFoundError:
        raise PageNotFound(name) from None
    hit = _cache.get(name)
    if hit and hit[0] == key:
        return hit[1]
    html = path.read_text(encoding="utf-8")
    tpl = _INLINE_SCRIPT.sub(f'<script nonce="{_NONCE_MARK}"', html)
    m = _HEAD.search(tpl)                                  # <head> 바로 뒤(charset 선언이 먼저 오면 그 뒤)
    if m:
        at = (_CHARSET.match(tpl, m.end()) or m).end()
    else:                                                  # <head> 를 생략한 문서 — doctype 뒤(맨 앞이면 쿼크 모드)
        d = _DOCTYPE.search(tpl)
        at = d.end() if d else 0
    tpl = tpl[:at] + "\n    " + SERVED_MARK + tpl[at:]
    _cache[name] = (key, tpl)
    return tpl


def render(name: str) -> tuple[str, dict]:
    """(HTML, 응답 헤더) — 요청마다 새 nonce."""
    nonce = secrets.token_urlsafe(16)
    headers = {"Cache-Control": "no-store"}
    csp = page_csp(nonce)
    if csp:
        headers["Content-Security-Policy"] = csp
    return _template(name).replace(_NONCE_MARK, nonce), headers


class WebStatic(StaticFiles):
    """web/assets · web/images — 그림은 하루 캐시, 스크립트·데이터는 매번 확인(ETag/Last-Modified 로 304).
    스크립트를 오래 캐시하면 배포 뒤에도 옛 화면 코드가 남아 새 서버 API 와 어긋난다."""
    _LONG = (".png", ".webp", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")

    def file_response(self, full_path, stat_result, scope, status_code=200):
        """Starlette 기본(수정 시각·크기 ETag) 대신 내용 ETag 로 304 를 판단한다.
        Last-Modified 는 뺀다 — 설치본 파일은 시각이 모두 0 이라, If-Modified-Since 만 보내는 클라이언트가
        내용이 바뀌어도 304 를 받게 된다(검증은 ETag 하나로)."""
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        response.headers["etag"] = _content_etag(str(full_path), stat_result)
        del response.headers["last-modified"]
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            p = path.replace("\\", "/").lower()
            resp.headers["Cache-Control"] = "public, max-age=86400" if p.endswith(self._LONG) else "no-cache"
        return resp
