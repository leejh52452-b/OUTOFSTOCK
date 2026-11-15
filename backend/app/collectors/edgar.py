"""해외 재무(미국) — SEC EDGAR XBRL companyfacts.

  · 인증키 없음. SEC 는 식별용 연락처를 User-Agent(settings.SEC_USER_AGENT)로 요구한다.
  · ticker→CIK 매핑(company_tickers.json)은 프로세스 1회만 내려받아 캐시(국내 DART corp_code 대응).
  · 최근 연간(10-K, fp=FY) 매출/순이익/총자산을 뽑는다. 오프라인/미존재면 None → overseas 서비스가 gen 폴백.
"""
import logging
import threading
from datetime import date

import requests

from app.config import settings
from app.retry import retry

log = logging.getLogger(__name__)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# 작은 단일 개념 API — 발행주식수만 필요할 때(수 MB 짜리 companyfacts 대신)
SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/EntityCommonStockSharesOutstanding.json"

# 매출은 회사마다 태그가 달라 우선순위대로 탐색(참고 스크립트와 동일 개념).
REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues", "SalesRevenueNet"]

_lock = threading.Lock()
_cik_cache = None                          # {대문자 ticker: cik_str}
_cik_failed_at = None                      # 매핑 내려받기가 마지막으로 실패한 시각(monotonic)
CIK_RETRY_AFTER = 1800                     # 실패 후 이 시간(초) 동안은 다시 시도하지 않음


def _ua():
    return {"User-Agent": settings.SEC_USER_AGENT}


@retry(times=3, delay=1.0, exceptions=(requests.RequestException,))
def _get_json(url):
    r = requests.get(url, headers=_ua(), timeout=20)
    r.raise_for_status()
    return r.json()


def ticker_to_cik(ticker):
    """대문자 ticker → 10자리 CIK 문자열. 실패/미존재 시 None. 매핑은 1회만 캐시."""
    global _cik_cache, _cik_failed_at
    import time
    with _lock:
        if _cik_cache is None:
            # 실패를 기억 — 예전엔 종목마다(34종목 × 재시도 3번 × 20초) 다시 받아, SEC 가 막히면 07:30·23:00 작업이
            # 30분 넘게 붙잡혔다(그동안 해외 상세 화면 요청도 이 잠금에서 대기).
            if _cik_failed_at is not None and time.monotonic() - _cik_failed_at < CIK_RETRY_AFTER:
                return None
            try:
                data = _get_json(TICKERS_URL)
                _cik_cache = {row["ticker"].upper(): row["cik_str"] for row in data.values()}
                _cik_failed_at = None
            except (requests.RequestException, KeyError, ValueError, AttributeError) as e:
                log.warning("EDGAR ticker 매핑 실패(%d분 동안 재시도 안 함): %s", CIK_RETRY_AFTER // 60, e)
                _cik_failed_at = time.monotonic()
                return None
    cik = _cik_cache.get(ticker.upper())
    return str(cik).zfill(10) if cik is not None else None


def _full_year(x):
    """기간 값(매출·순이익)이 '1년치'인가 — 10-K 안에는 4분기 값도 같은 end 날짜로 들어 있어,
    form/fp 만 보면 분기 값(연간의 약 1/4)을 고를 수 있었다. 시점 값(자산·자본, start 없음)은 통과."""
    s, e = x.get("start"), x.get("end")
    if not s:
        return True
    try:
        days = (date.fromisoformat(e) - date.fromisoformat(s)).days
    except (TypeError, ValueError):
        return False
    return 350 <= days <= 380


def _latest_annual(usg, names):
    """개념 후보 중 존재하는 것의 최신 연간(10-K/FY) 값. {value, fy, end} 또는 None.
    같은 기간 값이 여러 공시에 있으면 가장 나중에 제출된(filed) 것 — 정정 공시 반영."""
    for c in names:
        node = usg.get(c)
        if not node:
            continue
        annual = [x for x in node.get("units", {}).get("USD", [])
                  if x.get("form") == "10-K" and x.get("fp") == "FY" and x.get("val") is not None
                  and _full_year(x)]
        if annual:
            x = sorted(annual, key=lambda z: (z.get("end") or "", z.get("filed") or ""))[-1]
            return {"value": int(x["val"]), "fy": x.get("fy"), "end": x.get("end")}
    return None


def parse_shares(concept_json):
    """companyconcept(dei/EntityCommonStockSharesOutstanding) → 최신 발행주식수(int) 또는 None.
    클래스가 여러 개(예: GOOGL A/B/C)면 같은 기준일의 값을 합산."""
    rows = (concept_json or {}).get("units", {}).get("shares", [])
    rows = [r for r in rows if r.get("val") and r.get("end")]
    if not rows:
        return None
    last = max(r["end"] for r in rows)
    latest = [r for r in rows if r["end"] == last]
    by_accn = {}
    for r in latest:                                      # 같은 공시(accn) 안의 클래스별 값 합산
        by_accn.setdefault(r.get("accn"), []).append(int(r["val"]))
    return max(sum(v) for v in by_accn.values())


def shares_outstanding(ticker):
    """최신 발행주식수(dei) 또는 None — 시가총액 = 현재가 × 주식수 (Finnhub 키 없이도)."""
    cik = ticker_to_cik(ticker)
    if not cik:
        return None
    try:
        return parse_shares(_get_json(SHARES_URL.format(cik=cik)))
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.warning("EDGAR 주식수 실패 %s: %s", ticker, e)
        return None


def parse_financials(facts, ticker=""):
    """companyfacts JSON → 최근 연간 재무 dict 또는 None (네트워크와 분리해 테스트 가능)."""
    usg = (facts or {}).get("facts", {}).get("us-gaap", {})
    rev = _latest_annual(usg, REVENUE_CONCEPTS)
    ni = _latest_annual(usg, ["NetIncomeLoss"])
    at = _latest_annual(usg, ["Assets"])
    eq = _latest_annual(usg, ["StockholdersEquity",
                              "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"])
    anchor = rev or ni or at
    if not anchor:
        return None
    return {"entity": (facts or {}).get("entityName", ticker), "currency": "USD",
            "revenue": rev["value"] if rev else None,
            "net_income": ni["value"] if ni else None,
            "assets": at["value"] if at else None,
            "equity": eq["value"] if eq else None,
            "fiscal_year": anchor["fy"], "period_end": anchor["end"]}


def financials(ticker):
    """{entity, currency, revenue, net_income, assets, equity, fiscal_year, period_end} 또는 None."""
    cik = ticker_to_cik(ticker)
    if not cik:
        return None
    try:
        facts = _get_json(FACTS_URL.format(cik=cik))
    except (requests.RequestException, ValueError) as e:
        log.warning("EDGAR companyfacts 실패 %s: %s", ticker, e)
        return None
    return parse_financials(facts, ticker)
