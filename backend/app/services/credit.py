"""시장 기반 신용위험 — Merton(1974) 구조모형의 부도거리(DD)·부도확률(PD). (KMV 방식의 교육용 축약)

회계 기반 Altman Z''(재무제표) 와 짝을 이루는 '시장 기반' 지표다.
  주식 = 기업 자산(V)에 대한 콜옵션(행사가 = 부채 F). 시가총액 E 와 주가 변동성 σE 를 관측해서
  보이지 않는 자산가치 V 와 자산 변동성 σV 를 거꾸로 푼다.
      E   = V·N(d1) − F·e^(−rT)·N(d2)
      σE·E = N(d1)·σV·V
      d1 = [ln(V/F) + (r + σV²/2)T] / (σV√T),  d2 = d1 − σV√T
  부도거리 DD = [ln(V/F) + (r − σV²/2)T] / (σV√T)  — 자산가치가 부도점에서 표준편차 몇 개 떨어져 있나
  부도확률 PD = N(−DD)  (μ = r 로 둔 위험중립 확률 — 실제 확률과 다를 수 있음)
입력
  E  = 최신 시가총액(원)          σE = 최근 250거래일 일간수익률 연환산 변동성
  F  = 부도점(KMV 관행) = 유동부채 + ½ × 비유동부채(= 부채총계 − 유동부채)  — DART 재무상태표
  r  = 무위험이자율 — 그 시점의 국고채 3년(분석 계층 지표, ECOS·KIS 실데이터). 없으면 설정 RISK_FREE_RATE(기본 3%)   T = 1년
금융업(은행·보험·증권 등)은 부채가 '영업 재료'라 이 모형의 가정이 맞지 않아 제외한다.
※ 교육용 — 실제 여신 심사의 PD 는 장기 부도 데이터로 보정(calibration)한 내부등급 모형을 쓴다."""
import bisect
import math
import time
from datetime import date

from app import db
from app.config import settings

PD_BANDS = ((0.001, "매우 낮음"), (0.01, "낮음"), (0.05, "보통"), (0.20, "높음"))
_RF = {"at": None, "dates": [], "vals": [], "src": []}        # at: 마지막으로 읽은 시각(monotonic)
_RF_TTL = 3600


def _rf_series():
    """국고채 3년 실데이터 시계열(표본 'gen' 제외) — 1시간 캐시. 분석 계층이 없으면 빈 목록."""
    if _RF["at"] is not None and time.monotonic() - _RF["at"] < _RF_TTL:   # monotonic 은 부팅 후 초 — 0 을 '없음'으로 쓰면 안 됨
        return _RF
    dates, vals, src = [], [], []
    try:
        for r in db.query("SELECT trade_date, value, source FROM analytics.indicator_daily "
                          "WHERE series = 'KTB3Y' AND source <> 'gen' AND value > 0 AND value < 20 ORDER BY trade_date"):
            d = r["trade_date"]                         # 연결 로더가 date 를 'YYYY-MM-DD' 로 읽는다 — 날짜 계산용으로 되돌림
            dates.append(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]))
            vals.append(float(r["value"]) / 100)
            src.append(r["source"])
    except Exception:                                    # noqa: BLE001 — 분석 계층 전(구버전 DB)이면 설정값
        pass
    _RF.update(at=time.monotonic(), dates=dates, vals=vals, src=src)
    return _RF


def clear_rf_cache():
    _RF.update(at=None, dates=[], vals=[], src=[])


def risk_free_rate(as_of=None):
    """(연율, 출처 설명) — as_of '이전' 가장 최근 국고채 3년(시점 일관성: 그날 알 수 없던 금리는 쓰지 않는다).
    실데이터가 없거나 as_of 가 시계열보다 앞서면 설정값 RISK_FREE_RATE."""
    rf = _rf_series()
    if rf["dates"]:
        d = as_of if isinstance(as_of, date) else (date.fromisoformat(str(as_of)[:10]) if as_of else rf["dates"][-1])
        i = bisect.bisect_right(rf["dates"], d) - 1
        if i >= 0 and (d - rf["dates"][i]).days <= 31:      # 한 달 넘게 비어 있으면 낡은 값 대신 설정값
            return rf["vals"][i], f"국고채 3년 {rf['dates'][i].isoformat()}"
    return settings.RISK_FREE_RATE, "설정값(RISK_FREE_RATE)"
MIN_DAYS = 60
_FIN_WORDS = ("금융", "지주", "은행", "보험", "증권", "생명", "화재", "카드", "캐피탈", "리츠")


def _ncdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _d12(V, F, r, sv, T):
    d1 = (math.log(V / F) + (r + 0.5 * sv * sv) * T) / (sv * math.sqrt(T))
    return d1, d1 - sv * math.sqrt(T)


