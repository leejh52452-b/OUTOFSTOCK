"""모의투자(가상 시장 시즌) — 서버가 시계·체결가·정산을 정하고(팀 시안은 브라우저 localStorage), 미래 봉은 내보내지 않는다.

시계 규칙(팀 invest.html 과 같음) · 공개된 봉까지만 · 시즌 파일 검사·변경 거부 · 참가 = 지갑 출금(원장·분개) ·
현재가 즉시 체결(옛 봉 409·재전송 1번·초과 거부·동시 주문 줄 세움) · 주말 정산 = 마지막 종가 매도 → 지갑(1번) ·
계정 격리(RLS) · 탈퇴 시 삭제."""
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.config import settings
from app.security import rls
from app.services import account, auth, vmarket as V, wallet

client = TestClient(app)
PW = "Vmarket-pass-2026"
ROOT = Path(__file__).resolve().parents[1]
MON = datetime(2026, 11, 9)                    # 시즌 2 월요일


def at(day_offset, h, m=0, s=0):
    return MON.replace(day=MON.day + day_offset, hour=h, minute=m, second=s)


@pytest.fixture
def seasons():
    out = V.seed()
    assert out["new"] == 10 and not out["errors"], out
    return out


@pytest.fixture
def now(monkeypatch):
    box = {"t": at(0, 10, 5)}
    monkeypatch.setattr(clock, "now", lambda: box["t"])
    return box


def _user(name, topup=0):
    auth.register(name, PW)
    if topup:
        wallet.credit_once(name, topup, "iap", memo="시험 충전")
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _close(season, k, revealed):
    return V.payload(season)["stocks"][k]["candles"][V.HIST + revealed - 1][3]


# ── 시계(팀 clockOf 와 같은 규칙) ─────────────────────────────
@pytest.mark.parametrize("t, want", [
    (at(0, 8, 59, 59), ("pre", 0, 0)), (at(0, 9, 0), ("open", 1, 1)), (at(0, 9, 29, 59), ("open", 1, 1)),
    (at(0, 9, 30), ("open", 2, 2)), (at(0, 15, 0), ("open", 13, 13)), (at(0, 15, 29, 59), ("open", 13, 13)),
    (at(0, 15, 30), ("closed", 13, 13)), (at(1, 9, 0), ("open", 1, 14)), (at(4, 15, 30), ("closed", 13, 65)),
    (at(5, 10, 0), ("weekend", 13, 65)), (at(6, 23, 59), ("weekend", 13, 65)),
])
def test_clock_rules(t, want):
    c = V.clock_of(t)
    assert (c["phase"], c["today"], c["revealed"]) == want and c["id"] == 2


def test_season_numbering_and_next_change():
    assert V.clock_of(datetime(2026, 11, 2, 0, 0))["id"] == 1 and V.clock_of(datetime(2026, 11, 8, 23, 0))["id"] == 1
    assert V.clock_of(datetime(2026, 11, 1, 12, 0))["id"] == 0 and str(V.week_start(10)) == "2027-01-04"
    assert V.next_change(at(0, 8, 0)) == at(0, 9, 0)
    assert V.next_change(at(0, 9, 10)) == at(0, 9, 30)
    assert V.next_change(at(0, 15, 10)) == at(0, 15, 30)
    assert V.next_change(at(0, 15, 40)) == at(1, 0, 0)            # 다음 날 0시(장 시작 전으로 바뀜)
    assert V.next_change(at(4, 15, 40)) == at(5, 0, 0)            # 금요일 마감 → 토요일 0시(결과 발표)
    assert V.next_change(at(5, 10, 0)) == at(7, 0, 0)             # 주말 → 다음 월요일 0시(새 시즌)


