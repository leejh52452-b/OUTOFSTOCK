"""금융 리스크 지표 엔진 — 은행 리스크관리의 기본 언어(변동성·VaR·기대손실·최대낙폭) + VaR 모형 검증.

일별 종가에서 수익률 시계열을 만들고 다음을 계산한다:
  - 연율화 변동성(annualized volatility): 수익률 표준편차 x sqrt(252)
  - 역사적 VaR(Value at Risk): 수익률 분포 하위 분위수 손실 — '이 신뢰수준에서 하루 최대 예상손실'
  - 기대손실 ES(Expected Shortfall, CVaR): VaR 를 넘는 꼬리 손실의 평균 — 바젤 FRTB 시장리스크 기준(97.5%)
  - 최대낙폭(MDD, Max Drawdown): 고점 대비 최대 하락률
  - 기간 누적수익률
VaR 백테스트(모형 검증)
  - 롤링 아웃오브샘플: 날짜 t 의 VaR 는 t '이전' W 일로만 추정 → t 의 실제 손실과 비교(미래 정보 없음)
    (예전 방식은 같은 표본으로 VaR 를 만들고 같은 표본으로 검증해 초과율이 구조적으로 1−신뢰수준에 맞춰졌다)
  - Kupiec POF(초과 '횟수'가 맞나) · Christoffersen 독립성(초과가 '몰려서' 나나) · 조건부 적정성(둘 다)
  - 바젤 신호등(Traffic Light): 초과 횟수의 이항 누적확률 95% 미만 녹색 · 99.99% 미만 황색 · 이상 적색
    (250일·99% 기준이면 녹색 0~4회 · 황색 5~9회 · 적색 10회 이상 — BCBS 1996 백테스팅 체계)
numpy 로 벡터 연산. ※ 교육용 지표 — 실제 리스크 모형과는 가정·기간·분포가정이 다르다."""
import math

import numpy as np
from app import db

TRADING_DAYS = 252        # 연율화 기준 영업일 수
CHI2_95_1DF = 3.841       # 카이제곱(자유도 1) 95% 임계값 — Kupiec·독립성 검정 기각선
CHI2_95_2DF = 5.991       # 카이제곱(자유도 2) 95% 임계값 — 조건부 적정성(Christoffersen CC)
BACKTEST_WINDOW = 250     # VaR 추정 창(영업일) — 데이터가 짧으면 줄인다(최소 60)
BACKTEST_EVAL = 250       # 검증 구간(최근 영업일) — 바젤 백테스팅 표준 250일
MIN_OOS = 30              # 아웃오브샘플 관측이 이보다 적으면 표본 내(in-sample) 방식으로 대체


def _series(ticker):
    """(날짜, 수정 종가) — 액면분할·무상증자 날이 '−80% 폭락'으로 들어가 변동성·VaR·최대낙폭이 커지지 않게
    KRX 공식 등락률로 권리 이벤트를 찾아 그 이전 종가를 맞춘다(app/services/adjust.py)."""
    from app.services import adjust
    rows = db.query("SELECT trade_date, close, change_rate FROM daily_price WHERE ticker=:t AND close IS NOT NULL "
                    "AND close > 0 ORDER BY trade_date", {"t": ticker})
    dates, closes = [r["trade_date"] for r in rows], [r["close"] for r in rows]
    rates = {r["trade_date"]: float(r["change_rate"]) for r in rows if r["change_rate"] is not None}
    return dates, adjust.adjust_closes(dates, closes, rates)


def _closes(ticker):
    return _series(ticker)[1]


def daily_returns(closes):
    """단순 일간수익률 r_t = P_t / P_{t-1} - 1."""
    a = np.asarray(closes, dtype=float)
    if a.size < 2:
        return np.array([])
    return np.diff(a) / a[:-1]


def volatility(returns, annualize=True):
    """수익률 표준편차(표본, ddof=1). annualize=True 면 연율화."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return None
    v = float(np.std(r, ddof=1))
    return v * np.sqrt(TRADING_DAYS) if annualize else v


def historical_var(returns, conf=0.95):
    """역사적 VaR — 수익률 분포의 (1-conf) 분위수 손실을 '양수'로 반환.
    예) conf=0.95 → 하위 5% 분위수. 그 지점이 손실이면 크기를, 아니면 0."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return None
    q = float(np.quantile(r, 1 - conf))
    return -q if q < 0 else 0.0


