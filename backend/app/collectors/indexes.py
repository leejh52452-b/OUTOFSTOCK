"""국내 대표지수(코스피·코스닥) 수집 — 다중 소스 폴백 + 출처·기준일 기록(데이터 계보).

이전 문제
  · 지수는 KRX OpenAPI 'idx/kospi_dd_trd' 하나에만 의존했다. 이 API 는 주식 시세 API 와 '별도로'
    서비스 이용신청을 해야 하고, 안 돼 있으면 401 → 예외를 삼킨 뒤 상수(코스피 3,124.68)를 '오늘 날짜'로 저장했다.
    시세 출처가 krx 라서 화면엔 '표본' 표시도 없이 실데이터처럼 보였다(실제 지수와 수천 포인트 차이).
  · KRX 일별 데이터는 다음 영업일 08시 이후 제공(D+1) → 23시 배치로는 지수가 늘 하루 이상 늦었다.
해결 — 실데이터 모드의 소스 우선순위
  1) kis   : 한국투자증권 REST '국내업종 현재지수'(FHPUP02100000) — 장중 실시간 · 장 마감 후 당일 종가(KIS 키)
  2) yahoo : 야후 파이낸스 차트(^KS11 · ^KQ11) — 키 불필요, 일봉 이력 + 최신값
  3) krx   : KRX OpenAPI 지수 일별시세(D-1 종가) — '지수' 서비스 이용신청 필요(401 이면 즉시 중단·원인 기록)
  4) fdr   : FinanceDataReader 'KS11/KQ11'(공개 캐시 — 며칠 늦을 수 있음)
  5) kept  : 직전 실데이터를 그대로 유지(값·기준일 그대로) — 절대 상수로 덮지 않는다
  데모(gen)에서만 상수 표본을 쓰고 source='gen' 으로 남긴다 → 화면에 '표본' 배지.
모든 행에 source(출처)·as_of(그 값이 속한 거래일)·ts(받은 시각)를 남겨 '언제의 어떤 데이터인지' 추적한다.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import text

from app import db
from app.services import quotes

log = logging.getLogger(__name__)

CODES = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
KIS_ISCD = {"KOSPI": "0001", "KOSDAQ": "1001"}             # 업종코드: 0001 코스피 · 1001 코스닥
YAHOO_SYM = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
FDR_SYM = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"user-agent": "Mozilla/5.0 (OUTOFSTOCK capstone; educational)"}
# 데모 상수(표본) — 오프라인 미리보기 전용. 실데이터 모드에선 쓰지 않는다.
SAMPLE = {"KOSPI": (3124.68, 18.42, 0.59), "KOSDAQ": (842.17, -4.31, -0.51)}
OTHER_SAMPLE = [("USDKRW", "원/달러 환율", 1348.20, 3.10, 0.23), ("BOND3Y", "국고채 3년", 2.842, -0.021, -0.73)]
LIVE_REFRESH_SECONDS = 60                                   # 장중 API 요청 시 백그라운드 갱신 최소 간격
HISTORY_DAYS = 200


def _num(v):
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _signed(value, sign_code):
    """KIS 부호(1상한 2상승 3보합 4하한 5하락) 보정."""
    if value is None:
        return None
    if sign_code == "3":
        return 0.0
    if sign_code in ("4", "5"):
        return -abs(value)
    if sign_code in ("1", "2"):
        return abs(value)
    return value


def _prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── 소스 1: KIS(실시간) ───────────────────────────────────
def fetch_kis(code, now=None):
    """KIS '국내업종 현재지수' → {value, change, change_rate, open, high, low, as_of, source:'kis'}.
    응답에 날짜가 없어 기준일은 장 상태로 정한다: 평일 09시 이후 + 오늘 거래량>0 이면 오늘, 아니면 직전 영업일."""
    from app.collectors import realtime
    if not realtime.enabled():
        return None
    token = realtime.get_access_token()
    r = requests.get(f"{realtime._rest_base()}/uapi/domestic-stock/v1/quotations/inquire-index-price",
                     headers={"content-type": "application/json; charset=utf-8",
                              "authorization": f"Bearer {token}",
                              "appkey": realtime.settings.KIS_APP_KEY,
                              "appsecret": realtime.settings.KIS_APP_SECRET,
                              "tr_id": "FHPUP02100000", "custtype": "P"},
                     params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": KIS_ISCD[code]},
                     timeout=8)
    r.raise_for_status()
    j = r.json()
    if str(j.get("rt_cd")) != "0":
        raise RuntimeError(f"KIS {j.get('msg_cd')}: {j.get('msg1')}")
    return parse_kis(j.get("output") or {}, now)


def parse_kis(o, now=None):
    value = _num(o.get("bstp_nmix_prpr"))
    if not value:
        return None
    sign = str(o.get("prdy_vrss_sign") or "")
    n = (now or quotes.now_kst()).astimezone(quotes.KST)
    traded_today = (_num(o.get("acml_vol")) or 0) > 0
    today = n.date()
    as_of = today if (n.weekday() < 5 and n.hour >= 9 and traded_today) else _prev_weekday(today)
    return {"value": value, "change": _signed(_num(o.get("bstp_nmix_prdy_vrss")), sign),
            "change_rate": _signed(_num(o.get("bstp_nmix_prdy_ctrt")), sign),
            "open": _num(o.get("bstp_nmix_oprc")), "high": _num(o.get("bstp_nmix_hgpr")),
            "low": _num(o.get("bstp_nmix_lwpr")), "as_of": as_of.isoformat(), "source": "kis",
            "live": quotes.kr_phase(n) == "open" and traded_today}


# ── 소스 2: 야후 차트(이력 + 최신값) ─────────────────────
def fetch_yahoo(code, rng="1y"):
    """야후 차트 → 일봉(거래소 시간대 기준 날짜). 반환 {bars:[...], last:{...}}."""
    r = requests.get(YAHOO_URL.format(sym=YAHOO_SYM[code]), params={"range": rng, "interval": "1d"},
                     headers=UA, timeout=8)
    r.raise_for_status()
    return parse_yahoo(r.json())


def parse_yahoo(j):
    res = ((j or {}).get("chart") or {}).get("result") or []
    if not res:
        return None
    res = res[0]
    meta = res.get("meta") or {}
    off = int(meta.get("gmtoffset") or 32400)                  # 서울 +9h(초)
    tz = timezone(timedelta(seconds=off))
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = _num((q.get("close") or [None])[i] if i < len(q.get("close") or []) else None)
        if not c:
            continue

        def g(k):
            arr = q.get(k) or []
            v = _num(arr[i]) if i < len(arr) else None
            return v if v else c
        d = datetime.fromtimestamp(int(ts), tz).date().isoformat()
        if bars and bars[-1]["trade_date"] == d:                # 같은 날 중복(장중 갱신 봉) → 마지막 값
            bars.pop()
        bars.append({"trade_date": d, "open": g("open"), "high": g("high"), "low": g("low"), "close": c})
    price = _num(meta.get("regularMarketPrice"))
    mt = meta.get("regularMarketTime")
    if price and mt:                                            # 차트 마지막 봉보다 최신이면 그 값으로
        d = datetime.fromtimestamp(int(mt), tz).date().isoformat()
        if not bars or d > bars[-1]["trade_date"]:
            bars.append({"trade_date": d, "open": price, "high": price, "low": price, "close": price})
        elif d == bars[-1]["trade_date"]:
            bars[-1]["close"] = price
    _fill_changes(bars)
    return {"bars": bars, "last": bars[-1] if bars else None}


def _fill_changes(bars):
    prev = None
    for b in bars:
        if prev:
            b["change"] = round(b["close"] - prev, 2)
            b["change_rate"] = round((b["close"] / prev - 1) * 100, 2) if prev else 0.0
        else:
            b.setdefault("change", None)
            b.setdefault("change_rate", None)
        prev = b["close"]
    return bars


# ── 소스 3: KRX(전 영업일 종가) ───────────────────────────
def fetch_krx(max_days=6, today=None):
    """KRX 지수 일별시세 — 최근 영업일부터 거슬러 첫 데이터가 있는 날. 401/403(서비스 미신청)이면 즉시 중단.
    반환 {code: {value, change, change_rate, open, high, low, as_of}} (실패 시 예외)."""
    from app.collectors import krx
    if not krx.enabled():
        raise RuntimeError("KRX_API_KEY 미설정")
    d = _prev_weekday(today or quotes.now_kst().date())         # KRX 는 당일 데이터를 주지 않음(D+1)
    for _ in range(max_days):
        rows = krx.index_day(d.isoformat())                    # 401/403 은 krx.KrxAuthError 로 올라온다
        if rows:
            return {r["code"]: {**r, "as_of": d.isoformat()} for r in rows}
        d = _prev_weekday(d)
    return {}


# ── 소스 4: FDR 공개 캐시 ─────────────────────────────────
def fetch_fdr(code, days=HISTORY_DAYS):
    import FinanceDataReader as fdr
    from app.retry import call_with_timeout
    start = (quotes.now_kst().date() - timedelta(days=int(days * 1.5))).isoformat()
    df = call_with_timeout(fdr.DataReader, FDR_SYM[code], start, timeout=60)   # FDR 은 자체 시간 제한이 없다
    bars = []
    for idx, r in df.iterrows():
        c = _num(r.get("Close"))
        if not c:
            continue
        bars.append({"trade_date": idx.strftime("%Y-%m-%d"), "open": _num(r.get("Open")) or c,
                     "high": _num(r.get("High")) or c, "low": _num(r.get("Low")) or c, "close": c})
    return {"bars": _fill_changes(bars), "last": bars[-1] if bars else None}


# ── 저장 ──────────────────────────────────────────────────
def upsert_index(conn, code, name, value, change, rate, as_of, source, ts=None):
    conn.execute(text(
        "INSERT INTO market_index(code,name,value,change,change_rate,ts,source,as_of) "
        "VALUES(:c,:n,:v,:ch,:r,:ts,:src,:a) ON CONFLICT(code) DO UPDATE SET "
        "name=:n,value=:v,change=:ch,change_rate=:r,ts=:ts,source=:src,as_of=:a"),
        {"c": code, "n": name, "v": value, "ch": change, "r": rate, "ts": ts or quotes.iso_now(),
         "src": source, "a": as_of})


def store_history(conn, code, bars, source):
    """index_daily upsert(같은 날은 더 최신 값으로)."""
    if not bars:
        return 0
    conn.execute(text(
        "INSERT INTO index_daily(code,trade_date,open,high,low,close,change,change_rate,source) "
        "VALUES(:c,:d,:o,:h,:l,:cl,:ch,:r,:s) ON CONFLICT(code,trade_date) DO UPDATE SET "
        "open=:o,high=:h,low=:l,close=:cl,change=COALESCE(:ch,index_daily.change),"
        "change_rate=COALESCE(:r,index_daily.change_rate),source=:s"),
        [{"c": code, "d": b["trade_date"], "o": b.get("open"), "h": b.get("high"), "l": b.get("low"),
          "cl": b["close"], "ch": b.get("change"), "r": b.get("change_rate"), "s": source} for b in bars])
    return len(bars)


def seed_sample(conn):
    """데모(오프라인) — 상수 표본 + 결정론적 이력(미니차트용). source='gen'."""
    import random
    import zlib
    today = quotes.now_kst().date()
    for code, name in CODES.items():
        v, ch, rt = SAMPLE[code]
        upsert_index(conn, code, name, v, ch, rt, today.isoformat(), "gen")
        rng = random.Random(zlib.crc32(code.encode()))
        d, p, bars = today, v, []
        for _ in range(90):
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            bars.append({"trade_date": d.isoformat(), "close": round(p, 2)})
            p = p / (1 + rng.gauss(0.0004, 0.009))
            d -= timedelta(days=1)
        bars.reverse()
        for b in bars:
            b["open"] = b["high"] = b["low"] = b["close"]
        store_history(conn, code, _fill_changes(bars), "gen")
    for code, name, v, ch, rt in OTHER_SAMPLE:
        exists = conn.execute(text("SELECT source FROM market_index WHERE code=:c"), {"c": code}).first()
        if not exists or exists[0] in (None, "gen"):           # 실데이터(환율 등)가 있으면 덮지 않는다
            upsert_index(conn, code, name, v, ch, rt, today.isoformat(), "gen")


# ── 오케스트레이션 ────────────────────────────────────────
_refresh_lock = threading.Lock()
_last_bg = float("-inf")      # 마지막 백그라운드 갱신(monotonic) — '없음'은 -inf. 0.0 이면 부팅 직후(monotonic 이 작을 때)
                              # 5분 동안 지수 자가 복구가 막혔다(VM 이 막 켜졌을 때 표본 지수가 그대로)


def _latest(src, code, krx_cache, rng="1y"):
    """한 소스에서 최신값 1개 → {value, change, change_rate, as_of, source, (bars)} | None."""
    if src == "kis":
        return fetch_kis(code)
    if src == "yahoo":
        y = fetch_yahoo(code, rng)
        if y and y["last"]:
            lb = y["last"]
            return {"value": lb["close"], "change": lb.get("change"), "change_rate": lb.get("change_rate"),
                    "as_of": lb["trade_date"], "source": "yahoo", "bars": y["bars"]}
        return None
    if src == "krx":
        if krx_cache.get("data") is None:
            krx_cache["data"] = fetch_krx()
        k = krx_cache["data"].get(code)
        if not k:
            return None
        bar = {"trade_date": k["as_of"], "open": k.get("open") or k["value"], "high": k.get("high") or k["value"],
               "low": k.get("low") or k["value"], "close": k["value"], "change": k["change"],
               "change_rate": k["change_rate"]}
        return {"value": k["value"], "change": k["change"], "change_rate": k["change_rate"],
                "as_of": k["as_of"], "source": "krx", "bars": [bar]}
    if src == "fdr":
        f = fetch_fdr(code)
        if f and f["last"]:
            lb = f["last"]
            return {"value": lb["close"], "change": lb.get("change"), "change_rate": lb.get("change_rate"),
                    "as_of": lb["trade_date"], "source": "fdr", "bars": f["bars"]}
    return None


def refresh(real=True, sources=("kis", "yahoo", "krx", "fdr"), history=True):
    """코스피·코스닥 갱신. 반환 {code: {source, as_of, value} | {source:'kept'|'none', reason}, errors}.
    · 최신값: sources 순서대로 첫 성공(kis 실시간 → yahoo → krx(D-1) → fdr 캐시)
    · 이력(history=True): 최신값 소스가 이력을 주지 않으면(kis) yahoo → fdr 에서 따로 채운다
    네트워크 호출은 트랜잭션 밖에서 하고, 저장만 짧은 트랜잭션으로 묶는다(DB 잠금 최소화)."""
    if not real:
        with db.transaction() as conn:
            seed_sample(conn)
        db.set_meta("kr_index_source", "gen")
        return {"KOSPI": {"source": "gen"}, "KOSDAQ": {"source": "gen"}, "errors": {}}
    out, errors, hist = {}, {}, {}
    krx_cache = {}
    for code in CODES:
        got = None
        for src in sources:
            try:
                got = _latest(src, code, krx_cache, "1y" if history else "5d")
            except Exception as e:                              # noqa: BLE001 — 다음 소스로
                if src == "krx":
                    krx_cache["data"] = {}                      # 같은 실행에서 KRX 재호출 안 함(401 반복 방지)
                errors.setdefault(code, {})[src] = str(e)[:160]
                log.info("지수 %s %s 실패: %s", code, src, str(e)[:160])
                got = None
            if got:
                break
        if got and got.get("bars") and len(got["bars"]) > 1:
            hist[code] = (got["source"], got["bars"])
        elif history:                                           # kis/krx 는 1봉 → 이력은 별도 소스로
            for hs in ("yahoo", "fdr"):
                if hs not in sources and hs != "fdr":
                    continue
                try:
                    h = _latest(hs, code, krx_cache)
                except Exception as e:                          # noqa: BLE001
                    errors.setdefault(code, {})[hs + "_history"] = str(e)[:160]
                    h = None
                if h and h.get("bars"):
                    hist[code] = (hs, h["bars"])
                    break
        out[code] = got
    with db.transaction() as conn:
        for code, name in CODES.items():
            g = out[code]
            if g and g.get("source") == "kis":
                _fix_kis_session(conn, code, g)
            if code in hist:
                hsrc, bars = hist[code]
                store_history(conn, code, bars[-HISTORY_DAYS:], hsrc)
            if g and g.get("value"):
                if g.get("bars") and len(g["bars"]) == 1 or g["source"] == "kis":
                    store_history(conn, code, [{"trade_date": g["as_of"], "open": g.get("open") or g["value"],
                                                "high": g.get("high") or g["value"], "low": g.get("low") or g["value"],
                                                "close": g["value"], "change": g.get("change"),
                                                "change_rate": g.get("change_rate")}], g["source"])
                upsert_index(conn, code, name, g["value"], g.get("change"), g.get("change_rate"),
                             g["as_of"], g["source"])
                out[code] = {"source": g["source"], "as_of": g["as_of"], "value": g["value"]}
            else:
                prev = conn.execute(text("SELECT source, as_of FROM market_index WHERE code=:c"),
                                    {"c": code}).mappings().first()
                if prev and prev["source"] not in (None, "gen"):
                    out[code] = {"source": "kept", "as_of": prev["as_of"], "reason": errors.get(code)}
                else:                                           # 실데이터가 한 번도 없었다 → 표본(화면에 '표본' 배지)
                    v, ch, rt = SAMPLE[code]
                    upsert_index(conn, code, name, v, ch, rt, quotes.now_kst().date().isoformat(), "gen")
                    out[code] = {"source": "none", "reason": errors.get(code)}
    srcs = {v["source"] for v in out.values()}
    db.set_meta("kr_index_source", srcs.pop() if len(srcs) == 1 else "mixed")
    db.set_meta("kr_index_status", _json({"at": quotes.iso_now(), "result": out, "errors": errors}))
    krx_err = (errors.get("KOSPI") or {}).get("krx") or ""
    if "401" in krx_err or "403" in krx_err:
        log.warning("KRX 지수 API 거부(%s) — openapi.krx.co.kr 에서 'KOSPI 시리즈 일별시세정보'·'KOSDAQ 시리즈 "
                    "일별시세정보' 서비스 이용신청이 필요합니다(다른 소스로 대체함).", krx_err[:60])
    return {**out, "errors": errors}


def _fix_kis_session(conn, code, g):
    """KIS 지수 응답엔 날짜가 없다 — parse_kis 는 '오늘 거래분이 아니면 직전 평일'로 기준일을 잡는데, 평일 공휴일
    (대체공휴일·추석 등)이 끼면 그 휴일 날짜로 지수 봉이 생겼다(예: 10/5 대체공휴일 다음 날 08:40 → 10/5 봉,
    휴장일 낮에는 직전 세션 값이 '오늘' 봉으로). 받은 값·등락이 이미 저장된 값과 똑같고 그 기준일이 오늘 이전이면
    같은 세션의 값이므로 저장된 기준일을 그대로 쓴다(새 세션이면 종가·등락이 같을 수 없다)."""
    today = quotes.now_kst().date().isoformat()
    prev = conn.execute(text("SELECT value, change, as_of FROM market_index WHERE code=:c"),
                        {"c": code}).mappings().first()
    if prev and prev["as_of"] and prev["as_of"] < today and prev["value"] == g.get("value") \
            and (prev["change"] or 0) == (g.get("change") or 0):
        g["as_of"] = prev["as_of"]
        g["live"] = False


def _json(o):
    import json
    return json.dumps(o, ensure_ascii=False, default=str)[:3000]


def live_enabled():
    """API 요청 시 백그라운드 갱신을 할지 — 실데이터 모드에서만(테스트·데모에선 네트워크 호출 안 함)."""
    from app.config import settings
    return (getattr(settings, "INDEX_LIVE_REFRESH", True) and db.get_meta("market_source") == "krx"
            and (settings.MARKET_SOURCE or "krx").lower() != "gen")


BOOTSTRAP_SECONDS = 300                                      # 지수 행이 없거나 표본일 때 자가 복구 시도 간격


def maybe_refresh_async(now=None, bootstrap=False):
    """stale-while-revalidate — 장중에 마지막 갱신이 LIVE_REFRESH_SECONDS 넘게 지났으면
    응답은 지금 DB 값으로 바로 주고, 갱신은 백그라운드 스레드 하나로(동시 요청이 몰려도 한 번만).
    bootstrap=True(실데이터 모드인데 지수가 비었거나 표본) 이면 장 시간과 무관하게 5분에 한 번 자가 복구
    (이력까지 받아 미니차트·시장 국면 판단도 채운다)."""
    global _last_bg
    if not live_enabled():
        return False
    gap = BOOTSTRAP_SECONDS if bootstrap else LIVE_REFRESH_SECONDS
    if not bootstrap and quotes.kr_phase(now) != "open":
        return False
    if time.monotonic() - _last_bg < gap or not _refresh_lock.acquire(blocking=False):
        return False
    _last_bg = time.monotonic()

    def run():
        try:
            if bootstrap:
                refresh(real=True)
            else:
                refresh(real=True, sources=("kis", "yahoo"), history=False)
        except Exception as e:                                  # noqa: BLE001
            log.info("지수 백그라운드 갱신 실패: %s", e)
        finally:
            _refresh_lock.release()
    threading.Thread(target=run, daemon=True, name="index-refresh").start()
    return True


def history(code, n=60):
    rows = db.query("SELECT trade_date, close FROM index_daily WHERE code=:c ORDER BY trade_date DESC LIMIT :n",
                    {"c": code, "n": n})
    return rows[::-1]


def is_stale(as_of, today=None):
    """기준일이 '직전 영업일'보다 오래됐으면 True(주말·공휴일 1~2일 여유)."""
    if not as_of:
        return True
    try:
        d = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return True
    t = today or quotes.now_kst().date()
    return (t - d).days > 4
