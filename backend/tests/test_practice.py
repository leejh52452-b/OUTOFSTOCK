"""실습(과거 사례) 엔진 — 시작/진행/매매/멱등성/단일 active 규칙 검증."""
from fastapi.testclient import TestClient
from app.api import app
from app.services import practice
from app.collectors import marketgen

client = TestClient(app)


def _seed(scenario_id=None):
    for sc in practice.SCENARIOS:
        if scenario_id is None or sc["scenario_id"] == scenario_id:
            marketgen.seed_scenario_ohlc(sc, real=False)


def test_start_gives_10m_and_series():
    _seed("samsung-golden")
    st = practice.start("u1", "samsung-golden")
    assert st["account"]["cash"] == 10_000_000
    assert st["account"]["start_cash"] == 10_000_000
    assert len(st["series"]) >= practice.START_OFFSET      # 초기 공개 구간
    assert st["status"] == "active"
    assert "SMA5" in st["overlays"] and "SMA20" in st["overlays"]


def test_step_reveals_more_and_hides_future():
    _seed("hyundai-volume")
    st = practice.start("u2", "hyundai-volume")
    n0 = len(st["series"])
    st2 = practice.step(st["session_id"], 5)
    assert len(st2["series"]) > n0                          # 5일 더 공개
    assert st2["progress"]["current"] > st["progress"]["current"]
    # 미래 은닉: series 마지막 날짜가 커서와 동일
    assert st2["series"][-1]["trade_date"] == st2["cursor_date"]


def test_buy_then_sell_roundtrip():
    _seed("hyundai-volume")
    st = practice.start("u3", "hyundai-volume")
    sid = st["session_id"]
    price = st["price"]
    st = practice.trade(sid, "BUY", 10)
    assert st["account"]["holding_qty"] == 10
    assert st["account"]["cash"] == 10_000_000 - 10 * price
    st = practice.trade(sid, "SELL", 10)
    assert st["account"]["holding_qty"] == 0


def test_trade_idempotent():
    _seed("hyundai-volume")
    st = practice.start("u4", "hyundai-volume")
    sid = st["session_id"]
    practice.trade(sid, "BUY", 5, idem_key="k1")
    st = practice.trade(sid, "BUY", 5, idem_key="k1")       # 같은 키 → 1회만
    assert st["account"]["holding_qty"] == 5


def test_only_one_active_scenario():
    _seed()
    practice.start("u5", "samsung-golden")
    try:
        practice.start("u5", "naver-rsi")
        assert False, "진행 중 실습이 있는데 새 실습이 시작됨"
    except practice.PracticeError:
        pass


def test_list_scenarios_shape():
    _seed()
    d = practice.list_scenarios("u6")
    assert d["stats"]["total"] == 6
    assert len(d["items"]) == 6
    assert all("difficulty" in it and "objective" in it for it in d["items"])


def test_api_start_and_step():
    _seed("samsung-golden")
    r = client.post("/practice/start", json={"user_id": "api1", "scenario_id": "samsung-golden"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r2 = client.post(f"/practice/{sid}/step", json={"days": 1})
    assert r2.status_code == 200 and r2.json()["progress"]["current"] >= 1


def test_end_waits_for_in_flight_trade_and_settles_on_committed_balance():
    # 예전: '종료'가 행을 잠그지 않아, 커밋 직전의 주문을 무시한 잔고로 수익률을 확정했다(기록·잔고 불일치)
    import threading
    import time
    from sqlalchemy import text
    from app import db
    _seed("hyundai-volume")
    st = practice.start("u_end_race", "hyundai-volume")
    sid = st["session_id"]
    done = {}
    with db.transaction() as conn:                                        # 진행 중인 주문(잠금 + 잔고 변경)을 흉내
        conn.execute(text("SELECT 1 FROM practice_session WHERE session_id=:s FOR UPDATE"), {"s": sid})
        conn.execute(text("UPDATE practice_session SET cash=cash+1000000, version=version+1 WHERE session_id=:s"),
                     {"s": sid})
        t = threading.Thread(target=lambda: done.update(st=practice.end(sid)))
        t.start()
        time.sleep(0.4)
        assert t.is_alive()                                               # 주문이 끝날 때까지 기다림
    t.join(10)
    assert done["st"]["status"] == "done" and done["st"]["final_return"] == 10.0   # +100만원(10%) 반영
    assert practice.end(sid)["final_return"] == 10.0                      # 두 번 눌러도 다시 정산 안 함


def test_step_with_stale_read_cannot_move_finished_session(monkeypatch):
    # '다음'이 세션을 읽은 직후 '종료'가 커밋된 경우 — 예전엔 종료가 version 을 안 올려 커서가 끝난 세션에서 움직였다
    import pytest
    _seed("hyundai-volume")
    st = practice.start("u_step_done", "hyundai-volume")
    sid = st["session_id"]
    stale = practice._get(sid)                                            # 아직 active 로 읽힘
    ended = practice.end(sid)
    monkeypatch.setattr(practice, "_get", lambda _sid: stale)
    with pytest.raises(practice.PracticeError):
        practice.step(sid, 1)
    monkeypatch.undo()
    assert practice.get_state(sid)["cursor_date"] == ended["cursor_date"]


def test_concurrent_start_creates_one_active_session():
    # 예전: '시작하기' 연타 → 같은 실습의 진행 중 세션이 2개(마이페이지 '진행 중 2개')
    import threading
    from app import db
    _seed("hyundai-volume")
    outs = []
    ts = [threading.Thread(target=lambda: outs.append(practice.start("u_race_start", "hyundai-volume")))
          for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len({o["session_id"] for o in outs}) == 1
    assert db.query("SELECT COUNT(*) AS n FROM practice_session WHERE user_id='u_race_start' AND status='active'")[0]["n"] == 1