def expected_shortfall(returns, conf=0.975):
    """역사적 ES(CVaR) — (1-conf) 분위수 이하 '꼬리' 수익률의 평균 손실을 양수로. 꼬리가 이익뿐이면 0."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return None
    q = float(np.quantile(r, 1 - conf))
    tail = r[r <= q]
    if tail.size == 0:
        return None
    es = -float(tail.mean())
    return es if es > 0 else 0.0


def max_drawdown(closes):
    """최대낙폭(MDD) — 고점 대비 최대 하락률(음수, 예: -0.23 = -23%)."""
    a = np.asarray(closes, dtype=float)
    if a.size < 2:
        return None
    peak = np.maximum.accumulate(a)
    return float(((a - peak) / peak).min())


def cumulative_return(closes):
    if len(closes) < 2:
        return None
    return float(closes[-1] / closes[0] - 1)


def _kupiec_pof(n, x, p):
    """Kupiec 비율검정(Proportion Of Failures) LR 통계량.
    n=관측수, x=초과(breach)수, p=기대 실패확률(=1-신뢰수준). H0: 실제 실패율 = p.
    LR ~ chi2(1); LR > 3.841 이면 95%에서 'VaR 모델이 틀렸다'고 기각."""
    if n == 0:
        return None
    pi = x / n                                            # 관측 실패율(MLE)

    def _ll(rate):                                        # 이항 로그우도(경계에서 log 발산 방지)
        ll = 0.0
        if x > 0:
            ll += x * math.log(rate)
        if n - x > 0:
            ll += (n - x) * math.log(1 - rate)
        return ll

    return max(0.0, -2.0 * (_ll(p) - _ll(pi)))           # LR ≥ 0 (부동소수 −0.0 정리)


def _chi2_sf(x, df):
    """카이제곱 생존함수(p-값) — 자유도 1: erfc(√(x/2)), 자유도 2: e^(−x/2). (scipy 없이 정확한 닫힌 식)"""
    if x is None:
        return None
    x = max(float(x), 0.0)
    return math.erfc(math.sqrt(x / 2)) if df == 1 else math.exp(-x / 2)


def _christoffersen_ind(hits):
    """Christoffersen(1998) 독립성 검정 LR — 어제 초과했을 때 오늘 초과할 확률(π1)과
    어제 초과 안 했을 때 확률(π0)이 같은가. 다르면 '초과가 몰려서' 난다 = 변동성 군집을 VaR 가 못 따라감."""
    n = [[0, 0], [0, 0]]
    for a, b in zip(hits[:-1], hits[1:]):
        n[a][b] += 1
    n00, n01, n10, n11 = n[0][0], n[0][1], n[1][0], n[1][1]
    tot = n00 + n01 + n10 + n11
    if tot == 0 or (n01 + n11) == 0:
        return 0.0, n11

    def ll(p, k0, k1):                                    # k0 번 (1-p) · k1 번 p 의 로그우도(경계 안전)
        v = 0.0
        if k0:
            v += k0 * math.log(1 - p)
        if k1:
            v += k1 * math.log(p)
        return v
    pi = (n01 + n11) / tot
    pi0 = n01 / (n00 + n01) if (n00 + n01) else 0.0
    pi1 = n11 / (n10 + n11) if (n10 + n11) else 0.0
    lr = -2.0 * (ll(pi, n00 + n10, n01 + n11) - (ll(pi0, n00, n01) + ll(pi1, n10, n11)))
    return max(lr, 0.0), n11


def _binom_cdf(k, n, p):
    """이항 누적확률 P(X ≤ k) — 바젤 신호등 경계 계산용."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


def traffic_light(n, x, p):
    """바젤 신호등 — 초과 x 회의 누적확률이 95% 미만 녹색, 99.99% 미만 황색, 이상 적색.
    경계(녹색 최대·황색 최대)도 같이 돌려준다(250일·1% 면 4·9)."""
    cum, yellow_from, red_from = 0.0, None, None
    for k in range(0, n + 1):
        cum += math.comb(n, k) * p ** k * (1 - p) ** (n - k)
        if yellow_from is None and cum >= 0.95:
            yellow_from = k
        if red_from is None and cum >= 0.9999:
            red_from = k
            break
    yellow_from = n + 1 if yellow_from is None else yellow_from
    red_from = n + 1 if red_from is None else red_from
    zone = "green" if x < yellow_from else ("yellow" if x < red_from else "red")
    label = {"green": "녹색(적정)", "yellow": "황색(주의)", "red": "적색(부적정)"}[zone]
    return {"zone": zone, "label": label, "green_max": yellow_from - 1, "yellow_max": red_from - 1,
            "cum_prob": round(_binom_cdf(x, n, p), 4)}


