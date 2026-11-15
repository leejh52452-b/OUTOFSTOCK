"""내 포트폴리오 리스크 · 스트레스 테스트(/invest/risk) — VaR·ES(원화)·베타·시나리오·집중도."""
import datetime as dt

import numpy as np
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import invest, stress, wallet

client = TestClient(app)


def _seed(n=80, beta=1.5, seed=0):
    """코스피(yahoo 출처) + 베타 1.5 종목(005930) + 베타 0.5 종목(000660) 일봉."""
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.01, n)
    hi = beta * m + rng.normal(0, 0.002, n)
    lo = 0.5 * m + rng.normal(0, 0.002, n)
    d0 = dt.date(2026, 3, 2)
    days = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n + 1)]
    with db.transaction() as conn:
        from sqlalchemy import text
        for code_or_t, rets, base in (("KOSPI", m, 3000.0), ("005930", hi, 70000.0), ("000660", lo, 180000.0)):
            px = base * np.cumprod(np.concatenate([[1.0], 1 + rets]))
            for d, c in zip(days, px):
                if code_or_t == "KOSPI":
                    conn.execute(text("INSERT INTO index_daily(code, trade_date, close, source) VALUES('KOSPI',:d,:c,'yahoo')"),
                                 {"d": d, "c": float(c)})
                else:
                    conn.execute(text("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate) "
                                      "VALUES(:t,:d,:c,1000,0)"), {"t": code_or_t, "d": d, "c": float(c)})
    return days


def _last_close(t):
    return int(db.query("SELECT close FROM daily_price WHERE ticker=:t ORDER BY trade_date DESC LIMIT 1",
                        {"t": t})[0]["close"])


def test_no_positions_reports_reason():
    assert stress.invest_risk("nobody")["ok"] is False


def test_portfolio_risk_and_stress_scenarios(monkeypatch):
    _seed()
    monkeypatch.setattr(invest, "_current_price", lambda t, live=None: _last_close(t))
    monkeypatch.setattr(invest, "_latest_date", lambda t: "2026-05-22")
    from app.services import auth
    auth.register("risk_u", "Risk-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    wallet.credit_once("risk_u", 10_000_000, "attendance", memo="테스트 충전")
    invest.buy("risk_u", "005930", 10)
    invest.buy("risk_u", "000660", 2)
    r = stress.invest_risk("risk_u")
    assert r["ok"] and r["stats_ok"] and r["days"] == 80 and r["beta_complete"]
    b = {i["ticker"]: i["beta"] for i in r["items"]}
    assert abs(b["005930"] - 1.5) < 0.15 and abs(b["000660"] - 0.5) < 0.15     # 베타 추정
    assert 0 < r["var_95_krw"] <= r["es_975_krw"]                              # ES97.5 ≥ VaR95
    s10 = next(s for s in r["scenarios"] if s["market_shock"] == -0.10)
    expect = sum(i["value"] * i["beta"] * -0.10 for i in r["items"])
    assert abs(s10["loss"] - expect) / abs(expect) < 0.01 and s10["loss"] < 0
    crisis = next(s for s in r["scenarios"] if s["market_shock"] == -0.55)
    assert crisis["loss_pct"] >= -1.0                                          # 종목 손실 −100% 한도
    assert r["historical"][0]["loss"] < 0 and set(r["concentration"]) >= {"hhi", "effective_n", "limit_breach"}


def test_invest_risk_api_requires_login():
    assert client.get("/invest/risk").status_code == 401
