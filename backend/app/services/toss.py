"""토스페이먼츠(웹 결제) — 스토어 IAP 대신 웹 PG로 가상머니를 충전.

  · 결제는 클라이언트(토스 결제창)에서, 서버는 승인(confirm) API로 검증 후 지급 — IAP 와 동일 원칙.
  · prepare(주문 생성 pending) → 프론트가 토스 결제창 → confirm(승인 검증) → 지갑 적립. order_id 로 멱등.
  · TOSS_MODE=stub(기본): 토스 API 없이 모의 승인(로컬/테스트). real: 실제 승인 API 호출.
    → .env 에 토스 테스트 키(TOSS_CLIENT_KEY/TOSS_SECRET_KEY) 넣고 TOSS_MODE=real 로 실제 결제 테스트.
가상머니는 게임 내 재화(현금 환급 대상 아님).
"""
import base64
import secrets

import requests
from sqlalchemy import text

from app import clock
from app import crypto, db
from app.config import settings
from app.logging_conf import redact
from app.services import wallet

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"
PAYMENT_URL = "https://api.tosspayments.com/v1/payments/"          # + paymentKey — 결제 조회

# product_id → (표시명, 지급 코인, 결제 금액[원])
PRODUCTS = {
    "coins_small":  {"name": "가상머니 100만",   "coins": 1_000_000,  "amount": 1100},
    "coins_medium": {"name": "가상머니 550만",   "coins": 5_500_000,  "amount": 5500},
    "coins_large":  {"name": "가상머니 1,200만", "coins": 12_000_000, "amount": 11000},
    "coins_mega":   {"name": "가상머니 3,000만", "coins": 30_000_000, "amount": 27500},
}


class TossError(Exception):
    """결제 준비/승인 실패."""


def _now():
    return clock.iso_now()


def config():
    """프론트가 결제창을 띄울지 판단 — 클라이언트 키 + 모드."""
    return {"client_key": settings.TOSS_CLIENT_KEY, "mode": settings.TOSS_MODE,
            "enabled": bool(settings.TOSS_CLIENT_KEY)}


def products():
    return {"items": [{"product_id": pid, **p} for pid, p in PRODUCTS.items()]}


def prepare(username, product_id):
    """결제 주문 생성(pending). 프론트는 이 order_id/amount 로 토스 결제창을 연다."""
    p = PRODUCTS.get(product_id)
    if not p:
        raise TossError("존재하지 않는 상품입니다")
    order_id = "oos_" + secrets.token_hex(12)
    with db.transaction() as conn:
        wallet.ensure(conn, username)
        conn.execute(db.payment_order.insert().values(
            order_id=order_id, username=username, product_id=product_id,
            coins=p["coins"], amount=p["amount"], order_name=p["name"],
            status="pending", payment_key="", created_at=_now(), paid_at=None))
    return {"order_id": order_id, "amount": p["amount"], "order_name": p["name"],
            "coins": p["coins"], "client_key": settings.TOSS_CLIENT_KEY}


def _auth_header():
    return "Basic " + base64.b64encode((settings.TOSS_SECRET_KEY + ":").encode()).decode()


def _lookup_payment(payment_key, order_id, amount):
    """이미 승인된 결제인지 조회(GET /v1/payments/{paymentKey}) — 승인 응답을 잃어버린 뒤(시간 초과·창 닫힘·
    ALREADY_PROCESSED)의 복구용. 주문번호·금액까지 같아야 인정(남의 결제키로 충전 방지).
    결제키는 URL 경로에 그대로 붙이지 않고 인코딩한다('/'·'?' 가 섞인 키로 다른 API 경로를 부르지 못하게)."""
    try:
        r = requests.get(PAYMENT_URL + requests.utils.quote(str(payment_key), safe=""), timeout=10,
                         headers={"Authorization": _auth_header()})
        j = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return False
    if not isinstance(j, dict):
        return False
    try:
        return j.get("status") == "DONE" and j.get("orderId") == order_id and int(j.get("totalAmount")) == int(amount)
    except (TypeError, ValueError):
        return False


_already_done = _lookup_payment                        # 예전 이름(호환)


