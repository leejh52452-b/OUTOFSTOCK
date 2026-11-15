"""복식부기 원장 · 일 마감(대사) — 가상머니가 '어디서 와서 어디로 갔는지'를 항상 맞춰 본다.

  분개(post)   지갑 잔액이 바뀔 때마다(wallet.apply·환영금) 같은 트랜잭션 안에서 두 줄:
               사용자 계좌 W<계좌번호> +금액 / 상대 시스템 계정 -금액. 거래마다 합은 0 — DB 제약 트리거가 커밋 시 확인.
  상대 계정    환영금·출석 = sys:promotion(판촉비) · 인앱 충전 = sys:iap_clearing · 토스 = sys:pg_clearing ·
               송금 = sys:transfer_clearing(보내는 쪽·받는 쪽이 같은 계정을 거쳐 0 으로 상쇄) ·
               장기투자 = sys:invest_custody · 모의투자 시즌 = sys:vmarket_custody · 탈퇴 시 남은 잔액 = sys:forfeited · 도입 시점 잔액 = sys:opening_equity ·
               규칙에 없는 사유 = sys:suspense(가계정 — 0 이 아니면 마감 실패)
  일 마감      그날(KST) 분개의 차변·대변 합, 거래별 균형, 전체 시산표(모든 계정 합 = 0), 지갑 잔액 ↔ 계좌 분개 합,
               지갑이 없는 사용자 계좌 0(탈퇴 경합으로 잔액이 붕 뜨지 않았나), 송금 가계정 0, 가계정(suspense) 0 을 확인해
               ledger_close 에 남기고 전날 마감 해시와 잇는다.
               · 마감 해시(canon 2)는 합계뿐 아니라 그날 분개 '한 줄 한 줄'의 다이제스트를 덮는다 — 예전(canon 1)엔 합계만
                 덮어, 계좌 사이에 금액을 옮기는(합계는 그대로) 변조를 못 잡았다(결함 25).
               · 마감 해시는 같은 트랜잭션의 감사 로그(해시 체인 + 외부 증인)에도 남긴다 — 소유자 권한으로 분개와 마감 기록을
                 함께 고쳐 다시 계산해도 감사 체인의 해시와 달라 드러난다.
               · 놓친 날은 다음 실행이 날짜순으로 차례로 마감한다(close_pending). 오늘·미래·이미 마감한 날보다 앞선 날은
                 새로 마감하지 않는다 — 예전엔 스케줄러가 하루 꺼져 있으면 그날은 영영 마감되지 않고 사슬이 그날을 건너뛰었다(결함 24).
               같은 날을 다시 마감하면 새로 계산한 값이 남겨 둔 값과 같은지 본다(지난 분개 변조 탐지).
  개인정보     분개에는 아이디가 없다(계좌번호만). 탈퇴하면 지갑 행이 지워져 계좌번호와 사람의 연결이 사라진다 —
               금액 기록은 보존(원장 무결성)하되 더 이상 그 사람을 가리키지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, time, timedelta

from sqlalchemy import text

from app import clock, db

COUNTER = {
    "welcome": "sys:promotion", "attendance": "sys:promotion", "quiz_reward": "sys:promotion",
    "avatar_buy": "sys:avatar_shop",                                            # 양봉이 꾸미기 매출
    "iap": "sys:iap_clearing", "iap:toss": "sys:pg_clearing",
    "transfer_out": "sys:transfer_clearing", "transfer_in": "sys:transfer_clearing",
    "invest_buy": "sys:invest_custody", "invest_sell": "sys:invest_custody",
    "vmarket_entry": "sys:vmarket_custody", "vmarket_settle": "sys:vmarket_custody",   # 모의투자 시즌 참가금 ↔ 정산
    "forfeit": "sys:forfeited", "opening": "sys:opening_equity",
    "sim_stake": "sys:legacy_games", "sim_settle": "sys:legacy_games",            # 정리된 기능(예전 원장 사유)
    "shop_buy": "sys:legacy_shop", "shop_gift": "sys:legacy_shop",
}
SUSPENSE = "sys:suspense"
ZERO_ACCOUNTS = ("sys:transfer_clearing", SUSPENSE)        # 마감 때 잔액이 0 이어야 하는 계정
GENESIS = "0" * 64


def counter_account(reason: str, ref: str = "") -> str:
    return COUNTER.get(f"{reason}:{ref}") or COUNTER.get(reason) or SUSPENSE


def new_account_no() -> str:
    return "W" + secrets.token_hex(6).upper()


def account_of(conn, username: str) -> str:
    """사용자 지갑의 분개 계정 'W…' — 계좌번호가 없으면(예전 지갑) 여기서 만든다. 같은 트랜잭션에서는 한 번만 조회."""
    memo = db.tx_cache(conn).setdefault("acct", {})
    if username in memo:
        return memo[username]
    row = conn.execute(text("SELECT account_no FROM wallet WHERE username=:u"), {"u": username}).first()
    if row and row[0]:
        memo[username] = row[0]
        return row[0]
    no = new_account_no()
    conn.execute(text("UPDATE wallet SET account_no=:a WHERE username=:u AND account_no IS NULL"), {"a": no, "u": username})
    memo[username] = conn.execute(text("SELECT account_no FROM wallet WHERE username=:u"), {"u": username}).scalar()
    return memo[username]


def post(conn, username: str, amount: int, reason: str, ref: str = "", ledger_id: int | None = None) -> str:
    """두 줄 분개(같은 트랜잭션) — 사용자 계좌 +amount, 상대 계정 -amount. 반환: txn_id.
    시각은 DB 트랜잭션 시각(now()) — 한 업무 트랜잭션의 분개는 모두 같은 시각이다. 예전엔 분개마다 앱 시계를 따로 읽어,
    자정에 걸친 송금(출금 23:59:59.9 · 입금 00:00:00.1)은 송금 가계정이 이틀에 나뉘어 마감이 '가계정 0 아님'으로 실패했다(결함 27)."""
    if int(amount) == 0:
        return ""
    txn = secrets.token_hex(16)
    acct = account_of(conn, username)
    conn.execute(text("INSERT INTO journal_entry(txn_id, account, amount, reason, ledger_id, created_at) "
                      "VALUES(:t, :a, :m, :r, :l, now()), (:t, :c, :neg, :r, NULL, now())"),
                 {"t": txn, "a": acct, "m": int(amount), "r": reason[:20], "l": ledger_id,
                  "c": counter_account(reason, ref), "neg": -int(amount)})
    return txn


_OPENING_LOCK = 0x6F6F6C6F                # 'oolo'


def opening_balances() -> int:
    """도입 시점 잔액을 한 번 분개(sys:opening_equity) — 분개가 없는 지갑만. 반환: 처리한 지갑 수(멱등).
    여러 프로세스(API·스케줄러·실시간)가 동시에 켜지며 함께 부른다 — 트랜잭션 잠금으로 한 번에 하나만(결함 32).
    예전엔 마이그레이션 잠금이 풀린 뒤에 돌아, 둘이 같은 지갑을 '분개 없음'으로 보고 시작 잔액을 두 번 넣을 수 있었다."""
    n = 0
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _OPENING_LOCK})
        rows = conn.execute(text(
            "SELECT w.username, w.balance FROM wallet w WHERE COALESCE(w.balance, 0) <> 0 AND NOT EXISTS "
            "(SELECT 1 FROM journal_entry j WHERE j.account = w.account_no)")).mappings().all()
        for r in rows:
            post(conn, r["username"], int(r["balance"]), "opening")
            n += 1
    return n


def forfeit(conn, username: str) -> int:
    """탈퇴 — 남은 잔액을 소멸 계정으로(분개가 맞게 남도록). 반환: 소멸 금액.
    지갑 행을 잠그고(FOR UPDATE) 읽는다 — 예전엔 잠그지 않고 읽어, 진행 중이던 입금이 그 뒤에 커밋되면 그 금액이
    지갑 없는 계좌에 붕 떠 남았다(결함 23 — 계정 행 KEY SHARE 와 함께 막고, 마감이 '고아 계좌'로 한 번 더 확인)."""
    bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u FOR UPDATE"), {"u": username}).scalar()
    if bal:
        post(conn, username, -int(bal), "forfeit")
    return int(bal or 0)


# ── 일 마감 ─────────────────────────────────────────────────
CANON = 2                                   # 마감 해시 형식 — 1: 합계만 · 2: 합계 + 분개 한 줄 한 줄의 다이제스트
SETTLE = timedelta(minutes=5)               # 자정 직후 커밋되는 그날 분개를 기다리는 여유(스케줄러는 00:20 실행)


class CloseError(ValueError):
    """마감할 수 없는 날짜 — 아직 끝나지 않은 날 · 이미 마감한 날보다 앞선(빠진) 날."""


def _bounds(d: date):
    start = datetime.combine(d, time(0, 0), clock.KST)
    return start, start + timedelta(days=1)


def _totals(d: date) -> dict:
    s, e = _bounds(d)
    p = {"s": s, "e": e}
    day = db.query("SELECT COUNT(*) AS entries, COUNT(DISTINCT txn_id) AS txns, "
                   "COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS debit, "
                   "COALESCE(-SUM(amount) FILTER (WHERE amount < 0), 0) AS credit "
                   "FROM journal_entry WHERE created_at >= :s AND created_at < :e", p)[0]
    by_account = {r["k"]: int(r["v"]) for r in db.query(
        "SELECT CASE WHEN account LIKE 'W%' THEN 'W*' ELSE account END AS k, SUM(amount) AS v FROM journal_entry "
        "WHERE created_at >= :s AND created_at < :e GROUP BY 1 ORDER BY 1", p)}
    return {"entries": int(day["entries"]), "txns": int(day["txns"]), "debit": int(day["debit"]),
            "credit": int(day["credit"]), "by_account": by_account}


def _digest(d: date) -> str:
    """그날 분개 전부(번호순)의 SHA-256 — 번호·거래·계정·금액·사유·시각(UTC, 마이크로초) 한 줄씩. DB 안에서 계산(행을 옮기지 않음)."""
    s, e = _bounds(d)
    return db.query(
        "SELECT encode(sha256(convert_to(COALESCE(string_agg(concat_ws('|', entry_id, txn_id, account, amount, reason, "
        "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US')), E'\\n' ORDER BY entry_id), ''), "
        "'UTF8')), 'hex') AS h FROM journal_entry WHERE created_at >= :s AND created_at < :e", {"s": s, "e": e})[0]["h"]


def _current_checks() -> dict:
    """지금 시점 대사(날짜와 무관 — 한 번 계산해 여러 날 마감에 같이 쓴다)."""
    # 지갑 잔액 ↔ 계좌 분개 합 — 다르면 원장 밖에서 잔액이 바뀐 것
    mismatch = db.query(
        "SELECT w.account_no, w.balance, COALESCE(j.s, 0) AS journal FROM wallet w "
        "LEFT JOIN (SELECT account, SUM(amount) AS s FROM journal_entry GROUP BY account) j ON j.account = w.account_no "
        "WHERE COALESCE(w.balance, 0) <> COALESCE(j.s, 0) LIMIT 20")
    # 지갑이 없는(탈퇴한) 사용자 계좌는 0 이어야 한다 — 0 이 아니면 탈퇴와 진행 중 거래가 엇갈려 돈이 붕 뜬 것(결함 23)
    orphans = db.query(
        "SELECT account, SUM(amount) AS s FROM journal_entry WHERE account LIKE 'W%' AND NOT EXISTS "
        "(SELECT 1 FROM wallet w WHERE w.account_no = journal_entry.account) GROUP BY account HAVING SUM(amount) <> 0 "
        "LIMIT 20")
    return {"wallet_mismatch": [{"account": m["account_no"], "balance": int(m["balance"] or 0),
                                 "journal": int(m["journal"])} for m in mismatch],
            "orphan_accounts": [{"account": o["account"], "journal": int(o["s"])} for o in orphans]}


def _checks(d: date, current: dict | None = None) -> dict:
    s, e = _bounds(d)
    unbalanced = db.query("SELECT txn_id, SUM(amount) AS s FROM journal_entry WHERE created_at >= :s AND created_at < :e "
                          "GROUP BY txn_id HAVING SUM(amount) <> 0 LIMIT 20", {"s": s, "e": e})
    trial = db.query("SELECT COALESCE(SUM(amount), 0) AS s FROM journal_entry WHERE created_at < :e", {"e": e})[0]["s"]
    zero = {a: int(db.query("SELECT COALESCE(SUM(amount), 0) AS s FROM journal_entry WHERE account=:a AND created_at < :e",
                            {"a": a, "e": e})[0]["s"]) for a in ZERO_ACCOUNTS}
    return {"unbalanced_txns": [r["txn_id"] for r in unbalanced], "trial_balance": int(trial),
            **(current if current is not None else _current_checks()), "zero_accounts": zero}


def _canon(d: str, totals: dict, prev: str, digest: str | None = None, version: int = CANON) -> str:
    body = [d, totals["entries"], totals["txns"], totals["debit"], totals["credit"], sorted(totals["by_account"].items())]
    if version >= 2:
        body = [2, *body, digest]
    return json.dumps([*body, prev], ensure_ascii=False, separators=(",", ":"))


def _hash_for(ds: str, prev: str, version: int) -> str:
    d = date.fromisoformat(ds)
    return hashlib.sha256(_canon(ds, _totals(d), prev, _digest(d) if version >= 2 else None, version).encode()).hexdigest()


def _version(checks_json: str | None) -> int:
    try:
        return int(json.loads(checks_json or "{}").get("canon", 1))
    except (ValueError, TypeError, AttributeError):
        return 1


def _last_closed() -> date | None:
    r = db.query("SELECT MAX(close_date) AS d FROM ledger_close")[0]["d"]
    return date.fromisoformat(r) if r else None


def _closable(d: date) -> None:
    if d >= clock.today() or clock.now() < datetime.combine(d + timedelta(days=1), time(0, 0)) + SETTLE:
        raise CloseError(f"{d} 는 아직 끝나지 않은 날이라 마감할 수 없어요 — 어제까지만(자정 {SETTLE.seconds // 60}분 뒤부터)")


def _close(d: date, current: dict | None = None) -> dict:
    """한 날짜 마감(검증 없이) — close_pending·close_day 가 순서·날짜를 확인한 뒤 부른다."""
    ds = d.isoformat()
    totals, digest = _totals(d), _digest(d)
    prev_row = db.query("SELECT close_hash FROM ledger_close WHERE close_date < :d ORDER BY close_date DESC LIMIT 1",
                        {"d": ds})
    prev = prev_row[0]["close_hash"] if prev_row else GENESIS
    h = hashlib.sha256(_canon(ds, totals, prev, digest).encode()).hexdigest()
    checks = _checks(d, current)
    ok = (not checks["unbalanced_txns"] and checks["trial_balance"] == 0 and not checks["wallet_mismatch"]
          and not checks["orphan_accounts"] and all(v == 0 for v in checks["zero_accounts"].values())
          and totals["debit"] == totals["credit"])
    status = "ok" if ok else "fail"
    from app.services import audit
    with db.transaction() as conn:                 # 마감 기록과 감사 기록(해시 포함)을 함께 — 감사 체인이 마감 해시를 붙잡는다
        conn.execute(text("INSERT INTO ledger_close(close_date, status, entries, txns, debit, credit, checks, prev_hash, "
                          "close_hash, closed_at) VALUES(:d, :st, :en, :tx, :dr, :cr, :ck, :p, :h, :n)"),
                     {"d": ds, "st": status, "en": totals["entries"], "tx": totals["txns"], "dr": totals["debit"],
                      "cr": totals["credit"], "p": prev, "h": h, "n": clock.utc(),
                      "ck": json.dumps({**checks, "by_account": totals["by_account"], "canon": CANON, "digest": digest},
                                       ensure_ascii=False)})
        audit.record("ledger.close", "system", target=ds, conn=conn,
                     detail={"status": status, "entries": totals["entries"], "close_hash": h})
    if not ok:
        from app.security import events
        from app.services import alerts
        alerts.notify(f"ledger:close:{ds}", f"[원장] {ds} 마감 실패", json.dumps(checks, ensure_ascii=False)[:1500],
                      level="error")
        events.emit("ledger_close_fail", "critical", day=ds)        # → 돈이 오가는 기능 자동 중지(events.RULES)
    return {"date": ds, "status": status, **totals, "checks": checks, "close_hash": h}


def _recheck(ds: str) -> dict:
    """이미 마감한 날 — 다시 계산해 남긴 해시와 같은지(변조 탐지)만."""
    row = db.query("SELECT prev_hash, close_hash, status, checks FROM ledger_close WHERE close_date=:d", {"d": ds})[0]
    same = _hash_for(ds, row["prev_hash"], _version(row["checks"])) == row["close_hash"]
    if not same:
        from app.security import events
        events.emit("ledger_tamper", "critical", day=ds)
    return {"date": ds, "already_closed": True, "unchanged": same, "status": row["status"]}


def close_pending(until: date | str | None = None) -> list[dict]:
    """마감하지 않은 날을 날짜순으로 차례로 마감(until 까지, 기본 어제) — 스케줄러(매일 00:20)가 부른다.
    서버가 며칠 꺼져 있었어도 다음 실행이 빠진 날을 모두 메운다. 처음이면 첫 분개가 있는 날부터."""
    until = date.fromisoformat(until) if isinstance(until, str) else (until or clock.today() - timedelta(days=1))
    _closable(until)
    last = _last_closed()
    if last:
        start = last + timedelta(days=1)
    else:
        first = db.query("SELECT MIN((created_at AT TIME ZONE 'Asia/Seoul')::date) AS d FROM journal_entry")[0]["d"]
        start = min(date.fromisoformat(str(first)[:10]), until) if first else until   # 날짜는 'YYYY-MM-DD' 로 읽힌다
    out, current, d = [], None, start
    while d <= until:
        current = current or _current_checks()      # 지금 시점 대사는 한 번만(여러 날을 메울 때)
        out.append(_close(d, current))
        d += timedelta(days=1)
    return out


def close_day(d: date | str | None = None) -> dict:
    """그날 마감 — 이미 마감했으면 다시 계산해 남긴 값과 비교(변조 탐지)만 한다.
    아직 마감하지 않은 날이면 그날까지 빠진 날을 날짜순으로 함께 마감한다(사슬이 날짜를 건너뛰지 않게).
    오늘·미래(그날 분개가 아직 쌓이는 중)와 이미 마감한 날보다 앞선 날(사슬 중간에 끼울 수 없다)은 CloseError."""
    d = date.fromisoformat(d) if isinstance(d, str) else (d or clock.today() - timedelta(days=1))
    ds = d.isoformat()
    if db.query("SELECT 1 FROM ledger_close WHERE close_date=:d", {"d": ds}):
        return _recheck(ds)
    _closable(d)
    last = _last_closed()
    if last and d < last:
        raise CloseError(f"{ds} 는 이미 마감한 {last} 보다 앞선 날이라 새로 마감할 수 없어요(사슬 중간에 끼울 수 없음) — "
                         "ledger-verify 의 빠진 날로 남습니다")
    return close_pending(d)[-1]


def verify_closes() -> dict:
    """지난 마감 전부 다시 계산 — 해시 사슬, 각 날짜의 합계·분개 다이제스트, 감사 체인에 남긴 마감 해시(앵커),
    날짜 빠짐(gaps)을 본다. 반환 {ok, days, broken[], gaps[], anchored}."""
    rows = db.query("SELECT close_date, prev_hash, close_hash, checks FROM ledger_close ORDER BY close_date")
    anchors = {}
    for a in db.query("SELECT target, detail FROM audit_log WHERE action='ledger.close' ORDER BY audit_id"):
        try:
            h = json.loads(a["detail"] or "{}").get("close_hash")
        except (ValueError, AttributeError):
            h = None
        if h:
            anchors[a["target"]] = h
    prev, broken, gaps, anchored, last = GENESIS, [], [], 0, None
    for r in rows:
        ds = r["close_date"]
        d = date.fromisoformat(ds)
        if last is not None and d != last + timedelta(days=1):
            gaps.extend((last + timedelta(days=i)).isoformat() for i in range(1, (d - last).days))
        if r["prev_hash"] != prev or r["close_hash"] != _hash_for(ds, prev, _version(r["checks"])):
            broken.append(ds)
        elif ds in anchors:
            if anchors[ds] != r["close_hash"]:              # 마감 기록을 다시 계산해 바꿔 넣었다 — 감사 체인과 다름
                broken.append(ds)
            else:
                anchored += 1
        prev, last = r["close_hash"], d
    return {"ok": not broken and not gaps, "days": len(rows), "broken": broken, "gaps": gaps, "anchored": anchored}


def trial_balance() -> dict:
    """현재 시산표 — 계정별 합(사용자 계좌는 합쳐서 W*), 전체 합은 0."""
    rows = db.query("SELECT CASE WHEN account LIKE 'W%' THEN 'W*' ELSE account END AS k, SUM(amount) AS v "
                    "FROM journal_entry GROUP BY 1 ORDER BY 1")
    acc = {r["k"]: int(r["v"]) for r in rows}
    return {"accounts": acc, "total": sum(acc.values())}
