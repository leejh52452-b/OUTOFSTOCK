"""P2P 송금 — 친구(다른 계정)에게 가상머니를 보낸다.

  · 원자성 — 보내는 쪽 출금과 받는 쪽 입금을 한 트랜잭션으로. 잔액 부족이면 둘 다 롤백.
  · 멱등성 — 하나의 송금 키로 출금/입금 두 원장에 각각 멱등키를 부여(재전송해도 1회).
  · 방어 — 자기 자신 송금 금지, 미존재 계정 금지, 금액/한도 검증.
  · 이상거래탐지 — 한 트랜잭션 안에서 순서가 정해져 있다(결함 20·21):
      계정 확인(KEY SHARE) → 두 지갑 잠금(이름순) → 같은 키 처리 여부 → FDS 판정 → 출금·입금·판정 기록·사건
    잠금 뒤에 판정하므로 동시에 보낸 송금도 서로를 보고 채점되고, 이미 처리된 재시도는 다시 채점하지 않고 처음 결과를 준다.
    판정이 실행이 아니면(추가 인증·차단) TransferHeld — 돈은 움직이지 않고(롤백), 판정은 롤백 뒤 따로 남는다.
"""
import hashlib
import secrets

from sqlalchemy import text

from app import db
from app.config import settings
from app.services import wallet


class TransferError(Exception):
    """송금 규칙 위반(잔액 부족·자기송금·상대 없음·한도 초과 등)."""


class TransferConflict(TransferError):
    """같은 Idempotency-Key 로 내용(받는 사람·금액)이 다른 송금 — 409."""


class TransferHeld(TransferError):
    """이상거래탐지가 추가 인증(step_up)을 요구하거나 막은(block) 송금 — 아무것도 반영되지 않았다."""

    def __init__(self, verdict: dict):
        super().__init__(verdict["decision"])
        self.verdict = verdict


def _key(sender, client_key):
    """클라이언트 멱등키 → 원장 키. 보낸 사람으로 이름공간을 나누고 해시(32자) — 다른 사람이 같은 키를 써도
    받는 쪽 원장(ti:키)에서 충돌해 입금이 '이미 처리'로 빠지는 일이 없게, 컬럼 길이(80)도 넘지 않게."""
    return hashlib.sha256(f"{sender}\x00{client_key}".encode()).hexdigest()[:32]


