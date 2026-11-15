"""국내 실시간 시세 수집기 — 한국투자증권(KIS) OpenAPI.

  ① 웹소켓 H0STCNT0(실시간 체결가): 장중 체결마다 realtime_price 갱신 → 화면 'LIVE'
     (현재가·전일대비·등락률·시가·고가·저가·누적거래량·누적거래대금·영업일자 저장)
  ② REST 일자별 시세(FHKST01010400): 시작 시 + 장중 10분마다 + 장 마감 후 1회 → ohlc 에 최근 봉 반영
     → 장 마감 뒤에 수집기를 켜도 '오늘 종가'가 바로 보인다(KRX 일별 데이터는 다음 영업일 제공).
  ③ REST 국내업종 현재지수(FHPUP02100000): 장중 30초마다 코스피·코스닥 → market_index(source=kis, LIVE)

실행:  python run.py realtime-kr        (국내만)     /  python run.py realtime  (국내+해외 동시)
       python -m app.collectors.realtime            (위와 동일, 국내만)
시뮬:  python run.py realtime-sim  — 키·장시간 없이 '실시간 배관' 확인용. 화면엔 '시뮬레이션'으로 표시되고,
       멈추면(120초 후) 가짜 가격은 사라지고 실제 직전 종가로 돌아간다.
필요:  .env 의 KIS_APP_KEY / KIS_APP_SECRET (모의투자 계좌면 KIS_PAPER=true)
"""
import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta

import requests
import websocket
from sqlalchemy import text

from app.config import settings
from app.services import catalog, quotes
from app import db
from app.logging_conf import safe_error

log = logging.getLogger(__name__)

# H0STCNT0 실시간 체결가 필드 인덱스(한 레코드 = 46필드)
IDX_TICKER, IDX_TIME, IDX_PRICE, IDX_SIGN, IDX_CHANGE, IDX_RATE = 0, 1, 2, 3, 4, 5
IDX_OPEN, IDX_HIGH, IDX_LOW = 7, 8, 9
IDX_VOLUME, IDX_VALUE = 13, 14            # 누적 거래량 / 누적 거래대금
IDX_BSOP_DATE = 33                        # 영업일자(YYYYMMDD)
FIELDS_PER_RECORD = 46
MAX_SUBSCRIBE = 40                        # KIS 웹소켓 실시간 등록 한도(~41)
REFRESH_EVERY = timedelta(minutes=10)


def _rest_base():
    return ("https://openapivts.koreainvestment.com:29443" if settings.KIS_PAPER
            else "https://openapi.koreainvestment.com:9443")


def _ws_url():
    return ("ws://ops.koreainvestment.com:31000" if settings.KIS_PAPER
            else "ws://ops.koreainvestment.com:21000")


def enabled():
    return bool(settings.KIS_APP_KEY and settings.KIS_APP_SECRET)


# ── 인증 ──────────────────────────────────────────────────
def get_approval_key():
    """웹소켓 접속용 approval_key 발급."""
    r = requests.post(f"{_rest_base()}/oauth2/Approval",
                      json={"grant_type": "client_credentials",
                            "appkey": settings.KIS_APP_KEY,
                            "secretkey": settings.KIS_APP_SECRET},
                      timeout=10)
    r.raise_for_status()
    return r.json()["approval_key"]


_TOKEN_LOCK = threading.Lock()
TOKEN_ERRORS = ("EGW00121", "EGW00123")                 # 유효하지 않은 토큰 · 기간이 만료된 토큰


def _token_owner():
    """캐시한 토큰이 어느 앱키·서버(모의/실전)용인지 — 키를 바꾸거나 KIS_PAPER 를 바꾸면 새로 받는다."""
    import hashlib
    return hashlib.sha256(f"{settings.KIS_APP_KEY}|{bool(settings.KIS_PAPER)}".encode()).hexdigest()[:16]


def invalidate_token():
    db.set_meta("kis_token", "")
    db.set_meta("kis_token_exp", "")


