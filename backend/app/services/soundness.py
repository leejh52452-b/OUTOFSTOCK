"""재무 건전성 스코어 — 은행 신용분석의 축약판.

DART 재무(매출·영업이익·순이익·부채비율)로 수익성·안정성 지표를 만들고,
각 지표를 0~100 으로 정규화해 종합 스코어와 등급(안정/보통/주의)을 낸다.
+ 부실예측 모형 Altman Z''(자산·부채·이익잉여금) → 안전/회색/부실 위험 구간(distress).
+ 시장 기반 Merton 부도거리(DD)·부도확률(PD) — 시가총액·주가변동성·부채로 자산가치를 역산(merton, credit.py).
정책은행 여신·신용분석의 '재무 건전성 평가'를 단순화한 모델.
※ 교육용 — 실제 신용평가 모형(가중치·업종 조정·비재무 요소)과는 다르다."""
from app import db
from app.services import pit


_FIN_COLS = ("fiscal_year, revenue, operating_profit, net_income, total_equity, debt_ratio, disclosed_at, "
             "total_assets, current_assets, current_liabilities, total_liabilities, retained_earnings, fs_basis")
_FIN_WORDS = ("금융", "지주", "은행", "보험", "증권", "생명", "화재", "카드", "캐피탈", "리츠")


def _latest_financials(ticker):
    rows = db.query(f"SELECT {_FIN_COLS} FROM financial_statement WHERE ticker=:t "
                    "ORDER BY fiscal_year DESC LIMIT 1", {"t": ticker})
    return rows[0] if rows else None


# ── 부실예측: Altman Z''-score(1995, 비제조·신흥시장용) ─────────────────────
#   Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4
#     X1 = (유동자산 − 유동부채) ÷ 자산총계      운전자본 — 단기 지급 여력
#     X2 = 이익잉여금 ÷ 자산총계                누적 수익성(기업 나이·내부유보)
#     X3 = 영업이익 ÷ 자산총계                  자산 수익성(EBIT 대용)
#     X4 = 자본총계 ÷ 부채총계                  레버리지(장부가 기준)
#   구간: 2.60 초과 '안전' · 1.10~2.60 '회색' · 1.10 미만 '부실 위험'
#   은행·보험 등 금융업은 자산·부채 구조가 달라 적용하지 않는다.
#   ※ 교육용 — 실제 여신심사는 업종 보정·비재무 정보·현금흐름을 함께 본다.
ALTMAN_SAFE, ALTMAN_DISTRESS = 2.60, 1.10


def altman_z(fin, name=None):
    """재무 1건 → {z, zone, components} | {z: None, reason}. 항목이 모자라거나 금융업이면 계산하지 않는다."""
    if not fin:
        return {"z": None, "reason": "재무 데이터 없음"}
    if name and any(w in name for w in _FIN_WORDS):
        return {"z": None, "reason": "금융업은 적용 대상 아님"}
    ta, ca, cl = fin.get("total_assets"), fin.get("current_assets"), fin.get("current_liabilities")
    tl, re_, op, eq = (fin.get("total_liabilities"), fin.get("retained_earnings"), fin.get("operating_profit"),
                       fin.get("total_equity"))
    if not ta or ta <= 0 or None in (ca, cl, re_, op, eq) or not tl:
        return {"z": None, "reason": "대차대조표 항목 부족(DART 재수집 필요)"}
    x1, x2, x3, x4 = (ca - cl) / ta, re_ / ta, op / ta, eq / tl
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    zone = "안전" if z > ALTMAN_SAFE else ("회색" if z >= ALTMAN_DISTRESS else "부실 위험")
    return {"z": round(z, 2), "zone": zone,
            "components": {"working_capital": round(x1, 4), "retained_earnings": round(x2, 4),
                           "ebit": round(x3, 4), "equity_to_liabilities": round(x4, 4)},
            "thresholds": {"safe": ALTMAN_SAFE, "distress": ALTMAN_DISTRESS}}


def compute_ratios(fin):
    """원시 재무 → 비율 지표. 매출·분자 중 하나라도 None 이면 마진은 계산 불가(None)."""
    rev = fin.get("revenue")
    op, net, debt = fin.get("operating_profit"), fin.get("net_income"), fin.get("debt_ratio")
    op_margin = (op / rev * 100) if rev and op is not None else None     # 영업이익률(%)
    net_margin = (net / rev * 100) if rev and net is not None else None  # 순이익률(%)
    return {"operating_margin": _round(op_margin), "net_margin": _round(net_margin),
            "debt_ratio": debt}


def _round(x):
    return round(x, 2) if x is not None else None


def _score(value, good, bad):
    """value 를 0~100 으로 선형 매핑. good→100, bad→0, 범위 밖은 클램프.
    good<bad 면 '낮을수록 우수'(부채비율)로 자동 처리."""
    if value is None:
        return None
    if good == bad:
        return 50.0
    x = (value - bad) / (good - bad)
    return round(max(0.0, min(1.0, x)) * 100, 1)


def soundness(ticker, as_of=None):
    """재무 건전성 종합 — 비율·개별 스코어·종합 스코어·등급.
    as_of 지정 시 그 시점에 공시로 알 수 있던 재무로 평가(look-ahead bias 방지)."""
    fin = pit.financials_asof(ticker, as_of) if as_of else _latest_financials(ticker)
    if not fin:
        return {"ticker": ticker, "ok": False, "reason": "재무 데이터 없음", "as_of": as_of}

    r = compute_ratios(fin)
    # 부채비율은 total_liab/total_equity 라서 '자본잠식(음수 자본)'이면 음수가 된다.
    # 이는 최악(파산 위험)인데 선형 매핑에 그냥 넣으면 오히려 만점이 나오므로 0점으로 강제.
    debt = r["debt_ratio"]
    if debt is not None and debt < 0:
        debt_score = 0.0                                                     # 자본잠식 → 최악
    else:
        debt_score = _score(debt, good=50, bad=200)                         # 50%- 우수 / 200%+ 취약
    scores = {
        "operating_margin": _score(r["operating_margin"], good=20, bad=0),   # 영업이익률 20%+ 우수
        "net_margin": _score(r["net_margin"], good=15, bad=0),               # 순이익률 15%+ 우수
        "debt_ratio": debt_score,
    }
    comps = [s for s in scores.values() if s is not None]
    score = round(sum(comps) / len(comps), 1) if comps else None
    grade = None
    if score is not None:
        grade = "안정" if score >= 70 else "보통" if score >= 40 else "주의"
    from app.services import catalog, credit
    name = catalog.meta(ticker)["name"]
    return {"ticker": ticker, "ok": True, "as_of": as_of,
            "fiscal_year": fin.get("fiscal_year"), "disclosed_at": fin.get("disclosed_at"),
            "fs_basis": fin.get("fs_basis"),                          # CFS 연결 · OFS 별도 · None(표본/예전 수집분)
            "ratios": r, "scores": scores, "score": score, "grade": grade,
            "distress": altman_z(fin, name),                          # 회계 기반(재무제표)
            "merton": credit.merton_for(ticker, fin, name, as_of)}    # 시장 기반(주가·시총 → 부도거리·PD)
