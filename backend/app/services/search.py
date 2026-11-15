"""종목 검색 엔진 — 이름·코드·초성·자모(입력 중 글자)·별칭 + 랭킹 + 인메모리 색인 + 인기 검색.

매칭 등급(rank, 작을수록 위)
  0 정확히 일치     이름·코드·별칭 == 질의               '삼성전자' · '005930' · 'AAPL'
  1 이름 접두       '삼성' → 삼성전자 · 삼성SDI …
  2 코드 접두       '0059' → 005930 · 'NV' → NVDA
  3 입력 중 접두    '삼서'(→삼성) · 'ㅅㅅㅈ'(초성) · 'sams'(별칭)
  4 이름 포함       '전자' → 삼성전자
  5 초성·자모 포함  'ㅈㅈ' → 삼성전자
  6 코드 포함       '5930' → 005930
같은 등급 안에서는 시가총액(관심도) 큰 순 → 이름 짧은 순 → 코드 순.
→ '삼성' 엔터 = 삼성전자, 'ㅎㄷㅊ' = 현대차처럼 사람이 기대하는 1순위가 맨 위에 오게 한다.

이전 구현의 문제와 해결
  · 대소문자 구분: 'sk'·'naver'·'posco' 는 0건 → 소문자·공백 무시(normalize)
  · 한글 조합 중 글자('삼서', '삼성젅')는 0건 → 자모 분해 접두 비교로 끊김 없이 후보 유지
  · 결과 순서가 DB 순서(무작위) → 등급 + 시총 순 정렬(엔터=최상위 후보)
  · 타이핑마다 전체 종목 SELECT → 키 4종(정규화명·초성·자모·별칭)을 미리 계산한 색인을 60초 캐시
  · 자동완성 호출마다 search_log 기록 → 인기 검색어가 'ㅅ','사','삼' 같은 조각으로 오염
    → 자동완성(suggest)은 기록하지 않고, 사용자가 실제로 고른 검색(log_search)만 남긴다.
국내(stock)와 해외(overseas_stock)를 같은 규칙으로 한 번에 찾아 '애ㅍ'(→애플)도 동작한다.

2026-10-03 팀 요청 — '검색이 해외 주식만 된다'
  · 원인 1: 해외는 표가 비면 표본을 자동으로 깔지만(overseas.ensure_data) 국내 유니버스(stock)는 첫 수집이
    커밋되기 전(새 VM 설치 직후 수십 분)이나 run.py demo 전에는 비어 있어 국내 후보가 0건이었다
    → 국내 유니버스가 비어 있으면 큐레이션 종목(catalog.STOCKS)으로 검색한다(catalog.universe() 와 같은 규칙).
  · 원인 2: 해외 화면 검색창은 미국만(kind=us) 찾았다 → 국내도 함께 찾고 해외를 먼저(prefer=us).
  · 국내·해외를 따로 매칭해 한쪽 후보가 많아도 다른 쪽이 잘리지 않게 했다.
"""
import logging
import threading
import time
from datetime import timedelta

from app import db
from app.hangul import chosung, is_chosung_query, jamo, ko_reading, normalize

log = logging.getLogger(__name__)

MAX_QUERY = 50                     # 질의 최대 길이(남용 방지)
_TTL = 60                          # 색인 재생성 주기(초) — 수집 직후엔 invalidate() 로 즉시

# 자주 쓰는 다른 이름(영문·약칭) — '이름이 정확히 이것인 종목'이 있을 때만 붙는다(코드 하드코딩 없음).
ALIASES = {
    "삼성전자": ["samsung", "samsung electronics", "삼전"],
    "SK하이닉스": ["hynix", "sk hynix", "하닉"],
    "NAVER": ["네이버"],
    "카카오": ["kakao"],
    "현대차": ["현대자동차", "hyundai motor", "hyundai"],
    "기아": ["kia", "기아차"],
    "LG에너지솔루션": ["엘지에너지솔루션", "엔솔", "lges"],
    "LG화학": ["엘지화학", "lg chem"],
    "LG전자": ["엘지전자", "lg electronics"],
    "삼성바이오로직스": ["삼바", "samsung biologics"],
    "삼성SDI": ["삼성에스디아이"],
    "POSCO홀딩스": ["포스코홀딩스", "포스코", "posco"],
    "셀트리온": ["celltrion"],
    "KB금융": ["케이비금융", "국민은행"],
    "신한지주": ["신한금융", "신한은행"],
    "하나금융지주": ["하나금융", "하나은행"],
    "우리금융지주": ["우리금융", "우리은행"],
    "한국전력": ["한전", "kepco"],
    "현대모비스": ["모비스", "mobis"],
    "카카오뱅크": ["카뱅", "kakaobank"],
    "HD현대중공업": ["현대중공업"],
    "한화에어로스페이스": ["한화에어로"],
    "크래프톤": ["krafton", "배그"],
    "SK텔레콤": ["skt", "에스케이텔레콤"],
    "KT": ["케이티"],
    "HMM": ["현대상선"],
    "엔씨소프트": ["ncsoft", "엔씨"],
    "알테오젠": ["alteogen"],
}