def get_access_token():
    """REST 접근토큰 — 24시간 유효, 발급 횟수 제한(1분 1회)이 있어 app_meta 에 캐시해 재사용.
    · 캐시는 앱키·모의/실전별 — 예전엔 키를 바꾸거나 모의→실전으로 바꿔도 옛 토큰을 최대 24시간 계속 써서
      KIS 호출이 전부 EGW00121/00123 으로 실패했다.
    · 같은 프로세스의 스레드(봉 갱신·지수 폴러)가 동시에 발급을 요청하지 않게 잠금 — 1분 1회 제한에 걸려 한쪽이 실패."""
    from app import crypto
    with _TOKEN_LOCK:
        exp = db.get_meta("kis_token_exp")
        try:                                             # DB 에는 암호화해 둔다(DATA_ENCRYPTION_KEY) — 예전 평문 행도 읽힘
            tok = crypto.decrypt(db.get_meta("kis_token"), aad="app_meta:kis_token")
        except crypto.CryptoError:
            tok = None                                   # 키가 바뀌었으면 새로 받는다
        exp_dt = quotes.parse_ts(exp) if exp else None
        if tok and exp_dt and exp_dt - quotes.now_kst() > timedelta(minutes=10) \
                and db.get_meta("kis_token_owner") == _token_owner():
            return tok
        r = requests.post(f"{_rest_base()}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": settings.KIS_APP_KEY, "appsecret": settings.KIS_APP_SECRET},
                          timeout=10)
        r.raise_for_status()
        j = r.json()
        tok = j["access_token"]
        raw = j.get("access_token_token_expired")        # 'YYYY-MM-DD HH:MM:SS'(KST)
        try:
            exp_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=quotes.KST)
        except (TypeError, ValueError):
            exp_dt = quotes.now_kst() + timedelta(seconds=int(j.get("expires_in") or 86400))
        db.set_meta("kis_token", crypto.encrypt(tok, aad="app_meta:kis_token"))
        db.set_meta("kis_token_exp", exp_dt.isoformat(timespec="seconds"))
        db.set_meta("kis_token_owner", _token_owner())
        return tok


# ── 웹소켓 메시지 ─────────────────────────────────────────
def subscribe_message(approval_key, ticker, register=True):
    return json.dumps({
        "header": {"approval_key": approval_key, "custtype": "P",
                   "tr_type": "1" if register else "2", "content-type": "utf-8"},
        "body": {"input": {"tr_id": "H0STCNT0", "tr_key": ticker}},
    })


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _signed(value, sign_code):
    """KIS 부호코드(1상한 2상승 3보합 4하한 5하락)로 대비값 부호 보정 — 원값이 부호 없이 와도 안전."""
    if value is None:
        return None
    if sign_code == "3":
        return 0.0
    if sign_code in ("4", "5"):
        return -abs(value)
    if sign_code in ("1", "2"):
        return abs(value)
    return value


