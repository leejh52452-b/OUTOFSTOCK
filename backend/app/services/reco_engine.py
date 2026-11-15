"""오늘의 추천 종목 Top 5 — 멀티팩터 스코어링 모델(mf-2) + 적합성 필터 + 모델 검증(백테스트) + 감사 추적.

설계 원칙(금융권 '모형 관리' 관점)
  1) Point-in-time  : 기준일(as_of) 이후 정보는 절대 쓰지 않는다 — 일봉·재무(공시일)·뉴스(발행일)·시총 모두.
  2) 설명 가능성    : 종합점수 = Σ 가중치 × 팩터 z-점수. 종목마다 팩터별 기여도·근거 문장·주의 사항을 같이 준다.
  3) 위험 통제      : 적격성 필터(데이터·유동성·단기 과열·급락·거래정지 의심·자본잠식) + 분산 제약(상관·업종·우선주).
  4) 적합성 원칙    : 투자성향(안정형·중립형·공격형)에 맞지 않는 변동성 종목은 후보에서 뺀다(금융소비자보호법 취지).
  5) 검증·감사      : 결과를 reco_snapshot 에 남기고(재현·감사), 과거 기준일로 다시 돌려 성과·IC 를 잰다(백테스트).

팩터(모두 적격 종목 안에서 5~95% 윈저라이즈 → z-점수)
  momentum  모멘텀      (t-60 → t-5 수익률) ÷ 변동성      — 최근 1주 반전 효과를 피한 위험조정 추세
  technical 기술적 신호 골든크로스·RSI 반등·거래량 돌파 등 신호 강도(signals.evaluate_at) + 60일선 이격
  value     가치        이익수익률(E/P) · 장부가치비율(B/P)    — DART 재무(공시일 기준) ÷ 기준일 시총
  quality   재무 건전성 영업이익률 · ROE · (금융업 제외) 부채비율
  sentiment 뉴스 심리   최근 7일 호재/악재 점수 합 ÷ (기사 수 + 3)  — 기사 적은 종목의 과대평가 방지(축소 추정)
  flow      수급·관심   5일/60일 거래대금 비율(로그) · 상승일 거래량 비중
  lowrisk   안정성      −(연변동성) · −(최대낙폭)
시장 국면(상승·중립·하락)에 따라 가중치를 바꾼다 — 약세장일수록 안정성·건전성 비중 ↑, 모멘텀 ↓.
데이터가 아예 없는 팩터(예: DART 미수집)는 빼고 나머지 가중치를 다시 100%로 맞춘다(결측 종목은 중립 0).
본 기능은 교육용 예시이며 투자 권유가 아니다.
"""
import bisect
import json
import logging
import math
import threading
import time
from datetime import date, timedelta

import numpy as np

from app import db
from app.services import catalog, quotes, signals, soundness

log = logging.getLogger(__name__)

MODEL_VERSION = "mf-2.0"
NOTE = "멀티팩터 점수 기반 교육용 예시 · 투자 권유 아님"
MIN_BARS = 25                      # 기술적 신호 최소 봉 수(signals.evaluate 와 같음)
WINDOW_DAYS = 420                  # 계산에 쓰는 일봉 구간(달력일) — 실습용 과거 구간(수년 전)과 섞이지 않게
GAP_DAYS = 12                      # 이 이상 비면 '다른 구간'으로 보고 그 뒤만 사용
NEWS_DAYS = 7
NEWS_PRIOR = 3                     # 뉴스 축소 추정의 가상 중립 기사 수
MAX_CORR = 0.85                    # Top N 끼리 일간수익률 상관 상한(분산 효과)
MAX_PER_SECTOR = 2
MERTON_PD_CAUTION = 0.05           # 시장 기반 부도확률 5% 이상이면 '주의' 문구

FACTORS = ("momentum", "technical", "value", "quality", "sentiment", "flow", "lowrisk")
FACTOR_LABEL = {"momentum": "모멘텀", "technical": "기술적 신호", "value": "가치(저평가)",
                "quality": "재무 건전성", "sentiment": "뉴스 심리", "flow": "수급·관심", "lowrisk": "안정성"}
FACTOR_TAG = {"momentum": "상승 모멘텀", "technical": "추세 양호", "value": "저평가 매력",
              "quality": "재무 우량", "sentiment": "뉴스 호재 우세", "flow": "거래 증가", "lowrisk": "저변동 안정"}
FACTOR_DESC = {
    "momentum": "최근 3개월(마지막 1주 제외) 수익률을 변동성으로 나눈 위험조정 추세",
    "technical": "골든크로스·RSI 반등·거래량 돌파·이평선 지지 등 기술적 신호 강도 + 60일 이동평균 대비 위치",
    "value": "이익수익률(순이익÷시총, PER의 역수)과 장부가치비율(자본÷시총, PBR의 역수) — 공시일 기준 재무",
    "quality": "영업이익률·ROE(높을수록)·부채비율(낮을수록)·부실예측 Altman Z''(높을수록). 금융업은 부채·Z'' 제외",
    "sentiment": "최근 7일 관련 뉴스 제목의 호재·악재 점수 합 ÷ (기사 수+3) — 기사가 적으면 0 쪽으로 축소",
    "flow": "최근 5일 거래대금 ÷ 60일 평균(로그)과 최근 20일 중 상승일 거래량 비중",
    "lowrisk": "연환산 변동성과 최근 6개월 최대낙폭이 작을수록 높은 점수",
}
REGIME_WEIGHTS = {
    "bull":    {"momentum": .25, "technical": .20, "value": .10, "quality": .15, "sentiment": .10, "flow": .10, "lowrisk": .10},
    "neutral": {"momentum": .20, "technical": .20, "value": .15, "quality": .15, "sentiment": .10, "flow": .05, "lowrisk": .15},
    "bear":    {"momentum": .10, "technical": .15, "value": .15, "quality": .20, "sentiment": .10, "flow": .05, "lowrisk": .25},
}
REGIME_LABEL = {"bull": "상승 국면", "neutral": "중립 국면", "bear": "하락 국면"}
PROFILES = {                       # 적합성 원칙 — 성향별 허용 위험(교육용 기준)
    "안정형": {"max_vol": 0.30, "min_mdd": -0.30, "no_distress": True,
            "desc": "연변동성 30% 이하·최근 6개월 최대낙폭 30% 이내·부실 위험 구간(Altman Z'' < 1.1) 제외"},
    "중립형": {"max_vol": 0.50, "min_mdd": -0.50, "desc": "연변동성 50% 이하 종목만"},
    "공격형": {"max_vol": None, "min_mdd": None, "desc": "변동성 제한 없음"},
}
_FIN_WORDS = ("금융", "지주", "은행", "보험", "증권", "생명", "화재", "카드", "캐피탈", "리츠")

_CACHE = {}                    # key → (만료 시각, 값)
_TTL = 300
_CACHE_MAX = 64
_STALE_FACTOR = 12             # 만료 뒤 TTL×12 까지는 '가진 값 먼저 + 뒤에서 다시 계산', 그보다 오래면 기다려서 새로
_GUARD = threading.Lock()      # _CACHE·_LOCKS·_REFRESHING 변경 보호
_LOCKS = {}                    # key → Lock — 같은 키 동시 계산은 1번만(single-flight)
_REFRESHING = set()            # 뒤에서 다시 계산 중인 키
_GEN = [0]                     # clear_cache 세대 — 지운 뒤 끝난 옛 계산이 새 캐시를 덮지 않게


def _store(key, val, ttl, gen):
    with _GUARD:
        if gen != _GEN[0]:
            return
        now = time.monotonic()
        _CACHE[key] = (now + ttl, val)
        if len(_CACHE) > _CACHE_MAX:              # 크기 상한 — 오래 만료된 것부터, 그래도 넘치면 먼저 만료될 것부터
            for k, _v in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:len(_CACHE) - _CACHE_MAX]:
                _CACHE.pop(k, None)
                if k not in _REFRESHING:
                    _LOCKS.pop(k, None)


def _refresh(key, fn, ttl, gen):
    try:
        _store(key, fn(), ttl, gen)
    except Exception:                              # noqa: BLE001 — 실패하면 가진 값으로 계속 서비스
        log.exception("추천 캐시 백그라운드 갱신 실패: %s", key[:1])
    finally:
        with _GUARD:
            _REFRESHING.discard(key)


