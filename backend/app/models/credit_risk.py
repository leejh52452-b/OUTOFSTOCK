"""4번 기업 신용위험 모형 — DART 다년 재무 + 시장 피처 → 12개월 내 '부실 징후' 확률(PD).

라벨(부실 징후, 관측일 다음 365일 안에 하나라도)
  · 거래소 시장조치: 관리종목 지정, 상장폐지 사유 발생·실질심사, 정리매매
  · 주요사항보고: 부도 발생, 회생절차 개시 신청, 해산 사유, 채권은행 관리절차, 영업정지
  · 다음 사업연도 재무에서 자본잠식 50% 이상(자본총계 < 자본금 × 0.5)
관측(PIT, point-in-time): 사업연도 t 재무가 '공시된 날'(disclosed_at). 그날까지 알 수 있었던 값만 쓴다 —
  재무 = 그 공시, 시장 피처 = 그날 직전 월말, 라벨 = 그날 '이후'의 사건. 이미 부실 징후가 있던 기업은 제외.
분할: 시간순(사업연도) — 학습 ≤ 검증 연도 < 시험 연도. 무작위 분할은 같은 회사의 인접 연도가 양쪽에 들어가 낙관적이다.
모형: 로지스틱 회귀(해석: 표준화 계수·기업별 기여도) · 그래디언트 부스팅(비선형) — 둘 다 검증 연도로 확률 보정(Platt).
비교 기준: Altman Z''(회계) · Merton 부도확률(시장) — 같은 시험 표본에서 AUC·KS 를 나란히.
금융업(은행·보험·증권·카드·지주)은 재무 구조가 달라 제외. 교육용 — 실제 내부등급 모형은 장기 부도 데이터·전문가 판단을 쓴다.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import numpy as np

from app.models import common

log = logging.getLogger(__name__)
VERSION = "credit-1.0"
HORIZON_DAYS = 365
DISTRESS_ACTIONS = ("관리종목지정", "상장폐지사유", "정리매매", "부도", "회생절차", "해산", "채권은행관리", "영업정지")
FIN_SECTORS = ("금융", "은행", "카드·캐피탈", "금융지주·기타금융", "보험", "증권·금융서비스", "증권")
# 이름: (화면 설명, 위험 방향 — 값이 클수록 위험하면 +). 자기자본비율(=1−부채/자산)·Merton 부도거리(레버리지·변동성의
# 함수)는 다른 변수와 완전·강한 공선성이라 넣지 않는다(계수 부호가 뒤집혀 해석을 망친다) — Merton 은 비교 기준으로만.
FEATURES = {
    "roa": ("총자산이익률(ROA)", -1), "op_margin": ("영업이익률", -1), "debt_to_assets": ("부채/자산", +1),
    "current_ratio": ("유동비율", -1), "wc_ta": ("운전자본/자산", -1),
    "re_ta": ("이익잉여금/자산", -1), "ebit_ta": ("영업이익/자산", -1), "impair": ("자본/자본금(잠식 정도)", -1),
    "rev_growth": ("매출 증가율", -1), "loss_years": ("최근 3년 적자 연수", +1), "log_assets": ("자산 규모(log)", -1),
    "log_mktcap": ("시가총액(log)", -1), "ret_1y": ("1년 주가수익률", -1), "vol_1y": ("1년 주가변동성", +1),
    "dd_1y": ("1년 고점 대비 하락", -1), "pbr": ("PBR", -1),
}
GRADES = ((0.005, "A"), (0.01, "B"), (0.02, "C"), (0.05, "D"), (0.10, "E"))


def grade(pd):
    for cut, g in GRADES:
        if pd < cut:
            return g
    return "F"


def _div(a, b):
    try:
        if a is None or b is None or b == 0 or (isinstance(b, float) and math.isnan(b)):
            return np.nan
        return float(a) / float(b)
    except (TypeError, ValueError):
        return np.nan


# ── 데이터 ─────────────────────────────────────────────────
def load_frames():
    """분석 계층에서 재무·종목·사건·월말 시장 피처를 읽는다(pandas)."""
    import pandas as pd
    from app import db
    eng = db.get_engine()
    fin = pd.read_sql("SELECT * FROM analytics.fin_annual", eng)
    master = pd.read_sql("SELECT ticker, name, sector, ksic_code, sect_tp FROM analytics.security_master", eng)
    events = pd.read_sql("SELECT ticker, rcept_dt, action FROM analytics.disclosure WHERE ticker IS NOT NULL "
                         "AND action = ANY(%(a)s)", eng, params={"a": list(DISTRESS_ACTIONS)})
    market = pd.read_sql("SELECT ticker, month_end, mktcap, ret_1y, vol_1y, dd_1y FROM analytics.market_monthly", eng)
    return fin, master, events, market


def build_dataset(fin, master, events, market, today: date | None = None, horizon=HORIZON_DAYS):
    """관측 표(한 행 = 회사·사업연도, 공시일 기준) + 라벨 + 피처 + 비교 지표(Z''·Merton)."""
    import pandas as pd
    from app import clock
    from app.services import credit, soundness
    today = today or clock.now().date()                  # 서버 시간대와 무관하게 한국 날짜(app/clock.py)
    if fin.empty:
        return pd.DataFrame()
    f = fin.copy()
    f["fiscal_year"] = f["fiscal_year"].astype(int)
    f["obs_date"] = pd.to_datetime(f["disclosed_at"]).dt.date
    miss = f["obs_date"].isna()
    f.loc[miss, "obs_date"] = [date(int(y) + 1, 3, 31) for y in f.loc[miss, "fiscal_year"]]
    f = f.merge(master, on="ticker", how="left")
    f = f[~f["sector"].isin(FIN_SECTORS)]
    f = f[~f["ksic_code"].fillna("").str[:2].isin(["64", "65", "66"])]
    prev = fin[["ticker", "fiscal_year", "revenue", "net_income"]].copy()
    prev["fiscal_year"] = prev["fiscal_year"].astype(int) + 1
    f = f.merge(prev.rename(columns={"revenue": "rev_prev", "net_income": "ni_prev"}), on=["ticker", "fiscal_year"],
                how="left")
    # 최근 3년 적자 연수(t, t-1, t-2)
    ni = fin[["ticker", "fiscal_year", "net_income"]].copy()
    ni["fiscal_year"] = ni["fiscal_year"].astype(int)
    loss = {}
    for t, g in ni.groupby("ticker"):
        d = dict(zip(g["fiscal_year"], g["net_income"]))
        for y in d:
            loss[(t, y)] = sum(1 for k in (y, y - 1, y - 2) if d.get(k) is not None and d.get(k) < 0)
    f["loss_years"] = [loss.get((t, y), 0) for t, y in zip(f["ticker"], f["fiscal_year"])]
    # 다음 해 자본잠식(50% 이상) — 다음 해 공시가 관측일+horizon+90일 안에 나왔을 때만 라벨로
    nxt = fin[["ticker", "fiscal_year", "total_equity", "capital_stock", "disclosed_at"]].copy()
    nxt["fiscal_year"] = nxt["fiscal_year"].astype(int) - 1
    nxt = nxt.rename(columns={"total_equity": "te_next", "capital_stock": "cap_next", "disclosed_at": "disc_next"})
    f = f.merge(nxt, on=["ticker", "fiscal_year"], how="left")

    # 시장 피처: 관측일 직전 월말(60일 이내)
    f = f.sort_values("obs_date")
    if not market.empty:
        m = market.copy()
        m["month_end"] = pd.to_datetime(m["month_end"]).astype("datetime64[ns]")      # 해상도(s/us/ns)를 맞춘다
        f["_obs"] = pd.to_datetime(f["obs_date"]).astype("datetime64[ns]")
        f = pd.merge_asof(f.sort_values("_obs"), m.sort_values("month_end"), left_on="_obs", right_on="month_end",
                          by="ticker", direction="backward", tolerance=pd.Timedelta(days=62))
    for c in ("mktcap", "ret_1y", "vol_1y", "dd_1y"):
        if c not in f:
            f[c] = np.nan

    # 라벨: 관측일 다음 날 ~ horizon 일 안의 부실 징후 / 관측 전 1년 안에 이미 부실 징후가 있었으면 제외
    ev = events.copy()
    ev["rcept_dt"] = pd.to_datetime(ev["rcept_dt"]).dt.date
    by = {t: sorted(g["rcept_dt"]) for t, g in ev.groupby("ticker")}
    ys, prior, first_dates = [], [], []
    for t, od, te_n, cap_n, dn in zip(f["ticker"], f["obs_date"], f["te_next"], f["cap_next"], f["disc_next"]):
        ds = by.get(t, [])
        hit = [d for d in ds if od < d <= od + timedelta(days=horizon)]
        before = any(od - timedelta(days=365) < d <= od for d in ds)
        imp = (te_n is not None and cap_n is not None and not pd.isna(te_n) and not pd.isna(cap_n)
               and cap_n > 0 and te_n < 0.5 * cap_n and dn is not None and not pd.isna(dn)
               and pd.to_datetime(dn).date() <= od + timedelta(days=horizon + 90))
        ys.append(int(bool(hit) or bool(imp)))
        prior.append(before)
        first_dates.append(min(hit) if hit else None)
    f["y"] = ys
    f["event_date"] = first_dates
    f = f[~np.array(prior)]
    f = f[~f["sect_tp"].fillna("").str.startswith("관리종목")]
    f["label_complete"] = [od + timedelta(days=horizon) <= today for od in f["obs_date"]]

    # 피처
    ta, tl, te = f["total_assets"], f["total_liabilities"], f["total_equity"]
    f["roa"] = [_div(a, b) for a, b in zip(f["net_income"], ta)]
    f["op_margin"] = [_div(a, b) for a, b in zip(f["operating_profit"], f["revenue"])]
    f["debt_to_assets"] = [_div(a, b) for a, b in zip(tl, ta)]
    f["current_ratio"] = [_div(a, b) for a, b in zip(f["current_assets"], f["current_liabilities"])]
    f["wc_ta"] = [_div((a or 0) - (b or 0), c) if a is not None and b is not None else np.nan
                  for a, b, c in zip(f["current_assets"], f["current_liabilities"], ta)]
    f["re_ta"] = [_div(a, b) for a, b in zip(f["retained_earnings"], ta)]
    f["ebit_ta"] = [_div(a, b) for a, b in zip(f["operating_profit"], ta)]
    f["impair"] = [_div(a, b) for a, b in zip(te, f["capital_stock"])]
    f["rev_growth"] = [_div(a, b) - 1 if b and b > 0 else np.nan for a, b in zip(f["revenue"], f["rev_prev"])]
    f["log_assets"] = [math.log(a) if a and a > 0 else np.nan for a in ta]
    f["log_mktcap"] = [math.log(a) if a and a > 0 else np.nan for a in f["mktcap"]]
    f["pbr"] = [_div(a, b) if b and b > 0 else np.nan for a, b in zip(f["mktcap"], te)]

    # 비교 지표: Altman Z'' · Merton(같은 관측 시점 입력)
    zs, mpd, mdd = [], [], []
    for _, r in f.iterrows():
        rec = {k: (None if pd.isna(r.get(k)) else r.get(k)) for k in
               ("total_assets", "current_assets", "current_liabilities", "total_liabilities", "retained_earnings",
                "operating_profit", "total_equity")}
        z = soundness.altman_z(rec)
        zs.append(z["z"] if z.get("z") is not None else np.nan)
        F, _rule = credit.default_point(rec)
        m = credit.merton(r["mktcap"], r["vol_1y"], F, r=credit.risk_free_rate(r["obs_date"])[0]) \
            if F and not pd.isna(r["mktcap"]) and not pd.isna(r["vol_1y"]) else None      # 금리도 관측 시점 값(PIT)
        mpd.append(m["pd"] if m else np.nan)
        mdd.append(m["dd"] if m else np.nan)
    f["zscore"], f["merton_pd"], f["dd_merton"] = zs, mpd, mdd
    keep = ["ticker", "name", "sector", "fiscal_year", "obs_date", "y", "event_date", "label_complete"] + \
        list(FEATURES) + ["zscore", "merton_pd"]
    return f[keep].reset_index(drop=True)


# ── 학습·평가 ───────────────────────────────────────────────
def _split_years(years):
    ys = sorted(set(int(y) for y in years))
    if len(ys) < 4:
        raise ValueError(f"사업연도가 4개 이상 필요(현재 {len(ys)}개) — DART 다년 재무를 더 받으세요(run.py platform-history)")
    n_test = max(1, round(len(ys) * 0.25))
    test = ys[-n_test:]
    valid = [ys[-n_test - 1]]
    train = ys[:-n_test - 1]
    return train, valid, test


def _winsor(train, cols):
    lo = train[cols].quantile(0.01)
    hi = train[cols].quantile(0.99)
    return lo, hi


SIGN_MIN = 0.05            # 이보다 작은 계수는 잡음으로 보고 부호 검사에서 뺀다


def _sign_consistent(fit, cols):
    """로지스틱 계수 부호가 업무상 기대 방향(FEATURES[c][1])과 반대인 변수를 하나씩 뺀다(가장 크게 어긋난 것부터 다시 학습).
    공선성 때문에 '유동비율이 높을수록 위험'처럼 부호가 뒤집힌 변수가 남으면 심사 근거로 설명할 수 없다(스코어카드 관행).
    → (남긴 변수, [(뺀 변수, 그때 계수)], 마지막 모형)"""
    kept, dropped = list(cols), []
    model = fit(kept)
    while len(kept) > 1:
        coef = model[-1].coef_[0]
        bad = [(abs(w), c, float(w)) for c, w in zip(kept, coef) if abs(w) >= SIGN_MIN and np.sign(w) != FEATURES[c][1]]
        if not bad:
            break
        _, worst, w = max(bad)
        kept.remove(worst)
        dropped.append((worst, round(w, 4)))
        model = fit(kept)
    return kept, dropped, model


def train(df, data_source="real", record=True):
    """시간순 분할 학습·보정·평가 → {metrics, models, ...}. df = build_dataset 결과(라벨 완료 행만 사용)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    d = df[df["label_complete"]].copy()
    if d["y"].sum() < 10:
        raise ValueError(f"부실 징후 사례가 너무 적다({int(d['y'].sum())}건) — 시장조치·주요사항 공시 이력을 더 받으세요")
    tr_y, va_y, te_y = _split_years(d["fiscal_year"])
    cols = list(FEATURES)
    tr, va, te = (d[d["fiscal_year"].isin(s)] for s in (tr_y, va_y, te_y))
    lo, hi = _winsor(tr, cols)

    def X(x):
        return x[cols].clip(lo, hi, axis=1)

    def fit_logit(cs):
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                             LogisticRegression(C=0.5, max_iter=2000)).fit(X(tr)[cs], tr["y"])
    lcols, sign_dropped, logit = _sign_consistent(fit_logit, cols)      # 로지스틱은 부호가 상식과 맞는 변수만
    XM = {"logit": lambda x: X(x)[lcols], "gbm": X}                     # 부스팅은 전체(비선형 — 해석은 순열 중요도)
    gbm = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, l2_regularization=1.0,
                                         min_samples_leaf=40, random_state=7)
    gbm.fit(X(tr), tr["y"])
    # 확률 보정(Platt): 검증 연도에서 '점수 → 실제 확률' 로지스틱 한 줄을 다시 맞춘다(시험 연도는 손대지 않음)
    cal = {}
    for name, mdl in (("logit", logit), ("gbm", gbm)):
        s_va = mdl.predict_proba(XM[name](va))[:, 1]
        c = LogisticRegression(C=1e6, max_iter=1000)
        c.fit(np.log(np.clip(s_va, 1e-6, 1 - 1e-6) / (1 - np.clip(s_va, 1e-6, 1 - 1e-6))).reshape(-1, 1), va["y"])
        cal[name] = c

    def prob(name, mdl, x):
        s = np.clip(mdl.predict_proba(XM[name](x))[:, 1], 1e-6, 1 - 1e-6)
        return cal[name].predict_proba(np.log(s / (1 - s)).reshape(-1, 1))[:, 1]
    metrics = {"split": {"train_years": tr_y, "valid_years": va_y, "test_years": te_y,
                         "n": {"train": len(tr), "valid": len(va), "test": len(te)},
                         "events": {"train": int(tr["y"].sum()), "valid": int(va["y"].sum()), "test": int(te["y"].sum())}}}
    out = {}
    for name, mdl in (("logit", logit), ("gbm", gbm)):
        p = prob(name, mdl, te)
        out[name] = p
        metrics[name] = {**common.summarize(te["y"].values, p, p), "calibration": common.calibration_table(te["y"], p)}
    # 비교 기준(같은 시험 표본, 계산 가능한 행만)
    zt = te.dropna(subset=["zscore"])
    metrics["altman_z"] = {**common.summarize(zt["y"].values, -zt["zscore"].values), "coverage": round(len(zt) / max(1, len(te)), 3)}
    mt = te.dropna(subset=["merton_pd"])
    metrics["merton"] = {**common.summarize(mt["y"].values, mt["merton_pd"].values),
                         "coverage": round(len(mt) / max(1, len(te)), 3)}
    # 로지스틱 계수(표준화) — 해석
    coef = logit[-1].coef_[0]
    metrics["coefficients"] = sorted(({"feature": c, "label": FEATURES[c][0], "coef": round(float(w), 4)}
                                      for c, w in zip(lcols, coef)), key=lambda x: -abs(x["coef"]))
    metrics["sign_dropped"] = [{"feature": c, "label": FEATURES[c][0], "coef": w,
                                "expected": "+" if FEATURES[c][1] > 0 else "−"} for c, w in sign_dropped]
    # GBM 순열 중요도(시험 연도) — 어떤 변수를 섞으면 AUC 가 얼마나 떨어지나
    metrics["gbm_importance"] = _perm_importance(gbm, X(te), te["y"].values, cols)
    run_id = common.record_run("credit_risk", VERSION, data_source,
                               {"horizon_days": HORIZON_DAYS, "features": cols, "logit_features": lcols, "logit_C": 0.5,
                                "gbm": {"max_depth": 3, "lr": 0.05, "iter": 250}}, metrics) if record else None
    return {"run_id": run_id, "metrics": metrics, "logit": logit, "gbm": gbm, "cal": cal, "clip": (lo, hi),
            "cols": cols, "lcols": lcols, "test_prob": out}