def _ymd(s):
    s = str(s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def _record_to_tick(f):
    def g(i):
        return f[i] if i < len(f) else None
    price = _num(g(IDX_PRICE))
    if not g(IDX_TICKER) or not price:
        return None
    sign = g(IDX_SIGN)
    vol = _num(g(IDX_VOLUME))
    val = _num(g(IDX_VALUE))
    return {"ticker": g(IDX_TICKER), "price": price,
            "change": _signed(_num(g(IDX_CHANGE)), sign),
            "change_rate": _signed(_num(g(IDX_RATE)), sign),
            "open": _num(g(IDX_OPEN)) or None, "high": _num(g(IDX_HIGH)) or None,
            "low": _num(g(IDX_LOW)) or None,
            "volume": int(vol) if vol is not None else None,
            "value": int(val) if val else None,
            "session_date": _ymd(g(IDX_BSOP_DATE)),
            "ts": g(IDX_TIME)}


def parse_ticks(message):
    """평문 실시간 데이터('0|H0STCNT0|003|...') → 틱 리스트. 한 메시지에 여러 체결(건수 N)이 올 수 있다."""
    if not message or message[0] not in ("0", "1"):
        return []                                         # '{' 로 시작하는 제어 메시지
    parts = message.split("|")
    if len(parts) < 4 or parts[1] != "H0STCNT0":
        return []
    fields = parts[3].split("^")
    try:
        count = max(1, int(parts[2]))
    except ValueError:
        count = 1
    if count > 1 and len(fields) >= count * FIELDS_PER_RECORD:
        chunks = [fields[i * FIELDS_PER_RECORD:(i + 1) * FIELDS_PER_RECORD] for i in range(count)]
    else:
        chunks = [fields]
    return [t for t in (_record_to_tick(c) for c in chunks) if t]


def save_tick(tick, source="kis"):
    """틱 upsert. ts 는 '서버 수신 시각'(신선도=LIVE 판정용), session_date 는 체결 영업일."""
    db.execute("""INSERT INTO realtime_price(ticker, price, change, change_rate, open, high, low,
                                             volume, value, session_date, source, ts)
                  VALUES(:t, :p, :ch, :c, :o, :h, :l, :v, :val, :sd, :src, :ts)
                  ON CONFLICT(ticker) DO UPDATE SET
                    price=excluded.price, change=excluded.change, change_rate=excluded.change_rate,
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    volume=excluded.volume, value=excluded.value,
                    session_date=excluded.session_date, source=excluded.source, ts=excluded.ts""",
               {"t": tick["ticker"], "p": tick["price"], "ch": tick.get("change"),
                "c": tick.get("change_rate"), "o": tick.get("open"), "h": tick.get("high"),
                "l": tick.get("low"), "v": tick.get("volume"), "val": tick.get("value"),
                "sd": tick.get("session_date") or quotes.kr_session_date(),
                "src": tick.get("source") or source, "ts": quotes.iso_now()})


# ── REST: 최근 일봉(당일 포함) → ohlc ─────────────────────
def parse_daily_output(output):
    """inquire-daily-price output(최신순) → 봉 리스트(오래된→최신). 필드: stck_bsop_date/oprc/hgpr/lwpr/clpr/acml_vol."""
    bars = []
    for r in output or []:
        d = _ymd(r.get("stck_bsop_date"))
        c = _num(r.get("stck_clpr"))
        if not d or not c:
            continue
        bars.append({"date": d, "open": _num(r.get("stck_oprc")) or c, "high": _num(r.get("stck_hgpr")) or c,
                     "low": _num(r.get("stck_lwpr")) or c, "close": c,
                     "volume": int(_num(r.get("acml_vol")) or 0),
                     "change_rate": _signed(_num(r.get("prdy_ctrt")), r.get("prdy_vrss_sign"))})
    return sorted(bars, key=lambda b: b["date"])


def fetch_daily_bars(ticker, token):
    r = requests.get(f"{_rest_base()}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                     headers={"content-type": "application/json; charset=utf-8",
                              "authorization": f"Bearer {token}",
                              "appkey": settings.KIS_APP_KEY, "appsecret": settings.KIS_APP_SECRET,
                              "tr_id": "FHKST01010400", "custtype": "P"},
                     params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
                     timeout=10)
    try:
        j = r.json()
    except ValueError:
        j = None
    # KIS 는 초당 한도 초과(EGW00201) 등을 HTTP 500 + JSON 본문으로 준다 → 본문의 코드를 먼저 본다
    # (예전엔 raise_for_status 가 먼저 터져 'EGW00201' 이 메시지에 없어 재시도가 한 번도 안 됐다)
    if isinstance(j, dict) and str(j.get("rt_cd")) not in ("0", "None") and j.get("msg_cd"):
        raise RuntimeError(f"KIS {j.get('msg_cd')}: {j.get('msg1')}")
    r.raise_for_status()
    if not isinstance(j, dict):
        raise RuntimeError("KIS 응답 형식 오류")
    return parse_daily_output(j.get("output") or j.get("output2"))