def merton(E, sigma_e, F, r=None, T=1.0, tol=1e-10, max_iter=200):
    """Merton 연립방정식 풀이 — 반복법(σV 고정 → V 이분법 → σV 갱신)을 수렴할 때까지.
    반환 {V, sigma_v, dd, pd, converged, iterations} | None(입력 부적합)."""
    r = settings.RISK_FREE_RATE if r is None else r
    if not E or E <= 0 or not F or F <= 0 or not sigma_e or sigma_e <= 0:
        return None
    sv = sigma_e * E / (E + F)                            # 출발점: 레버리지로 희석한 주식 변동성
    V = E + F
    converged = False
    for it in range(1, max_iter + 1):
        lo, hi = E, E + F * math.exp(-r * T) + E          # f(lo) ≤ 0 ≤ f(hi)
        for _ in range(200):                              # E = 콜옵션 가치가 되는 V (단조 증가) — 이분법
            mid = 0.5 * (lo + hi)
            d1, d2 = _d12(mid, F, r, sv, T)
            f = mid * _ncdf(d1) - F * math.exp(-r * T) * _ncdf(d2) - E
            if f > 0:
                hi = mid
            else:
                lo = mid
            if hi - lo <= tol * max(1.0, mid):
                break
        V = 0.5 * (lo + hi)
        d1, _ = _d12(V, F, r, sv, T)
        sv_new = sigma_e * E / (_ncdf(d1) * V)
        if abs(sv_new - sv) <= 1e-8:
            sv, converged = sv_new, True
            break
        sv = sv_new
    dd = (math.log(V / F) + (r - 0.5 * sv * sv) * T) / (sv * math.sqrt(T))
    return {"V": V, "sigma_v": sv, "dd": dd, "pd": _ncdf(-dd), "converged": converged, "iterations": it}


def pd_band(pd):
    for cut, label in PD_BANDS:
        if pd < cut:
            return label
    return "매우 높음"


def default_point(fin):
    """KMV 부도점 = 유동부채 + ½·비유동부채. 유동부채가 없으면 부채총계(보수적)로."""
    if not fin:
        return None, None
    tl, cl = fin.get("total_liabilities"), fin.get("current_liabilities")
    if tl and cl is not None and tl > 0:
        return float(cl) + 0.5 * max(0.0, float(tl) - float(cl)), "유동부채 + ½×비유동부채"
    if tl and tl > 0:
        return float(tl), "부채총계(유동부채 없음 — 보수적)"
    return None, None


def _equity_inputs(ticker, as_of=None):
    """(시가총액, 연환산 주가변동성, 관측일수, 기준일) — as_of 이하 데이터만(시점 정합)."""
    cond, p = "", {"t": ticker}
    if as_of:
        cond, p["d"] = " AND trade_date <= :d", as_of
    rows = db.query("SELECT trade_date, close, market_cap FROM daily_price WHERE ticker=:t AND close > 0"
                    + cond + " ORDER BY trade_date DESC LIMIT 251", p)
    rows = rows[::-1]
    if len(rows) < MIN_DAYS + 1:
        return None, None, len(rows), None
    closes = [float(r["close"]) for r in rows]
    rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
    mc = next((float(r["market_cap"]) for r in reversed(rows) if r["market_cap"]), None)
    return mc, sd * math.sqrt(252), len(rets), rows[-1]["trade_date"]


def merton_for(ticker, fin, name="", as_of=None):
    """종목 하나의 Merton DD·PD (재무 fin 은 호출자가 시점에 맞춰 넘긴다)."""
    if name and any(w in name for w in _FIN_WORDS):
        return {"ok": False, "reason": "금융업은 적용 대상 아님"}
    F, rule = default_point(fin)
    if F is None:
        return {"ok": False, "reason": "부채 항목 없음(DART 재무상태표 재수집 필요)"}
    E, sigma_e, n, d = _equity_inputs(ticker, as_of)
    if not E or not sigma_e:
        return {"ok": False, "reason": f"시가총액·주가 이력 부족({n}일, 최소 {MIN_DAYS + 1}일)"}
    r, r_src = risk_free_rate(d or as_of)
    m = merton(E, sigma_e, F, r=r)
    if not m:
        return {"ok": False, "reason": "입력값이 모형 가정에 맞지 않음"}
    return {"ok": True, "dd": round(m["dd"], 2), "pd": round(m["pd"], 6), "pd_band": pd_band(m["pd"]),
            "asset_value": round(m["V"]), "asset_vol": round(m["sigma_v"], 4), "converged": m["converged"],
            "inputs": {"equity": round(E), "equity_vol": round(sigma_e, 4), "default_point": round(F),
                       "default_point_rule": rule, "risk_free": round(r, 5), "risk_free_source": r_src, "horizon_years": 1,
                       "days": n, "as_of": d},
            "note": "위험중립 확률(μ=r) · 교육용 — 실제 PD 는 부도 이력으로 보정한 모형을 쓴다"}
