"""해외 실시간 시세 + 시가총액 — Finnhub(finnhub.io) 무료 API.

  · /quote          → 현재가(c)·변화(d)·등락률%(dp)
  · /stock/profile2 → 회사명(name)·시가총액(marketCapitalization, 단위 백만 USD)·통화·거래소
FINNHUB_API_KEY 가 없거나 호출 실패면 None 을 돌려주고, 상위(overseas 서비스)가 gen 폴백한다.
※ 키는 settings(=환경변수)에서만 읽는다. 소스/번들에 절대 하드코딩하지 않는다.
"""
import logging

import requests

from app.config import settings
from app.retry import retry

log = logging.getLogger(__name__)
BASE = "https://finnhub.io/api/v1"


def enabled():
    return bool(settings.FINNHUB_API_KEY)


@retry(times=3, delay=1.0, exceptions=(requests.RequestException,))
def _get(path, params):
    # 키는 URL(?token=) 대신 헤더로 — 실패 시 requests 가 예외 메시지에 URL 을 통째로 넣어 로그에 키가 남던 문제
    r = requests.get(f"{BASE}{path}", params=params, timeout=10,
                     headers={"X-Finnhub-Token": settings.FINNHUB_API_KEY})
    r.raise_for_status()
    return r.json()


def parse_quote(q):
    """/quote 응답 → dict. c=현재가(장외엔 직전 체결가), d=대비, dp=등락률%, o/h/l=당일 시·고·저,
    pc=전일 종가, t=마지막 갱신 유닉스초(→ 거래일 판정)."""
    if not q:
        return None
    price = q.get("c")
    if not price:                          # 0 또는 None → 데이터 없음(잘못된 심볼)
        return None

    def f(k):
        try:
            v = q.get(k)
            return float(v) if v not in (None, 0, "") else None
        except (TypeError, ValueError):
            return None
    return {"price": float(price), "change": float(q.get("d") or 0.0),
            "change_rate": float(q.get("dp") or 0.0),
            "open": f("o"), "high": f("h"), "low": f("l"), "prev_close": f("pc"),
            "t": int(q.get("t") or 0)}


def quote(symbol):
    """{price, change, change_rate, open, high, low, prev_close, t} 또는 None."""
    if not enabled():
        return None
    try:
        q = _get("/quote", {"symbol": symbol})
    except (requests.RequestException, ValueError) as e:
        log.warning("finnhub quote 실패 %s: %s", symbol, e)
        return None
    return parse_quote(q)


