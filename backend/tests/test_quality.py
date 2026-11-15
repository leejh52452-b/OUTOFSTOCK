"""데이터 품질 검증 — 수집 데이터의 결측·이상치·참조무결성·신선도 검사(데이터 엔지니어링).
정상 데이터는 통과하고, 규칙별 불량 데이터는 정확히 잡아내며, 불량 행은 격리소에 기록된다."""

from app import db
from app.services import quality as q


def _ins(ticker, date, close, volume=1000, rate=1.0):
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES(:t,:d,:c,:v,:r)",
               {"t": ticker, "d": date, "c": close, "v": volume, "r": rate})


def test_clean_data_passes():
    _ins("005930", "2026-09-03", 71000, 15_000_000, 2.1)
    _ins("000660", "2026-09-03", 178000, 8_000_000, -1.5)
    _ins("035420", "2026-09-03", 210000, 1_000_000, 29.99)   # 상한가 근처 — 정상
    rep = q.check_daily_prices()
    assert rep["ok"] is True
    assert rep["total_issues"] == 0


def test_each_rule_catches_its_violation():
    _ins("005930", "2026-09-01", 71000, 15_000_000, 2.1)     # 정상
    _ins("005930", "2026-09-02", 0, 15_000_000, 1.0)         # positive_price 위반(종가 0)
    _ins("000660", "2026-09-02", 50000, -5, 1.0)             # nonneg_volume 위반(음수 거래량)
    _ins("035420", "2026-09-02", 200000, 1000, 45.0)         # price_limit 위반(+45% > 30%)
    _ins("999999", "2026-09-02", 1000, 1000, 1.0)            # orphan_ticker(stock 에 없음)
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES('005380','2026-09-02', NULL, 1000, 1.0)")   # null_close 위반

    rep = q.check_daily_prices()
    flagged = {c["rule"]: c["violations"] for c in rep["checks"]}
    assert rep["ok"] is False
    assert flagged["positive_price"] >= 1
    assert flagged["nonneg_volume"] >= 1
    assert flagged["price_limit"] >= 1
    assert flagged["orphan_ticker"] >= 1
    assert flagged["null_close"] >= 1


def test_normal_upper_limit_not_flagged():
    """상한가(정확히 +30%)는 정상이어야 한다 — 경계값이 오탐되지 않음."""
    _ins("005930", "2026-09-03", 71000, 1000, 30.0)
    rep = q.check_daily_prices()
    assert rep["ok"] is True


def test_freshness_flags_stale():
    _ins("005930", "2026-09-01", 71000)
    ok = q.check_freshness(max_age_days=3, as_of="2026-09-03")     # 2일 경과 → 신선
    stale = q.check_freshness(max_age_days=3, as_of="2026-09-30")  # 29일 경과 → 오래됨
    assert ok["ok"] is True and ok["age_days"] == 2
    assert stale["ok"] is False


def test_quarantine_records_issues():
    _ins("035420", "2026-09-02", 200000, 1000, 45.0)        # price_limit 위반
    rep = q.run_quality_checks(as_of="2026-09-02", quarantine=True)
    assert rep["ok"] is False
    rows = db.query("SELECT rule, detail FROM data_quality_issue WHERE rule='price_limit'")
    assert len(rows) >= 1