def _perm_importance(model, X, y, cols, repeats=3, seed=7):
    rng = np.random.default_rng(seed)
    base = common.auc(y, model.predict_proba(X)[:, 1]) or 0.5
    out = []
    for c in cols:
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[c] = rng.permutation(Xp[c].values)
            drops.append(base - (common.auc(y, model.predict_proba(Xp)[:, 1]) or 0.5))
        out.append({"feature": c, "label": FEATURES[c][0], "auc_drop": round(float(np.mean(drops)), 4)})
    return sorted(out, key=lambda x: -x["auc_drop"])


def score_latest(res, df, today: date | None = None):
    """종목마다 최신 관측(라벨 미완료 포함)에 보정된 PD·등급·위험 요인을 매겨 analytics.credit_score 에 저장."""
    from sqlalchemy import text
    from app import db
    lo, hi = res["clip"]
    cols = res["cols"]
    latest = df.sort_values("obs_date").groupby("ticker").tail(1)
    if latest.empty:
        return 0
    lcols = res.get("lcols", cols)
    Xl = latest[cols].clip(lo, hi, axis=1)[lcols]
    s = np.clip(res["logit"].predict_proba(Xl)[:, 1], 1e-6, 1 - 1e-6)
    pd_ = res["cal"]["logit"].predict_proba(np.log(s / (1 - s)).reshape(-1, 1))[:, 1]
    imp, scaler, lr = res["logit"][0], res["logit"][1], res["logit"][-1]
    z = scaler.transform(imp.transform(Xl))
    contrib = z * lr.coef_[0]
    import json
    n = 0
    with db.transaction() as conn:
        for i, (_, r) in enumerate(latest.iterrows()):
            top = [{"feature": lcols[j], "label": FEATURES[lcols[j]][0], "contrib": round(float(contrib[i, j]), 3)}
                   for j in np.argsort(-contrib[i])[:3] if contrib[i, j] > 0]
            conn.execute(text(
                "INSERT INTO analytics.credit_score(ticker, as_of, fiscal_year, model_version, pd, grade, zscore, "
                "merton_pd, factors) VALUES(:t, :d, :y, :v, :pd, :g, :z, :m, CAST(:f AS jsonb)) "
                "ON CONFLICT (ticker, as_of, model_version) DO UPDATE SET pd = EXCLUDED.pd, grade = EXCLUDED.grade, "
                "zscore = EXCLUDED.zscore, merton_pd = EXCLUDED.merton_pd, factors = EXCLUDED.factors"),
                {"t": r["ticker"], "d": r["obs_date"], "y": int(r["fiscal_year"]), "v": VERSION, "pd": float(pd_[i]),
                 "g": grade(float(pd_[i])), "z": None if np.isnan(r["zscore"]) else float(r["zscore"]),
                 "m": None if np.isnan(r["merton_pd"]) else max(float(r["merton_pd"]), 1e-12),
                 "f": json.dumps(top, ensure_ascii=False)})
            n += 1
    return n