def store_recent_bars(ticker, bars, keep=5):
    """최근 keep개 봉을 ohlc·daily_price 에 upsert. 시총은 직전 기록 시총을 종가 비율로 이월(주식수 불변)."""
    from app.collectors.marketgen import store_ohlc
    bars = bars[-keep:]
    if not bars:
        return 0
    with db.transaction() as conn:
        store_ohlc(conn, ticker, bars)
        ref = conn.execute(text("SELECT close, market_cap FROM daily_price WHERE ticker=:t "
                                "AND market_cap IS NOT NULL AND market_cap > 0 "
                                "ORDER BY trade_date DESC LIMIT 1"), {"t": ticker}).mappings().first()
        for b in bars:
            mc = int(ref["market_cap"] * b["close"] / ref["close"]) if ref and ref["close"] else None
            conn.execute(text(
                "INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
                "VALUES(:t,:d,:c,:v,:r,:m) ON CONFLICT(ticker,trade_date) DO UPDATE SET "
                "close=:c, volume=:v, change_rate=:r, "
                "market_cap=COALESCE(:m, daily_price.market_cap)"),
                {"t": ticker, "d": b["date"], "c": b["close"], "v": b["volume"],
                 "r": b.get("change_rate") or 0.0, "m": mc})
    return len(bars)


def refresh_latest_bars(tickers=None):
    """유니버스 최근 봉 갱신(당일 포함). 호출 간격은 KIS 한도(실전 20/초, 모의 2/초)에 맞춘다."""
    if not enabled():
        return {"ok": 0, "fail": 0, "skipped": "KIS 키 없음"}
    tickers = tickers or catalog.universe()
    try:
        token = get_access_token()
    except Exception as e:                               # noqa: BLE001 — 토큰 실패는 이 단계만 건너뛴다
        err = safe_error(e)
        log.warning("KIS 접근토큰 발급 실패 — 최근 봉 갱신 건너뜀: %s", err)
        return {"ok": 0, "fail": 0, "error": err}
    gap = 0.55 if settings.KIS_PAPER else 0.07
    ok = fail = 0
    renewed = False
    for t in tickers:
        for attempt in (1, 2):
            try:
                store_recent_bars(t, fetch_daily_bars(t, token))
                ok += 1
                break
            except Exception as e:                       # noqa: BLE001 — 한 종목 실패가 전체를 막지 않게
                msg = str(e)
                if attempt == 1 and not renewed and any(c in msg for c in TOKEN_ERRORS):
                    invalidate_token()                   # 토큰이 무효(키 교체·서버 측 폐기) → 1번만 새로 받아 재시도
                    token, renewed = get_access_token(), True
                    continue
                if attempt == 2 or "EGW00201" not in msg:    # 초당 한도 초과면 1회 재시도
                    log.warning("KIS 일봉 갱신 실패 %s: %s", t, e)
                    fail += 1
                    break
                time.sleep(1.0)
        time.sleep(gap)
    db.set_meta("kis_bars_refreshed", quotes.iso_now())
    if ok and quotes.kr_phase() == "closed":            # 장 마감(15:35) 뒤 받은 봉 = 종가 단일가까지 반영된 확정 종가
        quotes.mark_close_final(quotes.kr_session_date())
    log.info("KIS 최근 봉 갱신: 성공 %d / 실패 %d", ok, fail)
    return {"ok": ok, "fail": fail}


def _refresher(stop, tickers):
    """시작 시 1회 + 장중 10분마다 + 장 마감(15:40) 후 1회."""
    last, final_done = None, None
    while not stop.is_set():
        now = quotes.now_kst()
        phase = quotes.kr_phase(now)
        due = last is None or (phase == "open" and now - last >= REFRESH_EVERY)
        if phase == "closed" and now.time() >= datetime.strptime("15:40", "%H:%M").time() \
                and final_done != now.date():
            due, final_done = True, now.date()
        if due:
            try:
                refresh_latest_bars(tickers)
            except Exception as e:                       # noqa: BLE001
                log.warning("KIS 봉 갱신 중단: %s", e)
            last = quotes.now_kst()
        stop.wait(30)


