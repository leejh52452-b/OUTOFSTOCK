"""해외 주식(미국) — 국내 종목과 같은 방식(순위·검색·상세·차트·관심종목)으로 보여주는 읽기 계층.

데이터 흐름(국내 KRX/KIS 구조를 그대로 미러링):
  · 일봉        : overseas_ohlc   ← run.py real / fetch-overseas (FinanceDataReader=야후, 키 불필요) · demo(표본)
  · 실시간 시세 : overseas_quote  ← run.py realtime-us (Finnhub 웹소켓 + /quote, FINNHUB_API_KEY)
  · 발행주식수  : overseas_stock.shares ← SEC EDGAR(키 불필요) → 시가총액 = 현재가 × 주식수
  · 재무        : overseas_financial ← SEC EDGAR companyfacts (상세 조회 시 가져와 7일 캐시)
  · 환율/지수   : market_index(USDKRW·SPX·NASDAQ·DOW) ← 수출입은행 / FDR
현재가 규칙은 국내와 같다(quotes.effective): 정규장 LIVE / 장 마감 후 당일 종가 / 개장 전 직전 종가.
일봉이 하나도 없으면(업데이트 직후 등) 표본 데이터를 자동으로 깔아 화면이 비지 않게 하고, 출처를 '표본'으로 표기.
"""
import json
import random
import re
import threading
from datetime import timedelta

from app import clock
from app import db
from app.services import quotes
from app.services import indicators as ind

FALLBACK_USDKRW = 1350.0
_PERIOD_DAYS = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}
FIN_TTL_DAYS = 7

# symbol → (영문명, 한글명, 거래소, 섹터, 표본 기준가(USD), 발행주식수 근사) — 인기 미국 종목
US_STOCKS = {
    "AAPL":  ("Apple Inc.",              "애플",           "NASDAQ", "기술",        255.0, 14_800_000_000),
    "MSFT":  ("Microsoft Corp.",         "마이크로소프트", "NASDAQ", "기술",        480.0,  7_430_000_000),
    "NVDA":  ("NVIDIA Corp.",            "엔비디아",       "NASDAQ", "반도체",      180.0, 24_300_000_000),
    "AMZN":  ("Amazon.com Inc.",         "아마존",         "NASDAQ", "소비재",      225.0, 10_700_000_000),
    "GOOGL": ("Alphabet Inc. Class A",   "알파벳 A",       "NASDAQ", "커뮤니케이션", 280.0, 12_100_000_000),
    "META":  ("Meta Platforms Inc.",     "메타",           "NASDAQ", "커뮤니케이션", 650.0,  2_520_000_000),
    "TSLA":  ("Tesla Inc.",              "테슬라",         "NASDAQ", "자동차",      420.0,  3_300_000_000),
    "AVGO":  ("Broadcom Inc.",           "브로드컴",       "NASDAQ", "반도체",      340.0,  4_700_000_000),
    "AMD":   ("Advanced Micro Devices",  "AMD",            "NASDAQ", "반도체",      220.0,  1_620_000_000),
    "NFLX":  ("Netflix Inc.",            "넷플릭스",       "NASDAQ", "커뮤니케이션", 110.0,  4_240_000_000),
    "INTC":  ("Intel Corp.",             "인텔",           "NASDAQ", "반도체",       38.0,  4_700_000_000),
    "QCOM":  ("Qualcomm Inc.",           "퀄컴",           "NASDAQ", "반도체",      170.0,  1_070_000_000),
    "MU":    ("Micron Technology",       "마이크론",       "NASDAQ", "반도체",      230.0,  1_120_000_000),
    "ORCL":  ("Oracle Corp.",            "오라클",         "NYSE",   "기술",        220.0,  2_800_000_000),
    "CRM":   ("Salesforce Inc.",         "세일즈포스",     "NYSE",   "기술",        250.0,    950_000_000),
    "ADBE":  ("Adobe Inc.",              "어도비",         "NASDAQ", "기술",        340.0,    420_000_000),
    "PLTR":  ("Palantir Technologies",   "팔란티어",       "NASDAQ", "기술",        180.0,  2_370_000_000),
    "COIN":  ("Coinbase Global",         "코인베이스",     "NASDAQ", "금융",        280.0,    260_000_000),
    "JPM":   ("JPMorgan Chase & Co.",    "JP모건",         "NYSE",   "금융",        310.0,  2_750_000_000),
    "V":     ("Visa Inc.",               "비자",           "NYSE",   "금융",        340.0,  1_930_000_000),
    "MA":    ("Mastercard Inc.",         "마스터카드",     "NYSE",   "금융",        560.0,    900_000_000),
    "KO":    ("Coca-Cola Co.",           "코카콜라",       "NYSE",   "필수소비재",   70.0,  4_300_000_000),
    "PEP":   ("PepsiCo Inc.",            "펩시코",         "NASDAQ", "필수소비재",  145.0,  1_370_000_000),
    "MCD":   ("McDonald's Corp.",        "맥도날드",       "NYSE",   "소비재",      300.0,    710_000_000),
    "NKE":   ("Nike Inc.",               "나이키",         "NYSE",   "소비재",       65.0,  1_480_000_000),
    "DIS":   ("Walt Disney Co.",         "디즈니",         "NYSE",   "커뮤니케이션", 112.0,  1_800_000_000),
    "WMT":   ("Walmart Inc.",            "월마트",         "NASDAQ", "필수소비재",  110.0,  7_970_000_000),
    "COST":  ("Costco Wholesale",        "코스트코",       "NASDAQ", "필수소비재",  900.0,    440_000_000),
    "LLY":   ("Eli Lilly and Co.",       "일라이 릴리",    "NYSE",   "헬스케어",   1000.0,    900_000_000),
    "JNJ":   ("Johnson & Johnson",       "존슨앤드존슨",   "NYSE",   "헬스케어",    200.0,  2_410_000_000),
    "XOM":   ("Exxon Mobil Corp.",       "엑슨모빌",       "NYSE",   "에너지",      115.0,  4_300_000_000),
    "BA":    ("Boeing Co.",              "보잉",           "NYSE",   "산업재",      210.0,    760_000_000),
    "UBER":  ("Uber Technologies",       "우버",           "NYSE",   "기술",         85.0,  2_100_000_000),
    "SBUX":  ("Starbucks Corp.",         "스타벅스",       "NASDAQ", "소비재",       85.0,  1_140_000_000),
}
US_INDEX = {"SPX": ("S&P 500", "US500", 6800.0), "NASDAQ": ("나스닥", "IXIC", 23000.0),
            "DOW": ("다우존스", "DJI", 47500.0)}

