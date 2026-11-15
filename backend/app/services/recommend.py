"""③ 추천 — financial_statement → recommendation. 재무 특성 벡터 → 정규화 → 유클리드 거리."""
import math
import logging
from sqlalchemy import text
from app import db

log = logging.getLogger(__name__)


def _load_features():
    rows = db.query("SELECT ticker, revenue, operating_profit, net_income, debt_ratio "
                    "FROM financial_statement WHERE revenue IS NOT NULL AND revenue > 0 "
                    "ORDER BY fiscal_year")
    feats = {}
    for r in rows:
        rev = r["revenue"]
        feats[r["ticker"]] = [
            math.log(rev),                       # 규모(로그)
            (r["operating_profit"] or 0) / rev,  # 영업이익률
            (r["net_income"] or 0) / rev,        # 순이익률
            (r["debt_ratio"] or 0),              # 부채비율
        ]
    return feats


def _normalize(feats):
    tickers = list(feats)
    dim = len(next(iter(feats.values())))
    cols = [[feats[t][i] for t in tickers] for i in range(dim)]
    mins, maxs = [min(c) for c in cols], [max(c) for c in cols]
    return {t: [(feats[t][i] - mins[i]) / (maxs[i] - mins[i]) if maxs[i] > mins[i] else 0.0
                for i in range(dim)] for t in tickers}


def _euclid(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def build_recommendations(top_k=3):
    feats = _load_features()
    if len(feats) < 2:
        log.warning("재무 데이터 부족 — 추천 건너뜀")
        return
    norm = _normalize(feats)
    with db.transaction() as conn:
        conn.execute(text("DELETE FROM recommendation"))
        for base in norm:
            dists = sorted((_euclid(norm[base], norm[o]), o) for o in norm if o != base)
            for rank, (dist, o) in enumerate(dists[:top_k], start=1):
                conn.execute(text("INSERT INTO recommendation(base_ticker, rank, similar_ticker, distance) "
                                  "VALUES(:b, :r, :s, :d)"),
                             {"b": base, "r": rank, "s": o, "d": round(dist, 4)})
    log.info("추천 생성: %d종목", len(norm))


def _fin_rows(tickers):
    """종목별 최신 회계연도 재무 1행(매출·영업이익·순이익·부채비율·연도·작성 기준)."""
    if not tickers:
        return {}
    rows = db.query(
        "SELECT DISTINCT ON (ticker) ticker, fiscal_year, revenue, operating_profit, net_income, debt_ratio, fs_basis "
        "FROM financial_statement WHERE ticker = ANY(:ts) ORDER BY ticker, fiscal_year DESC, ingested_at DESC",
        {"ts": list(tickers)})
    return {r["ticker"]: r for r in rows}


def _ratios(r):
    rev = r.get("revenue") or 0
    pct = lambda x: round(x / rev * 100, 2) if (rev and x is not None) else None   # noqa: E731
    return {"fiscal_year": r.get("fiscal_year"), "revenue": rev or None,
            "op_margin": pct(r.get("operating_profit")), "net_margin": pct(r.get("net_income")),
            "debt_ratio": None if r.get("debt_ratio") is None else round(float(r["debt_ratio"]), 2),
            "fs_basis": r.get("fs_basis")}


def peers(ticker):
    """동종(재무 구조 유사) 기업 비교 — 여신 심사·기업 분석에서 쓰는 '비교 기업(Peer group)' 방식.
    재무 유사도 추천(build_recommendations: 매출 규모·영업이익률·순이익률·부채비율 거리)으로 고른 가까운 종목 3곳과
    이 종목의 주요 비율을 나란히 보여 준다. 업종 정보가 없는 종목이 많아 업종은 가리지 않는다(교육용)."""
    from app.services import catalog
    sim = db.query("SELECT rank, similar_ticker, distance FROM recommendation WHERE base_ticker=:t ORDER BY rank",
                   {"t": ticker})
    fin = _fin_rows([ticker] + [s["similar_ticker"] for s in sim])
    if not sim or ticker not in fin:
        return {"ticker": ticker, "available": False,
                "reason": "비교할 재무 데이터가 아직 없어요(DART 재무 수집 후 매일 23:00 에 계산)"}

    from app.services import quotes
    sample = quotes.market_source() != "krx"     # 표본 시세 모드의 재무는 데모(역산) 값 — 화면에 '표본' 표시

    def row(t, extra=None):
        m = catalog.meta(t)
        return {"ticker": t, "name": m["name"], "sector": m["sector"] or None, **_ratios(fin.get(t) or {}),
                **(extra or {})}
    return {"ticker": ticker, "available": True, "source": "sample" if sample else "dart", "base": row(ticker),
            "peers": [row(s["similar_ticker"], {"rank": s["rank"], "distance": s["distance"]})
                      for s in sim if s["similar_ticker"] in fin],
            "method": "매출 규모(로그)·영업이익률·순이익률·부채비율을 0~1로 맞춘 뒤 거리가 가까운 순(업종 무관)",
            "note": "교육용 · 재무 구조가 비슷하다는 뜻이지 같은 업종·같은 주가 흐름이라는 뜻은 아니에요"}
