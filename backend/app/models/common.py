"""모형 공통 — 평가 지표(AUC·KS·Brier·PR-AUC·보정표·상위 구간 포착률·IC)와 실행 이력 기록.

지표를 직접 구현하는 이유: 의미를 설명할 수 있어야 해서(면접) — sklearn 값과 같은지는 테스트로 맞춘다.
"""
from __future__ import annotations

import json
import math

import numpy as np


def auc(y, score):
    """ROC AUC — '부실 기업 하나와 정상 기업 하나를 뽑았을 때 부실 쪽 점수가 더 높을 확률'(순위 기반, 동점 0.5)."""
    y, s = np.asarray(y, dtype=int), np.asarray(score, dtype=float)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(allv):                                   # 동점은 평균 순위
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = ranks[:len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def ks(y, score):
    """KS 통계량 — 부실·정상 두 집단의 점수 누적분포 사이 최대 거리(신용평가에서 변별력 표준 지표)."""
    y, s = np.asarray(y, dtype=int), np.asarray(score, dtype=float)
    if y.sum() == 0 or y.sum() == len(y):
        return None
    order = np.argsort(-s, kind="mergesort")
    ys = y[order]
    tpr = np.cumsum(ys) / ys.sum()
    fpr = np.cumsum(1 - ys) / (1 - ys).sum()
    return float(np.max(np.abs(tpr - fpr)))


def brier(y, p):
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    return float(np.mean((p - y) ** 2))


def pr_auc(y, score):
    """정밀도-재현율 곡선 아래 넓이(평균 정밀도) — 부실이 드문(불균형) 문제에서 AUC 보다 냉정한 지표."""
    y, s = np.asarray(y, dtype=int), np.asarray(score, dtype=float)
    if y.sum() == 0:
        return None
    order = np.argsort(-s, kind="mergesort")
    ss, ys = s[order], y[order]
    cut = np.r_[np.where(np.diff(ss))[0], len(ys) - 1]            # 같은 점수는 한 임계값으로 묶는다(동점 처리)
    tp = np.cumsum(ys)[cut]
    precision = tp / (cut + 1)
    recall = tp / tp[-1]
    return float(np.sum((recall - np.r_[0, recall[:-1]]) * precision))


def calibration_table(y, p, bins=10):
    """예측 확률 10분위별 [예측 평균, 실제 비율, 건수] — 보정(calibration) 확인. 분위 경계는 예측값 기준."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    if len(p) == 0:
        return []
    order = np.argsort(p, kind="mergesort")
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk):
            out.append({"pred": round(float(p[chunk].mean()), 4), "actual": round(float(y[chunk].mean()), 4),
                        "n": int(len(chunk))})
    return out


def top_capture(y, score, frac=0.1):
    """점수 상위 frac(기본 10%) 안에 실제 부실이 몇 %나 들어오나(심사 우선순위의 효율)."""
    y, s = np.asarray(y, dtype=int), np.asarray(score, dtype=float)
    if y.sum() == 0:
        return None
    k = max(1, int(round(len(s) * frac)))
    top = np.argsort(-s, kind="mergesort")[:k]
    return float(y[top].sum() / y.sum())


def spearman(a, b):
    """순위 상관(IC) — 동점 평균 순위."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3:
        return None

    def rank(x):
        o = np.argsort(x, kind="mergesort")
        r = np.empty(len(x))
        r[o] = np.arange(len(x))
        return r
    ra, rb = rank(a), rank(b)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def summarize(y, score, prob=None):
    """분류 성능 한 묶음."""
    return {"n": int(len(y)), "events": int(np.sum(y)), "base_rate": round(float(np.mean(y)), 4) if len(y) else None,
            "auc": _r(auc(y, score)), "ks": _r(ks(y, score)), "pr_auc": _r(pr_auc(y, score)),
            "top10_capture": _r(top_capture(y, score)),
            "brier": _r(brier(y, prob)) if prob is not None else None}


def _r(x, k=4):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), k)


def record_run(model, version, data_source, params, metrics, notes=None):
    """모형 실행 기록 → run_id."""
    from sqlalchemy import text
    from app import db
    with db.transaction() as conn:
        return conn.execute(text(
            "INSERT INTO analytics.model_run(model, version, data_source, params, metrics, notes, finished_at) "
            "VALUES(:m, :v, :d, CAST(:p AS jsonb), CAST(:x AS jsonb), :n, now()) RETURNING run_id"),
            {"m": model, "v": version, "d": data_source, "p": json.dumps(params, ensure_ascii=False, default=str),
             "x": json.dumps(metrics, ensure_ascii=False, default=str), "n": notes}).scalar()


def latest_run(model, data_source=None):
    from app import db
    q = "SELECT run_id, model, version, data_source, started_at, finished_at, params, metrics, notes " \
        "FROM analytics.model_run WHERE model = :m"
    p = {"m": model}
    if data_source:
        q += " AND data_source = :d"
        p["d"] = data_source
    rows = db.query(q + " ORDER BY run_id DESC LIMIT 1", p)
    if not rows:
        return None
    r = rows[0]
    for k in ("started_at", "finished_at"):
        r[k] = r[k].isoformat() if r[k] else None
    return r


# ── 정보계 조회(분석가 CLI 전용 — 서비스 API 로 노출하지 않는다) ──────────────────────────
def report(model):
    """가장 최근 학습 기록(실데이터 우선) — python run.py model-report credit|return."""
    r = latest_run(model, "real") or latest_run(model)
    if not r:
        return {"model": model, "available": False,
                "message": "아직 학습 기록이 없어요 — python run.py "
                           + ("credit-train" if model == "credit_risk" else "return-validate")}
    return {"available": True, **r, "synthetic": r["data_source"] == "synthetic"}


def credit_score(ticker):
    """종목 신용위험 점수(공시일 기준 최신) — 등급·PD·요인 + Z''·Merton. 순위표로는 제공하지 않는다."""
    from app import db
    rows = db.query("SELECT as_of, fiscal_year, model_version, pd, grade, zscore, merton_pd, factors "
                    "FROM analytics.credit_score WHERE ticker = :t ORDER BY as_of DESC LIMIT 1", {"t": str(ticker)[:12]})
    if not rows:
        return {"ticker": ticker, "available": False, "message": "점수 없음(금융업이거나 재무·시세 이력이 부족)"}
    r = rows[0]
    return {"ticker": ticker, "available": True,
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()},
            "disclaimer": "교육용 통계 모형의 추정치입니다. 투자·신용 판단의 근거로 쓰지 마세요."}