_SYMBOL_RE = re.compile(r"[A-Z][A-Z.\-]{0,9}")
_seed_lock = threading.Lock()


# ── 공통 ──────────────────────────────────────────────────
def symbols():
    return list(US_STOCKS.keys())


def is_symbol(t):
    """해외 심볼 형식인지(영문 대문자로 시작). 국내 코드(005930 등 숫자 시작)와 구분."""
    return bool(t) and bool(_SYMBOL_RE.fullmatch(str(t)))


def known(symbol):
    if symbol in US_STOCKS:
        return True
    return bool(db.query("SELECT 1 FROM overseas_stock WHERE symbol=:s", {"s": symbol}))


def _et_tz():
    return quotes.to_et().tzinfo


def ensure_data():
    """해외 일봉이 하나도 없으면 표본 데이터를 1회 적재(업데이트 후 fetch 전에도 화면이 비지 않게)."""
    if db.query("SELECT 1 FROM overseas_ohlc LIMIT 1"):
        return
    with _seed_lock:
        if db.query("SELECT 1 FROM overseas_ohlc LIMIT 1"):
            return
        from app.collectors import overseas_market
        overseas_market.seed_overseas(real=False)


def data_source():
    """일봉 출처: fdr(실데이터) | gen(표본) | mixed(일부 표본)."""
    return db.get_meta("overseas_source", "gen")


# ── 결정론적 표본(gen) — 수집기가 사용 ─────────────────────
def seed_of(symbol):
    import zlib
    return zlib.crc32(symbol.encode()) & 0xFFFFFF


def gen_bars(symbol, n, end=None):
    """USD 가격대 결정론적 일봉(마지막 종가 = 표본 기준가 부근). 미국 영업일(주말 제외)."""
    base = US_STOCKS[symbol][4]
    rng = random.Random(seed_of(symbol))
    end = end or quotes.to_et().date()
    dates, d = [], end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= timedelta(days=1)
    dates.reverse()
    closes, p = [], base * rng.uniform(0.82, 0.95)
    for _ in range(n):
        g = (rng.random() + rng.random() + rng.random() - 1.5) / 1.5
        p = max(1.0, p * (1 + 0.0007 + 0.016 * g))
        closes.append(p)
    scale = base * (1 + rng.uniform(-0.02, 0.02)) / closes[-1]
    closes = [c * scale for c in closes]
    bars, prev = [], closes[0]
    for d, c in zip(dates, closes):
        o = prev * (1 + rng.uniform(-0.008, 0.008))
        hi = max(o, c) * (1 + abs(rng.uniform(0, 0.012)))
        lo = min(o, c) * (1 - abs(rng.uniform(0, 0.012)))
        bars.append({"trade_date": d, "open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2),
                     "close": round(c, 2), "volume": rng.randint(4_000_000, 90_000_000)})
        prev = c
    return bars