def _verify_with_toss(payment_key, order_id, amount):
    """토스 승인 API 호출. (ok, detail). stub 모드면 호출 없이 승인 간주."""
    if settings.TOSS_MODE != "real":
        return True, "stub"
    if not settings.TOSS_SECRET_KEY:
        return False, "TOSS_SECRET_KEY 미설정 (real 모드)"
    try:
        r = requests.post(CONFIRM_URL, timeout=10,
                          headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
                          json={"paymentKey": payment_key, "orderId": order_id, "amount": amount})
    except requests.RequestException as e:
        # 응답을 못 받았어도(시간 초과 등) 토스 쪽에선 승인됐을 수 있다 → 결제 조회로 한 번 더 확인
        if _lookup_payment(payment_key, order_id, amount):
            return True, "approved(lookup)"
        return False, redact(f"토스 승인 요청 실패: {type(e).__name__}")
    body = {}
    try:
        body = r.json() or {}
    except ValueError:
        pass
    if not isinstance(body, dict):
        body = {}
    if r.status_code == 200 and body.get("status") in ("DONE", "APPROVED"):
        return True, "approved"
    # 이전 승인 응답을 못 받고(시간 초과·창 닫힘) 다시 승인하면 토스는 '이미 처리된 결제'로 거절한다 → 조회로 확인
    if body.get("code") == "ALREADY_PROCESSED_PAYMENT" and _lookup_payment(payment_key, order_id, amount):
        return True, "approved(already)"
    return False, str(body.get("message") or f"승인 거절 (status {r.status_code})")[:200]


def confirm(username, payment_key, order_id, amount):
    """토스 결제 승인 검증 후 지갑 적립. 같은 주문은 한 번만 적립(멱등).
    주문 행을 잠그고(FOR UPDATE) 승인 → 같은 주문의 동시 승인(새로고침·두 번 호출)은 줄을 서서 두 번째는 '이미 처리'.
    예전: 동시 승인 중 늦은 쪽이 토스의 '이미 처리된 결제' 거절을 받아 주문을 failed 로 바꾸면, 먼저 승인된 쪽의
    적립(status='pending' 조건)이 빠져 '돈은 냈는데 가상머니 없음'이 될 수 있었고, 일시 오류로 failed 가 된 주문은
    다시 승인돼도 적립되지 않았다."""
    amount = int(amount)
    fail, out = None, None
    with db.transaction() as conn:
        # 계정 행부터(FOR SHARE) — 탈퇴(계정 행 FOR UPDATE)와 순서를 맞춘다. 예전엔 승인 중 탈퇴하면 잠금이 엇갈려
        # (주문 행 ↔ 지갑) DB 가 한쪽을 끊었고, 승인이 끊기면 토스 결제는 됐는데 주문·적립 기록이 사라질 수 있었다.
        # 탈퇴가 먼저 끝났으면 토스 승인을 부르기 전에 멈춘다(결제 확정 안 함).
        alive = conn.execute(text("SELECT 1 FROM app_user WHERE username=:u FOR SHARE"), {"u": username}).first()
        o = conn.execute(text("SELECT * FROM payment_order WHERE order_id=:o FOR UPDATE"),
                         {"o": order_id}).mappings().first() if alive else None
        if not alive:
            fail = "계정을 찾을 수 없습니다"
        elif not o:
            fail = "주문을 찾을 수 없습니다"
        elif o["username"] != username:
            fail = "본인 주문이 아닙니다"
        elif o["status"] == "paid":                  # 이미 처리된 주문 → 재적립 없음
            bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"), {"u": username}).scalar()
            out = {"ok": True, "duplicate": True, "coins": 0, "balance": int(bal or 0), "product_id": o["product_id"]}
        elif o["amount"] != amount:                  # 금액 위변조 방지
            fail = "결제 금액이 주문과 일치하지 않습니다"
        else:
            ok, detail = _verify_with_toss(payment_key, order_id, amount)
            if not ok:
                conn.execute(text("UPDATE payment_order SET status='failed' WHERE order_id=:o AND status<>'paid'"),
                             {"o": order_id})
                fail = detail or "결제 승인에 실패했습니다"   # 실패 기록은 커밋하고(아래에서) 오류로 알림
            else:
                conn.execute(text("UPDATE payment_order SET status='paid', payment_key=:k, paid_at=:t "
                                  "WHERE order_id=:o AND status IN ('pending','failed')"),
                             {"k": crypto.encrypt(str(payment_key or "stub")[:200], aad=f"payment_order:{order_id}"),
                              "t": _now(), "o": order_id})        # 결제 키는 암호화 저장(DATA_ENCRYPTION_KEY)
                wallet.credit(conn, username, o["coins"], "iap", ref="toss",
                              memo=f"{o['order_name']} 충전(토스)", idem_key=f"toss:{order_id}")
                bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"), {"u": username}).scalar()
                out = {"ok": True, "duplicate": False, "coins": o["coins"], "balance": int(bal),
                       "product_id": o["product_id"], "name": o["order_name"]}
    if fail:
        raise TossError(fail)
    return out


def history(username, limit=20):
    return db.query("SELECT order_id, product_id, coins, amount, status, created_at, paid_at "
                    "FROM payment_order WHERE username=:u ORDER BY created_at DESC LIMIT :n",
                    {"u": username, "n": limit})
