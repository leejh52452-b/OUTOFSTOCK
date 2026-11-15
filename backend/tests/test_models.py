"""4번 신용위험 모형·5번 수익률 예측 검증 — 지표 구현·시점 정합(PIT)·라벨 창·분할·누수 검출을 합성 데이터로 확인.
(실데이터 성능 숫자는 VM 에서 run.py credit-train / return-validate 로 — 여기서는 '방법이 맞는지'를 본다)"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app import db
from app.models import common, credit_risk as cr, return_model as rm



# ── 지표 ──────────────────────────────────────────────────
def test_metrics_match_sklearn():
    from sklearn.metrics import average_precision_score, roc_auc_score
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 500)
    s = y * 0.3 + rng.normal(0, 1, 500)
    s[:20] = 0.5                                                   # 동점 포함
    assert common.auc(y, s) == pytest.approx(roc_auc_score(y, s), abs=1e-9)
    assert common.pr_auc(y, s) == pytest.approx(average_precision_score(y, s), abs=1e-9)
    assert 0 < common.ks(y, s) < 1 and common.ks([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert common.top_capture([0, 0, 0, 1] * 25, list(range(100)), 0.1) == pytest.approx(3 / 25)   # 상위 10개(90~99) 중 91·95·99
    cal = common.calibration_table([0, 1] * 50, np.linspace(0, 1, 100))
    assert len(cal) == 10 and sum(c["n"] for c in cal) == 100


# ── 신용위험: 데이터 구성(PIT·라벨 창) ───────────────────────────
def _frames():
    fin = pd.DataFrame([
        {"ticker": "A", "fiscal_year": y, "fs_basis": "CFS", "revenue": 100, "gross_profit": 30, "operating_profit": 10,
         "net_income": 5, "total_assets": 200, "total_liabilities": 100, "total_equity": 100, "capital_stock": 20,
         "current_assets": 80, "current_liabilities": 50, "retained_earnings": 60, "disclosed_at": date(y + 1, 3, 20),
         "rcept_no": None, "source": "t"} for y in (2020, 2021, 2022)])
    master = pd.DataFrame([{"ticker": "A", "name": "가", "sector": "기계", "ksic_code": "29", "sect_tp": None},
                           {"ticker": "B", "name": "은행", "sector": "은행", "ksic_code": "641", "sect_tp": None}])
    fin_b = fin.assign(ticker="B")
    market = pd.DataFrame([{"ticker": "A", "month_end": pd.Timestamp("2022-02-28"), "mktcap": 150, "ret_1y": 0.1,
                            "vol_1y": 0.3, "dd_1y": -0.1},
                           {"ticker": "A", "month_end": pd.Timestamp("2022-03-31"), "mktcap": 999999, "ret_1y": 9.9,
                            "vol_1y": 9.9, "dd_1y": 0.0}])           # 관측일(3/20) 이후 값 — 쓰면 안 됨
    return pd.concat([fin, fin_b]), master, market


def test_dataset_is_point_in_time_and_label_window_is_exact():
    fin, master, market = _frames()
    obs = date(2022, 3, 20)                                         # FY2021 공시일
    events = pd.DataFrame([{"ticker": "A", "rcept_dt": obs + timedelta(days=365), "action": "관리종목지정"}])
    df = cr.build_dataset(fin, master, events, market, today=date(2026, 1, 1))
    assert set(df["ticker"]) == {"A"}                               # 금융업(은행) 제외
    r = df[df["fiscal_year"] == 2021].iloc[0]
    assert r["log_mktcap"] == pytest.approx(np.log(150))            # 관측일 '직전' 월말만(3/31 값 미사용)
    assert r["y"] == 1                                              # 관측일+365일 사건은 포함
    events2 = events.assign(rcept_dt=obs + timedelta(days=366))
    assert cr.build_dataset(fin, master, events2, market, today=date(2026, 1, 1)).query("fiscal_year == 2021")["y"].iloc[0] == 0
    events3 = events.assign(rcept_dt=obs)                           # 관측일 '당일' 사건 = 이미 알려진 부실 → 제외
    assert 2021 not in set(cr.build_dataset(fin, master, events3, market, today=date(2026, 1, 1))["fiscal_year"])


def test_next_year_capital_impairment_is_a_label_not_a_feature():
    fin, master, market = _frames()
    fin.loc[(fin.ticker == "A") & (fin.fiscal_year == 2022), "total_equity"] = 5      # FY2022 자본 5 < 자본금 20 × 0.5
    df = cr.build_dataset(fin, master, pd.DataFrame(columns=["ticker", "rcept_dt", "action"]), market,
                          today=date(2026, 1, 1))
    assert df.query("fiscal_year == 2021")["y"].iloc[0] == 1        # 다음 해 잠식 → 전년 관측의 라벨
    assert df.query("fiscal_year == 2021")["impair"].iloc[0] == pytest.approx(100 / 20)   # 피처는 그해 값


def test_time_split_and_synthetic_training_beats_chance():
    df = cr.build_dataset(*cr.synthetic_frames(n_firms=300), today=date(2026, 10, 1))
    res = cr.train(df, data_source="synthetic")
    m = res["metrics"]
    assert max(m["split"]["train_years"]) < m["split"]["valid_years"][0] < min(m["split"]["test_years"])
    assert m["logit"]["auc"] > 0.7 and m["gbm"]["auc"] > 0.7 and m["altman_z"]["coverage"] > 0.9
    pred = sum(c["pred"] * c["n"] for c in m["logit"]["calibration"]) / sum(c["n"] for c in m["logit"]["calibration"])
    assert abs(pred - m["logit"]["base_rate"]) < 0.03                # 보정 후 평균 PD ≈ 실제 부실률
    assert db.query("SELECT data_source FROM analytics.model_run WHERE run_id = :r", {"r": res["run_id"]})[0]["data_source"] == "synthetic"
    api = common.report("credit_risk")
    assert api["available"] and api["synthetic"] and api["metrics"]["logit"]["auc"] == m["logit"]["auc"]
    # 부호 검사: 남은 로지스틱 계수는 (잡음 수준이 아니면) 업무상 기대 방향과 같고, 뺀 변수는 반대 부호였다
    for c in m["coefficients"]:
        assert abs(c["coef"]) < cr.SIGN_MIN or (c["coef"] > 0) == (cr.FEATURES[c["feature"]][1] > 0), c
    for d in m["sign_dropped"]:
        assert (d["coef"] > 0) != (cr.FEATURES[d["feature"]][1] > 0) and d["feature"] not in res["lcols"]
    assert set(res["lcols"]) | {d["feature"] for d in m["sign_dropped"]} == set(cr.FEATURES)


def test_sign_check_drops_the_flipped_variable_and_refits():
    import numpy as np

    class Fake:                                                     # 계수만 흉내 내는 파이프라인
        def __init__(self, cs):
            w = {"current_ratio": 0.4, "roa": -0.3, "debt_to_assets": 0.2, "log_assets": 0.02}
            self.coef_ = np.array([[w.get(c, -0.1) for c in cs]])

        def __getitem__(self, i):
            return self
    kept, dropped, _ = cr._sign_consistent(lambda cs: Fake(cs), ["current_ratio", "roa", "debt_to_assets", "log_assets"])
    assert dropped == [("current_ratio", 0.4)]                      # 유동비율 +(기대 −) → 제외
    assert kept == ["roa", "debt_to_assets", "log_assets"]           # 자산 규모 +0.02 는 잡음 수준이라 남김


def test_score_latest_writes_grade_and_factors():
    df = cr.build_dataset(*cr.synthetic_frames(n_firms=200), today=date(2026, 10, 1))
    res = cr.train(df, data_source="synthetic", record=False)
    n = cr.score_latest(res, df)
    assert n == df["ticker"].nunique()
    t = df["ticker"].iloc[0]
    r = common.credit_score(t)
    assert r["available"] and r["grade"] in "ABCDEF" and 0 < r["pd"] < 1 and "disclaimer" in r
    assert common.credit_score("NOPE")["available"] is False


def test_too_few_events_or_years_is_explained():
    fin, master, market = _frames()
    df = cr.build_dataset(fin, master, pd.DataFrame(columns=["ticker", "rcept_dt", "action"]), market,
                          today=date(2026, 1, 1))
    with pytest.raises(ValueError, match="부실 징후 사례"):
        cr.train(df, record=False)


# ── 수익률 예측 검증 ─────────────────────────────────────────
def test_features_never_look_ahead():
    px = rm.synthetic_prices(n=3, days=200, seed=5)
    a = rm.features(px.copy())
    px2 = px.copy()
    last = px2["trade_date"].max()
    px2.loc[px2["trade_date"] > last - pd.Timedelta(days=10), "close"] *= 3   # 미래 가격만 바꾸면
    b = rm.features(px2)
    cut = last - pd.Timedelta(days=40)
    cols = list(rm.FEATURES)
    pd.testing.assert_frame_equal(a[a.trade_date < cut][cols].reset_index(drop=True),
                                  b[b.trade_date < cut][cols].reset_index(drop=True))    # 과거 피처는 그대로


def test_random_split_leaks_on_pure_noise_but_walk_forward_does_not():
    df = rm.features(rm.synthetic_prices(n=60, days=700, signal=0.0, seed=8))
    res = rm.validate(df, data_source="synthetic")
    m = res["metrics"]
    assert 0.46 < m["walk_forward"]["auc"] < 0.54                  # 예측할 게 없는 시장 → 우연 수준
    assert m["leakage_auc_gap"] > 0.01                              # 무작위 분할은 부풀려진다
    assert m["random_kfold"]["ic_t"] > 3 > abs(m["walk_forward"]["ic_t"] or 0)
    assert "예측력 없음" in m["verdict"]
    assert common.report("return_5d")["metrics"]["leakage_auc_gap"] == m["leakage_auc_gap"]


def test_walk_forward_detects_real_signal():
    df = rm.features(rm.synthetic_prices(n=60, days=700, signal=0.02, seed=9))
    m = rm.validate(df, data_source="synthetic", record=False)["metrics"]
    assert m["walk_forward"]["auc"] > 0.54 and m["baselines"]["momentum_20d"]["auc"] > 0.54


def test_merton_uses_point_in_time_treasury_rate():
    """Merton 무위험이자율 = 그 시점까지 알 수 있던 국고채 3년(실데이터). 표본·미래 값은 쓰지 않고, 없으면 설정값."""
    from datetime import date
    from sqlalchemy import text
    from app import db
    from app.config import settings
    from app.services import credit
    assert credit.risk_free_rate(date(2025, 6, 2)) == (settings.RISK_FREE_RATE, "설정값(RISK_FREE_RATE)")
    with db.transaction() as c:
        for d, v, src in (("2025-05-30", 2.41, "ecos"), ("2025-06-30", 2.55, "ecos"), ("2025-06-02", 9.99, "gen")):
            c.execute(text("INSERT INTO analytics.indicator_daily VALUES('KTB3Y', :d, :v, :s)"), {"d": d, "v": v, "s": src})
    credit.clear_rf_cache()
    r, src = credit.risk_free_rate(date(2025, 6, 2))
    assert abs(r - 0.0241) < 1e-9 and "2025-05-30" in src            # 6/2 기준: 6/30 값(미래)·표본 9.99 는 안 씀
    assert abs(credit.risk_free_rate(date(2025, 7, 1))[0] - 0.0255) < 1e-9
    assert credit.risk_free_rate(date(2025, 9, 1))[0] == settings.RISK_FREE_RATE   # 두 달 비면 낡은 값 대신 설정값
    assert credit.risk_free_rate(date(2024, 1, 1))[0] == settings.RISK_FREE_RATE   # 시계열 이전
