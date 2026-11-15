"""5번 수익률 예측 '검증 체계' — 5거래일 뒤 수익률을 맞힐 수 있는가가 아니라, '맞힌다고 착각하지 않는 법'.

목표: 종목의 다음 5거래일 수익률이 그날 전 종목 중앙값보다 높을까(시장 전체 등락을 뺀 상대 성과, 0/1).
비교하는 검증 방식
  A 무작위 K-겹(섞기)      — 흔한 실수. 5일 목표가 날마다 4일씩 겹치고(라벨 자기상관), 이웃 날짜의 거의 같은 피처가
                             학습·시험에 나뉘어 들어가 '외운 것'이 성능처럼 보인다(누수).
  B 시간순 전진(walk-forward) + 퍼지(purge) — 시험 구간 직전 5거래일의 학습 표본은 목표 기간이 시험과 겹치므로 버린다.
  누수 크기 = A 의 AUC − B 의 AUC.
기준선: 무작위(0.5) · 모멘텀(20일 수익률) · 단기 반전(−5일 수익률). 모형이 기준선을 못 넘으면 '예측력 없음'이 결론이다.
지표: AUC · 일별 순위상관(IC) 평균과 t값 · 상위/하위 20% 다음 5일 수익률 차이(거래비용 0.25% 차감 전·후).
한계(문서에 함께 적는다): 생존 편향(상장폐지 종목 포함 여부), 거래비용·슬리피지, 체제 변화, 다중 검정(여러 번 돌려 좋은 것만 고르면 우연).
"""
from __future__ import annotations

import logging
import math

import numpy as np

from app.models import common

log = logging.getLogger(__name__)
VERSION = "ret5-1.0"
H = 5
COST_BPS = 25                       # 왕복 거래비용(0.25%) 가정
FEATURES = {"ret_1": "1일 수익률(단기 반전)", "ret_5": "5일 수익률", "ret_20": "20일 수익률", "ret_60": "60일 수익률",
            "vol_20": "20일 변동성", "vol_ratio": "거래량 / 20일 평균", "dist_high": "52주 고가 대비",
            "mkt_ret_5": "시장 5일 수익률", "size": "시가총액(log)"}


# ── 패널 만들기 ─────────────────────────────────────────────
def features(px):
    """px: 열 = [ticker, trade_date, close, volume, mktcap] (정렬 무관) → 피처·목표가 붙은 패널."""
    px = px.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
    g = px.groupby("ticker", group_keys=False)
    c = px["close"].astype(float)
    px["ret_1"] = g["close"].pct_change(1)
    px["ret_5"] = g["close"].pct_change(5)
    px["ret_20"] = g["close"].pct_change(20)
    px["ret_60"] = g["close"].pct_change(60)
    px["vol_20"] = g["ret_1"].transform(lambda s: s.rolling(20).std())
    px["vol_ratio"] = px["volume"] / g["volume"].transform(lambda s: s.shift(1).rolling(20).mean())
    px["dist_high"] = c / g["close"].transform(lambda s: s.rolling(250, min_periods=60).max()) - 1
    px["size"] = np.log(px["mktcap"].astype(float).where(px["mktcap"] > 0))
    px["fwd_5"] = g["close"].shift(-H) / c - 1                     # 목표(미래) — 피처 계산에는 쓰지 않는다
    mkt = px.groupby("trade_date")["ret_5"].transform("median")
    px["mkt_ret_5"] = mkt
    med = px.groupby("trade_date")["fwd_5"].transform("median")
    px["y"] = (px["fwd_5"] > med).astype(float).where(px["fwd_5"].notna())
    px = px.replace([np.inf, -np.inf], np.nan)
    return px.dropna(subset=["y", "ret_60", "vol_20"]).reset_index(drop=True)


def load_panel(top_n=300, years=5):
    """분석 계층에서 거래대금 상위 top_n 종목 × 최근 years 년 — VM 메모리 안에 들어오는 크기."""
    import pandas as pd
    from app import db
    eng = db.get_engine()
    # 수정주가(V007): 종가 × adj, 거래량 ÷ adj — 액면분할 날이 '−80%'로 들어가면 5일·20일 수익률 피처와
    # 목표(다음 5일 수익률)가 함께 틀리고, 그 하루가 '예측할 수 없는 폭락'으로 성능을 흔든다.
    px = pd.read_sql(
        "WITH last AS (SELECT analytics.latest_trade_date() AS d), liq AS ("
        " SELECT ticker FROM analytics.price_history WHERE trade_date > (SELECT d FROM last) - 120"
        " GROUP BY ticker ORDER BY avg(value) DESC LIMIT %(n)s) "
        "SELECT a.ticker, a.trade_date, a.close * a.adj AS close, a.volume / a.adj AS volume, a.mktcap "
        "FROM analytics.adj_prices((SELECT d FROM last) - %(days)s, (SELECT d FROM last)) a "
        "JOIN liq ON liq.ticker = a.ticker WHERE NOT a.halted",
        eng, params={"n": int(top_n), "days": int(years * 365)})
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    return features(px)


# ── 평가 ──────────────────────────────────────────────────
def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=200, min_samples_leaf=50,
                                          random_state=7)