# ── 시즌 데이터 ───────────────────────────────────────────
def test_future_bars_never_leave_the_server(seasons, now):
    now["t"] = at(1, 10, 5)                                       # 화요일 10:05 → 13 + 3 = 16봉
    r = client.get("/vmarket/season")
    d = r.json()
    assert r.status_code == 200 and d["season"] == 2 and d["revealed"] == 16 and not d["ended"]
    assert {len(s["candles"]) for s in d["stocks"]} == {V.HIST + 16}
    assert all("scenario" not in s and "sector" not in s for s in d["stocks"])           # 장세는 끝난 뒤에만
    full = V.payload(2)["stocks"][0]["candles"]
    assert d["stocks"][0]["candles"][-1] == full[V.HIST + 15] and full[V.HIST + 16] not in d["stocks"][0]["candles"][-3:]
    assert d["clock"]["next_change_at"] == "2026-11-10T10:30:00"
    assert client.get("/vmarket/season?id=3").status_code == 404                        # 다음 시즌은 아직
    p = client.get("/vmarket/season?id=1").json()                                       # 지난 시즌은 전부 + 장세
    assert p["ended"] and p["revealed"] == 65 and len(p["stocks"][0]["candles"]) == 185 and p["stocks"][0]["scenario"]
    etag = r.headers["etag"]
    assert client.get("/vmarket/season", headers={"If-None-Match": etag}).status_code == 304   # 같은 봉 수 = 같은 응답
    now["t"] = at(1, 10, 30)
    assert client.get("/vmarket/season", headers={"If-None-Match": etag}).status_code == 200   # 새 봉
    assert not list((ROOT / "web").rglob("season_*")), "시즌 파일(미래 봉)을 정적 파일로 내보내지 않는다"


def test_import_validates_files_and_refuses_to_change_a_started_season(seasons, now, tmp_path):
    base = json.loads((V.DATA_DIR / "season_02.json").read_text(encoding="utf-8"))
    assert V.import_season(base, "season_02.json") == "same"
    bad = []
    b = json.loads(json.dumps(base)); b["stocks"] = b["stocks"][:4]; bad.append(b)                     # 종목 4개
    b = json.loads(json.dumps(base)); b["stocks"][0]["candles"][5][1] = 1; bad.append(b)               # 고가 < 저가
    b = json.loads(json.dumps(base)); b["stocks"][1]["candles"] = b["stocks"][1]["candles"][:-1]; bad.append(b)
    b = json.loads(json.dumps(base)); b["stocks"][2]["candles"][0][3] = 100.5; bad.append(b)           # 정수 아님
    b = json.loads(json.dumps(base)); b["stocks"][3]["name"] = "<img src=x>"; bad.append(b)            # 화면에 들어갈 이름
    b = json.loads(json.dumps(base)); b["stocks"][4]["scenario"] = "moon"; bad.append(b)
    for x in bad:
        with pytest.raises(V.VMarketError):
            V.import_season(x, "bad.json")
    changed = json.loads(json.dumps(base))
    changed["stocks"][0]["candles"][-1][3] += 10
    changed["stocks"][0]["candles"][-1][1] = max(changed["stocks"][0]["candles"][-1][1], changed["stocks"][0]["candles"][-1][3])
    with pytest.raises(V.VMarketError, match="이미 시작"):                                             # 시즌 2 는 시작함
        V.import_season(changed, "season_02.json")
    nxt = json.loads((V.DATA_DIR / "season_05.json").read_text(encoding="utf-8"))
    nxt["stocks"][0]["candles"][-1][2] -= 10
    assert V.import_season(nxt, "season_05_v2.json") == "replaced"                                   # 아직 안 시작한 시즌은 교체 가능
    js = tmp_path / "season_11.js"                                                                     # 팀 화면용 .js 형식도 읽는다
    eleven = dict(base, season=11)
    js.write_text("window.SEASON_DATA = window.SEASON_DATA || {};\nwindow.SEASON_DATA[11] = " + json.dumps(eleven) + ";\n",
                  encoding="utf-8")
    assert V.import_season(V.parse_file(js), js.name) == "new" and V.status(at(0, 10))["last_ready"] == 11
    db.execute("UPDATE vmarket_season SET payload = replace(payload, '\"종목 A\"', '\"종목 Z\"') WHERE season_id = 3")
    V.clear_cache()
    assert V.payload(3) is None                                                                        # 저장 뒤 변조 — 쓰지 않는다


