"""인앱결제(IAP) — Google Play / App Store 결제로 가상머니를 충전.

  · 상품 카탈로그 — 코드 상수(코인 팩). 실화폐 결제는 클라이언트(스토어 SDK)에서 발생하고,
    서버는 그 '영수증(구매 토큰)'을 검증해 가상머니를 지급한다.
  · 영수증 검증 — verify_receipt() 로 분리. stub(개발/데모)은 형식만 확인, strict 는 실제
    스토어 서버 검증 연동 지점(자격증명 필요, 이 배포엔 미연동). → 검증 로직 교체가 쉬움.
  · 재사용(replay) 방지 — receipt UNIQUE + 지갑 멱등키. 같은 영수증은 두 번 지급되지 않는다
    (긴 영수증은 해시 키로 저장, 회원 탈퇴 때도 영수증 기록은 이름만 지우고 남긴다).
가상머니는 게임 내 재화이며 현금 인출/환급 대상이 아니다(에듀테크 시뮬레이션 용도).
"""
import hashlib

from sqlalchemy import text

from app import clock
from app import db
from app.config import settings
from app.services import wallet

RECEIPT_MAX = 20_000            # 스토어 영수증(구매 토큰) 최대 길이 — 애플 영수증은 수 KB

# 상품 카탈로그 — product_id → (표시명, 지급 코인, 실화폐 표시가)
PRODUCTS = {
    "coins_small":  {"name": "가상머니 100만",   "coins": 1_000_000,  "price": "₩1,100"},
    "coins_medium": {"name": "가상머니 550만",   "coins": 5_500_000,  "price": "₩5,500"},
    "coins_large":  {"name": "가상머니 1,200만", "coins": 12_000_000, "price": "₩11,000"},
    "coins_mega":   {"name": "가상머니 3,000만", "coins": 30_000_000, "price": "₩27,500"},
}


class IAPError(Exception):
    """결제/검증 실패(미지원 스토어·잘못된 상품·영수증 검증 실패 등)."""


def _now():
    return clock.iso_now()


def receipt_key(receipt):
    """영수증 → 저장용 키('h:'+SHA-256). 원문은 저장하지 않는다(길이 제한·개인정보 최소화), 재사용 차단엔 해시면 충분.
    예전엔 원문을 그대로 넣어 수 KB 인 애플 영수증은 저장 단계에서 실패했다(실결제 연동 뒤엔 '결제됐는데 충전 안 됨').
    이미 저장된 원문·'sha256:' 형식 영수증은 initdb 마이그레이션이 같은 해시로 바꿔 재사용 차단이 그대로 이어진다."""
    return "h:" + hashlib.sha256(receipt.encode("utf-8", "surrogatepass")).hexdigest()


def products():
    """구매 가능한 코인 팩 목록."""
    return {"items": [{"product_id": pid, **p} for pid, p in PRODUCTS.items()]}


def verify_receipt(store, product_id, receipt):
    """스토어 영수증 검증. (ok, detail) 반환. 실제 검증 연동 지점(교체 용이).

    strict 모드에서 연동할 곳:
      · google → Google Play Developer API purchases.products.get 로 구매토큰 상태 확인
      · apple  → App Store Server API(또는 verifyReceipt)로 영수증 서명·상품 확인
    두 경우 모두 스토어 자격증명(서비스계정 / 공유비밀)이 필요하다.
    """
    if settings.IAP_VERIFY_MODE == "stub":
        return (len(receipt) >= 8, "")      # 데모: 서명 토큰이 존재한다고 가정하고 형식만 확인
    return (False, "스토어 서버 검증 미연동(IAP_VERIFY_MODE=strict) — 스토어 자격증명 연동 필요")


def purchase(username, store, product_id, receipt):
    """결제 영수증을 검증하고 가상머니를 지급. 같은 영수증 재요청은 재지급 없이 성공 반환."""
    store = (store or "").lower()
    if store not in ("google", "apple"):
        raise IAPError("지원하지 않는 스토어입니다 (google/apple)")
    p = PRODUCTS.get(product_id)
    if not p:
        raise IAPError("존재하지 않는 상품입니다")
    if not receipt or len(receipt) < 8 or len(receipt) > RECEIPT_MAX:
        raise IAPError("영수증이 유효하지 않습니다")
    ok, detail = verify_receipt(store, product_id, receipt)
    if not ok:
        raise IAPError(detail or "영수증 검증에 실패했습니다")
    with db.transaction() as conn:
        wallet.ensure(conn, username)
        r = conn.execute(text(
            "INSERT INTO iap_purchase(username, store, product_id, coins, price, receipt, status, created_at) "
            "VALUES(:u,:s,:pid,:c,:pr,:rc,'verified',:t) ON CONFLICT(receipt) DO NOTHING"),
            {"u": username, "s": store, "pid": product_id, "c": p["coins"],
             "pr": p["price"], "rc": receipt_key(receipt), "t": _now()})
        if r.rowcount == 0:                 # 이미 처리된 영수증(replay) → 재지급 없음
            bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                               {"u": username}).scalar()
            return {"ok": True, "duplicate": True, "coins": 0,
                    "balance": int(bal), "product_id": product_id, "name": p["name"]}
        # 멱등키는 영수증 해시(고정 길이) — 실 스토어 토큰이 길어도 지갑 멱등키 80자 한도 초과 방지
        wallet.credit(conn, username, p["coins"], "iap", ref=store, memo=f"{p['name']} 충전",
                      idem_key="iap:" + receipt_key(receipt)[2:])
        bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                           {"u": username}).scalar()
    return {"ok": True, "duplicate": False, "coins": p["coins"],
            "balance": int(bal), "product_id": product_id, "name": p["name"]}


def history(username, limit=20):
    """내 결제 이력."""
    return db.query("SELECT store, product_id, coins, price, status, created_at "
                    "FROM iap_purchase WHERE username=:u ORDER BY purchase_id DESC LIMIT :n",
                    {"u": username, "n": limit})
