"""실시간 알림 스트림(SSE) — 화면이 몇 초마다 묻는(polling) 대신, 바뀐 순간 서버가 밀어 준다.

  notify(conn, 사용자, 종류, 데이터)  업무 트랜잭션 안에서 pg_notify — PostgreSQL 은 '커밋될 때만' 알림을 보낸다
                                     (롤백된 송금의 잔액 알림이 나가는 일이 없다). 데이터는 작게(잔액·보낸 사람 정도).
  리스너(워커마다 1개)               전용 DB 연결로 LISTEN 하다가 받은 알림을 그 워커에 붙은 사용자 스트림들로 나눠 준다.
                                     워커·서버가 여럿이어도 DB 가 알림을 모두에게 뿌리므로 어느 워커에 붙어 있든 받는다.
  GET /events                        text/event-stream. 20초마다 하트비트(프록시가 끊지 않게) + 그때마다 세션을 다시 확인 —
                                     로그아웃·세션 폐기·계정 잠금이면 스트림을 닫는다(제로트러스트: 연결도 계속 검증).
                                     30분이면 서버가 닫고 브라우저 EventSource 가 다시 붙는다(재인증 기회).
알림은 '힌트'다 — 놓쳐도 화면은 다음 조회 때 맞춰진다(최선 노력). 돈의 정합성은 원장이, 전달 보장은 아웃박스가 맡는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from sqlalchemy import text

log = logging.getLogger(__name__)
CHANNEL = "oos_events"
HEARTBEAT = 20
MAX_LIFETIME = 1800
QUEUE_MAX = 100


def notify(conn, username: str, kind: str, data: dict | None = None) -> None:
    payload = json.dumps({"u": username, "k": kind, "d": data or {}}, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode()) > 7900:                 # NOTIFY 페이로드 한도(8000B) — 크면 종류만
        payload = json.dumps({"u": username, "k": kind, "d": {}}, ensure_ascii=False)
    conn.execute(text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": payload})


class Broker:
    """워커 하나의 구독자 목록 + LISTEN 스레드."""

    def __init__(self):
        self.subs: dict[str, set] = {}
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.delivered = 0
        self.dropped = 0

    def subscribe(self, username: str, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        with self.lock:
            self.subs.setdefault(username, set()).add((q, loop))
        self._ensure_listener()
        return q

    def unsubscribe(self, username: str, q: asyncio.Queue) -> None:
        with self.lock:
            for item in list(self.subs.get(username, ())):
                if item[0] is q:
                    self.subs[username].discard(item)
            if not self.subs.get(username):
                self.subs.pop(username, None)

    def dispatch(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        with self.lock:
            targets = list(self.subs.get(msg.get("u"), ()))
        for q, loop in targets:
            def put(q=q):
                try:
                    q.put_nowait(msg)
                    self.delivered += 1
                except asyncio.QueueFull:              # 느린 구독자 — 오래된 알림을 버린다(힌트일 뿐)
                    self.dropped += 1
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:                       # 이벤트 루프가 이미 닫힘
                pass

    def _ensure_listener(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._listen, name="sse-listener", daemon=True)
        self.thread.start()

    def _listen(self):
        import time
        import psycopg
        from app import db
        backoff = 1
        while not self.stop.is_set():
            try:
                url = db.get_engine().url
                extra = {k: (v if isinstance(v, str) else v[-1]) for k, v in url.query.items()}   # sslmode·sslrootcert·소켓 경로
                with psycopg.connect(**{"host": url.host, "port": url.port, "user": url.username,
                                        "password": url.password, "dbname": url.database, **extra},
                                     autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    backoff = 1
                    while not self.stop.is_set():
                        for n in conn.notifies(timeout=1.0, stop_after=50):
                            self.dispatch(n.payload)
            except Exception as e:                      # noqa: BLE001 — DB 재시작 등: 잠시 뒤 다시 붙는다
                log.warning("SSE 리스너 재연결(%s초): %s", backoff, type(e).__name__)
                time.sleep(backoff)
                backoff = min(30, backoff * 2)

    def stats(self) -> dict:
        with self.lock:
            return {"users": len(self.subs), "streams": sum(len(v) for v in self.subs.values()),
                    "delivered": self.delivered, "dropped": self.dropped,
                    "listener": bool(self.thread and self.thread.is_alive())}


broker = Broker()


def _sse(event: str, data: dict, eid: int | None = None) -> str:
    out = f"event: {event}\n"
    if eid is not None:
        out += f"id: {eid}\n"
    return out + f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def events(username: str, still_valid, max_lifetime: int | None = None, heartbeat: int | None = None):
    """비동기 생성기 — 연결 동안 알림을 SSE 형식으로. still_valid(): 세션이 아직 유효한가(하트비트마다 확인)."""
    max_lifetime = max_lifetime or MAX_LIFETIME
    heartbeat = heartbeat or HEARTBEAT
    loop = asyncio.get_running_loop()
    q = broker.subscribe(username, loop)
    started = loop.time()
    n = 0
    try:
        yield "retry: 5000\n\n" + _sse("hello", {"heartbeat": heartbeat})
        while loop.time() - started < max_lifetime:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                if not await asyncio.to_thread(still_valid):
                    yield _sse("session", {"reason": "ended"})
                    return
                yield ": ping\n\n"
                continue
            n += 1
            yield _sse(msg.get("k", "message"), msg.get("d", {}), n)
            if msg.get("k") == "session":                # 이 사용자의 세션이 끝났다는 알림 — 내 세션이면 바로 닫는다
                if not await asyncio.to_thread(still_valid):
                    return
        yield _sse("reconnect", {"reason": "lifetime"})
    finally:
        broker.unsubscribe(username, q)
