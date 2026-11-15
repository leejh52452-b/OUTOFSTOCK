"""가상머니 지갑 — 추가 기능(출석·인앱결제·단기시뮬·송금·아바타·장기투자) 공용 코어.

설계 원칙(기존 sim/game/practice 와 동일 계열):
  · 단일 진실원장 — 모든 잔액 변동은 wallet_ledger 에 1건씩 append. '잔액 == 원장 합'.
  · 원자성 — 잔액 갱신 + 원장 기록을 한 트랜잭션으로(호출자 트랜잭션에 합류 가능).
  · 멱등성 — (username, idem_key) 로 재전송 방지(더블클릭·네트워크 재시도 → 1회).
  · 동시성 — 지갑 행을 FOR UPDATE 로 잠근 뒤 처리(동시 요청은 줄을 서서 차례로) + version CAS 이중 확인.
             여러 지갑은 아이디 순서로 잠가 교착(deadlock)을 막는다.
  · 무결성 — 출금은 잔액 부족 시 거부(음수 잔액 불가).
  · 복식부기 — 잔액이 바뀔 때마다 같은 트랜잭션에서 분개 두 줄(사용자 계좌 ↔ 상대 시스템 계정, 합 0)을 남긴다
               (app/services/ledger.py). 일 마감이 잔액·원장·분개를 서로 대사한다.
반드시 로그인 계정(username)에 귀속된다. 지갑은 최초 접근 시 자동 생성(환영금 1회).
"""

from sqlalchemy import text

from app import clock
from app import db
from app.config import settings

# 원장 사유 → 한글 라벨(마이페이지/거래내역 표시용).
REASON_KO = {
    "welcome": "가입 환영금", "attendance": "출석 보상", "quiz_reward": "퀴즈 보상", "avatar_buy": "양봉이 꾸미기", "iap": "충전(인앱결제)",
    "sim_stake": "단기시뮬 베팅", "sim_settle": "단기시뮬 정산",
    "transfer_in": "송금 받음", "transfer_out": "송금 보냄",
    "shop_buy": "아바타 구매", "shop_gift": "아바타 선물", "invest_buy": "장기투자 매수",
    "invest_sell": "장기투자 매도", "vmarket_entry": "모의투자 참가금", "vmarket_settle": "모의투자 정산",
}


class WalletError(Exception):
    """지갑 규칙 위반(잔액 부족·잘못된 금액·동시성 충돌 등)."""


def _now():
    return clock.iso_now()


def idem(prefix, key):
    """클라이언트 멱등키 → 원장 idem_key(80자 이하). db.idem_key 와 같다."""
    return db.idem_key(prefix, key)


def hold(conn, username):
    """계정 행을 KEY SHARE 로 잡는다(이 트랜잭션이 끝날 때까지) — 탈퇴(계정 행 FOR UPDATE)와 줄을 세운다. 없으면 WalletError.
    · 진행 중이던 입출금이 먼저면 탈퇴는 그 커밋을 기다렸다가 최신 잔액을 소멸 처리한다.
    · 탈퇴가 먼저면 늦게 온 입출금은 '계정 없음'으로 실패한다 — 예전엔 지갑을 새로 만들어(환영금까지) 돈이 지갑 없는 계좌에
      붕 뜨거나, 나중에 같은 아이디로 가입한 사람이 물려받았다(결함 23).
    잠금 순서(교착 방지): 계정 → 업무 행(포지션·주문) → 지갑. KEY SHARE 는 삭제·키 변경만 막고 로그인 시각 갱신 같은
    일반 UPDATE 는 막지 않는다."""
    if not username:
        raise WalletError("로그인이 필요합니다")
    held = db.tx_cache(conn).setdefault("held", set())
    if username in held:                                  # 이 트랜잭션이 이미 잡았다 — 잠금은 커밋까지 유지된다
        return
    if not conn.execute(text("SELECT 1 FROM app_user WHERE username=:u FOR KEY SHARE"), {"u": username}).first():
        raise WalletError("계정을 찾을 수 없습니다")
    held.add(username)


