"""2026-10 전체 검토에서 나온 '돈이 움직이는 곳' 결함 회귀 테스트.

동시 요청은 실제 스레드 + 별도 DB 연결로 재현한다(행 잠금이 없으면 이중 지급·이중 차감이 난다)."""
import threading

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.collectors import marketgen
from app.services import account, attendance, iap, invest, toss, transfer, wallet
from app.services import auth as auth_svc

client = TestClient(app)


def _user(u, topup=50_000_000):
    auth_svc.register(u, "pass1234")
    if topup:
        wallet.credit_once(u, topup, "iap", memo="테스트 충전")
    return u


def _hdr(u):
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _race(n, fn):
    """fn(i) 를 n 개 스레드로 '동시에' 실행 → (결과 리스트, 예외 리스트)."""
    gate, out, errs = threading.Barrier(n), [None] * n, []

    def run(i):
        gate.wait()
        try:
            out[i] = fn(i)
        except Exception as e:                  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out, errs


# ── 장기 투자: 같은 포지션 동시 매도 ────────────────────────
def test_parallel_full_sells_pay_once():
    marketgen.seed_market(real=False)
    u = _user("race-sell@oos.io")
    pid = invest.buy(u, "005930", 10)["position_id"]
    before = wallet.balance(u)
    out, errs = _race(6, lambda i: invest.sell(u, pid))
    ok = [o for o in out if o]
    assert len(ok) == 1 and all(isinstance(e, invest.InvestError) for e in errs)
    assert wallet.balance(u) == before + ok[0]["proceeds"]          # 한 번만 지급
    assert wallet.reconcile(u)["ok"]
    row = db.query("SELECT status, quantity FROM invest_position WHERE position_id=:p", {"p": pid})[0]
    assert row["status"] == "closed" and row["quantity"] == 0


def test_parallel_partial_sells_never_go_negative():
    marketgen.seed_market(real=False)
    u = _user("race-part@oos.io")
    pid = invest.buy(u, "005930", 10)["position_id"]
    out, _errs = _race(4, lambda i: invest.sell(u, pid, 9))
    assert sum(1 for o in out if o) == 1                               # 9주는 한 번만 팔린다
    assert db.query("SELECT quantity FROM invest_position WHERE position_id=:p", {"p": pid})[0]["quantity"] == 1


def test_sell_quantity_zero_rejected_and_realized_pnl_accumulates(monkeypatch):
    marketgen.seed_market(real=False)
    u = _user("pnl@oos.io")
    h = _hdr(u)
    monkeypatch.setattr(invest, "_current_price", lambda t, live=None: 1000)
    pid = invest.buy(u, "005930", 10)["position_id"]
    r = client.post(f"/invest/{pid}/sell", headers=h, json={"quantity": 0})
    assert r.status_code == 400                                        # 예전엔 0 = 전량 매도
    monkeypatch.setattr(invest, "_current_price", lambda t, live=None: 1500)
    assert invest.sell(u, pid, 4)["realized_pnl"] == 2000
    summ = invest.positions(u)["summary"]
    assert summ["realized_pnl"] == 2000 and summ["open_count"] == 1    # 일부 매도분도 실현손익에 잡힘
    invest.sell(u, pid, 6)
    pos = invest.positions(u)
    assert pos["summary"]["realized_pnl"] == 5000                      # 예전엔 마지막 3000 만
    c = pos["closed"][0]
    assert c["quantity"] == 10 and c["sell_price"] == 1500 and c["realized_pnl"] == 5000
    assert c["realized_pct"] == 50.0


# ── 송금: 멱등키 · 교착 ────────────────────────────────────
def test_transfer_idempotency_header():
    a, b = _user("tx-a@oos.io"), _user("tx-b@oos.io", topup=0)
    h = _hdr(a)
    b0 = wallet.balance(b)
    body = {"to": b, "amount": 1234, "memo": "점심"}
    r1 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json=body).json()
    r2 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json=body).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert wallet.balance(b) == b0 + 1234                              # 두 번 보내도 1번
    r3 = client.post("/transfer", headers={**h, "Idempotency-Key": "click-1"}, json={**body, "amount": 99})
    assert r3.status_code == 409                                       # 같은 키 + 다른 내용
    long_key = "k" * 300
    assert client.post("/transfer", headers={**h, "Idempotency-Key": long_key}, json=body).status_code == 422
    ok_key = "x" * 150                                                 # 긴 키는 해시로 저장(500 아님)
    assert client.post("/transfer", headers={**h, "Idempotency-Key": ok_key}, json=body).status_code == 200


