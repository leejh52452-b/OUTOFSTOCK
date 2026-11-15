"""토스페이먼츠(웹 결제) — 주문 생성·승인(stub)·멱등·금액검증·소유권."""
from fastapi.testclient import TestClient

from app.api import app
from app.services import wallet

client = TestClient(app)


def _auth(u):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": "Bearer " + tok}, u


def test_config_and_products():
    cfg = client.get("/pay/toss/config").json()
    assert "client_key" in cfg and "enabled" in cfg


def test_prepare_confirm_credits_and_idempotent():
    h, u = _auth("toss1@oos.io")
    b0 = wallet.balance(u)
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()
    assert o["amount"] == 1100 and o["order_id"].startswith("oos_")
    r = client.post("/pay/toss/confirm", headers=h,
                    json={"paymentKey": "test_pk_1", "orderId": o["order_id"], "amount": o["amount"]}).json()
    assert r["ok"] and r["coins"] == 1_000_000 and r["duplicate"] is False
    assert wallet.balance(u) == b0 + 1_000_000
    r2 = client.post("/pay/toss/confirm", headers=h,
                     json={"paymentKey": "test_pk_1", "orderId": o["order_id"], "amount": o["amount"]}).json()
    assert r2["duplicate"] is True                              # 같은 주문 재확인 → 재적립 없음
    assert wallet.balance(u) == b0 + 1_000_000


def test_amount_mismatch_and_cross_user():
    h, u = _auth("toss2@oos.io")
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_medium"}).json()
    assert client.post("/pay/toss/confirm", headers=h,
                       json={"paymentKey": "x", "orderId": o["order_id"], "amount": 999}).status_code == 400
    hb, _ = _auth("toss3@oos.io")
    assert client.post("/pay/toss/confirm", headers=hb,
                       json={"paymentKey": "x", "orderId": o["order_id"], "amount": o["amount"]}).status_code == 400


def test_prepare_requires_login():
    assert client.post("/pay/toss/prepare", json={"product_id": "coins_small"}).status_code == 401


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def _real_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "TOSS_MODE", "real")
    monkeypatch.setattr(settings, "TOSS_SECRET_KEY", "test_sk_dummy")