def ensure(conn, username):
    """지갑이 없으면 생성(+환영금 1회). 호출자 트랜잭션 안에서 실행. 계정 행을 먼저 잡는다(hold).
    ON CONFLICT DO NOTHING 로 경합에 안전 — 실제로 생성한 트랜잭션만 환영금 원장을 남긴다.
    같은 트랜잭션에서 두 번째부터는 아무것도 하지 않는다(송금 한 건이 같은 확인을 여러 번 하던 것 — SQL 41 → 33)."""
    done = db.tx_cache(conn).setdefault("ensured", set())
    if username in done:
        return
    hold(conn, username)
    done.add(username)
    bonus = settings.WALLET_WELCOME_BONUS
    from app.services import ledger
    r = conn.execute(text("INSERT INTO wallet(username, balance, version, updated_at, account_no) "
                          "VALUES(:u, :b, 0, :t, :a) ON CONFLICT(username) DO NOTHING"),
                     {"u": username, "b": max(0, bonus), "t": _now(), "a": ledger.new_account_no()})
    if r.rowcount == 1 and bonus > 0:
        res = conn.execute(db.wallet_ledger.insert().values(
            username=username, reason="welcome", amount=bonus, balance_after=bonus,
            ref="", memo="가입 환영 가상머니", idem_key=f"welcome:{username}", created_at=_now()))
        ledger.post(conn, username, bonus, "welcome", ledger_id=res.inserted_primary_key[0])


def lock(conn, *usernames):
    """지갑 행 잠금(SELECT … FOR UPDATE) — 같은 지갑을 건드리는 요청은 이 트랜잭션이 끝날 때까지 줄을 선다.
    여러 지갑(송금)은 아이디 순서로 잠가 A→B·B→A 동시 송금이 교착(deadlock)되지 않게 한다.
    반환: {username: {"balance", "version"}}"""
    if not usernames or not all(usernames):
        raise WalletError("로그인이 필요합니다")
    names = sorted(set(usernames))
    for u in names:
        ensure(conn, u)
    rows = conn.execute(text("SELECT username, balance, version FROM wallet WHERE username = ANY(:us) "
                             "ORDER BY username FOR UPDATE"), {"us": names}).mappings().all()
    return {r["username"]: {"balance": int(r["balance"]), "version": r["version"]} for r in rows}


def apply(conn, username, amount, reason, ref="", memo="", idem_key=None):
    """잔액에 amount(+입금/-출금)를 반영하고 원장에 1건 기록(핵심 프리미티브).
    지갑 행을 먼저 잠그고(FOR UPDATE) 멱등키를 확인한다 → 동시에 온 같은 요청도 정확히 1번만 반영.
    같은 (username, idem_key) 가 이미 있으면 재적용하지 않고 현재 잔액을 반환(exactly-once)."""
    ensure(conn, username)
    # 행 잠금(FOR UPDATE) — 같은 지갑에 대한 동시 입출금을 줄 세운다. 예전엔 낙관적 잠금(version)만 있어
    # 동시 송금 20건 중 17건이 '동시 요청 충돌(OCC)'로 실패했다(돈은 맞았지만 사용자는 실패를 봄).
    # version 조건은 안전망으로 유지.
    w = conn.execute(text("SELECT balance, version FROM wallet WHERE username=:u FOR UPDATE"),
                     {"u": username}).mappings().first()
    if idem_key:                                         # 멱등 확인은 잠근 뒤에 — 같은 키 동시 재전송도 '이미 처리'로
        seen = conn.execute(text("SELECT ledger_id FROM wallet_ledger "        # 답한다(예전: 잠금 전 확인 → 둘 다
                                 "WHERE username=:u AND idem_key=:k"),          # 통과 → 늦은 쪽은 제약 위반 409)
                            {"u": username, "k": idem_key}).mappings().first()
        if seen:
            return {"balance": int(w["balance"]), "applied": False, "ledger_id": seen["ledger_id"]}
    new_bal = int(w["balance"]) + int(amount)
    if new_bal < 0:
        raise WalletError("잔액이 부족합니다")
    r = conn.execute(text("UPDATE wallet SET balance=:b, version=version+1, updated_at=:t "
                          "WHERE username=:u AND version=:v"),
                     {"b": new_bal, "t": _now(), "u": username, "v": w["version"]})
    if r.rowcount == 0:
        raise WalletError("다른 요청과 겹쳤어요 — 잠시 후 다시 시도해 주세요")
    res = conn.execute(db.wallet_ledger.insert().values(
        username=username, reason=reason, amount=int(amount), balance_after=new_bal,
        ref=str(ref or "")[:60], memo=(memo or "")[:200], idem_key=idem_key, created_at=_now()))
    from app.services import ledger, stream
    ledger.post(conn, username, int(amount), reason, ref=str(ref or ""), ledger_id=res.inserted_primary_key[0])
    stream.notify(conn, username, "wallet", {"balance": new_bal})          # 커밋되면 열린 화면의 잔액이 바로 바뀐다
    return {"balance": new_bal, "applied": True, "ledger_id": res.inserted_primary_key[0]}


