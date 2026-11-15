"""현재가 계산의 단일 진실 공급원 — 시장 시계(국내 KST·미국 ET) + '유효 시세(effective quote)'.

이전 문제:
  장중 틱(realtime_price)이 120초 안에 있으면 그 값, 아니면 DB 의 최신 일봉 종가를 썼다.
  → 장 마감(15:30) 2분 뒤부터 '오늘 종가'가 아니라 DB 에 있던 '전 거래일 종가'로 되돌아감.
    (KRX OpenAPI 일별 데이터는 다음 영업일에야 제공되므로 장 마감 직후엔 당일 일봉이 DB 에 없다)
해결:
  틱마다 '영업일(session_date)'을 함께 저장하고,
    · 틱이 신선(120초 이내)            → LIVE(그 가격)
    · 틱의 영업일 > 최신 일봉 날짜      → 틱이 더 최신 = '그 영업일 종가/직전 체결가'
    · 같은 날짜 봉이 이미 있고 틱은 식음 → 봉(공식 종가·장중 갱신 봉) — 끊긴 웹소켓의 마지막 틱이
                                        15:45 에 들어온 공식 종가를 덮던 문제 방지
    · 그 외                          → 최신 일봉 종가(전일 대비는 그 전 봉)
    · 시뮬레이션 틱(source=sim)은 신선할 때만 — 멈춘 뒤 가짜 가격이 '직전 데이터'로 남지 않게.
  같은 규칙을 국내(KIS)·해외(Finnhub) 모두에 적용한다.
"""
from datetime import datetime, date, time as dtime, timedelta, timezone
from functools import lru_cache

KST = timezone(timedelta(hours=9))
LIVE_FRESH_SECONDS = 120


@lru_cache(maxsize=16)
def _holidays(market, year):
    """거래소 휴장일(평일) — holidays 패키지(XKRX·XNYS). 설치 안 돼 있으면 빈 집합(주말만 휴장으로 봄)."""
    try:
        import holidays
        return frozenset(holidays.financial_holidays("XKRX" if market == "kr" else "XNYS", years=[year]))
    except Exception:                           # noqa: BLE001
        return frozenset()


def is_holiday(market, day):
    """day(date) 가 그 시장의 평일 휴장일인가(설·추석·NYSE 공휴일 등)."""
    return day in _holidays(market, day.year)

try:                                            # tzdata 가 없으면(일부 도커 이미지) 수동 DST 계산으로 폴백
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                               # noqa: BLE001
    ET = None


# ── 시계 ──────────────────────────────────────────────────
def now_kst():
    return datetime.now(KST)


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    """저장용 타임스탬프(ISO, +09:00)."""
    return now_kst().isoformat(timespec="seconds")


def parse_ts(s):
    """ISO 문자열·datetime → aware datetime. 날짜만/시간대 없음은 한국시간으로 본다(clock.py 규칙 — 예전엔 서버 로컬
    시간대로 해석해 UTC 서버에서 9시간 어긋났다). DB 의 timestamptz 는 이미 datetime 으로 온다. 실패 시 None."""
    if not s:
        return None
    if isinstance(s, datetime):
        d = s
    else:
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d


def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)   # 첫 일요일
    return d + timedelta(weeks=n - 1)


def _et_offset(utc_dt):
    """tzdata 없을 때의 미 동부 오프셋: DST(3월 둘째 일 02:00 ~ 11월 첫째 일 02:00) -4h, 그 외 -5h."""
    y = utc_dt.year
    start = datetime.combine(_nth_sunday(y, 3, 2), dtime(7, 0), timezone.utc)    # 02:00 EST = 07:00 UTC
    end = datetime.combine(_nth_sunday(y, 11, 1), dtime(6, 0), timezone.utc)     # 02:00 EDT = 06:00 UTC
    return timezone(timedelta(hours=-4 if start <= utc_dt < end else -5))


def to_et(dt=None):
    dt = (dt or now_utc()).astimezone(timezone.utc)
    return dt.astimezone(ET) if ET else dt.astimezone(_et_offset(dt))


