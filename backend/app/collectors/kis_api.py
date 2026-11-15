"""한국투자증권(KIS) REST 공통 호출기 — 시세분석·예탁원정보 API 를 같은 규칙으로 부른다.

  · 토큰: realtime.get_access_token()(app_meta 캐시, 1분 1회 발급 제한 대응)을 같이 쓴다.
  · 초당 한도: 실전 계좌 초당 20건 — 이 프로세스에서는 호출 간격을 0.07초 이상으로(≈14건/초) 둔다.
    그래도 EGW00201(초당 거래건수 초과)이 오면 잠시 쉬고 다시.
  · 토큰 오류(EGW00121·00123 만료/무효)는 토큰을 버리고 한 번만 다시 받는다.
  · 연속 조회: 응답 헤더 tr_cont 가 M/F 면 다음 쪽(tr_cont=N)을 이어 받는다(max_pages 까지).
  · KIS 는 오류도 HTTP 500 + JSON 본문(rt_cd≠0, msg_cd)으로 준다 → 본문 코드를 먼저 본다.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from app.config import settings

log = logging.getLogger(__name__)

MIN_INTERVAL = 0.07
RATE_LIMIT = "EGW00201"
TOKEN_ERRORS = ("EGW00121", "EGW00123")
_lock = threading.Lock()
_last = [0.0]


class KisError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"KIS {code}: {message}")
        self.code = code


def enabled():
    return bool(settings.KIS_APP_KEY and settings.KIS_APP_SECRET)


def _throttle():
    with _lock:
        wait = _last[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def _headers(token, tr_id, tr_cont=""):
    return {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
            "appkey": settings.KIS_APP_KEY, "appsecret": settings.KIS_APP_SECRET, "tr_id": tr_id,
            "custtype": "P", "tr_cont": tr_cont}


def get(path, tr_id, params, max_pages=1, retries=3):
    """GET → [본문(dict)] 쪽 목록. 실패면 KisError/requests 예외."""
    from app.collectors import realtime
    pages, tr_cont, token_retry = [], "", True
    attempt = 0
    while True:
        _throttle()
        token = realtime.get_access_token()
        r = requests.get(f"{realtime._rest_base()}{path}", headers=_headers(token, tr_id, tr_cont),
                         params=params, timeout=(5, 20))
        try:
            j = r.json()
        except ValueError:
            j = None
        code = str((j or {}).get("msg_cd") or "") if isinstance(j, dict) else ""
        if isinstance(j, dict) and str(j.get("rt_cd")) not in ("0", "None") and code:
            if code == RATE_LIMIT and attempt < retries:
                attempt += 1
                time.sleep(0.5 * attempt)
                continue
            if code in TOKEN_ERRORS and token_retry:
                token_retry = False
                realtime.invalidate_token()
                continue
            raise KisError(code, j.get("msg1") or "")
        r.raise_for_status()
        if not isinstance(j, dict):
            raise KisError("FORMAT", "응답 형식 오류")
        pages.append(j)
        cont = (r.headers.get("tr_cont") or "").strip()
        if cont in ("M", "F") and len(pages) < max_pages:
            tr_cont = "N"
            continue
        return pages


def rows(pages, key="output"):
    """쪽 목록에서 key(output·output1·output2)의 행을 모은다(단일 dict 도 리스트로)."""
    out = []
    for p in pages:
        v = p.get(key)
        if isinstance(v, dict):
            out.append(v)
        elif isinstance(v, list):
            out.extend(x for x in v if isinstance(x, dict))
    return out


def num(v, default=None):
    s = str(v if v is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def ymd(v):
    """'20261008' · '2026/10/08' · '2026-10-08' · '2026.10.08' → '2026-10-08'(아니면 None)."""
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else None
