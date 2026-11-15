"""해외(미국) 실시간 시세 수집기 — Finnhub.

  ① 웹소켓 wss://ws.finnhub.io : 체결(trade)마다 가격 갱신 → 화면 'LIVE'
     (미국 정규장 = 한국시간 23:30~06:00, 서머타임 기간 22:30~05:00. 프리/애프터마켓 체결도 반영)
  ② /quote 폴링(무료 한도 60회/분 이내) : 전일 종가·시가·고가·저가 기준값 +
     장이 닫혀 있어도 '직전 체결가(=당일 종가)'를 받아 장외 가격을 정확히 유지
  웹소켓 체결은 메모리에 모았다가 1초마다 DB 에 반영(초당 수백 건 체결에도 DB 부하 일정).

실행:  python run.py realtime-us      (국내+해외 동시: python run.py realtime)
필요:  .env 의 FINNHUB_API_KEY  (키는 환경변수로만 — 소스에 넣지 말 것)
"""
import json
import logging
import random
import threading
import time
from datetime import datetime, timezone

import websocket

from app.config import settings
from app.collectors import finnhub
from app.collectors.overseas_market import save_quote
from app.services import quotes
from app.services.overseas import US_STOCKS, ensure_data, quotes_map

log = logging.getLogger(__name__)
WS_URL = "wss://ws.finnhub.io?token={key}"
POLL_OPEN_SECONDS = 60           # 장중(프리·정규·애프터) /quote 주기
POLL_CLOSED_SECONDS = 600        # 장외 주기
FLUSH_SECONDS = 1.0


def enabled():
    return finnhub.enabled()


