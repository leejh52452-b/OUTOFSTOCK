"""내 포트폴리오 리스크 · 스트레스 테스트 — 장기 모의투자 보유 종목(현재가 평가) 기준.

은행 시장리스크 관리의 세 가지 질문을 보유 포트폴리오에 그대로 던진다.
  1) 평소의 최대 손실은?   1일 VaR 95% · 기대손실 ES 97.5%(역사적 시뮬레이션, 현재 비중 고정)
  2) 시장이 무너지면?      스트레스 테스트 — 단일요인(코스피 베타) 충격 시나리오 + 과거 최악 구간 재현
  3) 한쪽에 쏠렸나?        집중도 — HHI · 유효 종목 수 · 단일 종목 한도(40%) 초과 여부
가정(교육용 단순화): 베타 = 일간수익률 기준 cov(종목, 코스피)/var(코스피) · 시나리오 손실 = 평가액 × 베타 × 지수 충격
(종목별 손실은 −100% 를 넘지 않게 자름). 과거 시나리오의 지수 하락폭은 대략치. 실제 손실은 이와 다를 수 있다."""
import numpy as np

from app import db
from app.services import invest, portfolio
from app.services.risk import expected_shortfall, historical_var

MIN_DAYS = 20                      # 공통 거래일이 이보다 적으면 통계를 내지 않는다
SCENARIOS = (                      # (이름, 코스피 충격) — 과거 사례는 고점→저점 대략치
    ("코스피 −5% (급락일)", -0.05),
    ("코스피 −10% (단기 폭락)", -0.10),
    ("코스피 −20% (약세장 진입)", -0.20),
    ("코로나19 급락형 (2020.1~3월, 코스피 약 −36%)", -0.36),
    ("글로벌 금융위기형 (2007.10~2008.10, 코스피 약 −55%)", -0.55),
)


def _kospi_returns():
    """코스피 일간수익률 {날짜: r} — 실데이터 이력이 있으면 표본(gen) 이력은 섞지 않는다."""
    try:
        rows = db.query("SELECT trade_date, close, source FROM index_daily WHERE code='KOSPI' ORDER BY trade_date")
    except Exception:                                    # noqa: BLE001 — 지수 이력 표가 없어도 나머지는 계산
        return {}
    rows = [r for r in rows if r["source"] != "gen"]        # 표본(gen) 지수로 잰 베타는 의미가 없다 → 1 로 가정
    out = {}
    for a, b in zip(rows[:-1], rows[1:]):
        if a["close"] and b["close"]:
            out[b["trade_date"]] = float(b["close"]) / float(a["close"]) - 1
    return out


def _beta(stock_r, dates, mkt):
    """일간수익률 베타 — 종목·지수 공통 날짜가 MIN_DAYS 미만이면 None."""
    pairs = [(x, mkt[d]) for x, d in zip(stock_r, dates) if d in mkt]
    if len(pairs) < MIN_DAYS:
        return None
    s = np.array([p[0] for p in pairs])
    m = np.array([p[1] for p in pairs])
    vm = m.var(ddof=1)
    return float(np.cov(s, m, ddof=1)[0, 1] / vm) if vm > 0 else None


