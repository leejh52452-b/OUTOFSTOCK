"""복식부기 원장·일 마감(app/services/ledger.py · V012) — 잔액 ↔ 원장 ↔ 분개 대사, DB 수준 균형·추가만 보장."""
import secrets
import threading
import time as _time
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.services import account, auth, ledger, wallet

client = TestClient(app)
PW = "Ld-pass-2026"


def _h(u):
    auth.register(u, PW)
    tok = client.post("/auth/login", json={"username": u, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_every_wallet_change_posts_a_balanced_pair_and_reconciles():
    ha, _hb = _h("ld-a"), _h("ld-b")
    client.get("/wallet", headers=ha)                                      # 환영금
    client.post("/attendance/check-in", headers=ha)                        # 출석 보상
    assert client.post("/transfer", headers=ha, json={"to": "ld-b", "amount": 12_345}).status_code == 200
    for u in ("ld-a", "ld-b"):
        r = wallet.reconcile(u)
        assert r["ok"] and r["balance"] == r["ledger_sum"] == r["journal_sum"], r
    unbalanced = db.query("SELECT txn_id FROM journal_entry GROUP BY txn_id HAVING SUM(amount) <> 0")
    assert unbalanced == []
    tb = ledger.trial_balance()
    assert tb["total"] == 0 and tb["accounts"].get("sys:transfer_clearing", 0) == 0     # 송금 가계정은 상쇄
    assert not db.query("SELECT 1 FROM journal_entry WHERE account LIKE '%ld-%'")       # 분개엔 아이디가 없다


def test_database_rejects_unbalanced_and_edited_entries():
    with pytest.raises(Exception, match="분개 불균형"):
        with db.transaction() as conn:                                      # 한 줄만 넣고 커밋 → 제약 트리거가 거부
            conn.execute(db.journal_entry.insert().values(txn_id="t" * 32, account="sys:promotion", amount=100,
                                                          reason="x", created_at=clock.utc()))
    _h("ld-c")
    wallet.balance("ld-c")
    with pytest.raises(Exception, match="추가만"):
        db.execute("UPDATE journal_entry SET amount = amount + 1")
    with pytest.raises(Exception, match="추가만"):
        db.execute("DELETE FROM journal_entry")


def test_account_deletion_forfeits_balance_and_books_stay_balanced():
    h = _h("ld-del")
    bal = client.get("/wallet", headers=h).json()["balance"]
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200   # 탈퇴는 항상 비밀번호 재확인
    assert client.delete("/me", headers=h).json()["ok"]
    forfeited = db.query("SELECT amount FROM journal_entry WHERE account='sys:forfeited'")
    assert forfeited and forfeited[0]["amount"] == bal                     # 남은 잔액은 소멸 계정으로
    assert ledger.trial_balance()["total"] == 0


def _tomorrow(monkeypatch):
    """'내일'로 시계를 옮긴다 — 오늘 쌓은 분개를 마감해 보기 위해(마감은 끝난 날만 받는다)."""
    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(days=1))


def _book(day, pairs, reason="attendance"):
    """지난 날짜의 분개를 직접 넣는다(시스템 계정끼리 — 지갑 대사와 무관). pairs: [(차변 계정, 대변 계정, 금액)]."""
    at = datetime.combine(day, time(10, 0), clock.KST)
    with db.transaction() as conn:
        for dr, cr, amt in pairs:
            t = secrets.token_hex(16)
            conn.execute(db.journal_entry.insert(), [
                {"txn_id": t, "account": dr, "amount": amt, "reason": reason, "created_at": at},
                {"txn_id": t, "account": cr, "amount": -amt, "reason": reason, "created_at": at}])


def _owner_sql(*stmts):
    with db.transaction() as conn:
        conn.exec_driver_sql("ALTER TABLE journal_entry DISABLE TRIGGER trg_journal_append_only")
        conn.exec_driver_sql("ALTER TABLE ledger_close DISABLE TRIGGER trg_close_append_only")
        for st in stmts:
            conn.exec_driver_sql(st)
        conn.exec_driver_sql("ALTER TABLE journal_entry ENABLE TRIGGER trg_journal_append_only")
        conn.exec_driver_sql("ALTER TABLE ledger_close ENABLE TRIGGER trg_close_append_only")


def test_daily_close_is_chained_and_detects_tampering(as_owner, monkeypatch):
    h = _h("ld-close")
    client.get("/wallet", headers=h)
    today = clock.today()
    _tomorrow(monkeypatch)
    r = ledger.close_day(today)
    assert r["status"] == "ok" and r["debit"] == r["credit"] > 0 and r["checks"]["wallet_mismatch"] == []
    again = ledger.close_day(today)
    assert again["already_closed"] and again["unchanged"]
    assert ledger.verify_closes() == {"ok": True, "days": 1, "broken": [], "gaps": [], "anchored": 1}
    with pytest.raises(Exception, match="추가만"):
        db.execute("UPDATE ledger_close SET status='ok'")
    # 트리거를 끌 수 있는 권한(소유자)을 가진 내부자가 지난 분개 금액을 바꾼 경우
    with db.transaction() as conn:
        conn.exec_driver_sql("ALTER TABLE journal_entry DISABLE TRIGGER trg_journal_append_only")
        conn.exec_driver_sql("UPDATE journal_entry SET amount = amount * 2 WHERE account LIKE 'W%%'")
        conn.exec_driver_sql("UPDATE journal_entry SET amount = amount * 2 WHERE account = 'sys:promotion'")
        conn.exec_driver_sql("ALTER TABLE journal_entry ENABLE TRIGGER trg_journal_append_only")
    assert ledger.close_day(today)["unchanged"] is False                   # 다시 계산한 합계가 마감 기록과 다름
    assert ledger.verify_closes()["broken"] == [today.isoformat()]
    assert db.query("SELECT 1 FROM security_event WHERE kind='ledger_tamper'")


def test_unknown_reason_goes_to_suspense_and_fails_the_close(monkeypatch):
    _h("ld-sus")
    with db.transaction() as conn:
        wallet.credit(conn, "ld-sus", 500, "mystery_bonus")                # 규칙에 없는 사유
    today = clock.today()
    _tomorrow(monkeypatch)
    r = ledger.close_day(today)
    assert r["status"] == "fail" and r["checks"]["zero_accounts"]["sys:suspense"] == -500


# ── 일 마감 — 놓친 날·오늘·변조(결함 24·25) ─────────────────
def test_missed_days_are_closed_in_order_and_unfinished_days_are_refused():
    """스케줄러가 며칠 꺼져 있어도 다음 실행이 빠진 날을 날짜순으로 모두 메운다 — 예전엔 '어제'만 마감해 그 사이 날은
    영영 마감되지 않았고 사슬이 그날을 건너뛰었다. 오늘·미래는 분개가 아직 쌓이는 중이라 거부."""
    today = clock.today()
    for back in (5, 3, 2):                                                   # 4일 전은 분개 없는 날(빈 마감도 사슬에 든다)
        _book(today - timedelta(days=back), [("sys:promotion", "sys:iap_clearing", 1000 * back)])
    done = ledger.close_pending()
    assert [r["date"] for r in done] == [(today - timedelta(days=b)).isoformat() for b in (5, 4, 3, 2, 1)]
    assert all(r["status"] == "ok" for r in done)
    v = ledger.verify_closes()
    assert v["ok"] and v["days"] == 5 and v["gaps"] == [] and v["anchored"] == 5
    assert ledger.close_pending() == []                                     # 다시 돌려도 할 일 없음(멱등)
    for bad in (today, today + timedelta(days=3)):
        with pytest.raises(ledger.CloseError):
            ledger.close_day(bad)
    with pytest.raises(ledger.CloseError):                                   # 사슬 앞쪽(이미 마감한 날보다 앞)엔 끼울 수 없다
        ledger.close_day(today - timedelta(days=9))


def test_verify_reports_days_skipped_by_older_versions():
    """예전 버전이 남긴 빠진 날은 검증이 '빠진 날'로 드러낸다(깨진 날과 구분)."""
    today = clock.today()
    for back in (4, 2):
        ledger._close(today - timedelta(days=back))                         # 예전 동작: 날짜를 건너뛰어 마감
    v = ledger.verify_closes()
    assert not v["ok"] and v["broken"] == [] and v["gaps"] == [(today - timedelta(days=3)).isoformat()]


def test_aggregate_preserving_swap_is_invisible_to_totals_but_caught_by_digest(as_owner):
    """합계는 그대로 두고 거래 사이에서 금액을 옮기는 변조 — 합계만 덮던 해시(canon 1)는 못 잡았다(결함 25)."""
    d = clock.today() - timedelta(days=1)
    _book(d, [("sys:promotion", "sys:iap_clearing", 1000), ("sys:promotion", "sys:iap_clearing", 500)])
    ledger.close_pending()
    before = ledger._totals(d)
    ids = [r["entry_id"] for r in db.query("SELECT entry_id FROM journal_entry ORDER BY entry_id")]
    # 두 거래의 금액을 맞바꾼다(1000↔500) — 거래마다 균형, 계정별 합·건수·차변·대변 모두 그대로
    _owner_sql(f"UPDATE journal_entry SET amount = 500 WHERE entry_id = {ids[0]}",
               f"UPDATE journal_entry SET amount = -500 WHERE entry_id = {ids[1]}",
               f"UPDATE journal_entry SET amount = 1000 WHERE entry_id = {ids[2]}",
               f"UPDATE journal_entry SET amount = -1000 WHERE entry_id = {ids[3]}")
    assert ledger._totals(d) == before                                       # 합계만 보면 아무 일도 없었다
    assert ledger.verify_closes()["broken"] == [d.isoformat()]               # 분개 다이제스트가 잡는다


def test_rewritten_close_record_is_caught_by_the_audit_anchor(as_owner):
    """소유자 권한 내부자가 분개를 고치고 마감 기록까지 '맞게' 다시 계산해 넣어도 — 같은 트랜잭션에 감사 체인
    (외부 증인으로 고정)에 남긴 마감 해시와 달라 드러난다."""
    d = clock.today() - timedelta(days=1)
    _book(d, [("sys:promotion", "sys:iap_clearing", 1000)])
    ledger.close_pending()
    eid = db.query("SELECT MIN(entry_id) AS i FROM journal_entry")[0]["i"]
    _owner_sql(f"UPDATE journal_entry SET amount = 900 WHERE entry_id = {eid}",
               f"UPDATE journal_entry SET amount = -900 WHERE entry_id = {eid + 1}")
    forged = ledger._hash_for(d.isoformat(), ledger.GENESIS, ledger.CANON)    # 고친 분개로 다시 계산한 '맞는' 해시
    _owner_sql(f"UPDATE ledger_close SET close_hash = '{forged}' WHERE close_date = '{d.isoformat()}'")
    v = ledger.verify_closes()
    assert v["broken"] == [d.isoformat()] and v["anchored"] == 0


# ── 한 거래 한 시각 · 탈퇴 경합 · 시작 잔액(결함 27·23·32) ────
def test_entries_of_one_transaction_share_one_timestamp():
    """자정에 걸친 송금이 가계정을 이틀로 쪼개지 않게 — 한 트랜잭션의 분개는 모두 같은 시각(DB 트랜잭션 시각)."""
    for u in ("ld-t1", "ld-t2"):
        auth.register(u, PW)
    with db.transaction() as conn:
        wallet.debit(conn, "ld-t1", 1000, "transfer_out", ref="ld-t2")
        _time.sleep(0.05)
        wallet.credit(conn, "ld-t2", 1000, "transfer_in", ref="ld-t1")
    stamps = {r["created_at"] for r in db.query("SELECT created_at FROM journal_entry WHERE reason LIKE 'transfer_%'")}
    assert len(stamps) == 1


def test_late_wallet_write_cannot_resurrect_a_deleted_account():
    """탈퇴가 먼저 끝난 뒤 늦게 도착한 입금(이미 인증을 통과한 요청) — 예전엔 지갑을 새로 만들어(환영금까지) 넣었다."""
    auth.register("ld-gone", PW)
    wallet.balance("ld-gone")
    account.delete_account("ld-gone")
    with pytest.raises(wallet.WalletError):
        wallet.credit_once("ld-gone", 1000, "attendance")
    assert not db.query("SELECT 1 FROM wallet WHERE username='ld-gone'")
    assert ledger.trial_balance()["total"] == 0 and ledger._current_checks()["orphan_accounts"] == []


def test_deletion_waits_for_in_flight_credit_so_no_money_is_stranded():
    """진행 중이던 입금이 커밋되기 전에 탈퇴가 잔액을 읽으면, 그 입금이 지갑 없는 계좌에 붕 떴다(결함 23)."""
    auth.register("ld-race", PW)
    wallet.balance("ld-race")
    entered, release, errs = threading.Event(), threading.Event(), []

    def in_flight():
        try:
            with db.transaction() as conn:
                wallet.credit(conn, "ld-race", 5000, "attendance")
                entered.set()
                release.wait(10)
        except Exception as e:                                          # noqa: BLE001
            errs.append(e)
    t = threading.Thread(target=in_flight)
    t.start()
    assert entered.wait(10)
    d = threading.Thread(target=account.delete_account, args=("ld-race",))
    d.start()
    _time.sleep(0.5)                                                     # 탈퇴는 계정 행에서 기다린다
    assert d.is_alive()
    release.set()
    t.join(10)
    d.join(10)
    assert not errs and not d.is_alive()
    forfeited = db.query("SELECT COALESCE(SUM(amount), 0) AS s FROM journal_entry WHERE account='sys:forfeited'")[0]["s"]
    assert int(forfeited) == 1_000_000 + 5000                            # 환영금 + 진행 중이던 입금까지 소멸 처리
    assert ledger._current_checks()["orphan_accounts"] == []


def test_orphan_account_balance_fails_the_close(monkeypatch):
    """지갑 없는 사용자 계좌에 잔액이 남으면 마감 실패 — 예방(잠금)이 뚫려도 대사가 한 번 더 잡는다."""
    _book(clock.today(), [("WORPHAN00001", "sys:promotion", 700)], reason="welcome")
    _tomorrow(monkeypatch)
    r = ledger.close_day(clock.today() - timedelta(days=1))
    assert r["status"] == "fail" and r["checks"]["orphan_accounts"] == [{"account": "WORPHAN00001", "journal": 700}]


def test_opening_balances_run_once_even_when_processes_start_together():
    """API·스케줄러·실시간이 함께 켜지며 시작 잔액 분개를 동시에 — 예전엔 둘 다 '분개 없음'을 보고 두 번 넣을 수 있었다."""
    auth.register("ld-legacy", PW)
    db.execute("INSERT INTO wallet(username, balance, version, updated_at, account_no) "
               "VALUES('ld-legacy', 777, 0, '2026-01-01T00:00:00', 'WLEGACY00001')")
    barrier, out = threading.Barrier(4), []

    def go():
        barrier.wait()
        out.append(ledger.opening_balances())
    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert sorted(out) == [0, 0, 0, 1]
    assert db.query("SELECT COUNT(*) AS n FROM journal_entry WHERE account='WLEGACY00001'")[0]["n"] == 1