# ── 색인 ──────────────────────────────────────────────────
class _Index:
    def __init__(self, entries, built_at):
        self.entries = entries
        self.built_at = built_at


_lock = threading.Lock()
_index = None


def invalidate():
    """수집(유니버스 변경) 직후 호출 — 다음 검색에서 색인을 새로 만든다."""
    global _index
    _index = None


def _entry(kind, ticker, name, market, sector, mcap, extra_alias=()):
    n = normalize(name)
    raw = list(ALIASES.get(name, [])) + list(extra_alias) + [ko_reading(name)]   # + 'LG화학' → '엘지화학'
    aliases = list(dict.fromkeys(a for a in (normalize(x) for x in raw if x) if a and a != n))
    return {"type": kind, "ticker": ticker, "name": name or ticker, "market": market or "",
            "sector": sector or "", "mcap": float(mcap or 0),
            "n": n, "c": chosung(n), "ck": "".join(ch for ch in chosung(n) if is_chosung_query(ch)),
            "j": jamo(n), "t": (ticker or "").lower(), "aliases": aliases,
            "aj": [jamo(a) for a in aliases]}                # 별칭도 조합 중 글자로('엘ㅈ' → 엘지화학)


def _build():
    entries = []
    mcap = {r["ticker"]: r["market_cap"] for r in db.query(
        "SELECT DISTINCT ON (ticker) ticker, market_cap FROM daily_price "
        "WHERE market_cap IS NOT NULL ORDER BY ticker, trade_date DESC")}
    for r in db.query("SELECT ticker, name, market, sector FROM stock"):
        entries.append(_entry("kr", r["ticker"], r["name"], r["market"], r["sector"], mcap.get(r["ticker"])))
    if not entries:                                 # 국내 유니버스가 아직 비었음(첫 수집 커밋 전·run.py demo 전)
        entries.extend(_curated_kr(mcap))           # → 큐레이션 종목으로라도 국내 검색이 되게
    try:                                            # 해외(미국) — 없으면 표본을 깔아 둔다(overseas 와 같은 규칙)
        from app.services import overseas
        overseas.ensure_data()
        fx = overseas.usd_krw()["rate"]
        caps = {}
        for s, q in overseas.quotes_map().items():
            caps[s] = (q.get("market_cap") or 0) * fx
        for r in db.query("SELECT symbol, name, name_ko, exchange, sector FROM overseas_stock"):
            entries.append(_entry("us", r["symbol"], r["name_ko"] or r["name"], r["exchange"], r["sector"],
                                  caps.get(r["symbol"]), extra_alias=[r["name"] or ""]))
    except Exception as e:                          # noqa: BLE001 — 해외 표가 없어도 국내 검색은 돼야 한다
        log.debug("해외 검색 색인 생략: %s", e)
    return _Index(entries, time.monotonic())


def _curated_kr(mcap):
    """큐레이션 국내 종목(catalog.STOCKS) 색인 항목 — 시총은 DB 값, 없으면 기준가×상장주식수 근사(순서용)."""
    from app.services import catalog
    return [_entry("kr", t, name, market, sector, mcap.get(t) or base * shares)
            for t, (name, market, sector, base, shares) in catalog.STOCKS.items()]


def _get_index():
    global _index
    idx = _index
    if idx is not None and time.monotonic() - idx.built_at < _TTL:
        return idx
    with _lock:
        idx = _index
        if idx is None or time.monotonic() - idx.built_at >= _TTL:
            idx = _build()
            _index = idx
    return idx