# ── 참가·주문·정산 ────────────────────────────────────────
def test_join_order_settle_flow_with_wallet_ledger(seasons, now):
    h = _user("vm-flow", topup=5_000_000)
    bal0 = wallet.balance("vm-flow")
    now["t"] = at(0, 8, 30)                                                        # 월요일 장 시작 전 — 참가 가능, 주문 불가
    r = client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 999_999})
    assert r.status_code == 400 and "1,000,000" in r.json()["detail"]
    r = client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 2_000_000})
    assert r.status_code == 200 and r.json()["balance"] == bal0 - 2_000_000
    st = r.json()["current"]
    assert st["entry"] == st["cash"] == 2_000_000 and st["joinedAt"] == 0 and len(st["pos"]) == 5
    assert client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 1_000_000}).status_code == 400   # 시즌당 한 번
    o = {"season": 2, "stock": 0, "side": "buy", "qty": 10, "bar": 0}
    assert client.post("/vmarket/order", headers=h, json=o).status_code == 400     # 장 시작 전
    now["t"] = at(0, 10, 5)                                                        # 3봉 공개
    assert client.post("/vmarket/order", headers=h, json={**o, "bar": 2}).status_code == 409   # 화면이 본 봉이 옛것
    px = _close(2, 0, 3)
    r = client.post("/vmarket/order", headers={**h, "Idempotency-Key": "k1"}, json={**o, "bar": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["filled"]["price"] == px and d["current"]["cash"] == 2_000_000 - 10 * px
    assert d["current"]["pos"][0] == {"shares": 10, "avg": px, "realized": 0}
    assert d["current"]["trades"][-1] == {"type": "buy", "qty": 10, "price": px, "i": V.HIST + 2, "k": 0, "auto": False}
    again = client.post("/vmarket/order", headers={**h, "Idempotency-Key": "k1"}, json={**o, "bar": 3}).json()
    assert again["duplicate"] and again["current"]["pos"][0]["shares"] == 10                   # 재전송은 1번만
    big = client.post("/vmarket/order", headers=h, json={**o, "bar": 3, "qty": 10 ** 8})
    assert big.status_code == 400 and "현금" in big.json()["detail"]
    sell = client.post("/vmarket/order", headers=h, json={**o, "bar": 3, "side": "sell", "qty": 11})
    assert sell.status_code == 400 and "보유" in sell.json()["detail"]
    assert client.post("/vmarket/order", headers=h, json={**o, "bar": 3, "side": "hold"}).status_code == 400
    now["t"] = at(1, 9, 40)                                                         # 화요일 2봉 → 15
    px2 = _close(2, 0, 15)
    r = client.post("/vmarket/order", headers=h, json={**o, "bar": 15, "side": "sell", "qty": 4}).json()
    assert r["current"]["pos"][0]["shares"] == 6 and abs(r["current"]["realized"] - (px2 - px) * 4) < 1e-6
    b2 = client.post("/vmarket/order", headers=h, json={"season": 2, "stock": 3, "side": "buy", "qty": 5, "bar": 15}).json()
    cash_before_settle = b2["current"]["cash"]
    me = client.get("/vmarket/me", headers=h).json()
    assert me["season"] == 2 and me["current"]["cash"] == cash_before_settle and me["unsettled"] == []
    now["t"] = at(4, 15, 35)                                                        # 금요일 장마감 — 아직 정산 전
    assert client.post("/vmarket/settle", headers=h).json()["settled"] == []
    assert client.post("/vmarket/order", headers=h, json={**o, "bar": 65}).status_code == 400
    now["t"] = at(5, 9, 0)                                                          # 토요일 — 결과 발표
    assert client.get("/vmarket/me", headers=h).json()["unsettled"] == [2]
    last0, last3 = _close(2, 0, 65), _close(2, 3, 65)
    final = cash_before_settle + 6 * last0 + 5 * last3
    r = client.post("/vmarket/settle", headers=h).json()
    assert r["settled"] == [{"season": 2, "final": final, "auto_sold": 11, "auto_stocks": 2, "balance": bal0 - 2_000_000 + final}]
    assert wallet.balance("vm-flow") == bal0 - 2_000_000 + final
    assert client.post("/vmarket/settle", headers=h).json()["settled"] == []        # 두 번째는 할 일 없음
    assert V.settle_all(at(5, 9, 0)) == {"settled": 0, "failed": 0}
    me = client.get("/vmarket/me", headers=h).json()
    assert me["history"] == [{"id": 2, "entry": 2_000_000, "final": final}] and me["unsettled"] == []
    assert me["current"]["settled"] and [t["auto"] for t in me["current"]["trades"]][-2:] == [True, True]
    rows = db.query("SELECT reason, amount, ledger_id FROM wallet_ledger WHERE username='vm-flow' AND reason LIKE 'vmarket%' "
                    "ORDER BY ledger_id")
    assert [(x["reason"], x["amount"]) for x in rows] == [("vmarket_entry", -2_000_000), ("vmarket_settle", final)]
    for x in rows:                                                                  # 분개: 사용자 ↔ sys:vmarket_custody, 합 0
        je = db.query("SELECT account, amount FROM journal_entry WHERE txn_id = "
                      "(SELECT txn_id FROM journal_entry WHERE ledger_id = :l)", {"l": x["ledger_id"]})
        assert sum(j["amount"] for j in je) == 0 and "sys:vmarket_custody" in {j["account"] for j in je}
    assert wallet.reconcile("vm-flow")["ok"]
    logs = client.get("/me/logs/wallet?filter=invest", headers=h).json()["items"]
    assert {i["reason_ko"] for i in logs} >= {"모의투자 참가금", "모의투자 정산"}


def test_join_rules(seasons, now):
    h = _user("vm-join")                                                            # 환영금만 — 참가금보다 적을 수 있다
    small = wallet.balance("vm-join")
    r = client.post("/vmarket/join", headers=h, json={"season": 2, "entry": max(small + 1, settings.VMARKET_MIN_ENTRY)})
    assert r.status_code == 400 and "부족" in r.json()["detail"] and wallet.balance("vm-join") == small
    assert not db.query("SELECT 1 FROM vmarket_entry WHERE username='vm-join'")                  # 출금 실패 = 참가도 취소
    assert client.post("/vmarket/join", headers=h, json={"season": 3, "entry": 1_000_000}).status_code == 409
    now["t"] = at(5, 10, 0)
    assert client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 1_000_000}).status_code == 409   # 주말
    db.execute("DELETE FROM vmarket_season WHERE season_id = 3")
    V.clear_cache()
    now["t"] = at(7, 10, 0)                                                         # 시즌 3 — 데이터 없음
    r = client.post("/vmarket/join", headers=h, json={"season": 3, "entry": 1_000_000})
    assert r.status_code == 400 and "준비 중" in r.json()["detail"]
    assert client.get("/vmarket/season").json()["available"] is False


