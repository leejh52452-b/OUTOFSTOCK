"""금융 리스크 지표 — 변동성·VaR·최대낙폭(은행 리스크관리 지표) 검증."""
import numpy as np
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import risk


def _seed_series(ticker, closes, start_day=1):
    for i, c in enumerate(closes):
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
                   "VALUES(:t, :d, :c, 1000, 0)",
                   {"t": ticker, "d": f"2026-09-{start_day + i:02d}", "c": c})


def test_basic_metrics():
    _seed_series("005930", [100, 110, 99, 121])
    r = risk.risk_profile("005930")
    assert r["ok"] and r["days"] == 4
    assert r["cumulative_return"] == 0.21      # 121/100 - 1
    assert r["max_drawdown"] == -0.1           # 110 → 99
    assert r["annual_volatility"] > 0
    assert r["var_95"] >= 0


def test_flat_series_has_zero_risk():
    _seed_series("005930", [100, 100, 100, 100])
    r = risk.risk_profile("005930")
    assert r["annual_volatility"] == 0.0
    assert r["max_drawdown"] == 0.0
    assert r["cumulative_return"] == 0.0


def test_more_volatile_has_higher_volatility():
    _seed_series("005930", [100, 101, 102, 103, 104])          # 완만
    _seed_series("000660", [100, 130, 80, 140, 70])            # 급등락
    smooth = risk.risk_profile("005930")["annual_volatility"]
    jagged = risk.risk_profile("000660")["annual_volatility"]
    assert jagged > smooth


def test_var_positive_for_losing_series():
    losses = np.array([-0.05, -0.03, -0.02, 0.01, 0.02])
    v = risk.historical_var(losses, 0.95)
    assert v > 0                               # 하위 분위수가 손실 → 양수 VaR


def test_insufficient_data():
    _seed_series("005930", [100])
    assert risk.risk_profile("005930")["ok"] is False


def test_risk_api():
    _seed_series("005930", [100, 90, 120, 108])
    r = TestClient(app).get("/stocks/005930/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "annual_volatility" in body and "max_drawdown" in body