def gen_financials(symbol):
    name, _ko, _ex, _sec, base, shares = US_STOCKS[symbol]
    rng = random.Random(seed_of(symbol) ^ 0x1234567)
    mcap = base * shares
    revenue = int(mcap * rng.uniform(0.08, 0.35))
    net_income = int(revenue * rng.uniform(0.05, 0.30))
    assets = int(revenue * rng.uniform(1.1, 2.4))
    equity = int(assets * rng.uniform(0.25, 0.6))
    ly = clock.today().year - 1
    return {"entity": name, "currency": "USD", "revenue": revenue, "net_income": net_income,
            "assets": assets, "equity": equity, "fiscal_year": ly, "period_end": f"{ly}-12-31",
            "source": "gen"}


# ── 조회(쿼리 3번으로 전 종목) ─────────────────────────────
# 종목마다 최근 일봉 2개 — 예전엔 전체 일봉(수만 행)에 ROW_NUMBER() 를 매긴 뒤 2개만 남겼다(pg_stat_statements 총
# 소요 1위, 평균 40ms). 지금은 종목 목록을 PK 인덱스로 '건너뛰며'(loose index scan, 재귀 CTE) 뽑고, 종목마다 인덱스를
# 거꾸로 2행만 읽는다(LATERAL … LIMIT 2) — 읽는 행 수가 '전체 일봉'에서 '종목 수 × 2'로. EXPLAIN 12.3 → 3.3ms.
_LAST2 = (" CROSS JOIN LATERAL (SELECT o.trade_date, o.open, o.high, o.low, o.close, o.volume,"
          "   ROW_NUMBER() OVER (ORDER BY o.trade_date DESC) AS rn"
          "   FROM overseas_ohlc o WHERE o.symbol = k.symbol ORDER BY o.trade_date DESC LIMIT 2) b")
_ALL_KEYS = ("WITH RECURSIVE k(symbol) AS ("
             " (SELECT symbol FROM overseas_ohlc ORDER BY symbol LIMIT 1)"
             " UNION ALL SELECT (SELECT o.symbol FROM overseas_ohlc o WHERE o.symbol > k.symbol"
             "                   ORDER BY o.symbol LIMIT 1) FROM k WHERE k.symbol IS NOT NULL)"
             " SELECT k.symbol, b.* FROM k" + _LAST2 + " WHERE k.symbol IS NOT NULL")
_SOME_KEYS = ("SELECT k.symbol, b.* FROM (SELECT DISTINCT unnest(CAST(:ss AS varchar[])) AS symbol) k" + _LAST2)


def _rows(syms=None):
    flt = " WHERE symbol = ANY(:ss)" if syms is not None else ""
    params = {"ss": list(syms)} if syms is not None else {}
    bars = {}
    for r in db.query(_ALL_KEYS if syms is None else _SOME_KEYS, params):
        bars.setdefault(r["symbol"], [None, None])[r["rn"] - 1] = r
    ticks = {r["symbol"]: r for r in db.query("SELECT * FROM overseas_quote" + flt, params)}
    meta = {r["symbol"]: r for r in db.query("SELECT * FROM overseas_stock" + flt, params)}
    return bars, ticks, meta


def _meta_of(symbol, meta_row):
    s = US_STOCKS.get(symbol)
    m = meta_row or {}
    return {"name": m.get("name") or (s[0] if s else symbol),
            "name_ko": m.get("name_ko") or (s[1] if s else ""),
            "exchange": m.get("exchange") or (s[2] if s else ""),
            "sector": m.get("sector") or (s[3] if s else ""),
            "shares": m.get("shares") or (s[5] if s else None),
            "finnhub_mcap": m.get("market_cap"), "source": m.get("source") or "gen"}


