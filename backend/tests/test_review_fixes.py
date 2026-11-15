"""코드 감사에서 발견한 실제 버그들의 회귀 방지 테스트."""
from datetime import datetime, timedelta

import pytest

from app import db
from app.services import soundness, risk, auth
from app.services import monitoring as mon


def _seed_fin(ticker, rev, op, net, debt):
    db.execute("INSERT INTO financial_statement"
               "(ticker, fiscal_year, report_code, revenue, operating_profit, net_income, debt_ratio) "
               "VALUES(:t, 2025, '11011', :r, :o, :n, :d)",
               {"t": ticker, "r": rev, "o": op, "n": net, "d": debt})


def _seed_price(ticker, date, close, vol=1000, rate=0.0):
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES(:t, :d, :c, :v, :r)", {"t": ticker, "d": date, "c": close, "v": vol, "r": rate})


def _today(offset=0):
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


# ── soundness: 자본잠식(음수 부채비율)을 '최고점'으로 매기던 버그 ──
def test_soundness_negative_equity_is_worst_not_best():
    _seed_fin("005930", rev=1000, op=50, net=30, debt=-150)   # 음수 자본 = 파산위험
    s = soundness.soundness("005930")
    assert s["scores"]["debt_ratio"] == 0.0                   # 만점(100)이 아니라 최악(0)
    assert s["grade"] != "안정"


# ── soundness: 영업이익/순이익이 None 이면 500 크래시하던 버그 ──
def test_soundness_partial_financials_no_crash():
    _seed_fin("005930", rev=1000, op=None, net=None, debt=80)
    s = soundness.soundness("005930")
    assert s["ok"]
    assert s["ratios"]["operating_margin"] is None
    assert s["score"] is not None                             # 부채비율만으로도 산출


# ── risk: 정확히 2일치 데이터에서 round(None) 크래시하던 버그 ──
def test_risk_two_days_no_crash():
    _seed_price("005930", "2026-09-01", 100)
    _seed_price("005930", "2026-09-02", 110)
    r = risk.risk_profile("005930")
    assert r["ok"] is False                                    # 크래시 대신 정상 거부


# ── monitoring: /health 가 신선도/배치실패와 무관하게 항상 ok 이던 문제 ──
def test_health_ok_when_fresh_and_success():
    _seed_price("005930", _today(0), 100)
    mon.finish_run(mon.start_run(), "SUCCESS")
    h = mon.health()
    assert h["status"] == "ok" and h["issues"] == []


def test_health_degraded_when_stale():
    _seed_price("005930", _today(10), 100)                    # 10일 전 → 오래됨
    h = mon.health()
    assert h["status"] == "degraded" and "stale_data" in h["issues"]


def test_health_degraded_when_last_run_failed():
    _seed_price("005930", _today(0), 100)
    mon.finish_run(mon.start_run(), "FAILED", note="DART timeout")
    h = mon.health()
    assert h["status"] == "degraded" and "last_pipeline_failed" in h["issues"]


# ── auth: 없는 사용자 로그인도 여전히 거부(동작 보존) ──
def test_login_unknown_user_rejected():
    with pytest.raises(auth.AuthError):
        auth.login("nobody", "whatever")