def test_parallel_idempotent_transfer_and_crossing_transfers():
    a, b = _user("x-a@oos.io"), _user("x-b@oos.io")
    ba, bb = wallet.balance(a), wallet.balance(b)
    out, errs = _race(5, lambda i: transfer.send(a, b, 1000, idem_key="same-click"))
    assert not errs and sum(1 for o in out if not o["duplicate"]) == 1
    assert wallet.balance(b) == bb + 1000
    out, errs = _race(8, lambda i: transfer.send(a, b, 10) if i % 2 else transfer.send(b, a, 10))
    assert not errs                                                    # A→B·B→A 동시에도 교착 500 없음
    assert wallet.balance(a) == ba - 1000 and wallet.balance(b) == bb + 1000
    assert wallet.reconcile(a)["ok"] and wallet.reconcile(b)["ok"]


def test_transfer_memo_too_long_is_422_not_500():
    a, b = _user("m-a@oos.io"), _user("m-b@oos.io", topup=0)
    r = client.post("/transfer", headers=_hdr(a), json={"to": b, "amount": 1, "memo": "가" * 201})
    assert r.status_code == 422


# ── 레거시 모의투자 멱등키는 세션마다 ───────────────────────


# ── 아바타: 구매와 선물이 겹쳐도 한 명만 결제 ─────────────


# ── 단기 시뮬: 정산 1번 · 끝난 라운드 주문 거절 · 이어하기 ─────


# ── 출석: 동시 체크인도 1번 지급, 500 없음 ─────────────────
def test_parallel_check_in_rewards_once():
    u = _user("att-race@oos.io", topup=0)
    b0 = wallet.balance(u)
    out, errs = _race(5, lambda i: attendance.check_in(u, today="2026-10-01"))
    assert not errs and sum(1 for o in out if o["claimed"]) == 1
    assert wallet.balance(u) == b0 + attendance.settings.ATTENDANCE_DAILY_REWARD


# ── 결제: 토스 승인 재시도·동시 승인 ───────────────────────
def test_toss_failed_then_approved_retry_credits(monkeypatch):
    u = _user("toss-r@oos.io", topup=0)
    o = toss.prepare(u, "coins_small")
    b0 = wallet.balance(u)
    monkeypatch.setattr(toss, "_verify_with_toss", lambda *a: (False, "시간 초과"))
    try:
        toss.confirm(u, "pk1", o["order_id"], o["amount"])
    except toss.TossError:
        pass
    assert db.query("SELECT status FROM payment_order WHERE order_id=:o", {"o": o["order_id"]})[0]["status"] == "failed"
    monkeypatch.setattr(toss, "_verify_with_toss", lambda *a: (True, "approved"))
    r = toss.confirm(u, "pk1", o["order_id"], o["amount"])
    assert r["duplicate"] is False and r["coins"] == 1_000_000       # 예전엔 duplicate + 0코인
    assert wallet.balance(u) == b0 + 1_000_000


def test_toss_concurrent_confirms_credit_once_and_paid_not_overwritten(monkeypatch):
    u = _user("toss-c@oos.io", topup=0)
    o = toss.prepare(u, "coins_small")
    calls = []

    def verify(*a):
        calls.append(1)
        return (True, "approved") if len(calls) == 1 else (False, "ALREADY_PROCESSED")
    monkeypatch.setattr(toss, "_verify_with_toss", verify)
    b0 = wallet.balance(u)
    _out, _errs = _race(3, lambda i: toss.confirm(u, "pk", o["order_id"], o["amount"]))
    assert wallet.balance(u) == b0 + 1_000_000
    assert db.query("SELECT status FROM payment_order WHERE order_id=:o", {"o": o["order_id"]})[0]["status"] == "paid"
    assert len(calls) == 1                                             # 나머지는 paid 를 보고 멈춤


# ── 인앱결제: 긴 영수증 · 탈퇴 후 재사용 ───────────────────
def test_iap_long_receipt_and_replay_after_account_deletion():
    u = _user("iap-del@oos.io", topup=0)
    receipt = "r" * 5000                                               # 실제 애플 영수증처럼 긴 토큰
    assert iap.purchase(u, "apple", "coins_small", receipt)["duplicate"] is False
    stored = db.query("SELECT receipt FROM iap_purchase")[0]["receipt"]
    assert stored.startswith("h:") and len(stored) == 66               # 원문 대신 해시
    account.delete_account(u)
    v = _user("iap-new@oos.io", topup=0)
    assert iap.purchase(v, "apple", "coins_small", receipt)["duplicate"] is True   # 탈퇴해도 재사용 불가
    r = client.post("/iap/purchase", headers=_hdr(v), json={"store": "apple", "product_id": "coins_small",
                                                           "receipt": "q" * 20_001})
    assert r.status_code == 422


# ── 레거시 백테스트: 종료 후 매매 거절 · 동시 매수 · 사라진 날짜 ─────


def test_wallet_errors_map_to_json_not_500():
    u = _user("we@oos.io", topup=0)
    h = _hdr(u)
    r = client.post("/invest/buy", headers=h, json={"ticker": "005930", "quantity": 10_000_000})
    assert r.status_code == 400 and r.json()["detail"]
