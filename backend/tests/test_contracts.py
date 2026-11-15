"""데이터 계약·품질 지표(app/services/contracts.py)."""
from app import db
from app.collectors import marketgen
from app.services import contracts


def test_contracts_measure_and_record_and_flag_violations():
    marketgen.seed_market(real=False)
    r = contracts.run()
    assert 0 < r["score"] <= 1 and "daily_price" in r["datasets"]
    assert db.query("SELECT COUNT(*) AS n FROM dq_result")[0]["n"] == len(contracts.CONTRACTS)
    names = {(x["dataset"], x["check"]) for x in r["failed"]}
    assert ("daily_price", "invalid_price") not in names
    db.execute("UPDATE ohlc SET low = high + 1 WHERE ticker = (SELECT MIN(ticker) FROM ohlc)")   # 고저가 뒤집힘
    r2 = contracts.run()
    bad = next(x for x in r2["results"] if x["dataset"] == "ohlc" and x["check"] == "candle_inconsistent")
    assert bad["status"] == "fail" and bad["value"] > 0
    assert contracts.trend()[-1]["checks"] == 2 * len(contracts.CONTRACTS)
    assert {c["dimension"] for c in contracts.catalog()} == {"completeness", "accuracy", "consistency",
                                                             "timeliness", "validity"}


def test_every_executed_transfer_has_an_fds_decision():
    """통제 완전성 — 판정을 거치지 않은 송금 경로가 생기면 데이터 계약이 잡는다(결함 20의 탐지 통제)."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.services import auth, transfer
    c = TestClient(app)
    for u in ("dq-a", "dq-b"):
        auth.register(u, "Dq-pass-2026")
    tok = c.post("/auth/login", json={"username": "dq-a", "password": "Dq-pass-2026"}).json()["token"]
    assert c.post("/transfer", headers={"Authorization": f"Bearer {tok}"}, json={"to": "dq-b", "amount": 1000}).status_code == 200

    def unscreened():
        return next(x for x in contracts.run(record=False)["results"] if x["check"] == "transfer_unscreened")
    assert unscreened()["status"] == "pass"
    transfer.send("dq-a", "dq-b", 2000, screen=False)                      # 판정을 끄고 보낸 송금(우회 경로)
    bad = unscreened()
    assert bad["status"] == "fail" and bad["value"] == 1 and bad["severity"] == "critical"


def test_contract_dates_are_korean_dates_whatever_the_db_timezone():
    """CURRENT_DATE 는 DB 서버 시간대 기준 — UTC 서버면 00~09시(KST)에 하루 전이었다(결함 29)."""
    from sqlalchemy import text

    from app import clock
    assert all("CURRENT_DATE" not in x.sql for x in contracts.CONTRACTS)
    with db.get_engine().connect() as conn:
        conn.execute(text("SET TIME ZONE 'Etc/GMT+12'"))                    # 한국과 21시간 차이 — 하루 대부분 날짜가 다르다
        assert conn.execute(text(f"SELECT {contracts._KST_TODAY}")).scalar() == clock.today_str()   # date → 'YYYY-MM-DD'(ADR-0027)
        conn.rollback()