def kr_phase(now=None):
    """국내 장 상태: weekend | holiday(평일 휴장) | pre(개장 전) | open(정규장) | closed(장 마감)."""
    n = (now or now_kst()).astimezone(KST)
    if n.weekday() >= 5:
        return "weekend"
    if is_holiday("kr", n.date()):
        return "holiday"
    t = n.time()
    if t < dtime(9, 0):
        return "pre"
    if t <= dtime(15, 35):                       # 종가 단일가(15:20~15:30) 체결 반영 여유
        return "open"
    return "closed"


def mark_close_final(day):
    """국내 일봉이 '확정 종가'까지 들어온 마지막 거래일을 기록(뒤로 가지 않음) — KRX 적재(전 영업일까지 확정)·
    장 마감 뒤 KIS 갱신이 남긴다. 추천 감사 스냅샷은 이 날짜 이하의 봉으로만 남긴다."""
    from app import db
    if day and str(day) > (db.get_meta("kr_close_final") or ""):
        db.set_meta("kr_close_final", str(day))


def close_is_final(day):
    """day 의 국내 일봉이 확정 종가인가. 실데이터(krx) 모드만 기록으로 판정하고, 표본 모드는 예전 규칙(지난 날·장 마감 뒤)."""
    from app import db
    if not day:
        return False
    if market_source() == "krx":
        return str(day) <= (db.get_meta("kr_close_final") or "")
    return str(day) < kr_session_date() or kr_phase() in ("closed", "weekend", "holiday")


def us_phase(now=None):
    """미국 장 상태(현지 ET 기준): weekend | holiday | pre(프리마켓) | open(정규장) | after(애프터) | closed."""
    n = to_et(now)
    if n.weekday() >= 5:
        return "weekend"
    if is_holiday("us", n.date()):
        return "holiday"
    t = n.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "open"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "after"
    return "closed"


def kr_session_date(now=None):
    return (now or now_kst()).astimezone(KST).date().isoformat()


def us_session_date(now=None):
    return to_et(now).date().isoformat()


# ── 유효 시세 ─────────────────────────────────────────────
def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _session_close(session_date, tz):
    """그 영업일 정규장 마감 시각(aware) — 국내 15:30 KST · 미국 16:00 ET. 날짜를 못 읽으면 None."""
    try:
        d = date.fromisoformat(str(session_date)[:10])
    except ValueError:
        return None
    if tz is KST:
        return datetime.combine(d, dtime(15, 30), KST)
    if ET is not None:
        return datetime.combine(d, dtime(16, 0), ET)
    noon = datetime.combine(d, dtime(17, 0), timezone.utc)          # tzdata 없을 때: 그날의 동부 오프셋
    return datetime.combine(d, dtime(16, 0), _et_offset(noon))