# ── 합성 데이터(파이프라인 검증용 — 실데이터 결과가 아님) ─────────────
def synthetic_frames(n_firms=600, years=range(2015, 2025), seed=11):
    """잠재 건전성 h 가 재무 비율·주가·부실 확률을 함께 움직이는 가짜 기업들 — 모형·분할·지표 코드가
    '신호가 있으면 잡고, 없는 것은 안 잡는지' 확인하는 용도. 화면·문서에는 '합성'으로 표시한다."""
    import pandas as pd
    rng = np.random.default_rng(seed)
    fin, ev, mk, master = [], [], [], []
    for i in range(n_firms):
        t = f"S{i:05d}"
        master.append({"ticker": t, "name": f"합성{i}", "sector": "제조", "ksic_code": "26", "sect_tp": None})
        h = rng.normal(0, 1)
        ta = float(np.exp(rng.normal(25, 1.2)))
        for y in years:
            h = 0.85 * h + rng.normal(0, 0.5)                   # 건전성의 지속성
            roa = 0.03 + 0.04 * h + rng.normal(0, 0.02)
            lev = 1 / (1 + math.exp(0.9 * h)) * 0.9
            te, tl = ta * (1 - lev), ta * lev
            ni = roa * ta
            rev = ta * max(0.2, 0.9 + 0.1 * rng.normal())
            cap = ta * 0.08
            disc = date(y + 1, 3, int(rng.integers(10, 30)))
            fin.append({"ticker": t, "fiscal_year": y, "fs_basis": "CFS", "revenue": rev, "gross_profit": rev * 0.25,
                        "operating_profit": ni * 1.3, "net_income": ni, "total_assets": ta, "total_liabilities": tl,
                        "total_equity": te, "capital_stock": cap, "current_assets": ta * (0.4 + 0.05 * h),
                        "current_liabilities": tl * 0.6, "retained_earnings": te - cap, "disclosed_at": disc,
                        "rcept_no": None, "source": "synthetic"})
            mcap = te * float(np.exp(0.3 * h + rng.normal(0, 0.3)))
            mk.append({"ticker": t, "month_end": pd.Timestamp(disc) - pd.Timedelta(days=12), "mktcap": mcap,
                       "ret_1y": 0.08 * h + rng.normal(0, 0.25), "vol_1y": max(0.15, 0.45 - 0.08 * h + rng.normal(0, 0.05)),
                       "dd_1y": min(0.0, -0.25 + 0.1 * h + rng.normal(0, 0.1))})
            p = 1 / (1 + math.exp(4.2 + 1.6 * h))               # 기본 부실률 약 2~3%
            if rng.random() < p:
                ev.append({"ticker": t, "rcept_dt": disc + timedelta(days=int(rng.integers(30, 330))),
                           "action": "관리종목지정"})
            ta *= float(np.exp(0.03 + 0.05 * h + rng.normal(0, 0.05)))
    return (pd.DataFrame(fin), pd.DataFrame(master), pd.DataFrame(ev, columns=["ticker", "rcept_dt", "action"]),
            pd.DataFrame(mk))


def run(source="real", today=None):
    """CLI 진입점 — 데이터 적재 → 학습·평가 → 점수 저장. source='synthetic' 이면 합성 데이터로 파이프라인만 확인."""
    if source == "synthetic":
        frames = synthetic_frames()
        df = build_dataset(*frames, today=today or date(2026, 10, 1))
        res = train(df, data_source="synthetic")
        return {"run_id": res["run_id"], "metrics": res["metrics"], "rows": len(df)}
    from sqlalchemy import text
    from app import db
    with db.transaction() as conn:                       # 월말 시장 피처 갱신(소유자 권한 함수)
        conn.execute(text("SELECT analytics.refresh_market_monthly(1200)"))
    df = build_dataset(*load_frames(), today=today)
    res = train(df, data_source="real")
    n = score_latest(res, df)
    return {"run_id": res["run_id"], "metrics": res["metrics"], "rows": len(df), "scored": n}
