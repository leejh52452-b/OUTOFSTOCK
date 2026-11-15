"""모델 리스크 — VaR 백테스트(Kupiec POF) + 집중위험(HHI). 은행 리스크관리 심화."""
import datetime as dt

import numpy as np
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import risk, portfolio


def _seed_series(ticker, closes, start="2026-06-01"):
    d0 = dt.date.fromisoformat(start)
    for i, c in enumerate(closes):
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
                   "VALUES(:t, :d, :c, 1000, 0)",
                   {"t": ticker, "d": (d0 + dt.timedelta(days=i)).isoformat(), "c": round(float(c), 4)})


# ── Kupiec POF 통계량 ──
def test_kupiec_zero_when_observed_matches_expected():
    assert abs(risk._kupiec_pof(100, 5, 0.05)) < 1e-9         # 5/100 = 기대 5% → LR 0


def test_kupiec_rejects_when_too_many_breaches():
    lr = risk._kupiec_pof(100, 20, 0.05)                      # 20% ≫ 5%
    assert lr > risk.CHI2_95_1DF                              # 95%에서 모델 기각


# ── VaR 백테스트 ──
def test_var_backtest_structure_and_counts():
    px = 100 * np.cumprod(1 + np.random.default_rng(0).normal(0, 0.02, 60))
    _seed_series("005930", px)
    r = risk.var_backtest("005930", 0.95)
    assert r["ok"] and r["observations"] == 59
    assert 0 <= r["breaches"] <= 59
    assert r["expected_breaches"] == round(0.05 * 59, 2)
    assert isinstance(r["reject_model_at_95"], bool)


def test_var_backtest_needs_enough_data():
    _seed_series("005930", [100, 101, 102])
    assert risk.var_backtest("005930")["ok"] is False


def test_var_backtest_api():
    px = 100 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.02, 60))
    _seed_series("005930", px)
    r = TestClient(app).get("/stocks/005930/var-backtest")
    assert r.status_code == 200 and r.json()["ok"]


# ── 집중위험(HHI) ──
def test_concentration_equal_weights_fully_diversified():
    c = portfolio.concentration([1, 1, 1, 1])
    assert c["hhi"] == 0.25 and c["effective_n"] == 4.0
    assert c["max_weight"] == 0.25 and c["limit_breach"] is False


def test_concentration_flags_single_name_over_limit():
    c = portfolio.concentration([9, 1])                       # 90% / 10%
    assert c["max_weight"] == 0.9 and c["limit_breach"] is True
    assert c["effective_n"] < 1.5                             # 사실상 1종목 집중


def test_concentration_included_in_portfolio_analyze():
    _seed_series("005930", [100, 110, 104, 118, 109, 121, 115])
    _seed_series("000660", [100, 108, 103, 115, 110, 119, 114])
    a = portfolio.analyze(["005930", "000660"])
    assert a["ok"] and "concentration" in a
    assert set(a["concentration"]) == {"hhi", "effective_n", "max_weight", "limit_breach"}


# ── VaR 백테스트 고도화: 롤링 아웃오브샘플 · 바젤 신호등 · Christoffersen · ES ──
def test_traffic_light_reproduces_basel_250_day_table():
    tl = [risk.traffic_light(250, x, 0.01) for x in (0, 4, 5, 9, 10)]
    assert [t["zone"] for t in tl] == ["green", "green", "yellow", "yellow", "red"]
    assert tl[0]["green_max"] == 4 and tl[0]["yellow_max"] == 9                # BCBS(1996) 0~4 / 5~9 / 10+


def test_rolling_oos_catches_regime_shift_that_in_sample_hides():
    rng = np.random.default_rng(7)
    calm, storm = rng.normal(0, 0.008, 330), rng.normal(0, 0.035, 60)          # 잠잠하다가 변동성 4배
    px = 100 * np.cumprod(1 + np.concatenate([calm, storm]))
    _seed_series("005930", px, start="2025-01-01")
    r = risk.var_backtest("005930", 0.99)
    assert r["ok"] and r["method"] == "rolling_oos" and r["window"] == 194      # 389 수익률 → 창 = 389 // 2
    assert r["traffic_light"]["zone"] == "red" and r["reject_model_at_95"]     # 과거 창으론 폭풍을 못 따라감
    assert 0 <= r["kupiec_pvalue"] < 0.05 and r["recent_breaches"]
    assert r["christoffersen"]["clustered_breaches"] >= 1                       # 초과가 몰려서 발생


def test_christoffersen_and_pvalues_edge_cases():
    assert risk._christoffersen_ind([0] * 50) == (0.0, 0)                     # 초과 없음 → 0
    lr, n11 = risk._christoffersen_ind([0] * 40 + [1] * 5 + [0] * 40)          # 연속 5회 = 군집
    assert lr > risk.CHI2_95_1DF and n11 == 4
    assert abs(risk._chi2_sf(risk.CHI2_95_1DF, 1) - 0.05) < 1e-3
    assert abs(risk._chi2_sf(risk.CHI2_95_2DF, 2) - 0.05) < 1e-3


def test_expected_shortfall_is_beyond_var():
    r = np.random.default_rng(3).normal(0, 0.02, 2000)
    es, var975 = risk.expected_shortfall(r, 0.975), risk.historical_var(r, 0.975)
    assert es > var975 > 0                                                     # 꼬리 '평균' ≥ 꼬리 '경계'
    px = 100 * np.cumprod(1 + r[:120])
    _seed_series("005930", px)
    prof = risk.risk_profile("005930")
    assert prof["es_975"] >= prof["var_95"]
    a = portfolio.analyze(["005930"])
    assert a["ok"] and a["portfolio_es_975"] is not None