def quotes_map(syms=None):
    """{symbol: 유효시세 + 메타(한글명·거래소·섹터·시총)}."""
    if syms is not None and not list(syms):
        return {}
    ensure_data()
    bars, ticks, meta = _rows(syms)
    now, tz = quotes.now_utc(), _et_tz()
    out = {}
    for s in set(bars) | set(ticks):
        last, prev = bars.get(s, [None, None])
        q = quotes.effective(last, prev, ticks.get(s), now=now, tz=tz, close_bar_wins=True)
        if not q:
            continue
        m = _meta_of(s, meta.get(s))
        mcap = int(q["price"] * m["shares"]) if m["shares"] else m["finnhub_mcap"]
        out[s] = {**q, **m, "symbol": s, "market_cap": mcap}
    return out


def usd_krw():
    rows = db.query("SELECT value, ts FROM market_index WHERE code='USDKRW'")
    rate = float(rows[0]["value"]) if rows and rows[0]["value"] else FALLBACK_USDKRW
    return {"rate": rate, "source": db.get_meta("fx_source", "fallback"),
            "ts": rows[0]["ts"] if rows else None}


_SORT = {"value": ("value", True), "volume": ("volume", True), "marcap": ("market_cap", True),
         "rise": ("change_rate", True), "fall": ("change_rate", False), "price": ("price", True)}


def _item(q, fx):
    return {"symbol": q["symbol"], "ticker": q["symbol"], "name": q["name"], "name_ko": q["name_ko"],
            "exchange": q["exchange"], "sector": q["sector"], "currency": "USD",
            "price": q["price"], "change": q["change"], "change_rate": q["change_rate"],
            "volume": q["volume"], "value": q["value"], "market_cap": q["market_cap"],
            "marcap": q["market_cap"], "price_krw": int(q["price"] * fx),
            "live": q["status"] == "live", "status": q["status"], "session_date": q["session_date"]}


def stock_list(sort="value", limit=50):
    """해외 순위표 — 국내와 같은 정렬 키(value|volume|marcap|rise|fall, +price)."""
    key, desc = _SORT.get(sort, _SORT["value"])
    qmap = quotes_map()
    fx = usd_krw()["rate"]
    items = [_item(q, fx) for q in qmap.values() if q["prev_close"] is not None]
    items.sort(key=lambda r: (r.get(key) or 0), reverse=desc)
    top = items[:limit]
    for i, r in enumerate(top, 1):
        r["rank"] = i
    st = quotes.status_payload([qmap[r["symbol"]] for r in top], quotes.us_phase(), data_source())
    return {"sort": sort, "live": st["live"], "as_of": st["session_date"], "status": st,
            "source": data_source(), "usd_krw": fx, "items": top}


def search(q, limit=10):
    """심볼·영문명·한글명 검색(애플/aapl/apple 모두)."""
    ql = (q or "").strip().lower()
    if not ql:
        return {"items": []}
    ensure_data()
    hits = []
    for r in db.query("SELECT symbol, name, name_ko, exchange FROM overseas_stock"):
        sym, nm, ko = r["symbol"].lower(), (r["name"] or "").lower(), (r["name_ko"] or "").lower()
        if sym.startswith(ql) or ql in nm or ql in ko:
            rank = 0 if sym == ql else (1 if sym.startswith(ql) else 2)
            hits.append((rank, r["symbol"], r))
    hits.sort(key=lambda x: (x[0], x[1]))
    return {"items": [{"symbol": r["symbol"], "ticker": r["symbol"], "name": r["name"],
                       "name_ko": r["name_ko"], "exchange": r["exchange"]} for _, _, r in hits[:limit]]}


def financials(symbol):
    """재무 — 실데이터 종목은 EDGAR(7일 캐시, 실패 시 None=‘없음’), 표본 종목은 표본 재무."""
    if not known(symbol):
        return None
    meta = db.query("SELECT source FROM overseas_stock WHERE symbol=:s", {"s": symbol})
    if not meta or meta[0]["source"] != "fdr":
        return gen_financials(symbol) if symbol in US_STOCKS else None
    row = db.query("SELECT payload, updated_at FROM overseas_financial WHERE symbol=:s", {"s": symbol})
    if row:
        upd = quotes.parse_ts(row[0]["updated_at"])
        if upd and (quotes.now_utc() - upd).days < FIN_TTL_DAYS:
            data = json.loads(row[0]["payload"]) if row[0]["payload"] else None
            return data
    from app.collectors import edgar
    f = edgar.financials(symbol)
    data = {**f, "source": "edgar"} if f else None
    db.execute("INSERT INTO overseas_financial(symbol, payload, updated_at) VALUES(:s, :p, :t) "
               "ON CONFLICT(symbol) DO UPDATE SET payload=:p, updated_at=:t",
               {"s": symbol, "p": json.dumps(data) if data else None, "t": quotes.iso_now()})
    return data


