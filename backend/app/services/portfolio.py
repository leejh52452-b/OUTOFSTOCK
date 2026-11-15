"""포트폴리오 리스크 분석 — 개별 종목 리스크를 '상관관계를 반영해' 포트폴리오 단위로 집계.

은행 리스크관리의 핵심 개념: 분산투자 효과(diversification)를 공분산 행렬로 정량화한다.
  - 종목 간 수익률 상관행렬(correlation matrix)
  - 포트폴리오 변동성  σ_p = sqrt(wᵀ Σ w)     (Σ=공분산행렬, w=비중벡터)
  - 분산효과: 상관=1 가정의 '가중평균 개별변동성' 대비 실제 포트폴리오 변동성이 얼마나 낮은가
  - 포트폴리오 역사적 VaR
numpy 선형대수로 계산. ※ 교육용 — 실제 리스크 모형(요인모형·EWMA 등)과는 가정이 다르다."""
import numpy as np
from app import db
from app.services.risk import TRADING_DAYS, historical_var, expected_shortfall


def _clean(m):
    """numpy 배열 → 리스트(소수 4자리), NaN·inf 는 None — 가격이 한 번도 안 움직인 종목은 상관계수가
    NaN 이라 JSON 변환에서 500 이 났다."""
    arr = np.round(np.atleast_2d(np.asarray(m, dtype=float)), 4)
    return [[(None if not np.isfinite(x) else float(x)) for x in row] for row in arr]


def _aligned_returns(tickers):
    """여러 종목을 '공통 거래일'에 맞춘 일간수익률 행렬 (행=날짜, 열=종목)."""
    series = {}
    for t in tickers:
        rows = db.query("SELECT trade_date, close FROM daily_price "
                        "WHERE ticker=:t AND close IS NOT NULL ORDER BY trade_date", {"t": t})
        series[t] = {r["trade_date"]: r["close"] for r in rows}
    if not series or any(not s for s in series.values()):
        return None, []
    common = sorted(set.intersection(*[set(s) for s in series.values()]))
    if len(common) < 3:                                   # 수익률 2개 이상 필요
        return None, common
    price = np.array([[series[t][d] for t in tickers] for d in common], dtype=float)  # (T, N)
    returns = np.diff(price, axis=0) / price[:-1]         # (T-1, N)
    return returns, common


def analyze(tickers, weights=None):
    """포트폴리오 리스크 종합 — 변동성·분산효과·VaR·상관행렬."""
    if not tickers:
        return {"ok": False, "reason": "종목이 없습니다"}
    if weights is not None and len(weights) != len(tickers):
        return {"ok": False, "reason": "비중 개수가 종목 수와 다릅니다"}

    returns, common = _aligned_returns(tickers)
    if returns is None:
        return {"ok": False, "reason": "공통 거래일이 부족(3일 이상 필요)"}

    n = len(tickers)
    w = np.asarray(weights, dtype=float) if weights is not None else np.full(n, 1.0 / n)
    if not np.all(np.isfinite(w)) or np.any(w < 0):
        return {"ok": False, "reason": "비중은 0 이상의 숫자여야 합니다(공매도 비중은 지원하지 않음)"}
    if w.sum() == 0:
        return {"ok": False, "reason": "비중 합이 0입니다"}
    w = w / w.sum()                                       # 합=1 로 정규화

    cov = np.atleast_2d(np.cov(returns, rowvar=False, ddof=1))   # 일간 공분산행렬
    port_var_daily = max(float(w @ cov @ w), 0.0)               # wᵀΣw (음수 방어)
    port_vol = float(np.sqrt(port_var_daily * TRADING_DAYS))    # 연율화 포트폴리오 변동성

    indiv_vol = np.sqrt(np.diag(cov) * TRADING_DAYS)           # 개별 연율화 변동성
    weighted_avg_vol = float(w @ indiv_vol)                    # 상관=1 가정 시 변동성
    div_ratio = (weighted_avg_vol / port_vol) if port_vol > 0 else None
    risk_reduction = (1 - port_vol / weighted_avg_vol) if weighted_avg_vol > 0 else None

    port_returns = returns @ w                                 # 포트폴리오 일간수익률
    var95 = historical_var(port_returns, 0.95)
    es975 = expected_shortfall(port_returns, 0.975)

    with np.errstate(invalid="ignore", divide="ignore"):       # 움직이지 않은 종목은 상관계수 미정의(NaN → null)
        corr = np.corrcoef(returns, rowvar=False) if n >= 2 else None
    conc = concentration(w.tolist())                      # 집중위험(익스포저 기반)

    return {
        "ok": True, "tickers": tickers, "days": len(common),
        "weights": [round(x, 4) for x in w.tolist()],
        "portfolio_volatility": round(port_vol, 4),
        "weighted_avg_volatility": round(weighted_avg_vol, 4),
        "diversification_ratio": round(div_ratio, 4) if div_ratio is not None else None,
        "risk_reduction": round(risk_reduction, 4) if risk_reduction is not None else None,
        "portfolio_var_95": round(var95, 4) if var95 is not None else None,
        "portfolio_es_975": round(es975, 4) if es975 is not None else None,
        "individual_volatility": {t: round(float(v), 4) for t, v in zip(tickers, indiv_vol)},
        "correlation": _clean(corr) if corr is not None else None,
        "concentration": {k: conc[k] for k in ("hhi", "effective_n", "max_weight", "limit_breach")},
    }


def concentration(weights, limit=0.4):
    """집중위험 — 허핀달지수(HHI=Σwᵢ²)·유효종목수(1/HHI)·최대 단일비중과 한도 초과 여부.
    은행이 한도관리에 쓰는 '익스포저 기반' 집중도(변동성 기반 분산비율과 상보적).
    HHI 는 1/N(완전분산)~1(1종목 집중), 유효종목수는 '실질적으로 몇 종목에 분산됐나'."""
    w = np.asarray(weights, dtype=float)
    if w.size == 0 or w.sum() == 0:
        return {"ok": False, "reason": "비중이 없습니다"}
    w = w / w.sum()
    hhi = float(np.sum(w ** 2))
    return {"ok": True, "hhi": round(hhi, 4), "effective_n": round(1.0 / hhi, 2),
            "max_weight": round(float(w.max()), 4), "limit": limit,
            "limit_breach": bool(w.max() > limit)}