# ── 매칭 ──────────────────────────────────────────────────
def _rank(e, q, qj, cho_q):
    """한 종목의 (매칭 등급, 매칭 종류) — 매칭 안 되면 None."""
    best = None

    def take(r, why):
        nonlocal best
        if best is None or r < best[0]:
            best = (r, why)
    n, t = e["n"], e["t"]
    if q == n or q == t:
        return 0, "exact"
    if n.startswith(q):
        take(1, "prefix")
    if t.startswith(q):
        take(2, "code")
    for a in e["aliases"]:
        if q == a:
            return 0, "alias"
        if a.startswith(q):
            take(3, "alias")
        elif len(q) >= 2 and q in a:
            take(5, "alias")
    if best is not None and best[0] <= 2:
        return best
    if cho_q:                                       # 초성만: 'ㅅㅅㅈㅈ' · 'ㅎㅇㄴㅅ'(SK하이닉스의 한글 부분)
        if e["ck"].startswith(q) or e["c"].startswith(q):
            take(3, "chosung")
        elif len(q) >= 2 and q in e["c"]:               # 초성 1글자 '포함'은 너무 넓어 제외
            take(5, "chosung")
    else:                                           # 조합 중 글자 포함: '삼서' · '삼성젅' · '삼ㅅ'
        if e["j"].startswith(qj):
            take(3, "typing")
        elif any(a.startswith(qj) for a in e["aj"]):    # 별칭을 치는 중: '엘ㅈ'(엘지화학) · '에스ㅋ'(SK하이닉스)
            take(3, "alias")
        elif len(qj) >= 3 and qj in e["j"]:
            take(5, "typing")
    if q in n:
        take(4, "contains")
    if len(q) >= 2 and q in t:
        take(6, "code")
    return best


def match(query, limit=10, kinds=("kr", "us")):
    """질의 → 매칭 후보(등급·시총 순). 각 항목 {type, ticker, name, market, sector, match, rank}."""
    q = normalize((query or "")[:MAX_QUERY])
    if not q:
        return []
    qj, cho_q = jamo(q), is_chosung_query(q)
    hits = []
    for e in _get_index().entries:
        if e["type"] not in kinds:
            continue
        m = _rank(e, q, qj, cho_q)
        if m is not None:
            hits.append((m[0], -e["mcap"], len(e["name"]), e["ticker"], m[1], e))
    hits.sort(key=lambda h: h[:4])
    return [{"type": e["type"], "ticker": e["ticker"], "name": e["name"], "market": e["market"],
             "sector": e["sector"], "match": why, "rank": r}
            for r, _m, _l, _t, why, e in hits[:max(1, limit)]]


# ── 공개 API ──────────────────────────────────────────────
def search_stocks(query, limit=10, log_it=True):
    """(하위호환) 국내 종목 검색. 결과 [{ticker, name, market, sector, match}].
    log_it=True 면 질의를 검색 로그에 남긴다(명시적 검색 버튼/엔터 용도)."""
    q = (query or "").strip()[:MAX_QUERY]
    if not q:
        return []
    rows = match(q, limit, kinds=("kr",))
    if log_it:
        log_search(q, rows[0]["ticker"] if rows and rows[0]["rank"] == 0 else None)
    return [{k: r[k] for k in ("ticker", "name", "market", "sector", "match")} for r in rows]