def _index_poller(stop, every=30):
    """코스피·코스닥 실시간 지수 — 장중 30초마다 + 장 마감(15:40) 후 1회 KIS REST 로 market_index 갱신.
    (웹소켓 구독 한도(~41)를 종목에 쓰고, 지수는 REST 로 가볍게 — 홈 지수 카드가 'LIVE' 로 바뀐다)"""
    from app.collectors import indexes
    close_done = None
    while not stop.is_set():
        now = quotes.now_kst()
        phase = quotes.kr_phase(now)
        after_close = (phase == "closed" and now.time() >= datetime.strptime("15:40", "%H:%M").time()
                       and close_done != now.date())
        if phase == "open" or after_close:
            try:
                indexes.refresh(real=True, sources=("kis",), history=False)
            except Exception as e:                       # noqa: BLE001
                log.warning("KIS 지수 갱신 실패: %s", e)
            if after_close:
                close_done = now.date()
        stop.wait(every)


def _top_by_mcap(n):
    """시총 상위 n (최신 daily_price 기준). 코드순으로 자르던 버그(삼성바이오 등 누락) 수정."""
    rows = db.query("SELECT ticker FROM (SELECT DISTINCT ON (ticker) ticker, market_cap FROM daily_price "
                    "ORDER BY ticker, trade_date DESC) t ORDER BY market_cap DESC NULLS LAST LIMIT :n",
                    {"n": n})
    uni = set(catalog.universe())
    out = [r["ticker"] for r in rows if r["ticker"] in uni]
    return out or catalog.universe()[:n]


def _ws_window(now=None):
    """웹소켓 접속 시간대 — 평일 08:50~15:40 KST(그 외엔 데이터가 안 흐르므로 대기)."""
    n = now or quotes.now_kst()
    t = n.time()
    return n.weekday() < 5 and not quotes.is_holiday("kr", n.date()) \
        and datetime.strptime("08:50", "%H:%M").time() <= t <= datetime.strptime("15:40", "%H:%M").time()


def control_error(message):
    """제어 메시지(JSON) 중 '실패' 응답 → (msg_cd, msg1, tr_key), 아니면 None.
    예: 같은 앱키로 다른 곳(다른 PC·tmux 와 systemd 중복 실행)에서 접속 중이면 'ALREADY IN USE appkey',
    구독 한도 초과면 'MAX SUBSCRIBE OVER' — 예전엔 조용히 버려져 '왜 실시간이 안 들어오는지' 알 수 없었다."""
    try:
        data = json.loads(message)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    rt_cd = str(body.get("rt_cd", "0")).strip()
    if rt_cd in ("0", ""):
        return None
    head = data.get("header") if isinstance(data.get("header"), dict) else {}
    return (str(body.get("msg_cd") or ""), str(body.get("msg1") or ""), str(head.get("tr_key") or ""))


