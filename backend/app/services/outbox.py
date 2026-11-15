"""트랜잭셔널 아웃박스 — '돈은 옮겼는데 알림이 안 갔다' / '알림은 갔는데 거래는 롤백됐다'를 없앤다.

  publish(conn, topic, payload)  업무 트랜잭션 안에서 사건을 적는다 — 업무가 롤백되면 사건도 없다.
  dispatch()                     ① 짧은 트랜잭션으로 사건을 '빌린다'(리스) — FOR UPDATE SKIP LOCKED 로 고르고, 시도 횟수 +1,
                                    available_at 을 리스 만료 시각으로 미룬 뒤 바로 커밋(중계기가 여럿이어도 같은 사건을 동시에 잡지 않음)
                                 ② 처리기는 트랜잭션 '밖'에서 실행 — 외부 전송(웹훅·메일, 채널마다 10초)이 DB 잠금·트랜잭션을 붙잡지 않는다
                                 ③ 사건마다 짧은 트랜잭션으로 done / 재시도(2^n 초 뒤, 최대 1시간) / 8번째 실패면 dead
                                 중계기가 처리 중에 죽으면 리스가 끝난 뒤 다른 중계기가 다시 잡는다(시도 횟수는 이미 올라가 있어
                                 매번 중계기를 죽이는 사건도 결국 dead 로 빠진다).
                                 예전(결함 26): 꺼낸 트랜잭션 안에서 처리기를 돌려, 전송이 길어지면 유휴 트랜잭션 제한(60초)에 끊겨
                                 묶음 전체가 롤백 → 이미 보낸 알림까지 다시 보냈고, 처리기 안의 전송 실패는 삼켜져 '완료'로 남았다(유실).
  처리기(HANDLERS)               transfer.completed → 고액 송금 모니터링 알림(운영). 새 소비자(SIEM·분석 적재)는 처리기만 추가.
                                 실패는 예외로 알린다(삼키면 재시도되지 않는다).
  최소 1회 전달(at-least-once): 처리기는 같은 사건을 두 번 받아도 괜찮아야 한다(알림은 사건 키로 72시간 중복 억제).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import text

from app import clock, db

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
LARGE_TRANSFER = 10_000_000


def publish(conn, topic: str, payload: dict) -> None:
    now = clock.utc()
    conn.execute(text("INSERT INTO outbox(topic, payload, created_at, available_at) VALUES(:t, :p, :n, :n)"),
                 {"t": topic[:40], "p": json.dumps(payload, ensure_ascii=False, default=str), "n": now})


LEASE = timedelta(minutes=5)          # 빌린 사건을 이 안에 끝내지 못하면(중계기 사망) 다른 중계기가 다시 잡는다
BATCH = 10                            # 한 번에 빌리는 수 — 최악(채널 3개 × 10초)이어도 리스 안에 끝나는 크기
DEDUP_HOURS = 72                      # 같은 사건의 알림은 이 안에 한 번만(재시도·리스 만료로 다시 와도)


class DeliveryFailed(RuntimeError):
    """처리기가 사건을 전달하지 못함 — 재시도 대상."""


def _transfer_completed(p: dict):
    if int(p.get("amount", 0)) >= LARGE_TRANSFER:                   # 고액 송금 사후 모니터링(운영 알림)
        from app.services import alerts
        r = alerts.notify(f"transfer:large:{p.get('ref')}", "[모니터링] 고액 송금",
                          f"금액 {int(p['amount']):,}원 · 보낸 계좌 {p.get('from_acct')} → 받는 계좌 {p.get('to_acct')}",
                          level="warn", dedup_hours=DEDUP_HOURS)
        if r["failed"] and not r["sent"]:                             # 모든 채널 실패 — 삼키면 '완료'로 유실된다
            raise DeliveryFailed("알림 채널 전부 실패: " + ",".join(r["failed"]))


HANDLERS = {"transfer.completed": _transfer_completed}


def _claim(limit: int) -> list[dict]:
    """처리할 사건을 빌린다(짧은 트랜잭션) — 시도 +1, 리스 만료까지 다른 중계기에 안 보이게."""
    now = clock.utc()
    with db.transaction() as conn:
        rows = conn.execute(text(
            "UPDATE outbox SET attempts = attempts + 1, available_at = :lease WHERE id IN ("
            " SELECT id FROM outbox WHERE status='pending' AND available_at <= :n ORDER BY id LIMIT :l "
            " FOR UPDATE SKIP LOCKED) RETURNING id, topic, payload, attempts"),
            {"n": now, "l": limit, "lease": now + LEASE}).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: r["id"])


def dispatch(limit: int = 100) -> dict:
    """대기 중인 사건을 처리. 반환 {done, retried, dead}."""
    out = {"done": 0, "retried": 0, "dead": 0}
    left = max(0, int(limit))
    while left > 0:
        batch = _claim(min(BATCH, left))
        if not batch:
            break
        left -= len(batch)
        for r in batch:
            _handle(r, out)
    return out


def _handle(r: dict, out: dict) -> None:
    fn = HANDLERS.get(r["topic"])
    try:
        if fn is None:
            raise LookupError(f"처리기 없음: {r['topic']}")
        fn(json.loads(r["payload"]))                             # 트랜잭션 밖 — 외부 전송이 DB 를 붙잡지 않는다
    except Exception as e:                                       # noqa: BLE001 — 처리기 실패는 사건 단위로 다시 시도
        n = int(r["attempts"])
        dead = n >= MAX_ATTEMPTS
        db.execute("UPDATE outbox SET status=:s, available_at=:t, last_error=:e WHERE id=:i AND status='pending'",
                   {"s": "dead" if dead else "pending", "i": r["id"], "e": f"{type(e).__name__}: {e}"[:300],
                    "t": clock.utc() + timedelta(seconds=min(3600, 2 ** n))})
        out["dead" if dead else "retried"] += 1
        if dead:
            log.error("outbox dead letter #%s %s", r["id"], r["topic"])
        return
    db.execute("UPDATE outbox SET status='done', delivered_at=:n WHERE id=:i AND status='pending'",
               {"n": clock.utc(), "i": r["id"]})
    out["done"] += 1


def stats() -> dict:
    rows = db.query("SELECT status, COUNT(*) AS n, MIN(created_at) AS oldest FROM outbox GROUP BY status")
    return {r["status"]: {"count": int(r["n"]), "oldest": r["oldest"].isoformat() if r["oldest"] else None} for r in rows}


def purge(days: int = 30) -> int:
    return db.execute("DELETE FROM outbox WHERE status='done' AND delivered_at < :c",
                      {"c": clock.utc() - timedelta(days=days)}) or 0
