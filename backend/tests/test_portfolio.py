"""포트폴리오 리스크 분석 — 상관관계 기반 분산효과 정량화(은행 리스크관리) 검증.

핵심 증명: 완전 역상관 종목을 섞으면 포트폴리오 위험이 거의 상쇄되고,
완전 상관 종목은 분산효과가 없다(가중평균 변동성 그대로).
"""
import numpy as np

from app import db
from app.services import portfolio


def _seed(ticker, closes, start_day=1):
    for i, c in enumerate(closes):
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
                   "VALUES(:t, :d, :c, 1000, 0)",
                   {"t": ticker, "d": f"2026-09-{start_day + i:02d}", "c": round(float(c), 4)})


def _mirror(closes):
    """수익률 부호를 정확히 뒤집은 시계열 → 원본과 상관 -1(완전 역상관)."""
    a = np.asarray(closes, dtype=float)
    rets = np.diff(a) / a[:-1]
    b = [100_000.0]
    for r in rets:
        b.append(b[-1] * (1 - r))
    return b


# 원화 종가는 정수 원으로 저장된다(ADR-0027) — 값이 작으면 반올림이 수익률을 흔들어 '완전 역상관'이 깨지므로 만 원 단위로
BASE = [100_000, 110_000, 104_000, 118_000, 109_000, 121_000, 115_000]


def test_perfectly_correlated_no_diversification():
    """동일 시계열 2종목 → 상관 1, 분산효과 없음(포트폴리오 변동성=가중평균)."""
    _seed("005930", BASE)
    _seed("000660", BASE)
    r = portfolio.analyze(["005930", "000660"])
    assert r["ok"]
    assert r["correlation"][0][1] == 1.0
    assert abs(r["portfolio_volatility"] - r["weighted_avg_volatility"]) < 1e-6
    assert abs(r["risk_reduction"]) < 1e-6                 # 위험 감소 ~0


def test_perfectly_hedged_cancels_risk():
    """완전 역상관 2종목 동일비중 → 위험이 거의 상쇄(risk_reduction ~1)."""
    _seed("005930", BASE)
    _seed("000660", _mirror(BASE))
    r = portfolio.analyze(["005930", "000660"])
    assert r["ok"]
    assert r["correlation"][0][1] <= -0.9999                    # 정수 원 반올림만큼(1원/10만 원)의 오차
    assert r["portfolio_volatility"] < r["weighted_avg_volatility"] * 0.05   # 거의 0
    assert r["risk_reduction"] > 0.9


def test_weights_are_normalized():
    _seed("005930", BASE)
    _seed("000660", _mirror(BASE))
    r = portfolio.analyze(["005930", "000660"], weights=[3, 1])
    assert r["weights"] == [0.75, 0.25]                   # 합=1 로 정규화


def test_weight_length_mismatch_rejected():
    _seed("005930", BASE)
    _seed("000660", BASE)
    r = portfolio.analyze(["005930", "000660"], weights=[1.0])
    assert r["ok"] is False


def test_insufficient_common_days():
    _seed("005930", [100, 110])                           # 2일 → 수익률 1개
    _seed("000660", [100, 90])
    assert portfolio.analyze(["005930", "000660"])["ok"] is False


