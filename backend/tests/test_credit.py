"""시장 기반 신용위험 — Merton 부도거리(DD)·부도확률(PD). 교과서 예제 재현 · 단조성 · 시점 정합 · API."""
import datetime as dt

import numpy as np
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import credit

client = TestClient(app)


def test_reproduces_hull_textbook_example():
    """Hull 『Options, Futures, and Other Derivatives』 — E=3, σE=80%, 부채 10, r=5%, T=1
    → V≈12.40, σV≈21.23%, d2≈1.1408, 위험중립 PD≈12.7%."""
    m = credit.merton(3.0, 0.80, 10.0, r=0.05)
    assert m["converged"]
    assert abs(m["V"] - 12.40) < 0.01 and abs(m["sigma_v"] - 0.2123) < 0.0005
    assert abs(m["dd"] - 1.1408) < 0.001 and abs(m["pd"] - 0.127) < 0.001


def test_monotonic_in_leverage_and_volatility():
    base = credit.merton(100, 0.30, 50, r=0.03)
    more_debt = credit.merton(100, 0.30, 150, r=0.03)
    more_vol = credit.merton(100, 0.60, 50, r=0.03)
    assert more_debt["dd"] < base["dd"] and more_vol["dd"] < base["dd"]       # 부채↑·변동성↑ → 부도거리↓
    assert base["pd"] < more_debt["pd"] and base["pd"] < more_vol["pd"]
    assert credit.merton(0, 0.3, 50) is None and credit.merton(100, 0.3, 0) is None
    assert credit.pd_band(0.0005) == "매우 낮음" and credit.pd_band(0.3) == "매우 높음"


def _seed(ticker, n, vol, mcap=5e12, start="2025-09-01", seed=0):
    rng = np.random.default_rng(seed)
    px = 50000 * np.cumprod(1 + rng.normal(0, vol / np.sqrt(252), n))
    d0 = dt.date.fromisoformat(start)
    for i, c in enumerate(px):
        db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
                   "VALUES(:t,:d,:c,1000,0,:m)", {"t": ticker, "d": (d0 + dt.timedelta(days=i)).isoformat(),
                                                   "c": float(c), "m": mcap * float(c) / 50000})


FIN = {"total_liabilities": 4e12, "current_liabilities": 2e12}


def test_merton_for_uses_kmv_default_point_and_point_in_time():
    _seed("005930", 300, 0.30)
    r = credit.merton_for("005930", FIN, "삼성전자")
    assert r["ok"] and r["inputs"]["default_point"] == 3e12                    # 유동 2조 + ½×비유동 2조
    assert r["inputs"]["days"] == 250 and 0.2 < r["inputs"]["equity_vol"] < 0.4
    assert r["dd"] > 2 and r["pd"] < 0.01 and r["pd_band"] in ("매우 낮음", "낮음")
    early = credit.merton_for("005930", FIN, "삼성전자", as_of="2025-11-15")
    assert early["ok"] and early["inputs"]["as_of"] <= "2025-11-15"            # 기준일 이후 시세 미사용
    assert credit.merton_for("105560", FIN, "KB금융")["ok"] is False           # 금융업 제외
    assert credit.merton_for("005930", {}, "삼성전자")["ok"] is False           # 부채 항목 없음


def test_short_history_and_api_payload():
    _seed("000660", 30, 0.4)
    assert "부족" in credit.merton_for("000660", FIN, "SK하이닉스")["reason"]
    _seed("005930", 300, 0.30)
    db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,revenue,operating_profit,net_income,"
               "total_equity,debt_ratio,disclosed_at,total_assets,current_assets,current_liabilities,"
               "total_liabilities,retained_earnings) VALUES('005930',2025,'11011',3e14,4e13,3e13,3e14,30,"
               "'2026-03-10',4e14,2e14,2e12,4e12,2.5e14)")
    s = client.get("/stocks/005930/soundness").json()
    assert s["ok"] and s["merton"]["ok"] and s["distress"]["z"] is not None   # 회계 기반 + 시장 기반 함께