def _daily_stats(df, score):
    """일별 IC(순위상관)와 상·하위 20% 다음 5일 수익률 차이."""
    d = df.assign(score=score)
    ics, spreads = [], []
    for _, g in d.groupby("trade_date"):
        if len(g) < 20:
            continue
        ic = common.spearman(g["score"], g["fwd_5"])
        if ic is not None:
            ics.append(ic)
        q = g["score"].rank(pct=True)
        spreads.append(g.loc[q > 0.8, "fwd_5"].mean() - g.loc[q <= 0.2, "fwd_5"].mean())
    ic_mean = float(np.mean(ics)) if ics else None
    ic_t = float(ic_mean / (np.std(ics, ddof=1) / math.sqrt(len(ics)))) if len(ics) > 2 and np.std(ics) > 0 else None
    sp = float(np.nanmean(spreads)) if spreads else None
    # 5일마다 갈아탄다고 보면 매 기간 왕복 비용 1번
    return {"ic_mean": common._r(ic_mean), "ic_t": common._r(ic_t, 2), "days": len(ics),
            "spread_5d_bps": None if sp is None else round(sp * 1e4, 1),
            "spread_after_cost_bps": None if sp is None else round(sp * 1e4 - COST_BPS, 1)}


def _eval(df, score):
    return {"auc": common._r(common.auc(df["y"].values, score)), **_daily_stats(df, score)}


def random_kfold(df, k=5, seed=7):
    """A: 행을 무작위로 섞어 K 겹 — 누수가 있는 방식(비교용)."""
    cols = list(FEATURES)
    idx = np.random.default_rng(seed).permutation(len(df))
    scores = np.full(len(df), np.nan)
    for f in range(k):
        te = idx[f::k]
        tr = np.setdiff1d(idx, te)
        m = _model().fit(df.iloc[tr][cols], df.iloc[tr]["y"])
        scores[te] = m.predict_proba(df.iloc[te][cols])[:, 1]
    return _eval(df, scores)


def walk_forward(df, n_splits=5, purge=H, min_train_days=250):
    """B: 시간순 확장 창. 시험 구간 직전 purge 거래일의 학습 표본은 목표 기간이 겹쳐 제외."""
    cols = list(FEATURES)
    dates = np.array(sorted(df["trade_date"].unique()))
    if len(dates) < min_train_days + n_splits * 20:
        raise ValueError(f"기간이 짧다({len(dates)}거래일) — 최소 {min_train_days + n_splits * 20}거래일")
    test_dates = np.array_split(dates[min_train_days:], n_splits)
    parts, scores = [], []
    for block in test_dates:
        start = block[0]
        cut = dates[max(0, np.searchsorted(dates, start) - purge)]       # 이 날짜 '미만'만 학습
        tr = df[df["trade_date"] < cut]
        te = df[df["trade_date"].isin(block)]
        m = _model().fit(tr[cols], tr["y"])
        parts.append(te)
        scores.append(m.predict_proba(te[cols])[:, 1])
    out = df.loc[np.concatenate([p.index.values for p in parts])]
    return _eval(out, np.concatenate(scores)), out


def validate(df, data_source="real", record=True):
    a = random_kfold(df)
    b, oos = walk_forward(df)
    base = {"momentum_20d": _eval(oos, oos["ret_20"].values), "reversal_5d": _eval(oos, -oos["ret_5"].values),
            "random": _eval(oos, np.random.default_rng(1).random(len(oos)))}
    metrics = {"rows": int(len(df)), "tickers": int(df["ticker"].nunique()),
               "period": [str(df["trade_date"].min())[:10], str(df["trade_date"].max())[:10]],
               "random_kfold": a, "walk_forward": b, "baselines": base,
               "leakage_auc_gap": None if a["auc"] is None or b["auc"] is None else round(a["auc"] - b["auc"], 4),
               "cost_bps": COST_BPS, "horizon_days": H}
    beats = b["auc"] is not None and all(b["auc"] > (v["auc"] or 0) + 0.005 for v in base.values())
    metrics["verdict"] = ("시간순 검증에서 기준선을 넘음 — 거래비용 뒤 수익·다른 기간 재현 확인 필요" if beats else
                          "시간순 검증에서 기준선을 의미 있게 넘지 못함 — '예측력 없음'이 정직한 결론")
    run_id = common.record_run("return_5d", VERSION, data_source, {"features": list(FEATURES), "horizon": H,
                                                                     "purge": H, "model": "HistGB(depth 4)"},
                               metrics) if record else None
    return {"run_id": run_id, "metrics": metrics}


# ── 합성 데이터(검증 체계 자체를 시험) ───────────────────────────
def synthetic_prices(n=120, days=900, signal=0.0, seed=3):
    """시장 요인 + 개별 잡음의 무작위 보행. signal>0 이면 20일 모멘텀이 다음 날 수익률을 약하게 설명한다.
    signal=0 이면 '예측할 것이 없는' 시장 — 누수 없는 검증은 AUC≈0.5 가 나와야 한다."""
    import pandas as pd
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-04", periods=days)
    mkt = rng.normal(0.0003, 0.01, days)
    rows = []
    for i in range(n):
        p, beta = 10000.0, rng.uniform(0.6, 1.4)
        size = float(np.exp(rng.normal(26, 1)))
        hist = []
        for t in range(days):
            mom = (hist[-1] / hist[-21] - 1) if len(hist) > 21 else 0.0
            r = float(np.clip(beta * mkt[t] + rng.normal(0, 0.02) + signal * np.clip(mom, -0.5, 0.5), -0.3, 0.3))
            p *= 1 + r
            hist.append(p)
            rows.append((f"Z{i:04d}", dates[t], p, float(rng.lognormal(10, 0.4)), size * p / 10000))
    return pd.DataFrame(rows, columns=["ticker", "trade_date", "close", "volume", "mktcap"])


def run(source="real"):
    if source == "synthetic":
        return validate(features(synthetic_prices()), data_source="synthetic")
    return validate(load_panel(), data_source="real")
