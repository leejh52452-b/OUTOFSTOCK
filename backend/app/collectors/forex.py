"""원/달러 환율 — 한국수출입은행 현재환율 공개 API(AP01, 매매기준율 deal_bas_r).

  · EXIM_API_KEY 는 선택. 키가 없거나 오프라인/휴일(빈 응답)이면 None → overseas 서비스가 폴백 환율 사용.
  · 주말/공휴일엔 데이터가 없어 최근 영업일까지 며칠 뒤로 탐색한다.
해외 종목의 현재가/시총을 원화로 환산해 보여주기 위한 용도.
"""
import logging
from datetime import timedelta

import requests

from app import clock
from app.config import settings
from app.retry import retry

log = logging.getLogger(__name__)
URL = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"


def enabled():
    return bool(settings.EXIM_API_KEY)


@retry(times=2, delay=1.0, exceptions=(requests.RequestException,))
def _get(day):
    r = requests.get(URL, timeout=10,
                     params={"authkey": settings.EXIM_API_KEY, "searchdate": day, "data": "AP01"})
    r.raise_for_status()
    return r.json()


def _usd_rate(rows):
    for row in rows or []:
        if row.get("cur_unit") == "USD":
            try:
                return float(str(row.get("deal_bas_r", "")).replace(",", ""))
            except (ValueError, TypeError):
                return None
    return None


def usd_krw_quote(max_days=10):
    """최근 고시일의 USD 매매기준율과 그 직전 고시일 값 → {rate, date, prev, prev_date} 또는 None.
    (등락 = 직전 영업일 대비. 주말·공휴일엔 데이터가 없어 날짜를 뒤로 탐색)"""
    if not enabled():
        return None
    d = clock.today()
    found = []
    for _ in range(max_days):
        try:
            rows = _get(d.strftime("%Y%m%d"))
        except (requests.RequestException, ValueError) as e:
            log.warning("수출입은행 환율 실패 %s: %s", d, e)
            break
        rate = _usd_rate(rows)
        if rate:
            found.append((d.isoformat(), rate))
            if len(found) == 2:
                break
        d -= timedelta(days=1)             # 데이터 없는 날(휴일) → 전일로
    if not found:
        return None
    (date1, rate1), prev = found[0], (found[1] if len(found) > 1 else (None, None))
    return {"rate": rate1, "date": date1, "prev": prev[1], "prev_date": prev[0]}


def usd_krw():
    """USD 매매기준율(float) 또는 None. 최근 7일 내 영업일을 뒤로 탐색."""
    q = usd_krw_quote(max_days=7) if enabled() else None
    return q["rate"] if q else None


def usd_krw_pair(max_days=12):
    """(최근 영업일 매매기준율, 그 전 영업일 매매기준율) — 다른 작업 라인 코드·테스트가 쓰던 형식. 실패하면 (None, None)."""
    q = usd_krw_quote(max_days=max_days)
    return (q["rate"], q["prev"]) if q else (None, None)