def invest_risk(username):
    pos = invest.positions(username)
    held = [p for p in pos["open"] if p["value"] > 0]
    if not held:
        return {"ok": False, "reason": "보유 종목이 없어요 — 종목을 매수하면 리스크를 계산해요"}
    by = {}                                              # 같은 종목 여러 포지션 → 합산
    for p in held:
        b = by.setdefault(p["ticker"], {"ticker": p["ticker"], "name": p["name"], "value": 0})
        b["value"] += p["value"]
    tickers = list(by)
    values = np.array([by[t]["value"] for t in tickers], dtype=float)
    total = float(values.sum())
    w = values / total
    conc = portfolio.concentration(w.tolist())
    returns, common = portfolio._aligned_returns(tickers)
    if returns is not None and len(returns) > 250:          # 최근 1년(250거래일)만 — 오래된 국면이 섞이지 않게
        returns, common = returns[-250:], common[-251:]
    out = {"ok": True, "total_value": int(total), "positions": len(held), "tickers": len(tickers),
           "concentration": {k: conc[k] for k in ("hhi", "effective_n", "max_weight", "limit", "limit_breach")},
           "assumptions": ["역사적 시뮬레이션(현재 비중 고정) · 1일 기준", "베타 = 일간수익률 cov(종목, 코스피)/var(코스피)",
                           "시나리오 손실 = 평가액 × 베타 × 지수 충격(종목별 −100% 한도) — 과거 사례 하락폭은 대략치",
                           "교육용 단순화 — 실제 손실은 다를 수 있어요"]}
    if returns is None or len(returns) < MIN_DAYS:
        out.update(stats_ok=False, reason=f"공통 거래일이 부족해요({len(common)}일, 최소 {MIN_DAYS + 1}일)")
        items = [{"ticker": t, "name": by[t]["name"], "value": int(by[t]["value"]), "weight": round(float(x), 4),
                  "beta": None} for t, x in zip(tickers, w)]
        out["items"] = items
        return out
    rdates = common[1:]
    rp = returns @ w
    var95 = historical_var(rp, 0.95) or 0.0
    es975 = expected_shortfall(rp, 0.975) or 0.0
    mkt = _kospi_returns()
    betas = [_beta(returns[:, i], rdates, mkt) for i in range(len(tickers))]
    have_beta = all(b is not None for b in betas)
    port_beta = float(np.dot(w, [b if b is not None else 1.0 for b in betas]))
    scen = []
    for name, shock in SCENARIOS:
        loss = 0.0
        for v, b in zip(values, betas):
            loss += v * max(-1.0, (b if b is not None else 1.0) * shock)    # 종목 손실은 −100% 한도
        scen.append({"name": name, "market_shock": shock, "loss": int(round(loss)),
                     "loss_pct": round(loss / total, 4)})
    worst_i = int(np.argmin(rp))
    win = min(20, len(rp))
    cum = np.array([np.prod(1 + rp[i:i + win]) - 1 for i in range(0, len(rp) - win + 1)])
    worst_j = int(np.argmin(cum)) if len(cum) else None
    hist = [{"name": f"과거 최악의 하루 ({rdates[worst_i]})", "loss": int(round(total * float(rp[worst_i]))),
             "loss_pct": round(float(rp[worst_i]), 4)}]
    if worst_j is not None:
        hist.append({"name": f"과거 최악의 {win}거래일 ({rdates[worst_j]}~{rdates[worst_j + win - 1]})",
                     "loss": int(round(total * float(cum[worst_j]))), "loss_pct": round(float(cum[worst_j]), 4)})
    out.update({
        "stats_ok": True, "days": len(rp), "period": {"from": rdates[0], "to": rdates[-1]},
        "var_95": round(var95, 4), "var_95_krw": int(round(total * var95)),
        "es_975": round(es975, 4), "es_975_krw": int(round(total * es975)),
        "annual_volatility": round(float(rp.std(ddof=1) * np.sqrt(252)), 4) if len(rp) > 1 else None,
        "beta": round(port_beta, 3), "beta_complete": have_beta, "beta_source": "kospi" if mkt else "assumed",
        "scenarios": scen, "historical": hist,
        "items": [{"ticker": t, "name": by[t]["name"], "value": int(by[t]["value"]), "weight": round(float(x), 4),
                   "beta": round(b, 3) if b is not None else None} for t, x, b in zip(tickers, w, betas)],
    })
    if not mkt:
        out["assumptions"].append("코스피 실데이터 이력이 없어 베타를 1로 가정했어요(python run.py fetch-index 로 이력 수집)")
    return out
