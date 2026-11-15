import json
from fastapi.testclient import TestClient
from app import db
from app.api import app
from app.collectors.realtime import parse_ticks, save_tick, subscribe_message


def parse_tick(message):
    ticks = parse_ticks(message)
    return ticks[0] if ticks else None

client = TestClient(app)

# H0STCNT0 샘플: 종목코드^체결시간^현재가^부호^전일대비^등락률^ ... ^(index 13=누적거래량)
SAMPLE = "0|H0STCNT0|001|" + "^".join(
    ["005930", "093021", "71500", "2", "500", "0.70", "0", "0", "0", "0", "0", "0", "0", "1234567"])


def test_parse_tick_fields():
    t = parse_tick(SAMPLE)
    assert t["ticker"] == "005930"
    assert t["price"] == 71500.0
    assert t["change_rate"] == 0.70
    assert t["volume"] == 1234567


def test_control_and_empty_messages_ignored():
    assert parse_tick('{"header":{"tr_id":"PINGPONG"}}') is None
    assert parse_tick("") is None


def test_subscribe_message_shape():
    msg = json.loads(subscribe_message("APPROVAL", "005930"))
    assert msg["header"]["approval_key"] == "APPROVAL"
    assert msg["header"]["tr_type"] == "1"
    assert msg["body"]["input"]["tr_id"] == "H0STCNT0"
    assert msg["body"]["input"]["tr_key"] == "005930"


# ── 확장 파싱(다건 메시지·부호·시/고/저·영업일) + REST 일자별 + 시뮬 표시 ──
from app.collectors import marketgen                                     # noqa: E402
from app.collectors.realtime import (parse_daily_output, store_recent_bars,    # noqa: E402
                                     run_sim, _top_by_mcap)


def _rec(over=None):
    f = ["0"] * 46
    vals = {0: "005930", 1: "153000", 2: "82300", 3: "2", 4: "1800", 5: "2.24", 7: "80600",
            8: "82500", 9: "80400", 13: "15000000", 14: "1230000000000", 33: "20260923"}
    vals.update(over or {})
    for i, v in vals.items():
        f[i] = str(v)
    return f


def test_multi_record_message_and_sign_normalization():
    r2 = _rec({0: "000660", 2: "171000", 3: "5", 4: "2000", 5: "1.16"})     # 부호 5(하락), 값은 양수로 옴
    ticks = parse_ticks("0|H0STCNT0|002|" + "^".join(_rec() + r2))
    assert [t["ticker"] for t in ticks] == ["005930", "000660"]
    assert ticks[0]["session_date"] == "2026-09-23" and ticks[0]["open"] == 80600
    assert ticks[0]["value"] == 1230000000000 and ticks[0]["change"] == 1800
    assert ticks[1]["change"] == -2000 and ticks[1]["change_rate"] == -1.16


def test_save_tick_stores_extended_fields_with_kst_ts():
    save_tick(parse_ticks("0|H0STCNT0|001|" + "^".join(_rec()))[0])
    r = db.query("SELECT * FROM realtime_price WHERE ticker='005930'")[0]
    assert r["session_date"] == "2026-09-23" and r["source"] == "kis"
    assert r["high"] == 82500 and r["low"] == 80400 and r["value"] == 1230000000000
    assert r["ts"].isoformat().endswith("+09:00")              # timestamptz → 한국시간 datetime(연결 시간대 Asia/Seoul)


def _daily():
    return [{"stck_bsop_date": "20260923", "stck_oprc": "80600", "stck_hgpr": "82500", "stck_lwpr": "80400",
             "stck_clpr": "82300", "acml_vol": "15000000", "prdy_vrss": "1800", "prdy_vrss_sign": "2",
             "prdy_ctrt": "2.24"},
            {"stck_bsop_date": "20260922", "stck_oprc": "80000", "stck_hgpr": "81000", "stck_lwpr": "79500",
             "stck_clpr": "80500", "acml_vol": "9000000", "prdy_vrss": "500", "prdy_vrss_sign": "5",
             "prdy_ctrt": "0.62"}]


def test_parse_kis_daily_output():
    bars = parse_daily_output(_daily())
    assert [b["date"] for b in bars] == ["2026-09-22", "2026-09-23"]          # 오래된 → 최신
    assert bars[-1]["close"] == 82300 and bars[0]["change_rate"] == -0.62      # 부호코드 5 → 음수


def test_store_recent_bars_carries_market_cap_forward():
    db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
               "VALUES('005930','2026-09-19',80000,1,0,500000000000000)")
    store_recent_bars("005930", parse_daily_output(_daily()))
    r = db.query("SELECT market_cap FROM daily_price WHERE ticker='005930' AND trade_date='2026-09-23'")[0]
    assert r["market_cap"] == int(500000000000000 * 82300 / 80000)
    assert db.query("SELECT close FROM ohlc WHERE ticker='005930' AND trade_date='2026-09-23'")[0]["close"] == 82300