def test_login_required_for_personal_and_money_routes(seasons):
    assert client.get("/vmarket/me").status_code == 401
    assert client.post("/vmarket/join", json={"season": 2, "entry": 1_000_000}).status_code in (401, 403)
    assert client.post("/vmarket/order", json={"season": 2, "stock": 0, "side": "buy", "qty": 1, "bar": 1}).status_code in (401, 403)
    assert client.post("/vmarket/settle").status_code in (401, 403)
    assert client.get("/vmarket/season").status_code == 200                        # 차트는 공개


def test_concurrent_orders_cannot_overspend(seasons, now):
    h = _user("vm-race", topup=2_000_000)
    client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 1_000_000})
    px = _close(2, 1, 3)
    qty = 1_000_000 // px                                                           # 한 번에 현금 전부
    codes = []

    def go():
        codes.append(client.post("/vmarket/order", headers=h,
                                 json={"season": 2, "stock": 1, "side": "buy", "qty": qty, "bar": 3}).status_code)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 400, 400, 400], codes
    e = db.query("SELECT cash FROM vmarket_entry WHERE username='vm-race'")[0]
    assert e["cash"] == 1_000_000 - qty * px and e["cash"] >= 0


def test_rows_are_isolated_per_account_and_deleted_with_the_account(seasons, now):
    ha = _user("vm-a", topup=1_000_000)
    _user("vm-b", topup=1_000_000)
    client.post("/vmarket/join", headers=ha, json={"season": 2, "entry": 1_000_000})
    client.post("/vmarket/order", headers=ha, json={"season": 2, "stock": 2, "side": "buy", "qty": 1, "bar": 3})
    with rls.acting_as("vm-b"):
        for t in ("vmarket_entry", "vmarket_position", "vmarket_trade"):
            assert db.query(f"SELECT count(*) AS n FROM {t}")[0]["n"] == 0, t      # 남의 행은 DB 가 숨긴다
    with rls.acting_as("vm-a"):
        assert db.query("SELECT count(*) AS n FROM vmarket_position")[0]["n"] == 5
    account.delete_account("vm-a")
    for t in ("vmarket_entry", "vmarket_position", "vmarket_trade"):
        assert db.query(f"SELECT count(*) AS n FROM {t} WHERE username='vm-a'")[0]["n"] == 0