def parse_ws_message(raw):
    """Finnhub 웹소켓 메시지 → [(symbol, price, ts_ms, volume)] (trade 가 아니면 빈 리스트)."""
    try:
        m = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if m.get("type") != "trade":
        return []
    out = []
    for d in m.get("data") or []:
        try:
            out.append((d["s"], float(d["p"]), int(d.get("t") or 0), int(d.get("v") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class _Book:
    """종목별 최신 상태(메모리) — 폴링 기준값 + 웹소켓 체결을 합쳐 1초마다 DB 반영."""

    def __init__(self):
        self.lock = threading.Lock()
        self.s = {}
        self.dirty = set()

    def base(self, sym, q, sdate):
        """/quote 결과 반영. 같은 거래일에 이미 웹소켓 체결이 있으면 가격은 더 최신인 체결가를 유지.
        /quote 가 '이전 거래일' 값이면(프리마켓엔 /quote 가 아직 전날 종가·시각을 준다) 새 거래일 상태를
        되돌리지 않고 전일 종가만 맞춘다 — 예전엔 폴링마다 오늘 프리마켓가 ↔ 전날 종가로 가격이 뒤집혔다."""
        with self.lock:
            cur = self.s.get(sym) or {}
            if cur.get("session_date") and sdate and sdate < cur["session_date"]:
                if q.get("price"):
                    cur["prev_close"] = q["price"]            # 그 거래일의 확정 종가 = 오늘의 전일 종가
                self.dirty.discard(sym)
                return dict(cur)
            same = cur.get("session_date") == sdate
            highs = [v for v in (q.get("high"), cur.get("high") if same else None) if v]
            lows = [v for v in (q.get("low"), cur.get("low") if same else None) if v]
            keep_ws = same and cur.get("from_ws") and cur.get("price")
            self.s[sym] = {"prev_close": q.get("prev_close"),
                           "open": q.get("open") or (cur.get("open") if same else None),
                           "high": max(highs) if highs else None, "low": min(lows) if lows else None,
                           "session_date": sdate, "price": cur["price"] if keep_ws else q["price"],
                           "from_ws": bool(keep_ws),
                           "t": cur.get("t") if keep_ws else (q.get("t") or None)}
            self.dirty.discard(sym)                  # 아래 write() 가 곧바로 반영
            return dict(self.s[sym])

    @staticmethod
    def write(sym, st):
        pc, p = st.get("prev_close"), st["price"]
        save_quote(sym, {"price": p, "change": (p - pc) if pc else None,
                         "change_rate": round((p / pc - 1) * 100, 2) if pc else None,
                         "prev_close": pc, "open": st.get("open"), "high": st.get("high"),
                         "low": st.get("low"), "t": None},
                   source="finnhub", session_date=st.get("session_date"), trade_ts=st.get("t"))

    def trade(self, sym, price, ts_ms):
        sdate = quotes.to_et(datetime.fromtimestamp(ts_ms / 1000, timezone.utc)).date().isoformat() \
            if ts_ms else quotes.us_session_date()
        with self.lock:
            cur = self.s.setdefault(sym, {})
            if cur.get("session_date") and cur["session_date"] != sdate:   # 새 거래일 첫 체결
                cur.update(open=price, high=price, low=price,
                           prev_close=cur.get("price") or cur.get("prev_close"))
            cur.update(price=price, session_date=sdate, from_ws=True, t=(ts_ms / 1000) if ts_ms else time.time(),
                       high=max(price, cur.get("high") or price), low=min(price, cur.get("low") or price))
            if not cur.get("open"):
                cur["open"] = price
            self.dirty.add(sym)

    def flush(self):
        with self.lock:
            items = [(s, dict(self.s[s])) for s in self.dirty]
            self.dirty.clear()
        for sym, st in items:
            self.write(sym, st)
        return len(items)


def _poller(stop, book, syms):
    """/quote 폴링(공식 전일 종가·시가·고가·저가 기준). 한 종목의 DB 오류가 스레드를 끝내지 않게 —
    예전엔 PostgreSQL 재시작 한 번에 폴러 스레드가 조용히 죽고 프로세스는 살아 있어(자동 재시작도 안 됨)
    그때부터 기준값 없이 체결가만으로 계산했다."""
    while not stop.is_set():
        started = time.time()
        for s in syms:
            if stop.is_set():
                break
            try:
                q = finnhub.quote(s)
                if q:
                    sdate = quotes.to_et(datetime.fromtimestamp(q["t"], timezone.utc)).date().isoformat() \
                        if q.get("t") else quotes.us_session_date()
                    st = book.base(s, q, sdate)       # 웹소켓 체결이 더 최신이면 그 가격 유지
                    if st.get("price"):               # 가격이 없으면(프리마켓 직후 /quote 빈 값) 기존 값 유지
                        book.write(s, st)
            except Exception as e:                    # noqa: BLE001
                log.warning("Finnhub 폴링 %s 실패(계속): %s", s, e)
                stop.wait(5)
            time.sleep(1.05)                          # 무료 60회/분
        period = POLL_OPEN_SECONDS if quotes.us_phase() in ("pre", "open", "after") else POLL_CLOSED_SECONDS
        stop.wait(max(1.0, period - (time.time() - started)))


def run_realtime(stop=None, symbols=None):
    """웹소켓 + 폴링 상주. 끊기면 자동 재접속(최대 60초 백오프). Ctrl+C 또는 stop.set() 으로 종료."""
    if not enabled():
        log.error("FINNHUB_API_KEY 없음 — .env 에 설정하세요 (해외 실시간 건너뜀)")
        return
    from app import db
    db.init_db()
    ensure_data()
    stop = stop or threading.Event()
    syms = symbols or list(US_STOCKS)
    book = _Book()
    threading.Thread(target=_poller, args=(stop, book, syms), daemon=True, name="finnhub-poll").start()
    backoff = 1
    while not stop.is_set():
        if quotes.us_phase() not in ("pre", "open", "after"):
            stop.wait(60)                             # 장외: 폴링만(직전 종가 유지)
            continue
        ws = None
        try:
            from app.security import egress
            egress.check_host("ws.finnhub.io", "websocket")      # 나가는 연결 허용 목록(웹소켓은 requests 밖)
            ws = websocket.create_connection(WS_URL.format(key=settings.FINNHUB_API_KEY), timeout=5)
            for s in syms:
                ws.send(json.dumps({"type": "subscribe", "symbol": s}))
            log.info("Finnhub 실시간 구독: %d종목", len(syms))
            last_flush = time.time()                  # 백오프는 첫 체결이 온 뒤에 초기화(바로 끊는 서버에 1초마다 재접속 방지)
            while not stop.is_set() and quotes.us_phase() in ("pre", "open", "after"):
                try:
                    raw = ws.recv()
                    if raw == "":
                        raise ConnectionError("연결 종료")
                    for sym, price, ts_ms, _v in parse_ws_message(raw):
                        backoff = 1
                        book.trade(sym, price, ts_ms)
                except websocket.WebSocketTimeoutException:
                    pass
                if time.time() - last_flush >= FLUSH_SECONDS:
                    book.flush()
                    last_flush = time.time()
        except KeyboardInterrupt:
            stop.set()
        except Exception as e:                        # noqa: BLE001
            log.warning("Finnhub 웹소켓 끊김: %s — %d초 후 재접속", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            try:
                book.flush()
            except Exception as e:                    # noqa: BLE001 — DB 순간 오류로 수집기가 죽지 않게
                log.warning("Finnhub 시세 저장 실패: %s", e)
            if ws is not None:
                try:
                    ws.close()
                except Exception:                     # noqa: BLE001
                    pass
    log.info("해외 실시간 수집 종료")


def run_sim(interval=1.0, stop=None, max_steps=None):
    """해외 시뮬레이션 — 키·미국 장시간 없이 배관 확인(source='sim', 화면 '시뮬레이션', 멈추면 자동 복귀)."""
    from app import db
    db.init_db()
    ensure_data()
    stop = stop or threading.Event()
    base = {s: q for s, q in quotes_map().items() if q.get("price")}
    state = {s: {"p": q["price"], "h": q["price"], "l": q["price"]} for s, q in base.items()}
    steps = 0
    while not stop.is_set() and (max_steps is None or steps < max_steps):
        for s, st in state.items():
            b = base[s]["price"]
            p = round(min(max(st["p"] * (1 + random.gauss(0, 0.0012)), b * 0.8), b * 1.2), 2)
            st.update(p=p, h=max(st["h"], p), l=min(st["l"], p))
            save_quote(s, {"price": p, "change": p - b, "change_rate": round((p / b - 1) * 100, 2),
                           "prev_close": b, "open": b, "high": st["h"], "low": st["l"], "t": None},
                       source="sim")
        steps += 1
        stop.wait(interval)
    log.info("해외 시뮬레이션 종료")


if __name__ == "__main__":
    import sys
    from app.logging_conf import setup_logging
    setup_logging()
    run_sim() if "--sim" in sys.argv else run_realtime()