def var_backtest(ticker, conf=0.95, window=None):
    """VaR 백테스트 — VaR 추정치를 실제 손실과 대조해 '모형이 맞는지'를 검증(모형 리스크 관리).
    롤링 아웃오브샘플: 날짜 t 의 VaR = t 이전 W 영업일의 역사적 VaR → t 의 수익률이 −VaR 보다 나쁘면 초과(breach).
    최근 최대 250일을 평가하고 Kupiec POF · Christoffersen 독립성/조건부 적정성 · 바젤 신호등으로 판정.
    데이터가 짧아 아웃오브샘플 관측이 30일 미만이면 표본 내(in-sample) 방식으로 대체하고 method 로 표시한다."""
    dates, closes = _series(ticker)
    if len(closes) < 30:
        return {"ticker": ticker, "ok": False, "reason": "백테스트에 최소 30일 필요"}
    r = daily_returns(closes)
    n_all = int(r.size)
    rdates = dates[1:]
    p = 1 - conf
    W = int(window) if window else min(BACKTEST_WINDOW, max(60, n_all // 2))
    if n_all - W >= MIN_OOS:
        method = "rolling_oos"
        start = max(W, n_all - BACKTEST_EVAL)
        hits, var_path = [], []
        for t in range(start, n_all):
            v = historical_var(r[t - W:t], conf)
            var_path.append(v)
            hits.append(1 if r[t] < -v else 0)
        hit_dates = [rdates[t] for t, h in zip(range(start, n_all), hits) if h]
        current_var = historical_var(r[n_all - W:], conf)  # 내일 적용될 VaR(최근 W일)
        period = {"from": rdates[start], "to": rdates[-1]}
    else:                                                 # 데이터가 짧다 → 표본 내(참고용)
        method, W = "in_sample", n_all
        current_var = historical_var(r, conf)
        hits = [1 if x < -current_var else 0 for x in r]
        hit_dates = [d for d, h in zip(rdates, hits) if h]
        period = {"from": rdates[0], "to": rdates[-1]}
    n = len(hits)
    breaches = int(sum(hits))
    lr = _kupiec_pof(n, breaches, p)
    reject = lr is not None and lr > CHI2_95_1DF
    lr_ind, clustered = _christoffersen_ind(hits)
    lr_cc = (lr or 0.0) + lr_ind
    tl = traffic_light(n, breaches, p)
    if tl["zone"] == "red" or reject:
        verdict = "모델 부적정(재검토 필요)"
    elif tl["zone"] == "yellow" or lr_ind > CHI2_95_1DF:
        verdict = "주의(초과가 많거나 몰려서 발생)"
    else:
        verdict = "모델 적정"
    if method == "in_sample":
        verdict += " · 표본 내 검증(데이터 부족 — 참고용)"
    return {
        "ticker": ticker, "ok": True, "confidence": conf, "method": method, "window": W,
        "period": period, "observations": n,
        "var": round(current_var, 4), "breaches": breaches, "expected_breaches": round(p * n, 2),
        "breach_rate": round(breaches / n, 4) if n else None,
        "kupiec_lr": round(lr, 4) if lr is not None else None,
        "kupiec_pvalue": round(_chi2_sf(lr, 1), 4) if lr is not None else None,
        "reject_model_at_95": reject,
        "christoffersen": {"lr_ind": round(lr_ind, 4), "p_ind": round(_chi2_sf(lr_ind, 1), 4),
                           "reject_ind": lr_ind > CHI2_95_1DF, "clustered_breaches": clustered,
                           "lr_cc": round(lr_cc, 4), "p_cc": round(_chi2_sf(lr_cc, 2), 4),
                           "reject_cc": lr_cc > CHI2_95_2DF},
        "traffic_light": tl,
        "recent_breaches": hit_dates[-5:],
        "verdict": verdict,
    }


def risk_profile(ticker):
    """종목의 리스크 지표 묶음 — API/화면에서 바로 쓰는 형태."""
    closes = _closes(ticker)
    if len(closes) < 3:                                   # 표본 변동성(ddof=1)에 수익률 2개 이상 필요
        return {"ticker": ticker, "ok": False, "reason": "가격 데이터 부족(3일 이상 필요)"}
    r = daily_returns(closes)
    return {
        "ticker": ticker, "ok": True, "days": len(closes),
        "cumulative_return": round(cumulative_return(closes), 4),
        "annual_volatility": round(volatility(r), 4),
        "var_95": round(historical_var(r, 0.95), 4),
        "var_99": round(historical_var(r, 0.99), 4),
        "es_975": round(expected_shortfall(r, 0.975) or 0.0, 4),     # 바젤 FRTB 기준 기대손실
        "max_drawdown": round(max_drawdown(closes), 4),
    }