def test_scheduler_settles_ended_seasons_for_everyone(seasons, now):
    users = []
    for i in range(3):
        h = _user(f"vm-s{i}", topup=1_500_000)
        client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 1_000_000 + i})
        client.post("/vmarket/order", headers=h, json={"season": 2, "stock": i, "side": "buy", "qty": 3, "bar": 3})
        users.append(f"vm-s{i}")
    assert V.settle_all(at(4, 15, 59)) == {"settled": 0, "failed": 0}               # 금요일엔 아직
    assert V.status(at(5, 0, 1))["unsettled"] == 3
    out = V.settle_all(at(5, 0, 5))
    assert out == {"settled": 3, "failed": 0}
    for i, u in enumerate(users):
        fin = db.query("SELECT final FROM vmarket_entry WHERE username=:u", {"u": u})[0]["final"]
        assert fin == 1_000_000 + i - 3 * _close(2, i, 3) + 3 * _close(2, i, 65)
        assert wallet.reconcile(u)["ok"]
    assert V.settle_all(at(5, 0, 10)) == {"settled": 0, "failed": 0}


def test_status_and_next_season_alert(seasons, now, monkeypatch):
    st = V.status(at(0, 10))
    assert st["current_ready"] and st["next_ready"] and st["last_ready"] == 10 and st["weeks_left"] == 8
    sent = []
    monkeypatch.setattr("app.services.alerts.notify", lambda *a, **k: sent.append((a, k)) or {})
    V.check_upcoming(datetime(2027, 1, 8, 18, 0))                                    # 시즌 10 금요일 — 11 없음
    assert sent and "시즌 11" in sent[0][0][2]
    from app.services import monitoring
    assert "vmarket_next_season_missing" not in monitoring.feature_status()["issues"]


def test_orders_are_rate_limited_per_account(seasons, now, monkeypatch):
    monkeypatch.setattr(settings, "VMARKET_ORDERS_PER_MIN", 3)
    h = _user("vm-rate", topup=1_000_000)
    client.post("/vmarket/join", headers=h, json={"season": 2, "entry": 1_000_000})
    o = {"season": 2, "stock": 0, "side": "buy", "qty": 1, "bar": 3}
    codes = [client.post("/vmarket/order", headers=h, json=o).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert db.query("SELECT count(*) AS n FROM vmarket_trade WHERE username='vm-rate'")[0]["n"] == 3