def test_sim_ticks_are_tagged_sim():
    marketgen.seed_market(real=False)
    run_sim(interval=0, max_steps=1)
    rows = db.query("SELECT source, session_date FROM realtime_price")
    assert rows and {r["source"] for r in rows} == {"sim"}
    d = client.get("/stocks/005930/detail").json()
    assert d["status"] == "sim" and d["live"] is False


def test_subscribe_top_by_market_cap_not_code_order():
    marketgen.seed_market(real=False)
    top = _top_by_mcap(3)
    caps = {r["ticker"]: r["market_cap"] for r in db.query(
        "SELECT DISTINCT ON (ticker) ticker, market_cap FROM daily_price ORDER BY ticker, trade_date DESC")}
    assert top == sorted(caps, key=lambda t: caps[t], reverse=True)[:3]


# ── 운영: 장외 로그 폭주 방지 · 구독 실패 응답 표시 ──
def test_control_error_detects_kis_failures_only():
    from app.collectors.realtime import control_error
    busy = json.dumps({"header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                       "body": {"rt_cd": "1", "msg_cd": "OPSP8996", "msg1": "ALREADY IN USE appkey"}})
    assert control_error(busy) == ("OPSP8996", "ALREADY IN USE appkey", "005930")
    ok = json.dumps({"header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                     "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"}})
    assert control_error(ok) is None
    assert control_error('{"header":{"tr_id":"PINGPONG"}}') is None
    assert control_error("not json") is None and control_error("[1,2]") is None


class _Stop:
    """n 번 기다리면 멈추는 가짜 stop 이벤트(실제로 기다리지 않음)."""
    def __init__(self, n):
        self.n, self.waits = n, 0

    def is_set(self):
        return self.waits >= self.n

    def wait(self, _secs=None):
        self.waits += 1
        return self.is_set()

    def set(self):
        self.waits = self.n


def _quiet_realtime(monkeypatch, rt):
    from app.config import settings
    monkeypatch.setattr(settings, "KIS_APP_KEY", "test-key-0000")
    monkeypatch.setattr(settings, "KIS_APP_SECRET", "test-secret-0000")
    monkeypatch.setattr(rt, "_refresher", lambda *a, **k: None)          # REST 갱신 스레드는 이 테스트와 무관
    monkeypatch.setattr(rt, "_index_poller", lambda *a, **k: None)


def test_off_hours_wait_logs_once_not_every_minute(monkeypatch, caplog):
    import logging
    from app.collectors import realtime as rt
    _quiet_realtime(monkeypatch, rt)
    monkeypatch.setattr(rt, "_ws_window", lambda now=None: False)
    with caplog.at_level(logging.INFO, logger="app.collectors.realtime"):
        rt.run_realtime(tickers=["005930"], stop=_Stop(30))                 # 장외 30분
    waits = [r for r in caplog.records if "장 시간 외" in r.getMessage()]
    assert len(waits) == 1                                                  # 예전: 30줄(1분마다)


def test_subscription_errors_are_logged_once_per_connection(monkeypatch, caplog):
    import logging
    from app.collectors import realtime as rt
    _quiet_realtime(monkeypatch, rt)
    monkeypatch.setattr(rt, "_ws_window", lambda now=None: True)
    monkeypatch.setattr(rt, "get_approval_key", lambda: "APPROVAL")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    stop = _Stop(1)
    err = json.dumps({"header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                      "body": {"rt_cd": "1", "msg_cd": "OPSP8996", "msg1": "ALREADY IN USE appkey"}})

    class _WS:
        def __init__(self):
            self.msgs, self.sent = [err, err.replace("005930", "000660"), SAMPLE, ""], []

        def send(self, m):
            self.sent.append(m)

        def recv(self):
            return self.msgs.pop(0)

        def close(self):
            pass
    ws = _WS()
    monkeypatch.setattr(rt.websocket, "create_connection", lambda *a, **k: ws)
    with caplog.at_level(logging.INFO, logger="app.collectors.realtime"):
        rt.run_realtime(tickers=["005930", "000660"], stop=stop)            # 빈 메시지 → 끊김 → 대기 1번 → 종료
    errs = [r.getMessage() for r in caplog.records if "응답 오류" in r.getMessage()]
    assert len(errs) == 1 and "ALREADY IN USE appkey" in errs[0]           # 종목마다 오면 1번만
    assert db.query("SELECT price FROM realtime_price WHERE ticker='005930'")[0]["price"] == 71500.0