def effective(last_bar, prev_bar, tick, now=None, tz=KST, fresh_seconds=LIVE_FRESH_SECONDS, close_bar_wins=False):
    """최신 일봉 2개 + 최신 틱 → 화면에 보여줄 '현재가' 한 벌.

    last_bar/prev_bar: {trade_date, open, high, low, close, volume} (없으면 None)
    tick: {price, change, change_rate, prev_close, open, high, low, volume, value,
           session_date, source, ts} (없으면 None)
    반환: price·change·change_rate·prev_close·open·high·low·volume·value·session_date
          status(live|sim|close)·from_tick·ts
    """
    now = now or now_utc()
    t_ts = parse_ts(tick.get("ts")) if tick else None
    fresh = bool(t_ts and 0 <= (now - t_ts).total_seconds() <= fresh_seconds) or \
        bool(t_ts and -5 <= (now - t_ts).total_seconds() < 0)      # 시계 오차 약간 허용
    t_price = _f(tick.get("price")) if tick else None
    source = (tick or {}).get("source") or ""
    s_date = None
    if tick:
        s_date = tick.get("session_date") or (t_ts.astimezone(tz).date().isoformat() if t_ts else None)
    d_last = last_bar["trade_date"] if last_bar else None

    use_tick = False
    if t_price and t_price > 0:
        if source == "sim":
            use_tick = fresh                                      # 시뮬레이션: 신선할 때만
        else:
            use_tick = fresh or d_last is None or (s_date is not None and s_date >= d_last)
            # 정규장 마감 뒤에는 '마감 전에 끊긴 식은 틱'보다 같은 날 일봉(마감 후 갱신되는 공식 종가)이 우선.
            # 예전: 실시간 수집기가 14:00 에 멈추면 15:45 에 공식 종가 봉이 들어와도 14:00 가격이 '종가'로 남았다.
            # 장중(마감 전)엔 그대로 틱 우선 — 거래가 뜸한 종목은 마지막 체결(틱)이 10분 주기 봉보다 최신일 수 있다.
            if use_tick and not fresh and s_date is not None and s_date == d_last and t_ts is not None:
                close_at = _session_close(s_date, tz)
                # close_bar_wins(미국): 정규장 마감 뒤 같은 날 공식 일봉이 들어왔으면 식은 틱(애프터마켓 마지막 체결 포함)보다
                # 공식 종가 — 예전엔 애프터마켓 체결가가 다음 개장 전까지 '종가'로 남고 차트 캔들·52주까지 덮었다.
                if close_at is not None and (t_ts < close_at <= now or (close_bar_wins and close_at <= now)):
                    use_tick = False

    if use_tick:
        price = t_price
        chg, rate, prev_close = _f(tick.get("change")), _f(tick.get("change_rate")), _f(tick.get("prev_close"))
        if prev_close is None and chg is not None:
            prev_close = price - chg
        if prev_close is None and rate is not None and rate > -100:
            prev_close = price / (1 + rate / 100)
        if prev_close is None:                                    # 틱에 전일 정보가 없으면 일봉에서
            ref = last_bar if (last_bar and s_date and s_date > d_last) else prev_bar
            prev_close = _f(ref["close"]) if ref else None
        same_day = bool(last_bar and s_date == d_last)
        o = _f(tick.get("open")) or (_f(last_bar["open"]) if same_day else None) or price
        h = _f(tick.get("high")) or (_f(last_bar["high"]) if same_day else None) or price
        lo = _f(tick.get("low")) or (_f(last_bar["low"]) if same_day else None) or price
        vol = tick.get("volume")
        if vol is None and last_bar:                              # 틱에 거래량이 없으면(해외 시세) 최근 봉 거래량
            vol = last_bar.get("volume")
        vol = int(vol or 0)
        val = tick.get("value")
        val = int(val) if val else int(price * vol)
        status = "sim" if source == "sim" else ("live" if fresh else "close")
        session = s_date or d_last
    elif last_bar:
        price = _f(last_bar["close"]) or 0.0
        prev_close = _f(prev_bar["close"]) if prev_bar else None
        official = _f(last_bar.get("dp_rate"))              # KRX/KIS 공식 등락률(기준가 대비)
        if official is not None and official > -100 and price and prev_close and \
                abs((price / prev_close - 1) * 100 - official) > 1.0:
            # 액면분할·권리락 날: 전날 종가와 그냥 비교하면 -80% 같은 가짜 급락 → 공식 기준가로 환산
            prev_close = round(price / (1 + official / 100), 4)
        o, h, lo = _f(last_bar["open"]) or price, _f(last_bar["high"]) or price, _f(last_bar["low"]) or price
        vol = int(last_bar.get("volume") or 0)
        val = int(price * vol)
        rate = None
        status, session = "close", d_last
    else:
        return None

    change = round(price - prev_close, 4) if prev_close else 0.0
    if use_tick and _f(tick.get("change_rate")) is not None:
        change_rate = round(_f(tick.get("change_rate")), 2)
    else:
        change_rate = round((price / prev_close - 1) * 100, 2) if prev_close else 0.0
    return {"price": price, "change": change, "change_rate": change_rate,
            "prev_close": prev_close, "open": o, "high": max(h, price), "low": min(lo, price),
            "volume": vol, "value": val, "session_date": session, "status": status,
            "live": status == "live", "from_tick": use_tick,
            "ts": (tick or {}).get("ts") if use_tick else None}


SEGMENT_GAP_DAYS = 12