def suggest(query, limit=8, with_quotes=True, kind="all", prefer="kr"):
    """자동완성 — 국내+해외 후보와 현재가·등락률. 기록하지 않는다(타이핑 조각이 인기 검색어를 오염시키지 않게).
    kind=all 이면 국내·해외를 함께: prefer 쪽(기본 국내, 해외 화면은 us)이 주 시장이고 다른 시장은 3칸까지 보장
    (주 시장 결과가 적으면 남는 칸을 다른 시장으로). 정렬은 매칭 등급 → 같은 등급이면 주 시장 먼저.
    kind=kr/us 면 그 시장만."""
    t0 = time.perf_counter()
    limit = max(1, min(int(limit or 8), 20))
    kinds = {"kr": ("kr",), "us": ("us",)}.get(kind, ("kr", "us"))
    main = "us" if prefer == "us" else "kr"
    # 시장별로 따로 매칭 — 한쪽 후보가 많아도 다른 쪽이 잘리지 않게
    # (예전: 두 시장을 한 번에 limit+6 개로 자른 뒤 나눠서, 국내 상위 후보가 많으면 해외가 통째로 빠졌다)
    found = {k: match(query, limit, kinds=(k,)) if k in kinds else [] for k in ("kr", "us")}
    first, other = found[main], found["us" if main == "kr" else "kr"]
    n_other = min(len(other), 3 if first else limit)         # 다른 시장 몫(주 시장 결과가 없으면 전부)
    n_first = min(len(first), limit - n_other)
    n_other = min(len(other), limit - n_first)               # 주 시장이 적으면 남는 칸을 다른 시장으로
    picked = sorted(first[:n_first] + other[:n_other],
                    key=lambda r: (r["rank"], 0 if r["type"] == main else 1))
    if with_quotes and picked:
        _attach_quotes(picked)
    for r in picked:
        r.pop("rank", None)
    return {"query": (query or "")[:MAX_QUERY], "items": picked,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


def _attach_quotes(items):
    from app.services import quotes
    kr = [r["ticker"] for r in items if r["type"] == "kr"]
    us = [r["ticker"] for r in items if r["type"] == "us"]
    try:
        qk = quotes.kr_quotes(kr) if kr else {}
    except Exception:                                # noqa: BLE001
        qk = {}
    try:
        from app.services import overseas
        qu = overseas.quotes_map(us) if us else {}
    except Exception:                                # noqa: BLE001
        qu = {}
    for r in items:
        q = (qk if r["type"] == "kr" else qu).get(r["ticker"])
        r["price"] = q.get("price") if q else None
        r["change_rate"] = q.get("change_rate") if q else None
        r["live"] = bool(q and q.get("status") == "live")


def known(ticker):
    """색인에 있는(국내·해외) 종목 코드인지 — 인기 집계에 없는 코드가 섞이지 않게."""
    t = str(ticker or "")
    return any(e["ticker"] == t for e in _get_index().entries)


def log_search(keyword, ticker=None):
    """사용자가 실제로 고른 검색 1건 기록(엔터·결과 클릭). ticker 가 있으면 '인기 종목' 집계에 쓰인다."""
    from app.services import quotes
    kw = (keyword or "").strip()[:100]
    if not kw and not ticker:
        return
    params = {"k": kw or ticker, "tk": (ticker or None) and str(ticker)[:20], "t": quotes.iso_now()}
    try:
        db.execute("INSERT INTO search_log(keyword, ticker, searched_at) VALUES(:k, :tk, :t)", params)
    except Exception:                                # noqa: BLE001 — 마이그레이션 전 DB(ticker 열 없음)
        db.execute("INSERT INTO search_log(keyword, searched_at) VALUES(:k, :t)", params)


def popular_searches(limit=5):
    """(하위호환) 검색로그 키워드 집계 → 인기검색어 [{keyword, cnt}]."""
    return db.query("SELECT keyword, COUNT(*) AS cnt FROM search_log "
                    "GROUP BY keyword ORDER BY cnt DESC, keyword LIMIT :n", {"n": max(1, min(limit, 50))})


def popular_stocks(limit=8, days=7):
    """최근 days 일 동안 사용자가 가장 많이 고른 종목. 기록이 부족하면 거래대금 상위로 채운다(빈 화면 방지).
    반환 {items:[{type,ticker,name,cnt}], basis:'search'|'mixed'|'value'}."""
    from app.services import quotes
    limit = max(1, min(int(limit or 8), 20))
    since = (quotes.now_kst().date() - timedelta(days=max(1, int(days or 7)) - 1)).isoformat()   # 오늘 포함 days 일
    try:
        rows = db.query("SELECT ticker, COUNT(*) AS cnt FROM search_log WHERE ticker IS NOT NULL "
                        "AND searched_at >= :s GROUP BY ticker ORDER BY cnt DESC, ticker LIMIT :n",
                        {"s": since, "n": limit})
    except Exception:                                # noqa: BLE001 — 마이그레이션 전 DB
        rows = []
    names = {e["ticker"]: e for e in _get_index().entries}
    items = [{"type": names[r["ticker"]]["type"], "ticker": r["ticker"], "name": names[r["ticker"]]["name"],
              "cnt": r["cnt"]} for r in rows if r["ticker"] in names]
    basis = "search" if items else "value"
    if len(items) < limit:
        try:
            from app.services import market
            have = {i["ticker"] for i in items}
            for r in market.stock_list("value", limit * 2)["items"]:
                if len(items) >= limit:
                    break
                if r["ticker"] not in have:
                    items.append({"type": "kr", "ticker": r["ticker"], "name": r["name"], "cnt": 0})
            if basis == "search" and any(i["cnt"] == 0 for i in items):
                basis = "mixed"
        except Exception as e:                       # noqa: BLE001
            log.debug("인기 종목 보충 실패: %s", e)
    return {"items": items[:limit], "basis": basis, "days": days}