def _cached(key, fn, ttl=_TTL):
    """TTL 캐시 + stale-while-revalidate + single-flight.
    · 신선하면 그대로.  · 만료됐지만 너무 오래되진 않았으면 가진 값을 바로 주고 뒤에서 1번만 다시 계산.
    · 없거나 너무 오래됐으면 계산 — 같은 키로 동시에 온 요청은 하나만 계산하고 나머지는 그 결과를 받는다.
    예전: 5분마다 첫 요청이 추천 엔진 계산(1~2초, 작은 VM 에선 더)을 기다렸고, 그 사이 들어온 요청도 각자 같은 계산을 반복."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    gen = _GEN[0]
    if hit and now - hit[0] <= ttl * _STALE_FACTOR:
        with _GUARD:
            start = key not in _REFRESHING
            if start:
                _REFRESHING.add(key)
        if start:
            threading.Thread(target=_refresh, args=(key, fn, ttl, gen), name="reco-cache-refresh", daemon=True).start()
        return hit[1]
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        val = fn()
        _store(key, val, ttl, gen)
        return val


def cache_has(key, ttl=_TTL):
    """이 키로 바로 줄 값이 있나 — 신선하거나, 뒤에서 다시 계산하는 동안 줄 수 있는(TTL×_STALE_FACTOR 이내) 값.
    API 가 '새로 계산해야 하는 요청'만 골라 횟수를 제한할 때 쓴다(캐시로 답하는 요청은 제한하지 않음)."""
    hit = _CACHE.get(key)
    return bool(hit) and time.monotonic() - hit[0] <= ttl * _STALE_FACTOR


def cache_busy(key):
    """이 키를 지금 계산 중인가(첫 계산·뒤에서 갱신) — 그 결과를 기다리는 요청은 '새 계산'이 아니다.
    (학교 VPN 처럼 여러 명이 한 IP 로 보일 때, 같은 화면을 동시에 연 사람들이 제한에 걸리지 않게)"""
    with _GUARD:
        lock = _LOCKS.get(key)
        return key in _REFRESHING or bool(lock and lock.locked())


def clear_cache():
    with _GUARD:
        _GEN[0] += 1
        _CACHE.clear()
        _LOCKS.clear()


def risk_level(vol):
    """연변동성 → 위험 구간(교육용 자체 기준 — 금융위 상품 위험등급과는 별개)."""
    if vol is None:
        return "알 수 없음"
    return "낮음" if vol < 0.20 else "보통" if vol < 0.35 else "높음" if vol < 0.50 else "매우 높음"


def is_financial(name):
    return any(w in (name or "") for w in _FIN_WORDS)


# ── 수치 유틸 ─────────────────────────────────────────────
def _sma(x, w):
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[w - 1:] = (cs[w:] - cs[:-w]) / w
    return out


def _rsi(x, w=14):
    out = np.full(len(x), np.nan)
    if len(x) <= w:
        return out
    d = np.diff(x)
    g, l_ = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    ag, al = g[:w].mean(), l_[:w].mean()
    for i in range(w, len(x)):
        if i > w:
            ag = (ag * (w - 1) + g[i - 1]) / w
            al = (al * (w - 1) + l_[i - 1]) / w
        out[i] = (100.0 if ag > 0 else 50.0) if al == 0 else 100 - 100 / (1 + ag / al)   # 무변동 구간은 중립 50
    return out


def _nl(a):
    """numpy 배열 → 파이썬 리스트(NaN→None) — signals.evaluate_at 입력 형식."""
    return [None if (v != v) else float(v) for v in a]


def _winsor_z(vals):
    """{key: 값|None} → {key: z} — 5~95% 윈저라이즈 후 표준화. 결측·분산 0 은 0(중립)."""
    keys = [k for k, v in vals.items() if v is not None and math.isfinite(v)]
    out = {k: 0.0 for k in vals}
    if len(keys) < 2:
        return out
    arr = np.array([vals[k] for k in keys], dtype=float)
    if len(arr) >= 10:
        lo, hi = np.percentile(arr, [5, 95])
        arr = np.clip(arr, lo, hi)
    sd = arr.std()
    if sd <= 1e-12:
        return out
    z = (arr - arr.mean()) / sd
    for k, v in zip(keys, z):
        out[k] = float(np.clip(v, -3, 3))
    return out


def _pct_rank(vals, key, higher_better=True):
    """해당 종목 값이 상위 몇 %인지(작을수록 상위). 값 없으면 None."""
    v = vals.get(key)
    xs = [x for x in vals.values() if x is not None and math.isfinite(x)]
    if v is None or not xs:
        return None
    better = sum(1 for x in xs if (x > v if higher_better else x < v))
    return max(1, round(100 * (better + 1) / len(xs)))


def _spearman(a, b):
    if len(a) < 5:
        return None
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


# ── 데이터 적재(한 번에 읽어 메모리에서 기준일별로 자른다) ──────────
def _or_close(b, k):
    """봉의 시가·고가·저가 — 없거나 0 이하면 종가(signals.evaluate 와 같은 규칙)."""
    v = b.get(k)
    return float(v) if v is not None and v > 0 else float(b["close"])


class Panel:
    """유니버스 일봉·시총·재무·뉴스·지수 — 기준일 d 의 계산은 d 이하 데이터만 읽는다."""

    def __init__(self, universe, end=None, start=None):
        self.universe = list(universe)
        end = end or self._latest()
        self.end = end
        if not end:
            self.series = {}
            return
        start = start or (date.fromisoformat(end) - timedelta(days=WINDOW_DAYS)).isoformat()
        self.start = start
        rows = db.query("SELECT ticker, trade_date, open, high, low, close, volume FROM ohlc "
                        "WHERE ticker = ANY(:ts) AND trade_date >= :s AND trade_date <= :e "
                        "ORDER BY ticker, trade_date", {"ts": self.universe, "s": start, "e": end})
        by = {}
        for r in rows:
            by.setdefault(r["ticker"], []).append(r)
        # 수정주가: 액면분할·무상증자 날의 가짜 −80% 가 모멘텀·변동성·최대낙폭을 망쳐 몇 달 동안 그 종목이 부당하게
        # 빠지지 않게(KRX 공식 등락률로 권리 이벤트를 찾아 이전 봉을 맞춤). 비율이 바뀌는 건 이벤트를 '건너는' 구간뿐이라
        # 기준일 d 의 계산에 미래 정보가 섞이지 않는다(이벤트 전 봉끼리의 비율은 그대로).
        from app.services import adjust
        rates = adjust.rates_for(list(by), start, end)
        self.series = {}
        for t, bars in by.items():
            bars = adjust.adjust_bars(_contiguous_tail(bars), rates.get(t, {}))
            if len(bars) < 2:
                continue
            c = np.array([float(b["close"] or 0) for b in bars])
            if (c <= 0).any():
                keep = [i for i, x in enumerate(c) if x > 0]
                bars = [bars[i] for i in keep]
                c = c[keep]
            # 시가·고가·저가가 없거나 0 이하(거래정지일 KRX 0 값)면 종가로 — 0 저가가 '이평선 지지' 같은 가짜 신호를 만들었다
            h = np.array([_or_close(b, "high") for b in bars])
            lo = np.array([_or_close(b, "low") for b in bars])
            o = np.array([_or_close(b, "open") for b in bars])
            v = np.array([float(b["volume"] or 0) for b in bars])
            s5, s20, s60, rsi = _sma(c, 5), _sma(c, 20), _sma(c, 60), _rsi(c, 14)
            self.series[t] = {
                "dates": [b["trade_date"] for b in bars], "c": c, "h": h, "lo": lo, "o": o, "v": v,
                "s5": s5, "s20": s20, "s60": s60, "rsi": rsi,
                "lists": (_nl(c), _nl(h), _nl(lo), _nl(o), _nl(v), _nl(s5), _nl(s20), _nl(s60), _nl(rsi))}
        self.calendar = sorted({d for s in self.series.values() for d in s["dates"]})
        # 시총(일자별)
        self.mcap = {}
        for r in db.query("SELECT ticker, trade_date, market_cap FROM daily_price WHERE ticker = ANY(:ts) "
                          "AND trade_date >= :s AND trade_date <= :e AND market_cap IS NOT NULL AND market_cap > 0 "
                          "ORDER BY ticker, trade_date", {"ts": self.universe, "s": start, "e": end}):
            m = self.mcap.setdefault(r["ticker"], ([], []))
            m[0].append(r["trade_date"])
            m[1].append(float(r["market_cap"]))
        # 재무(공시일 기준) — 공시일이 비어 있으면 '결산 후 90일(사업보고서 법정 기한)'에 알았다고 보수적으로 가정
        self.fin = {}
        for r in db.query("SELECT ticker, fiscal_year, revenue, operating_profit, net_income, total_equity, "
                          "debt_ratio, disclosed_at, total_assets, current_assets, current_liabilities, "
                          "total_liabilities, retained_earnings FROM financial_statement WHERE ticker = ANY(:ts)",
                          {"ts": self.universe}):
            known = r["disclosed_at"] or f"{int(r['fiscal_year']) + 1}-03-31"
            self.fin.setdefault(r["ticker"], []).append((known, r))
        for t in self.fin:
            self.fin[t].sort(key=lambda x: (x[0], x[1]["fiscal_year"]))
        # 뉴스 감성(발행일 기준) — 종목·날짜별로 DB 에서 집계(기사 수·호재·악재·점수 합).
        # 예전: 기사 한 건씩(운영 한 달이면 10만 행) 파이썬으로 읽어 추천 첫 계산의 대부분을 차지했다.
        self.news = {}
        nstart = (date.fromisoformat(start) - timedelta(days=NEWS_DAYS + 1)).isoformat()
        try:
            # 기사가 '알려진 거래일' = 발행일. 단 장 마감(15:30) 뒤 기사는 다음 날로 — 그날 종가에 사는 추천·백테스트가
            # 장 마감 뒤 저녁 뉴스(실적 발표 등)를 미리 보던 미래참조를 막는다(발행 시각이 없는 예전 기사는 발행일).
            for r in db.query("SELECT ticker, d, COUNT(*) AS n, SUM(pos) AS pos, SUM(neg) AS neg, SUM(score) AS score "
                              "FROM (SELECT ns.ticker AS ticker, "
                              "  CASE WHEN n.published_ts IS NOT NULL "
                              "        AND to_char(n.published_ts AT TIME ZONE 'Asia/Seoul', 'HH24:MI') > '15:30' "
                              "       THEN n.published_at + 1 ELSE n.published_at END AS d, "
                              "  CASE WHEN s.label = 'POS' THEN 1 ELSE 0 END AS pos, "
                              "  CASE WHEN s.label = 'NEG' THEN 1 ELSE 0 END AS neg, COALESCE(s.score, 0) AS score "
                              "  FROM news_sentiment s JOIN news_stock ns ON ns.news_id = s.news_id "
                              "  JOIN news n ON n.news_id = s.news_id "
                              "  WHERE ns.ticker = ANY(:ts) AND n.published_at >= :s AND n.published_at <= :e) x "
                              "GROUP BY ticker, d",
                              {"ts": self.universe, "s": nstart, "e": end}):
                if r["d"]:
                    self.news.setdefault(r["ticker"], []).append(
                        (str(r["d"])[:10], int(r["n"]), int(r["pos"]), int(r["neg"]), float(r["score"] or 0)))
        except Exception as e:                       # noqa: BLE001 — 뉴스 표가 없어도 추천은 돌아야 한다
            log.debug("뉴스 감성 적재 생략: %s", e)
        for t in self.news:
            self.news[t].sort()
        # 지수(시장 국면 판단용) — 실데이터 이력이 있으면 표본(gen) 이력은 섞지 않는다
        try:
            kospi = db.query("SELECT trade_date, close, source FROM index_daily WHERE code='KOSPI' "
                             "AND trade_date <= :e ORDER BY trade_date", {"e": end})
        except Exception:                            # noqa: BLE001 — 지수 이력 표가 없어도 추천은 돈다
            kospi = []
        real = [r for r in kospi if r["source"] != "gen"]
        self.kospi = real if real else kospi
        self.meta = catalog.meta_map(self.universe)

    def _latest(self):
        r = db.query("SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY(:ts)", {"ts": self.universe})
        return r[0]["d"] if r else None

    # 기준일 이하 조회
    def pos(self, t, d):
        s = self.series.get(t)
        if not s:
            return None
        i = bisect.bisect_right(s["dates"], d) - 1
        return i if i >= 0 else None

    def mcap_at(self, t, d):
        m = self.mcap.get(t)
        if not m:
            return None
        i = bisect.bisect_right(m[0], d) - 1
        return m[1][i] if i >= 0 else None

    def fin_at(self, t, d):
        """d 종가 시점에 알 수 있던 최신 재무 — 공시일이 d 보다 앞선 것만(당일 공시는 장 마감 뒤일 수 있어 다음 날부터)."""
        rows = self.fin.get(t) or []
        known = [r for k, r in rows if k < d]
        return known[-1] if known else None

    def news_at(self, t, d):
        """기준일 d 이전 NEWS_DAYS 일의 일자별 집계 [(날짜, 기사 수, 호재, 악재, 점수 합)]."""
        lo = (date.fromisoformat(d) - timedelta(days=NEWS_DAYS)).isoformat()
        return [x for x in self.news.get(t, []) if lo <= x[0] <= d]


def _contiguous_tail(bars):
    """긴 공백(예: 실습용 2021년 구간 ↔ 최근 구간) 뒤의 연속 구간만(quotes.recent_segment 공용)."""
    return quotes.recent_segment(bars, GAP_DAYS)


# ── 종목 하나의 원시 특성(기준 위치 i) ──────────────────────
def features(s, i):
    c, v = s["c"], s["v"]
    n = i + 1
    f = {"n": n, "close": float(c[i])}

    def ret(k):
        return float(c[i] / c[i - k] - 1) if i - k >= 0 and c[i - k] > 0 else None
    f["ret1"], f["ret5"], f["ret20"] = ret(1), ret(5), ret(20)
    w = min(60, i)
    rets = np.diff(c[max(0, i - w):i + 1]) / c[max(0, i - w):i]
    f["vol"] = float(rets.std(ddof=1) * math.sqrt(252)) if len(rets) >= 5 else None
    f["mom"] = float(c[i - 5] / c[i - w] - 1) if w > 10 and c[i - w] > 0 else None
    m_lo = max(0, i - 119)
    seg = c[m_lo:i + 1]
    peak = np.maximum.accumulate(seg)
    f["mdd"] = float(((seg - peak) / peak).min()) if len(seg) >= 2 else None
    sma = s["s60"][i] if not np.isnan(s["s60"][i]) else s["s20"][i]
    f["trend"] = float(c[i] / sma - 1) if sma == sma and sma > 0 else None
    val = c * v
    f["avg_value20"] = float(val[max(0, i - 19):i + 1].mean())
    v60 = val[max(0, i - 59):i + 1].mean()
    f["value_ratio"] = float(val[max(0, i - 4):i + 1].mean() / v60) if v60 > 0 else None
    lo20 = max(1, i - 19)
    up = sum(v[t] for t in range(lo20, i + 1) if c[t] > c[t - 1])
    tot = v[lo20:i + 1].sum()
    f["upvol"] = float(up / tot) if tot > 0 else None
    flat = 0
    for t in range(i, 0, -1):                     # 종가 변화 없는 연속 일수(거래정지 의심)
        if c[t] == c[t - 1]:
            flat += 1
        else:
            break
    f["flat_run"] = flat
    f["zero_vol"] = int((v[max(0, i - 2):i + 1] <= 0).all())
    L = s["lists"]
    sig = signals.evaluate_at(L[0], L[1], L[2], L[3], L[4], L[5], L[6], L[7], L[8], i) if n >= MIN_BARS else []
    f["signals"] = sig
    f["sig_strength"] = float(sum(wt for _, wt in sig))
    rets60 = np.diff(c[max(0, i - 60):i + 1]) / c[max(0, i - 60):i]
    f["_rets"] = dict(zip(s["dates"][max(1, i - 59):i + 1], rets60[-min(len(rets60), 60):].tolist()))
    return f


def _eligibility(f, fin, min_value):
    """(적격 여부, 제외 사유) — 투자 적격성 필터."""
    if f["n"] < MIN_BARS:
        return False, "데이터 부족(25거래일 미만)"
    if f["flat_run"] >= 5 or f["zero_vol"]:
        return False, "거래정지·유동성 의심(가격 변화·거래 없음)"
    if f["avg_value20"] < min_value:
        return False, "유동성 부족(20일 평균 거래대금 하한 미만)"
    if (f["ret1"] or 0) >= 0.20 or (f["ret5"] or 0) >= 0.35:
        return False, "단기 과열(1일 +20%·5일 +35% 이상 — 추격매수 위험)"
    if f["ret20"] is not None and f["ret20"] <= -0.40:
        return False, "급락 중(20일 −40% 이하)"
    if fin and fin.get("total_equity") is not None and fin["total_equity"] <= 0:
        return False, "자본잠식(재무 위험)"
    return True, None


def _regime(panel, d, feats):
    """시장 국면 — 60일선 위 종목 비율(breadth) + (있으면) 코스피 60일선·20일 수익률."""
    above = [1 if (f["trend"] or 0) > 0 else 0 for f in feats.values() if f["trend"] is not None]
    breadth = sum(above) / len(above) if above else 0.5
    idx = [r["close"] for r in panel.kospi if r["trade_date"] <= d]
    idx_up = idx_ret20 = None
    if len(idx) >= 60:
        idx_up = idx[-1] > sum(idx[-60:]) / 60
        idx_ret20 = idx[-1] / idx[-21] - 1
    if breadth >= 0.6 and idx_up is not False:
        reg = "bull"
    elif breadth <= 0.4 or (idx_up is False and (idx_ret20 or 0) < -0.05):
        reg = "bear"
    else:
        reg = "neutral"
    return reg, round(breadth, 3), idx_ret20


# ── 기준일 d 의 점수 계산(백테스트와 '오늘'이 같은 함수를 쓴다) ──────────
def score_universe(panel, d, min_value=None):
    from app.config import settings
    min_value = settings.RECO_MIN_VALUE if min_value is None else min_value
    feats, excluded, fins = {}, {}, {}
    allf = {}
    for t in panel.universe:
        i = panel.pos(t, d)
        if i is None:
            continue
        s = panel.series[t]
        if (date.fromisoformat(d) - date.fromisoformat(s["dates"][i])).days > GAP_DAYS:
            excluded[t] = "최근 시세 없음"
            continue
        f = features(s, i)
        f["as_of_bar"] = s["dates"][i]
        allf[t] = f
    # 유동성 하한 = min(절대 하한, 유니버스 중앙값의 5%) — 동종 대비 거래가 '말라 있는' 종목만 거른다
    if allf:
        med = float(np.median([f["avg_value20"] for f in allf.values()]))
        min_value = min(min_value, med * 0.05)
    for t, f in allf.items():
        fin = panel.fin_at(t, d)
        ok, why = _eligibility(f, fin, min_value)
        if not ok:
            excluded[t] = why
            continue
        feats[t], fins[t] = f, fin
    if not feats:
        return {"as_of": d, "rows": {}, "excluded": excluded, "regime": "neutral", "breadth": None,
                "weights": REGIME_WEIGHTS["neutral"], "used": []}
    # 원시 팩터 값
    raw = {k: {} for k in ("mom", "sig", "trend", "ep", "bp", "opm", "roe", "debt", "altz", "sent", "vratio",
                           "upvol", "vol", "mdd")}
    extra = {}
    for t in feats:                                  # 결측은 None(→ 표준화 후 중립 0)
        for k in raw:
            raw[k][t] = None
    for t, f in feats.items():
        raw["mom"][t] = (f["mom"] / max(f["vol"] or 0.0, 0.05)) if f["mom"] is not None else None
        raw["sig"][t] = f["sig_strength"]
        raw["trend"][t] = f["trend"]
        raw["vratio"][t] = math.log(f["value_ratio"]) if f["value_ratio"] and f["value_ratio"] > 0 else None
        raw["upvol"][t] = f["upvol"]
        raw["vol"][t] = f["vol"]
        raw["mdd"][t] = -(f["mdd"] or 0) if f["mdd"] is not None else None
        fin, mc = fins.get(t), panel.mcap_at(t, d)
        name = panel.meta[t]["name"]
        e = {"per": None, "pbr": None, "roe": None, "opm": None, "debt": None, "financial": is_financial(name),
             "altz": None, "altz_zone": None}
        if fin and mc:
            ni, eq, rev, op = fin.get("net_income"), fin.get("total_equity"), fin.get("revenue"), fin.get("operating_profit")
            raw["ep"][t] = ni / mc if ni is not None else None
            raw["bp"][t] = eq / mc if eq else None
            e["per"] = round(mc / ni, 1) if ni and ni > 0 else None
            e["pbr"] = round(mc / eq, 2) if eq and eq > 0 else None
        if fin:
            rev, op, ni, eq = fin.get("revenue"), fin.get("operating_profit"), fin.get("net_income"), fin.get("total_equity")
            raw["opm"][t] = op / rev if rev and op is not None and rev > 0 else None
            raw["roe"][t] = ni / eq if eq and ni is not None and eq > 0 else None
            if not e["financial"] and fin.get("debt_ratio") is not None:
                raw["debt"][t] = float(fin["debt_ratio"])
            e["opm"] = round(raw["opm"][t] * 100, 1) if raw["opm"].get(t) is not None else None
            e["roe"] = round(raw["roe"][t] * 100, 1) if raw["roe"].get(t) is not None else None
            e["debt"] = fin.get("debt_ratio")
            az = soundness.altman_z(fin, name)                  # 부실예측(금융업·항목 부족이면 None)
            if az.get("z") is not None:
                raw["altz"][t] = az["z"]
                e["altz"], e["altz_zone"] = az["z"], az["zone"]
        news = panel.news_at(t, d)
        nn = sum(x[1] for x in news)
        e["news_n"] = nn
        e["news_pos"] = sum(x[2] for x in news)
        e["news_neg"] = sum(x[3] for x in news)
        raw["sent"][t] = (sum(x[4] for x in news) / (nn + NEWS_PRIOR)) if nn else None
        extra[t] = e
    Z = {k: _winsor_z(v) for k, v in raw.items()}
    cov = {k: sum(1 for x in v.values() if x is not None) for k, v in raw.items()}

    def comb(parts):                               # 가중 합 ÷ |가중치| 합 (음수 가중 = '낮을수록 좋음')
        tot = sum(abs(w) for _, w in parts)
        return {t: sum(w * Z[k].get(t, 0.0) for k, w in parts) / tot for t in feats}
    fac = {
        "momentum": dict(Z["mom"]),
        "technical": comb([("sig", .6), ("trend", .4)]),
        "value": comb([("ep", .5), ("bp", .5)]),
        "quality": comb([("opm", 1), ("roe", 1)] + ([("debt", -1)] if cov["debt"] else [])
                        + ([("altz", 1)] if cov["altz"] else [])),
        "sentiment": dict(Z["sent"]),
        "flow": comb([("vratio", .5), ("upvol", .5)]),
        "lowrisk": comb([("vol", -.6), ("mdd", -.4)]),
    }
    fac = {k: _winsor_z(v) if any(abs(x) > 1e-12 for x in v.values()) else v for k, v in fac.items()}
    used = [k for k, need in (("momentum", ("mom",)), ("technical", ("sig", "trend")), ("value", ("ep", "bp")),
                              ("quality", ("opm", "roe")), ("sentiment", ("sent",)), ("flow", ("vratio", "upvol")),
                              ("lowrisk", ("vol",))) if any(cov[x] for x in need)]
    reg, breadth, idx_ret20 = _regime(panel, d, feats)
    base_w = REGIME_WEIGHTS[reg]
    tot = sum(base_w[k] for k in used) or 1.0
    weights = {k: round(base_w[k] / tot, 4) if k in used else 0.0 for k in FACTORS}
    rows = {}
    for t, f in feats.items():
        contrib = {k: weights[k] * fac[k][t] for k in FACTORS}
        rows[t] = {"ticker": t, "f": f, "e": extra[t], "z": {k: fac[k][t] for k in FACTORS},
                   "contrib": contrib, "composite": sum(contrib.values())}
    comps = sorted(r["composite"] for r in rows.values())
    for r in rows.values():
        below = bisect.bisect_left(comps, r["composite"])
        same = bisect.bisect_right(comps, r["composite"]) - below
        r["score"] = round(100 * (below + 0.5 * same) / len(comps), 1)
    return {"as_of": d, "rows": rows, "excluded": excluded, "regime": reg, "breadth": breadth,
            "index_ret20": idx_ret20, "weights": weights, "used": used, "raw": raw,
            "coverage": {k: cov[k] for k in cov}}


def _grade(score):
    return "S" if score >= 95 else "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"


def _corr(a, b):
    common = sorted(set(a) & set(b))
    if len(common) < 20:
        return None
    x = np.array([a[k] for k in common])
    y = np.array([b[k] for k in common])
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _base_name(name):
    n = (name or "").strip()
    for suf in ("2우B", "3우B", "우B", "우C", "우"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            return n[: -len(suf)]
    return n


def suitable(row, profile):
    rule = PROFILES.get(profile or "")
    if not rule:
        return True
    vol, mdd = row["f"]["vol"], row["f"]["mdd"]
    if rule["max_vol"] is not None and (vol is None or vol > rule["max_vol"]):
        return False
    if rule["min_mdd"] is not None and (mdd is None or mdd < rule["min_mdd"]):
        return False
    if rule.get("no_distress") and row["e"].get("altz") is not None and row["e"]["altz"] < soundness.ALTMAN_DISTRESS:
        return False
    return True


def select(scored, panel, limit=5, profile=None):
    """점수순으로 고르되 분산 제약(상관·업종·우선주) + 적합성(성향) 적용. 모자라면 제약을 풀어 채운다."""
    cands = sorted(scored["rows"].values(), key=lambda r: (-r["composite"], r["ticker"]))
    skipped = {}
    pool = []
    for r in cands:
        if suitable(r, profile):
            pool.append(r)
        else:
            skipped[r["ticker"]] = "투자성향 대비 위험 높음"
    picked, sectors = [], {}
    for r in pool:
        if len(picked) >= limit:
            break
        meta = panel.meta[r["ticker"]]
        sec = meta.get("sector") or ""
        if sec and sectors.get(sec, 0) >= MAX_PER_SECTOR:
            skipped[r["ticker"]] = "같은 업종 편중"
            continue
        if any(_base_name(panel.meta[p["ticker"]]["name"]) == _base_name(meta["name"]) for p in picked):
            skipped[r["ticker"]] = "보통주·우선주 중복"
            continue
        cmax = max((c for c in (_corr(r["f"]["_rets"], p["f"]["_rets"]) for p in picked) if c is not None),
                   default=None)
        if cmax is not None and cmax > MAX_CORR:
            skipped[r["ticker"]] = f"이미 고른 종목과 상관 {cmax:.2f}(분산 효과 낮음)"
            continue
        picked.append(r)
        if sec:
            sectors[sec] = sectors.get(sec, 0) + 1
    if len(picked) < limit:                          # 작은 유니버스: 분산 제약만 풀어서 보충(적합성은 유지)
        for r in pool:
            if len(picked) >= limit:
                break
            if not any(r is p for p in picked):
                picked.append(r)
                skipped.pop(r["ticker"], None)
    return picked, skipped


# ── 설명(근거·주의) ───────────────────────────────────────
def _fmt_pct(x, digits=1):
    return f"{x * 100:+.{digits}f}%" if x is not None else "-"


def explain(r, scored):
    f, e, raw = r["f"], r["e"], scored["raw"]
    t = r["ticker"]
    reasons, cautions = [], []
    order = sorted((k for k in FACTORS if scored["weights"][k] > 0), key=lambda k: -r["contrib"][k])
    for k in order:
        if len(reasons) >= 3 or r["contrib"][k] <= 0.02:
            break
        if k == "momentum" and f["mom"] is not None:
            p = _pct_rank(raw["mom"], t)
            reasons.append(f"최근 3개월(마지막 1주 제외) 수익률 {_fmt_pct(f['mom'])} — 위험 대비 추세 상위 {p}%")
        elif k == "technical":
            sig = [s for s, _ in f["signals"]]
            if sig:
                reasons.append(f"기술적 신호: {' · '.join(sig[:3])}")
            elif f["trend"] is not None:
                reasons.append(f"60일 이동평균보다 {_fmt_pct(f['trend'])} 위에서 추세 유지")
        elif k == "value":
            bits = [f"PER {e['per']}배" if e["per"] else None, f"PBR {e['pbr']}배" if e["pbr"] else None]
            bits = [b for b in bits if b]
            if bits:
                p = _pct_rank(raw["ep"], t)
                reasons.append(f"{' · '.join(bits)} — 이익 대비 주가가 낮은 편(상위 {p}%)")
        elif k == "quality":
            bits = [f"영업이익률 {e['opm']}%" if e["opm"] is not None else None,
                    f"ROE {e['roe']}%" if e["roe"] is not None else None,
                    f"부채비율 {e['debt']:.0f}%" if (e["debt"] is not None and not e["financial"]) else None,
                    f"Altman Z'' {e['altz']}({e['altz_zone']})" if e["altz"] is not None else None]
            bits = [b for b in bits if b]
            if bits:
                reasons.append("재무 건전성: " + " · ".join(bits))
        elif k == "sentiment" and e["news_n"]:
            reasons.append(f"최근 {NEWS_DAYS}일 관련 뉴스 {e['news_n']}건 — 호재 {e['news_pos']} · 악재 {e['news_neg']}")
        elif k == "flow" and f["value_ratio"]:
            reasons.append(f"최근 5일 거래대금이 60일 평균의 {f['value_ratio']:.1f}배 · 상승일 거래 비중 "
                           f"{(f['upvol'] or 0) * 100:.0f}%")
        elif k == "lowrisk" and f["vol"] is not None:
            reasons.append(f"연변동성 {f['vol'] * 100:.0f}% · 6개월 최대낙폭 {(f['mdd'] or 0) * 100:.0f}% — "
                           "같은 기간 다른 종목보다 안정적")
    worst = min(FACTORS, key=lambda k: r["contrib"][k])
    if r["contrib"][worst] < -0.08:
        msg = {"momentum": "최근 3개월 추세는 다른 종목보다 약한 편", "technical": "기술적 추세 신호는 약한 편",
               "value": "이익·자산 대비 주가가 다른 종목보다 높은 편", "quality": "수익성·재무 건전성은 낮은 편",
               "sentiment": "최근 뉴스는 악재가 우세", "flow": "최근 거래가 줄어드는 중",
               "lowrisk": f"다른 후보보다 변동성이 큰 편(연 {(f['vol'] or 0) * 100:.0f}%)"}[worst]
        cautions.append(msg)
    if (f["ret5"] or 0) >= 0.15:
        cautions.append(f"최근 5일 {_fmt_pct(f['ret5'])} 급등 — 단기 변동 주의")
    if e.get("altz") is not None and e["altz"] < soundness.ALTMAN_DISTRESS:
        cautions.append(f"부실예측 지표 Altman Z'' {e['altz']} — 부실 위험 구간(< {soundness.ALTMAN_DISTRESS})")
    if "value" in scored["used"] and e["per"] is None and e["pbr"] is None:
        cautions.append("재무 데이터 없음 — 가격·수급 지표 위주로 평가")
    mt = e.get("merton") or {}
    if mt.get("ok") and mt["pd"] >= MERTON_PD_CAUTION:
        cautions.append(f"시장 기반 부도확률(Merton) {mt['pd'] * 100:.1f}% · 부도거리 {mt['dd']} — 신용위험 주의")
    return reasons, cautions


def _tag(r, used_tags):
    sig = r["f"]["signals"]
    pos = sorted((k for k in FACTORS if r["contrib"][k] > 0), key=lambda k: -r["contrib"][k])
    if sig and "technical" in pos[:2]:
        free = [s for s, _ in sig if s not in used_tags]
        if free:
            return free[0]
    for k in pos:
        tg = FACTOR_TAG[k]
        if tg not in used_tags:
            return tg
    return sig[0][0] if sig else (FACTOR_TAG[pos[0]] if pos else "종합 점수 상위")


def _brief(r, tag):
    """목록 한 줄용 짧은 근거 — 상위 기여 팩터 2개를 숫자 하나씩으로(대표 태그와 겹치는 건 뺌)."""
    f, e = r["f"], r["e"]
    out = []
    for k in sorted((k for k in FACTORS if r["contrib"][k] > 0.02), key=lambda k: -r["contrib"][k]):
        if FACTOR_TAG[k] == tag:
            continue
        txt = None
        if k == "momentum" and f["mom"] is not None:
            txt = f"3개월 {f['mom'] * 100:+.0f}%"
        elif k == "technical":
            sig = [s for s, _ in f["signals"] if s != tag]
            txt = sig[0] if sig else (f"60일선 {f['trend'] * 100:+.0f}%" if f["trend"] is not None else None)
        elif k == "value":
            txt = f"PER {e['per']}배" if e["per"] else (f"PBR {e['pbr']}배" if e["pbr"] else None)
        elif k == "quality":
            txt = f"ROE {e['roe']}%" if e["roe"] is not None else (f"영업이익률 {e['opm']}%" if e["opm"] is not None else None)
        elif k == "sentiment" and e["news_n"]:
            txt = f"호재 뉴스 {e['news_pos']}건"
        elif k == "flow" and f["value_ratio"]:
            txt = f"거래대금 {f['value_ratio']:.1f}배"
        elif k == "lowrisk" and f["vol"] is not None:
            txt = f"변동성 {f['vol'] * 100:.0f}%"
        if txt:
            out.append(txt)
        if len(out) >= 2:
            break
    return " · ".join(out)


def _item(rank, r, scored, panel, used_tags):
    f, e = r["f"], r["e"]
    meta = panel.meta[r["ticker"]]
    if "merton" not in e and not e.get("financial"):              # 고른 종목만 시장 기반 부도거리 계산(기준일 시점)
        from app.services import credit
        e["merton"] = credit.merton_for(r["ticker"], panel.fin_at(r["ticker"], scored["as_of"]), meta["name"],
                                        scored["as_of"])
    reasons, cautions = explain(r, scored)
    tag = _tag(r, used_tags)
    used_tags.add(tag)
    pos = sorted((k for k in FACTORS if r["contrib"][k] > 0.02), key=lambda k: -r["contrib"][k])
    tags = [s for s, _ in f["signals"]] + [FACTOR_TAG[k] for k in pos[:3]]
    tags = [tag] + [t for t in dict.fromkeys(tags) if t != tag]
    return {"rank": rank, "ticker": r["ticker"], "name": meta["name"], "market": meta["market"],
            "sector": meta.get("sector") or "", "score": r["score"], "grade": _grade(r["score"]),
            "composite": round(r["composite"], 3), "tag": tag, "tags": tags[:5], "brief": _brief(r, tag),
            "reasons": reasons, "cautions": cautions,
            "factors": {k: round(r["z"][k], 2) for k in FACTORS},
            "contrib": {k: round(r["contrib"][k], 3) for k in FACTORS},
            "metrics": {"ret5": f["ret5"], "ret20": f["ret20"], "mom": f["mom"], "trend": f["trend"],
                        "volatility": f["vol"], "mdd": f["mdd"], "avg_value20": round(f["avg_value20"]),
                        "per": e["per"], "pbr": e["pbr"], "roe": e["roe"], "opm": e["opm"], "debt_ratio": e["debt"],
                        "altman_z": e["altz"], "altman_zone": e["altz_zone"],
                        "merton_dd": (e.get("merton") or {}).get("dd"), "merton_pd": (e.get("merton") or {}).get("pd"),
                        "news": e["news_n"], "news_pos": e["news_pos"], "news_neg": e["news_neg"]},
            "risk": {"level": risk_level(f["vol"]), "volatility": f["vol"], "mdd": f["mdd"]},
            "entry_date": f["as_of_bar"], "entry_price": f["close"]}


def _model_info(scored, n_universe, profile, skipped=None):
    from collections import Counter
    ex = Counter(scored["excluded"].values())
    return {"version": MODEL_VERSION, "regime": scored["regime"], "regime_label": REGIME_LABEL[scored["regime"]],
            "breadth": scored["breadth"], "weights": scored["weights"],
            "factors_used": scored["used"], "universe": n_universe, "eligible": len(scored["rows"]),
            "excluded": dict(ex), "profile": profile or None,
            "profile_rule": PROFILES.get(profile or "", {}).get("desc"),
            "constraints": {"max_corr": MAX_CORR, "max_per_sector": MAX_PER_SECTOR},
            "diversified_out": dict(Counter((skipped or {}).values()))}


def recommend(limit=5, profile=None, as_of=None, panel=None):
    """기준일(기본 최신) Top N + 모델 정보. 같은 입력이면 같은 결과(결정론적)."""
    universe = catalog.universe()
    panel = panel or Panel(universe, end=as_of)
    d = as_of or panel.end
    if not d or not panel.series:
        return {"as_of": None, "items": [], "model": None}
    scored = score_universe(panel, d)
    picked, skipped = select(scored, panel, limit, profile)
    used_tags = set()
    items = [_item(i, r, scored, panel, used_tags) for i, r in enumerate(picked, 1)]
    return {"as_of": d, "items": items, "model": _model_info(scored, len(universe), profile, skipped)}


# ── 감사 추적: 스냅샷 ─────────────────────────────────────
def save_snapshot(res, profile=None):
    """한 번 '공개된' 추천은 바꾸지 않는다(ON CONFLICT DO NOTHING) — 사후 검증·감사의 근거."""
    if not res.get("as_of") or not res.get("items"):
        return 0
    prof = profile or "all"
    now = quotes.iso_now()
    rows = [{"a": res["as_of"], "p": prof, "v": MODEL_VERSION, "r": it["rank"], "t": it["ticker"], "n": it["name"],
             "s": it["score"], "c": it["composite"], "ep": it["entry_price"], "tag": it["tag"][:40],
             "d": json.dumps({"reasons": it["reasons"], "contrib": it["contrib"], "risk": it["risk"],
                              "regime": res["model"]["regime"], "weights": res["model"]["weights"]},
                             ensure_ascii=False)[:2000],
             "ts": now} for it in res["items"]]
    try:
        with db.transaction() as conn:
            from sqlalchemy import text
            conn.execute(text(
                "INSERT INTO reco_snapshot(as_of, profile, model_version, rank, ticker, name, score, composite, "
                "entry_price, tag, detail, created_at) VALUES(:a,:p,:v,:r,:t,:n,:s,:c,:ep,:tag,:d,:ts) "
                "ON CONFLICT DO NOTHING"), rows)
    except Exception as e:                             # noqa: BLE001 — 기록 실패가 화면을 막지 않게
        log.warning("추천 스냅샷 저장 실패: %s", e)
        return 0
    return len(rows)


def snapshot(as_of, profile=None):
    return db.query("SELECT as_of, profile, model_version, rank, ticker, name, score, composite, entry_price, tag, "
                    "detail, created_at FROM reco_snapshot WHERE as_of=:a AND profile=:p "
                    "ORDER BY created_at, rank", {"a": as_of, "p": profile or "all"})


def snapshot_dates(limit=30, profile=None):
    return [r["as_of"] for r in db.query(
        "SELECT DISTINCT as_of FROM reco_snapshot WHERE profile=:p ORDER BY as_of DESC LIMIT :n",
        {"p": profile or "all", "n": max(1, min(limit, 365))})]


# ── 모델 검증: 워크포워드 백테스트 ───────────────────────────
BT_DAYS = (60, 90, 120, 180, 250, 400)       # 백테스트 허용 값 — 요청 값은 가장 가까운 값으로 맞춘다
BT_HORIZONS = (5, 10, 20, 40, 60)              # 보유 기간·기준일 간격(거래일)
BT_LIMITS = (3, 5, 10)
MONITOR_REF_DAYS = (20, 40, 60, 120, 250)      # 모형 모니터링 비교 기준(몇 거래일 전과 비교)


def _nearest(v, allowed):
    """allowed 중 v 에 가장 가까운 값(같으면 작은 쪽)."""
    return min(allowed, key=lambda a: (abs(a - v), a))


def backtest_params(days=120, horizon=20, step=None, limit=5):
    """백테스트 파라미터 정규화 — 허용 값 중 가장 가까운 값으로 맞춘다(실제 쓴 값은 응답 params 에). 캐시 키도 이 값.
    예전엔 범위 안의 아무 값이나 받아, 값만 바꾼 요청마다 수 초짜리 계산이 새로 돌았고(캐시 무력화 → CPU 독점·
    공용 캐시에서 Top 5 밀어냄), step=1·400일이면 한 번 계산도 기준일 140개(VM 규모 4초+)였다.
    지금: horizon·step ∈ 5/10/20/40/60 거래일(step 기본 = horizon → 기간 안 겹침) · days ∈ 60~400 · limit ∈ 3/5/10
    → 기준일 최대 80개, 경우의 수도 수천 → 수백만 분의 1."""
    horizon = _nearest(int(horizon or 20), BT_HORIZONS)
    step = _nearest(int(step or horizon), BT_HORIZONS)
    days = max(horizon + step, _nearest(int(days or 120), BT_DAYS))
    limit = _nearest(int(limit or 5), BT_LIMITS)
    return days, horizon, step, limit


def monitor_ref_days(ref_days=60):
    return _nearest(int(ref_days or 60), MONITOR_REF_DAYS)


def backtest(days=120, horizon=20, step=None, limit=5, profile=None, cost_bps=None):
    """과거 기준일마다 '그날까지의 데이터로만' Top N 을 뽑고 horizon 거래일 뒤 수익률을 잰다(워크포워드).
    · 벤치마크: 같은 날 적격 종목 동일가중 평균 · 초과수익 = Top N − 벤치마크(왕복 거래비용 차감)
    · IC(정보계수): 종합점수와 이후 수익률의 순위상관(스피어만) — 점수가 수익 '순서'를 맞히는 힘
    · step 기본 = horizon → 기간이 겹치지 않아 누적 곡선을 그대로 복리로 이을 수 있다."""
    from app.config import settings
    days, horizon, step, limit = backtest_params(days, horizon, step, limit)
    cost = (settings.RECO_COST_BPS if cost_bps is None else float(cost_bps)) / 10000
    universe = catalog.universe()
    panel = Panel(universe)
    cal = panel.calendar if panel.series else []
    if len(cal) < MIN_BARS + horizon + 1:
        return {"available": False, "reason": "백테스트에 필요한 일봉이 부족해요", "params": {"days": days}}
    last = len(cal) - 1 - horizon
    first = max(MIN_BARS + 35, len(cal) - 1 - days)
    periods = []
    for k in range(first, last + 1, step):
        d, d_fwd = cal[k], cal[k + horizon]
        scored = score_universe(panel, d)
        if not scored["rows"]:
            continue
        picked, _ = select(scored, panel, limit, profile)
        fwd = {}
        for t in scored["rows"]:
            i0, i1 = panel.pos(t, d), panel.pos(t, d_fwd)
            s = panel.series[t]
            if i0 is None or i1 is None or i1 <= i0 or s["dates"][i1] != d_fwd:
                continue
            fwd[t] = float(s["c"][i1] / s["c"][i0] - 1)
        top = [fwd[r["ticker"]] for r in picked if r["ticker"] in fwd]
        if not top or len(fwd) < 2:
            continue
        bench = float(np.mean(list(fwd.values())))
        top_ret = float(np.mean(top)) - cost
        ks = [t for t in fwd if t in scored["rows"]]
        ic = _spearman([scored["rows"][t]["composite"] for t in ks], [fwd[t] for t in ks])
        periods.append({"date": d, "exit": d_fwd, "top": round(top_ret, 4), "bench": round(bench, 4),
                        "excess": round(top_ret - bench, 4), "ic": round(ic, 3) if ic is not None else None,
                        "hits": sum(1 for x in top if x > bench), "n": len(top), "regime": scored["regime"],
                        "picks": [panel.meta[r["ticker"]]["name"] for r in picked]})
    if not periods:
        return {"available": False, "reason": "검증할 기간이 없어요", "params": {"days": days, "horizon": horizon}}
    ex = np.array([p["excess"] for p in periods])
    ics = np.array([p["ic"] for p in periods if p["ic"] is not None])
    cum_t = cum_b = 1.0
    curve, peak, mdd = [], 1.0, 0.0
    for p in periods:
        cum_t *= 1 + p["top"]
        cum_b *= 1 + p["bench"]
        peak = max(peak, cum_t)
        mdd = min(mdd, cum_t / peak - 1)
        curve.append({"date": p["exit"], "top": round(cum_t - 1, 4), "bench": round(cum_b - 1, 4)})
    hits = sum(p["hits"] for p in periods)
    n_picks = sum(p["n"] for p in periods)
    ic_mean = float(ics.mean()) if len(ics) else None
    ic_std = float(ics.std(ddof=1)) if len(ics) >= 2 else None
    summary = {
        "periods": len(periods), "avg_top": round(float(np.mean([p["top"] for p in periods])), 4),
        "avg_bench": round(float(np.mean([p["bench"] for p in periods])), 4),
        "avg_excess": round(float(ex.mean()), 4), "win_rate": round(float((ex > 0).mean()), 3),
        "hit_rate": round(hits / n_picks, 3) if n_picks else None,
        "ic_mean": round(ic_mean, 3) if ic_mean is not None else None,
        "icir": round(ic_mean / ic_std, 2) if ic_mean is not None and ic_std else None,
        "cum_top": round(cum_t - 1, 4), "cum_bench": round(cum_b - 1, 4), "max_drawdown": round(mdd, 4),
    }
    verdict = ("검증 표본이 적어 결론을 내리기 이른 단계" if len(periods) < 6 else
               "벤치마크 대비 초과수익·IC 모두 양(+) — 점수가 수익 순서를 어느 정도 맞힘"
               if (summary["avg_excess"] > 0 and (summary["ic_mean"] or 0) > 0) else
               "초과수익 또는 IC 가 음(−) — 가중치·팩터 재검토 필요")
    return {"available": True, "model_version": MODEL_VERSION,
            "params": {"days": days, "horizon": horizon, "step": step, "limit": limit, "profile": profile,
                       "cost_bps": round(cost * 10000, 1)},
            "summary": summary, "verdict": verdict, "periods": periods, "curve": curve,
            "caveats": ["과거 성과는 미래 수익을 보장하지 않아요.",
                        "유니버스가 '현재' 시총 상위 종목이라 생존 편향이 있어요(사라진 종목은 빠짐).",
                        f"왕복 거래비용 {cost * 100:.2f}%만 반영했고 세금·슬리피지는 단순화했어요.",
                        "재무는 공시일(없으면 결산 후 90일) 이후에만, 뉴스는 발행일 이후에만 반영해 미래 정보를 쓰지 않아요."]}


# ── 모형 모니터링: 입력 변수 안정성(PSI/CSI) · 성능(IC) 추이 ────────────
# 표준화(z-점수)는 매일 분포를 N(0,1) 근처로 맞추므로 '점수' 분포는 늘 안정적으로 보인다 → 드리프트를 숨긴다.
# 그래서 표준화 '이전'의 원시 입력(변동성·모멘텀·밸류에이션…) 분포가 기준 시점 대비 얼마나 변했는지를 본다.
# 판정 기준(신용평가 모형 관행): PSI < 0.10 안정 · 0.10~0.25 주의 · ≥ 0.25 재검토.
PSI_WARN, PSI_ALERT = 0.10, 0.25
# kind — market: 시장 국면에 따라 '원래' 움직이는 변수(급등락·변동성 장세면 분포가 통째로 이동) → 크게 바뀌어도 '주의'
#        valuation/financial: 3개월 사이 크게 바뀌면 데이터 오류·구조 변화 의심 → '재검토'
MONITOR_VARS = (("vol", "연변동성", "market"), ("mdd", "6개월 최대낙폭(크기)", "market"),
                ("mom", "모멘텀(위험조정)", "market"), ("trend", "60일선 이격", "market"),
                ("sig", "기술적 신호 강도", "market"), ("vratio", "거래대금 증감(로그)", "market"),
                ("upvol", "상승일 거래 비중", "market"), ("sent", "뉴스 심리", "market"),
                ("ep", "이익수익률 E/P", "valuation"), ("bp", "장부가치비율 B/P", "valuation"),
                ("roe", "ROE", "financial"), ("opm", "영업이익률", "financial"))
KIND_LABEL = {"market": "시장", "valuation": "밸류에이션", "financial": "재무"}


def psi(ref, cur, bins=None):
    """PSI(Population Stability Index) = Σ (실제% − 기준%) × ln(실제% / 기준%).
    기준 분포의 분위수로 구간을 나눠(동일 빈도) 현재 분포가 각 구간에 얼마나 들어오는지 비교.
    표본이 작으면 구간을 5개로 줄인다. 빈 구간은 0.01% 로 보정(로그 발산 방지)."""
    r = np.asarray([x for x in ref if x is not None and math.isfinite(x)], dtype=float)
    c = np.asarray([x for x in cur if x is not None and math.isfinite(x)], dtype=float)
    if len(r) < 20 or len(c) < 20:
        return None
    k = bins or (10 if min(len(r), len(c)) >= 100 else 5)
    inner = np.unique(np.quantile(r, np.linspace(0, 1, k + 1))[1:-1])
    if len(inner) < 2:                                   # 값이 거의 한 점에 몰림(분위수 구간이 안 나뉨)
        return None
    rc = np.bincount(np.searchsorted(inner, r, side="right"), minlength=len(inner) + 1)
    cc = np.bincount(np.searchsorted(inner, c, side="right"), minlength=len(inner) + 1)
    rp = np.maximum(rc / rc.sum(), 1e-4)
    cp = np.maximum(cc / cc.sum(), 1e-4)
    return float(np.sum((cp - rp) * np.log(cp / rp)))


def _psi_status(v, kind="financial"):
    """PSI → 판정. 시장 변수는 국면 변화로 크게 움직이는 게 정상이라 상한을 '주의'로 둔다."""
    if v is None:
        return "데이터 부족"
    if v < PSI_WARN:
        return "안정"
    if v < PSI_ALERT or kind == "market":
        return "주의"
    return "재검토"


def monitor(ref_days=60, panel=None):
    """모형 모니터링 리포트 — ① 입력 변수별 PSI(기준일 = 약 3개월 전 vs 최신일, 같은 적격성 규칙)
    ② 적격 모집단·시장 국면 변화 ③ 백테스트 IC 추이(최근 3기간 평균이 음수면 '성능 저하' 경보)."""
    universe = catalog.universe()
    panel = panel or Panel(universe)
    cal = panel.calendar if panel.series else []
    if len(cal) < ref_days + MIN_BARS + 5:
        return {"available": False, "reason": "모니터링 기준 시점을 잡기엔 일봉이 부족해요"}
    d_cur, d_ref = cal[-1], cal[-1 - ref_days]
    cur, ref = score_universe(panel, d_cur), score_universe(panel, d_ref)
    if not cur["rows"] or not ref["rows"]:
        return {"available": False, "reason": "적격 종목이 없어 비교할 수 없어요"}
    rows = []
    for key, label, kind in MONITOR_VARS:
        rv = [x for x in ref["raw"][key].values() if x is not None]
        cv = [x for x in cur["raw"][key].values() if x is not None]
        v = psi(rv, cv)
        rows.append({"var": key, "label": label, "kind": kind, "kind_label": KIND_LABEL[kind],
                     "psi": round(v, 4) if v is not None else None,
                     "status": _psi_status(v, kind), "n_ref": len(rv), "n_cur": len(cv),
                     "ref_median": round(float(np.median(rv)), 4) if rv else None,
                     "cur_median": round(float(np.median(cv)), 4) if cv else None})
    scored = [r for r in rows if r["psi"] is not None]
    worst = max(scored, key=lambda r: r["psi"]) if scored else None
    days, horizon, step, limit = backtest_params(180, 20, None, 5)
    bt = _cached(("bt", days, horizon, step, limit, None, len(universe)),
                 lambda: backtest(days, horizon, step, limit, None), ttl=600)
    ics = [{"date": p["date"], "ic": p["ic"]} for p in (bt.get("periods") or [])][-12:]
    recent = [x["ic"] for x in ics[-3:] if x["ic"] is not None]
    recent_mean = float(np.mean(recent)) if recent else None
    ic_status = "데이터 부족" if recent_mean is None else ("저하" if recent_mean < 0 else "양호")
    alerts = [r["label"] for r in rows if r["status"] == "재검토"]
    warns = [r["label"] for r in rows if r["status"] == "주의"]
    regime_shift = [r["label"] for r in rows if r["kind"] == "market" and (r["psi"] or 0) >= PSI_ALERT]
    status = "재검토" if alerts or ic_status == "저하" else ("주의" if warns else "안정")
    actions = []
    if alerts:
        actions.append(f"재무·밸류에이션 입력 분포가 크게 바뀜({', '.join(alerts)}) — 수집 데이터 오류·공시 반영 여부 확인 후 "
                       "윈저라이즈 구간·가중치 재추정 검토")
    if regime_shift:
        actions.append(f"시장 국면 변화로 분포 이동({', '.join(regime_shift)}) — 횡단면 표준화가 흡수하지만 국면 가중치 적정성 점검")
    if ic_status == "저하":
        actions.append("최근 3개 검증 기간 IC 평균이 음수 — 점수가 수익 순서를 못 맞히는 중, 팩터 재검토")
    if not actions:
        actions.append("유의미한 분포 변화·성능 저하 없음 — 정기 모니터링 유지")
    return {"available": True, "model_version": MODEL_VERSION, "ref_date": d_ref, "cur_date": d_cur,
            "ref_days": ref_days, "status": status, "psi": rows, "worst": worst,
            "thresholds": {"warn": PSI_WARN, "alert": PSI_ALERT,
                           "rule": "PSI < 0.10 안정 · 0.10~0.25 주의 · ≥ 0.25 재검토(시장 변수는 국면 변화라 '주의'까지)"},
            "population": {"ref": len(ref["rows"]), "cur": len(cur["rows"])},
            "regime": {"ref": REGIME_LABEL[ref["regime"]], "cur": REGIME_LABEL[cur["regime"]]},
            "ic": {"series": ics, "recent_mean": round(recent_mean, 3) if recent_mean is not None else None,
                   "overall_mean": (bt.get("summary") or {}).get("ic_mean"), "status": ic_status},
            "actions": actions,
            "note": "z-점수는 매일 다시 표준화돼 드리프트를 숨기므로, 표준화 전 원시 입력의 분포 변화를 본다."}


# ── 모형 검증: 벤치마크 비교(Champion–Challenger) ──────────────────────
# 금융권 모형 검증은 '복잡한 모형이 단순한 대안보다 실제로 나은가'를 같은 기간·같은 규칙으로 비교한다.
# 같은 기준일·같은 적격성·같은 분산/적합성 규칙에서 '가중치만' 바꿔 Top N 을 다시 뽑는다(팩터 z-점수는 공유).
CHALLENGERS = (
    ("champion", "현재 모형 mf-2.0 (국면별 가중치)", None),
    ("equal", "동일 가중 (7팩터 각 1/7)", {k: 1 / len(FACTORS) for k in FACTORS}),
    ("momentum", "모멘텀 단일", {"momentum": 1.0}),
    ("lowrisk", "저변동(안정성) 단일", {"lowrisk": 1.0}),
)


def _rescore(scored, weights):
    """같은 팩터 z-점수에 다른 가중치를 적용한 scored 사본(행은 얕은 복사)."""
    used = [k for k in FACTORS if k in scored["used"]]
    tot = sum(weights.get(k, 0.0) for k in used) or 1.0
    w = {k: (weights.get(k, 0.0) / tot if k in used else 0.0) for k in FACTORS}
    rows = {}
    for t, r in scored["rows"].items():
        contrib = {k: w[k] * r["z"][k] for k in FACTORS}
        rows[t] = {**r, "contrib": contrib, "composite": sum(contrib.values())}
    return {**scored, "rows": rows, "weights": w}


def compare_models(days=180, horizon=20, step=None, limit=5, cost_bps=None):
    """Champion–Challenger — 워크포워드 기간마다 모형별 Top N 초과수익·IC 를 같은 조건으로 비교.
    champion − challenger 기간별 초과수익 차이의 평균·표준오차·t값 → '유의하게 낫다/차이 없다/못하다'."""
    from app.config import settings
    days, horizon, step, limit = backtest_params(days, horizon, step, limit)
    cost = (settings.RECO_COST_BPS if cost_bps is None else float(cost_bps)) / 10000
    panel = Panel(catalog.universe())
    cal = panel.calendar if panel.series else []
    if len(cal) < MIN_BARS + horizon + 1:
        return {"available": False, "reason": "비교에 필요한 일봉이 부족해요"}
    last, first = len(cal) - 1 - horizon, max(MIN_BARS + 35, len(cal) - 1 - days)
    per = {key: [] for key, _, _ in CHALLENGERS}
    for k in range(first, last + 1, step):
        d, d_fwd = cal[k], cal[k + horizon]
        base = score_universe(panel, d)
        if not base["rows"]:
            continue
        fwd = {}
        for t in base["rows"]:
            i0, i1 = panel.pos(t, d), panel.pos(t, d_fwd)
            s = panel.series[t]
            if i0 is not None and i1 is not None and i1 > i0 and s["dates"][i1] == d_fwd:
                fwd[t] = float(s["c"][i1] / s["c"][i0] - 1)
        if len(fwd) < 2:
            continue
        bench = float(np.mean(list(fwd.values())))
        for key, _, w in CHALLENGERS:
            sc = base if w is None else _rescore(base, w)
            picked, _ = select(sc, panel, limit, None)
            top = [fwd[r["ticker"]] for r in picked if r["ticker"] in fwd]
            if not top:
                continue
            ks = [t for t in fwd if t in sc["rows"]]
            ic = _spearman([sc["rows"][t]["composite"] for t in ks], [fwd[t] for t in ks])
            per[key].append({"date": d, "excess": float(np.mean(top)) - cost - bench, "ic": ic})
    if not per["champion"]:
        return {"available": False, "reason": "검증할 기간이 없어요"}
    champ = {p["date"]: p["excess"] for p in per["champion"]}
    models = []
    for key, label, w in CHALLENGERS:
        ps = per[key]
        ex = np.array([p["excess"] for p in ps]) if ps else np.array([])
        ics = [p["ic"] for p in ps if p["ic"] is not None]
        row = {"key": key, "label": label, "weights": w, "periods": len(ps),
               "avg_excess": round(float(ex.mean()), 4) if len(ex) else None,
               "win_rate": round(float((ex > 0).mean()), 3) if len(ex) else None,
               "ic_mean": round(float(np.mean(ics)), 3) if ics else None,
               "cum_excess": round(float(np.prod(1 + ex) - 1), 4) if len(ex) else None}
        if key != "champion":
            diffs = np.array([champ[p["date"]] - p["excess"] for p in ps if p["date"] in champ])
            n = len(diffs)
            se = float(diffs.std(ddof=1) / math.sqrt(n)) if n >= 2 else None
            t = float(diffs.mean() / se) if se else None
            row["vs_champion"] = {
                "n": n, "mean_diff": round(float(diffs.mean()), 4) if n else None,
                "t_stat": round(t, 2) if t is not None else None,
                "champion_better_share": round(float((diffs > 0).mean()), 3) if n else None,
                "verdict": ("표본 부족(6기간 미만) — 판단 보류" if n < 6 else
                            "현재 모형이 유의하게 우수(t ≥ 2)" if t is not None and t >= 2 else
                            "현재 모형이 유의하게 열위(t ≤ −2) — 단순 모형 대비 재검토" if t is not None and t <= -2 else
                            "유의한 차이 없음 — 복잡도 대비 이득이 불분명")}
        models.append(row)
    return {"available": True, "model_version": MODEL_VERSION,
            "params": {"days": days, "horizon": horizon, "step": step, "limit": limit,
                       "cost_bps": round(cost * 10000, 1)},
            "models": models,
            "method": "같은 기준일·적격성·분산 규칙에서 가중치만 바꿔 Top N 재선정 → 기간별 초과수익 차이의 t값(쌍체)",
            "caveats": ["|t| ≥ 2 를 유의 기준으로 근사했어요 — 기간 수가 적을수록 실제 임계값은 더 커요(6기간이면 약 2.45).",
                        "기간이 겹치지 않게 step=horizon 으로 잡아 기간별 차이를 독립 표본처럼 봤어요.",
                        "과거 비교일 뿐 미래 우위를 보장하지 않아요."]}