def credit(conn, username, amount, reason, ref="", memo="", idem_key=None):
    """입금(+). 호출자 트랜잭션 안에서."""
    amount = int(amount)
    if amount <= 0:
        raise WalletError("입금액은 1 이상이어야 합니다")
    return apply(conn, username, amount, reason, ref, memo, idem_key)


def debit(conn, username, amount, reason, ref="", memo="", idem_key=None):
    """출금(-). 잔액 부족 시 WalletError. 호출자 트랜잭션 안에서."""
    amount = int(amount)
    if amount <= 0:
        raise WalletError("출금액은 1 이상이어야 합니다")
    return apply(conn, username, -amount, reason, ref, memo, idem_key)


def credit_once(username, amount, reason, ref="", memo="", idem_key=None):
    """단발 입금(자체 트랜잭션)."""
    with db.transaction() as conn:
        return credit(conn, username, amount, reason, ref, memo, idem_key)


def debit_once(username, amount, reason, ref="", memo="", idem_key=None):
    """단발 출금(자체 트랜잭션)."""
    with db.transaction() as conn:
        return debit(conn, username, amount, reason, ref, memo, idem_key)


def balance(username):
    """현재 잔액(없으면 생성 후 0 또는 환영금). 조회는 읽기만 — 지갑이 있으면 SELECT 한 번(예전엔 조회마다 계정 잠금 +
    INSERT … ON CONFLICT 를 실행해 GET 이 쓰기를 했다). 지갑이 없을 때만(첫 조회) 만든다."""
    with db.transaction() as conn:
        b = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"), {"u": username}).scalar()
        if b is not None:
            return int(b)
        ensure(conn, username)
        return int(conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                                {"u": username}).scalar())


def ledger(username, limit=50):
    """최근 거래원장(내림차순). 사유 한글 라벨 포함."""
    rows = db.query("SELECT ledger_id, reason, amount, balance_after, ref, memo, created_at "
                    "FROM wallet_ledger WHERE username=:u ORDER BY ledger_id DESC LIMIT :n",
                    {"u": username, "n": limit})
    for r in rows:
        r["reason_ko"] = REASON_KO.get(r["reason"], r["reason"])
    return rows


def reconcile(username):
    """정합성 대사 — 저장된 잔액 ↔ 원장 합계 ↔ 복식부기 계좌 분개 합(세 값이 모두 같아야 한다)."""
    with db.transaction() as conn:
        ensure(conn, username)
        bal, acct = conn.execute(text("SELECT balance, account_no FROM wallet WHERE username=:u"),
                                 {"u": username}).first()
        s = conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM wallet_ledger WHERE username=:u"),
                         {"u": username}).scalar()
        j = conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM journal_entry WHERE account=:a"),
                         {"a": acct}).scalar()
    return {"balance": int(bal), "ledger_sum": int(s), "journal_sum": int(j),
            "ok": int(bal) == int(s) == int(j)}


def overview(username, ledger_limit=20):
    """지갑 요약 — 잔액 + 최근 거래내역(마이페이지/지갑 화면·/me 공용)."""
    return {"username": username, "balance": balance(username),
            "ledger": ledger(username, ledger_limit)}