def detail(symbol):
    """해외 종목 상세 — 국내 상세와 같은 항목(시·고·저·거래량·거래대금·시총·PER·PBR·52주) + 재무 + 환율."""
    symbol = (symbol or "").upper()
    if not known(symbol):
        return None
    q = quotes_map([symbol]).get(symbol)
    if not q:
        return None
    year = _bars(symbol, 252)
    year = quotes.merge_bar(year, q)
    fin = financials(symbol)
    fx = usd_krw()
    mcap = q["market_cap"]
    per = round(mcap / fin["net_income"], 2) if (fin and mcap and (fin.get("net_income") or 0) > 0) else None
    pbr = round(mcap / fin["equity"], 2) if (fin and mcap and (fin.get("equity") or 0) > 0) else None
    return {
        "symbol": symbol, "ticker": symbol, "name": q["name"], "name_ko": q["name_ko"],
        "exchange": q["exchange"], "sector": q["sector"], "currency": "USD",
        "price": q["price"], "change": round(q["change"], 4), "change_rate": q["change_rate"],
        "live": q["status"] == "live", "status": q["status"], "session_date": q["session_date"],
        "phase": quotes.us_phase(), "source": q["source"],
        "price_krw": int(q["price"] * fx["rate"]),
        "fundamentals": {
            "open": q["open"], "high": q["high"], "low": q["low"], "prev_close": q["prev_close"],
            "volume": q["volume"], "value": q["value"],
            "market_cap": mcap, "market_cap_krw": int(mcap * fx["rate"]) if mcap else None,
            "per": per, "pbr": pbr,
            "high_52w": max(b["high"] for b in year) if year else q["high"],
            "low_52w": min(b["low"] for b in year) if year else q["low"],
            "revenue": fin.get("revenue") if fin else None,
            "net_income": fin.get("net_income") if fin else None,
            "assets": fin.get("assets") if fin else None,
            "equity": fin.get("equity") if fin else None,
            "fiscal_year": fin.get("fiscal_year") if fin else None,
            "period_end": fin.get("period_end") if fin else None,
        },
        "financials_source": (fin or {}).get("source", "none"),
        "forex": fx,
    }


def _bars(symbol, n):
    return db.query("SELECT trade_date, open, high, low, close, volume FROM overseas_ohlc "
                    "WHERE symbol=:s ORDER BY trade_date DESC LIMIT :n", {"s": symbol, "n": n})[::-1]


def ohlc(symbol, period="6M", indicators=None):
    symbol = (symbol or "").upper()
    if not known(symbol):
        return {"symbol": symbol, "period": period, "series": [], "overlays": {}}
    ensure_data()
    n = _PERIOD_DAYS.get(period, 126)
    bars = quotes.merge_bar(_bars(symbol, n + 60), quotes_map([symbol]).get(symbol))
    if not bars:
        return {"symbol": symbol, "period": period, "series": [], "overlays": {}}
    closes = [b["close"] for b in bars]
    want = [i.strip().upper() for i in (indicators or "").split(",") if i.strip()]
    overlays = {}
    for k, w in (("SMA5", 5), ("SMA20", 20), ("SMA60", 60)):
        if k in want:
            overlays[k] = ind.sma(closes, w)
    if "RSI14" in want:
        overlays["RSI14"] = ind.rsi(closes, 14)
    cut = max(0, len(bars) - n)
    return {"symbol": symbol, "period": period, "series": bars[cut:],
            "overlays": {k: v[cut:] for k, v in overlays.items()}}


def indices():
    """미국 3대 지수 + 원/달러 — 해외주식 화면 상단."""
    ensure_data()
    rows = db.query("SELECT code, name, value, change, change_rate, ts FROM market_index "
                    "WHERE code IN ('SPX','NASDAQ','DOW','USDKRW') "
                    "ORDER BY CASE code WHEN 'SPX' THEN 1 WHEN 'NASDAQ' THEN 2 WHEN 'DOW' THEN 3 ELSE 4 END")
    return {"items": rows, "phase": quotes.us_phase(), "source": db.get_meta("us_index_source", "gen")}


def config():
    from app.collectors import finnhub
    return {"source": data_source(), "realtime_enabled": finnhub.enabled(),
            "live_enabled": finnhub.enabled(), "phase": quotes.us_phase(),
            "symbols": symbols()}