def recent_segment(bars_asc, gap_days=SEGMENT_GAP_DAYS):
    """시간순 봉 목록에서 '마지막 연속 구간'만 — 달력일 gap_days 를 넘는 공백 뒤만 남긴다.
    ohlc 표에는 실습 시나리오용 과거 구간(예: 2023년)도 같은 종목 코드로 들어 있어서, '최근 N개'만 읽으면
    수년 전 봉이 섞인다(52주 최고/최저·1년 차트가 틀어지던 원인). 명절 연휴(최대 9~10일)는 끊지 않는다."""
    cut = 0
    for k in range(1, len(bars_asc)):
        try:
            gap = (date.fromisoformat(str(bars_asc[k]["trade_date"])[:10])
                   - date.fromisoformat(str(bars_asc[k - 1]["trade_date"])[:10])).days
        except ValueError:
            continue
        if gap > gap_days:
            cut = k
    return bars_asc[cut:]


def merge_bar(bars_asc, q):
    """차트 캔들에 '당일 봉'을 반영 — 틱이 최신봉보다 뒤 영업일이면 새 봉 추가, 같은 날이면 갱신.
    (장 마감 후 KRX 일봉이 아직 없어도 차트 마지막 캔들 = 화면 현재가 가 되게)"""
    if not q or not q.get("from_tick") or q.get("status") == "sim" or not q.get("session_date"):
        return bars_asc
    bar = {"trade_date": q["session_date"], "open": q["open"], "high": q["high"],
           "low": q["low"], "close": q["price"], "volume": q["volume"]}
    if not bars_asc or q["session_date"] > bars_asc[-1]["trade_date"]:
        return bars_asc + [bar]
    if q["session_date"] == bars_asc[-1]["trade_date"]:
        return bars_asc[:-1] + [bar]
    return bars_asc


# ── 국내 일괄 조회(쿼리 2번으로 전 종목) ───────────────────
def _kr_rows(tickers=None):
    from app import db
    flt = " WHERE ticker = ANY(:ts)" if tickers is not None else ""
    params = {"ts": list(tickers)} if tickers is not None else {}
    bars = {}
    for r in db.latest_per_ticker("ohlc", "trade_date, open, high, low, close, volume",
                                  None if tickers is None else list(tickers), n=2):
        bars.setdefault(r["ticker"], [None, None])[r["rn"] - 1] = r
    # 최신 봉 날짜의 공식 등락률(KRX FLUC_RT, daily_price) — effective() 가 액면분할·권리락 날의 가짜 급락(-80%)을 바로잡는 데 쓴다
    if bars:
        rates = {(r["ticker"], r["trade_date"]): r["change_rate"]
                 for r in db.latest_per_ticker("daily_price", "trade_date, change_rate", list(bars), n=1)}
        for t, (last, _prev) in bars.items():
            if last is not None:
                last["dp_rate"] = rates.get((t, last["trade_date"]))
    ticks = {r["ticker"]: r for r in db.query("SELECT * FROM realtime_price" + flt, params)}
    return bars, ticks


def kr_quotes(tickers=None, now=None):
    """{ticker: 유효 시세}. tickers=None 이면 일봉이 있는 전 종목."""
    if tickers is not None and not list(tickers):
        return {}
    now = now or now_utc()
    bars, ticks = _kr_rows(tickers)
    out = {}
    for t in (set(bars) | set(ticks)):
        last, prev = bars.get(t, [None, None])
        q = effective(last, prev, ticks.get(t), now=now, tz=KST)
        if q:
            out[t] = q
    return out


def kr_quote(ticker, now=None):
    return kr_quotes([ticker], now=now).get(ticker)


# ── 데이터 출처 / 상태 라벨 ────────────────────────────────
def market_source():
    """국내 일봉 출처: krx(실데이터) | gen(표본). 기록이 없으면 unknown."""
    from app import db
    return db.get_meta("market_source", "unknown")


def status_payload(quotes_iter, phase, source):
    """목록 헤더용 요약: 하나라도 LIVE 면 live, 아니면 대표 영업일."""
    qs = [q for q in quotes_iter if q]
    live = any(q["status"] == "live" for q in qs)
    sim = any(q["status"] == "sim" for q in qs)
    dates = sorted({q["session_date"] for q in qs if q.get("session_date")})
    return {"live": live, "sim": sim, "phase": phase, "source": source,
            "session_date": dates[-1] if dates else None}