def run_realtime(tickers=None, stop=None):
    """웹소켓 상주 + REST 갱신 스레드. 끊기면 자동 재접속(1→2→4…최대 60초 백오프)."""
    if not enabled():
        log.error("KIS_APP_KEY/SECRET 없음 — .env 에 설정하세요 (국내 실시간 건너뜀)")
        return
    stop = stop or threading.Event()
    db.init_db()
    universe = catalog.universe()
    subs = tickers or _top_by_mcap(MAX_SUBSCRIBE)
    if len(subs) > MAX_SUBSCRIBE:
        log.warning("실시간은 최대 %d종목만 구독합니다 (요청 %d개 중 상위 %d개)",
                    MAX_SUBSCRIBE, len(subs), MAX_SUBSCRIBE)
        subs = subs[:MAX_SUBSCRIBE]
    threading.Thread(target=_refresher, args=(stop, universe), daemon=True,
                     name="kis-bars").start()
    threading.Thread(target=_index_poller, args=(stop,), daemon=True, name="kis-index").start()
    backoff = 1
    waiting = False
    while not stop.is_set():
        if not _ws_window():
            if not waiting:                               # 대기에 들어갈 때 1번만(예전: 장외 내내 1분마다 → 하루 ~1,000줄)
                log.info("국내 장 시간 외 — 웹소켓 대기(평일 08:50 재접속, 최근 종가는 REST 로 갱신됨)")
                waiting = True
            stop.wait(60)
            continue
        waiting = False
        ws = None
        try:
            approval_key = get_approval_key()
            from app.security import egress
            egress.check_url(_ws_url(), "websocket")             # 나가는 연결 허용 목록(웹소켓은 requests 밖)
            ws = websocket.create_connection(_ws_url(), timeout=30)
            for t in subs:
                ws.send(subscribe_message(approval_key, t))
                time.sleep(0.05)
            log.info("KIS 실시간 구독: %d종목 (%s)", len(subs), _ws_url())
            reported = set()                              # 백오프는 첫 데이터가 온 뒤에만 초기화(아래) — 예전엔
                                                          # 접속하자마자 1초로 되돌려, 바로 끊는 서버(앱키 중복 사용 등)에
                                                          # 1초마다 재접속·approval_key 재발급을 반복했다
            while not stop.is_set() and _ws_window():
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not message:
                    raise ConnectionError("빈 메시지(연결 종료)")
                if message[0] in ("0", "1"):              # 실시간 데이터
                    backoff = 1
                    for tick in parse_ticks(message):
                        save_tick(tick)
                    continue
                err = control_error(message)              # 제어 메시지(JSON)
                if err and err[0] not in reported:        # 같은 오류는 접속당 1번만(종목마다 오면 40줄)
                    reported.add(err[0])
                    log.warning("KIS 실시간 응답 오류 %s: %s (종목 %s)", *err)
                try:
                    data = json.loads(message)
                    if data.get("header", {}).get("tr_id") == "PINGPONG":
                        ws.send(message)                  # keep-alive: 그대로 되돌려보냄
                except (ValueError, TypeError, AttributeError):
                    pass
        except KeyboardInterrupt:
            stop.set()
        except Exception as e:                            # noqa: BLE001
            log.warning("KIS 웹소켓 끊김: %s — %d초 후 재접속", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:                         # noqa: BLE001
                    pass
    log.info("국내 실시간 수집 종료")


def run_sim(interval=1.0, stop=None, max_steps=None):
    """시뮬레이션 — KIS 키·장 시간 없이 실시간 갱신 배관을 눈으로 확인(가짜 가격, source='sim').
    직전 종가에서 작은 랜덤워크(±0.15%/틱, 하루 ±29% 제한). 화면엔 '시뮬레이션'으로 표시되고,
    멈추면 120초 뒤 자동으로 실제 직전 종가로 돌아간다(가짜 가격이 남지 않음)."""
    stop = stop or threading.Event()
    db.init_db()
    base, state = {}, {}
    for t, q in quotes.kr_quotes(catalog.universe()).items():
        if q.get("price"):
            base[t] = float(q["price"])
            state[t] = {"p": base[t], "o": base[t], "h": base[t], "l": base[t], "v": 0}
    if not base:
        log.error("시뮬레이션할 시세가 없음 — 먼저 python run.py demo(또는 real) 로 데이터를 넣으세요")
        return
    log.info("실시간 시뮬레이션 시작: %d종목, %.1f초 간격 (Ctrl+C 종료)", len(base), interval)
    steps = 0
    try:
        while not stop.is_set() and (max_steps is None or steps < max_steps):
            for t, s in state.items():
                b = base[t]
                p = s["p"] * (1 + random.gauss(0, 0.0015))
                p = min(max(p, b * 0.71), b * 1.29)
                p = float(round(p / 10) * 10) if b >= 1000 else round(p, 2)
                s.update(p=p, h=max(s["h"], p), l=min(s["l"], p), v=s["v"] + random.randint(100, 20_000))
                save_tick({"ticker": t, "price": p, "change": p - b,
                           "change_rate": round((p / b - 1) * 100, 2), "open": s["o"], "high": s["h"],
                           "low": s["l"], "volume": s["v"], "value": int(p * s["v"]),
                           "session_date": quotes.kr_session_date(), "source": "sim"}, source="sim")
            steps += 1
            stop.wait(interval)
    except KeyboardInterrupt:
        pass
    log.info("시뮬레이션 종료")


if __name__ == "__main__":
    import sys
    from app.logging_conf import setup_logging
    setup_logging()
    if "--sim" in sys.argv:
        run_sim()
    else:
        run_realtime()