def test_concurrent_confirms_credit_once_and_never_mark_paid_order_failed(monkeypatch):
    # 결제 완료 화면이 승인 API 를 두 번 부르는 경우(새로고침·React StrictMode) — 늦은 쪽은 토스에서
    # '이미 처리된 결제'를 받는다. 예전엔 그 쪽이 주문을 failed 로 바꿔 먼저 승인된 쪽의 적립이 빠질 수 있었다.
    import threading
    import time

    from app.services import toss
    _real_mode(monkeypatch)
    h, u = _auth("toss4@oos.io")
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()
    calls = []

    def fake_post(_url, **kw):
        calls.append(kw["json"]["orderId"])
        time.sleep(0.3)                                              # 승인 응답이 느린 사이 두 번째 요청이 들어옴
        if len(calls) == 1:
            return _Resp(200, {"status": "DONE"})
        return _Resp(400, {"code": "ALREADY_PROCESSED_PAYMENT", "message": "이미 처리된 결제 입니다."})
    monkeypatch.setattr(toss.requests, "post", fake_post)
    monkeypatch.setattr(toss.requests, "get", lambda *_a, **_k: _Resp(404, {}))
    b0 = wallet.balance(u)
    res = []
    body = {"paymentKey": "pk_dup", "orderId": o["order_id"], "amount": o["amount"]}
    ts = [threading.Thread(target=lambda: res.append(client.post("/pay/toss/confirm", headers=h, json=body)))
          for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(r.status_code for r in res) == [200, 200]
    assert sorted(r.json()["duplicate"] for r in res) == [False, True]
    assert len(calls) == 1                                           # 두 번째는 잠금 뒤 'paid' 를 보고 토스 호출 안 함
    assert wallet.balance(u) == b0 + 1_000_000


def test_failed_then_retried_confirm_credits(monkeypatch):
    from app.services import toss
    _real_mode(monkeypatch)
    h, u = _auth("toss5@oos.io")
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()
    body = {"paymentKey": "pk_retry", "orderId": o["order_id"], "amount": o["amount"]}

    def timeout(*_a, **_k):
        raise toss.requests.Timeout("read timeout")
    monkeypatch.setattr(toss.requests, "post", timeout)
    monkeypatch.setattr(toss.requests, "get", lambda *_a, **_k: _Resp(404, {}))     # 조회해도 승인 기록 없음
    assert client.post("/pay/toss/confirm", headers=h, json=body).status_code == 400   # 일시 오류 → failed
    b0 = wallet.balance(u)
    monkeypatch.setattr(toss.requests, "post", lambda *_a, **_k: _Resp(200, {"status": "DONE"}))
    r = client.post("/pay/toss/confirm", headers=h, json=body).json()
    assert r["ok"] and not r["duplicate"] and wallet.balance(u) == b0 + 1_000_000      # 예전: duplicate·적립 0


def test_already_processed_payment_is_recovered_by_lookup(monkeypatch):
    # 첫 승인 응답을 잃어버린 뒤 재시도 → 토스는 '이미 처리' → 결제 조회(주문번호·금액 일치)로 확인 후 적립
    from app.services import toss
    _real_mode(monkeypatch)
    h, u = _auth("toss6@oos.io")
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()
    monkeypatch.setattr(toss.requests, "post", lambda *_a, **_k: _Resp(400, {"code": "ALREADY_PROCESSED_PAYMENT"}))
    monkeypatch.setattr(toss.requests, "get", lambda url, **_k: _Resp(200, {
        "status": "DONE", "orderId": o["order_id"], "totalAmount": o["amount"]}))
    b0 = wallet.balance(u)
    r = client.post("/pay/toss/confirm", headers=h,
                    json={"paymentKey": "pk_lost", "orderId": o["order_id"], "amount": o["amount"]}).json()
    assert r["ok"] and wallet.balance(u) == b0 + 1_000_000
    o2 = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()
    monkeypatch.setattr(toss.requests, "get", lambda url, **_k: _Resp(200, {
        "status": "DONE", "orderId": "someone_else", "totalAmount": o2["amount"]}))           # 남의 결제키
    assert client.post("/pay/toss/confirm", headers=h,
                       json={"paymentKey": "pk_other", "orderId": o2["order_id"], "amount": o2["amount"]}).status_code == 400


def test_timeout_but_approved_is_recovered_immediately(monkeypatch):
    # 승인 요청이 시간 초과로 끝났지만 토스에선 승인된 경우 → 바로 결제 조회로 확인해 적립(다시 누를 필요 없음)
    from app.services import toss
    _real_mode(monkeypatch)
    h, u = _auth("toss7@oos.io")
    o = client.post("/pay/toss/prepare", headers=h, json={"product_id": "coins_small"}).json()

    def timeout(*_a, **_k):
        raise toss.requests.Timeout("read timeout")
    seen = []

    def lookup(url, **_k):
        seen.append(url)
        return _Resp(200, {"status": "DONE", "orderId": o["order_id"], "totalAmount": o["amount"]})
    monkeypatch.setattr(toss.requests, "post", timeout)
    monkeypatch.setattr(toss.requests, "get", lookup)
    b0 = wallet.balance(u)
    r = client.post("/pay/toss/confirm", headers=h,
                    json={"paymentKey": "pk/../x?y", "orderId": o["order_id"], "amount": o["amount"]}).json()
    assert r["ok"] and wallet.balance(u) == b0 + 1_000_000
    assert seen and seen[0].endswith("/payments/pk%2F..%2Fx%3Fy")             # 결제키는 경로로 해석되지 않게 인코딩