def send(sender, to, amount, memo="", idem_key=None, *, risk=frozenset(), fresh=False, screen=True):
    """송금. idem_key(클라이언트의 Idempotency-Key)가 같으면 몇 번을 보내도 1번만 처리하고 처음 결과를 돌려준다.
    두 지갑을 아이디 순서로 잠근 뒤 처리 → A→B·B→A 가 동시에 와도 교착 없이 차례로 처리된다.
    risk·fresh: 세션 위험 신호·최근 재인증 여부(FDS 입력). screen=False 는 시험·이관용 — 판정 없이 실행된 송금은
    데이터 계약(wallet.transfer_unscreened)이 매일 잡아낸다(기본은 항상 판정)."""
    to = (to or "").strip()
    amount = int(amount)
    memo = (memo or "").strip()
    if not sender:
        raise TransferError("로그인이 필요합니다")
    if not to:
        raise TransferError("받는 사람을 입력하세요")
    if to == sender:
        raise TransferError("자기 자신에게는 송금할 수 없습니다")
    if amount <= 0:
        raise TransferError("송금액은 1 이상이어야 합니다")
    if amount > settings.TRANSFER_MAX:
        raise TransferError(f"1회 송금 한도({settings.TRANSFER_MAX:,}원)를 초과했습니다")
    if len(memo) > 200:
        raise TransferError("메모는 200자까지 쓸 수 있어요")
    from app.security import dlp                          # 메모에 섞인 주민등록번호·카드번호는 저장 전에 가린다
    memo, masked = dlp.guard(memo, "transfer.memo", sender)
    if not db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": to}):
        raise TransferError("받는 사람 계정을 찾을 수 없습니다")
    key = _key(sender, idem_key) if idem_key else secrets.token_hex(8)
    from app.security import rls
    from app.services import fds
    try:
        # 받는 사람 지갑·원장(남의 행)에 입금해야 한다 — 이 트랜잭션만 RLS 상승(사유: transfer). 보내는 쪽은 인가된 주체.
        with rls.elevated("transfer"), db.transaction() as conn:
            # 받는 계정을 트랜잭션 안에서 다시 확인하고 탈퇴와 줄 세운다(KEY SHARE — 삭제만 막고 로그인 시각 갱신은 막지 않음).
            # 예전엔 확인 직후 상대가 탈퇴하면 없는 계정 이름으로 지갑이 새로 생겨 돈이 들어갔고, 나중에 같은 아이디로
            # 가입한 사람이 그 잔액을 물려받았다.
            try:
                wallet.hold(conn, to)                     # 같은 트랜잭션의 지갑 확인은 이 잠금을 다시 잡지 않는다(tx_cache)
            except wallet.WalletError:
                raise TransferError("받는 사람 계정을 찾을 수 없습니다") from None
            wallet.lock(conn, sender, to)                 # 두 지갑을 이름순으로 잠금(맞송금 교착 방지) — 없으면 만든다
            done = conn.execute(text("SELECT amount, ref FROM wallet_ledger WHERE username=:u AND idem_key=:k"),
                                {"u": sender, "k": f"to:{key}"}).mappings().first()
            if done and (int(done["amount"]) != -amount or done["ref"] != to):
                raise TransferConflict("같은 요청 키로 내용이 다른 송금은 할 수 없어요(새로 보내려면 새 키)")
            if not done:
                # 판정은 잠금·멱등 확인 '뒤'에 — 같은 사람의 동시 송금은 위 잠금에서 줄을 서 있으므로 앞 송금이 반영된
                # 상태로 채점된다. 이미 처리된 재시도는 여기까지 오지 않는다(처음 결과를 그대로 준다).
                verdict = fds.screen(conn, sender, to, amount, risk, fresh) if screen else None
                if verdict and not fds.executes(verdict):
                    raise TransferHeld(verdict)           # 롤백 — 판정 기록은 아래 except 에서 따로
                wallet.debit(conn, sender, amount, "transfer_out", ref=to,
                             memo=memo or f"{to} 님에게 송금", idem_key=f"to:{key}")
                wallet.credit(conn, to, amount, "transfer_in", ref=sender,
                              memo=memo or f"{sender} 님이 보냄", idem_key=f"ti:{key}")
                if verdict:                               # 실행된 송금과 판정은 함께 커밋(ref = 송금 키)
                    fds.record(verdict, conn=conn, ref=key)
            bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                               {"u": sender}).scalar()
            if not done:                                  # 같은 트랜잭션에 사건 기록(아웃박스) — 계좌번호만(아이디 없음)
                from app.services import ledger, outbox
                outbox.publish(conn, "transfer.completed",
                               {"ref": key, "amount": amount, "from_acct": ledger.account_of(conn, sender),
                                "to_acct": ledger.account_of(conn, to)})
    except TransferHeld as e:
        fds.record(e.verdict)                             # 막힌 시도도 남긴다(롤백 뒤 자동 커밋) — 차단이면 보안 이벤트
        raise
    except wallet.WalletError as e:
        raise TransferError(str(e))
    return {"ok": True, "amount": amount, "from": sender, "to": to,
            "balance": int(bal), "memo": memo, "duplicate": bool(done), **({"masked": masked} if masked else {})}


def history(username, limit=30):
    """내 송금 이력(보냄/받음) — 원장에서 추출. ref=상대 계정."""
    rows = db.query(
        "SELECT reason, amount, balance_after, ref AS counterparty, memo, created_at "
        "FROM wallet_ledger WHERE username=:u AND reason IN ('transfer_in','transfer_out') "
        "ORDER BY ledger_id DESC LIMIT :n", {"u": username, "n": limit})
    for r in rows:
        r["direction"] = "in" if r["reason"] == "transfer_in" else "out"
    return rows
