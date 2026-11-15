"""시장 지수 + 종목 순위 — 홈/종목 화면. (성향별 추천 상품은 products.py)
현재가는 quotes.kr_quotes() 한 곳에서 계산한다(장중 LIVE / 장 마감 후 당일 종가 / 개장 전 직전 종가).
→ 장 마감 2분 뒤 '전 거래일 종가'로 되돌아가던 문제 해결."""
from app import db
from app.services import quotes

LIVE_FRESH_SECONDS = quotes.LIVE_FRESH_SECONDS




def indices(with_history=True, hist_n=30):
    """홈 지수(국내 4종) — 항목마다 출처(source)·기준일(as_of)·상태(status)·오래됨(stale) + 미니차트용 이력.
    status: live(KIS 실시간) · delayed(장중 지연 시세) · close(종가) · sample(표본) · unknown(구버전 행, 재수집 필요)
    장중이면 응답은 바로 주고 지수 갱신은 백그라운드로 1회(stale-while-revalidate, 실데이터 모드에서만).
    해외 지수(SPX 등)는 /overseas/indices 로 분리."""
    from app.collectors import indexes
    order = ("WHERE code IN ('KOSPI','KOSDAQ','USDKRW','BOND3Y') "
             "ORDER BY CASE code WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 WHEN 'USDKRW' THEN 3 ELSE 4 END")
    try:
        rows = db.query("SELECT code, name, value, change, change_rate, ts, source, as_of FROM market_index " + order)
    except Exception:                                    # noqa: BLE001 — 마이그레이션 전(구버전 표) 호환
        rows = db.query("SELECT code, name, value, change, change_rate, ts FROM market_index " + order)
        with_history = False
    try:                                                 # 실데이터 모드인데 비었거나 표본·출처불명이면 자가 복구
        have = {r["code"]: r.get("source") for r in rows}
        bootstrap = any(have.get(c) in (None, "gen") for c in ("KOSPI", "KOSDAQ"))
        indexes.maybe_refresh_async(bootstrap=bootstrap)
    except Exception:                                    # noqa: BLE001 — 갱신 실패가 조회를 막지 않게
        pass
    phase, now = quotes.kr_phase(), quotes.now_utc()
    today = quotes.kr_session_date()
    for r in rows:
        src = r.get("source")
        ts = quotes.parse_ts(r.get("ts"))
        fresh = bool(ts and 0 <= (now - ts).total_seconds() <= 180)
        if src == "gen":
            st = "sample"
        elif src is None:
            st = "unknown"
        elif phase == "open" and fresh and r.get("as_of") == today:
            st = "live" if src == "kis" else "delayed"
        else:
            st = "close"
        r["status"] = st
        r["stale"] = st not in ("sample", "unknown") and indexes.is_stale(r.get("as_of"))
        if with_history and r["code"] in indexes.CODES:
            r["history"] = [h["close"] for h in indexes.history(r["code"], hist_n)]
    return {"items": rows, "source": quotes.market_source(), "phase": phase,
            "index_source": db.get_meta("kr_index_source")}


def _mcap_map(tickers=None):
    """최신 시총 + 그 날짜의 종가 → 현재가 비율로 환산하기 위한 기준. tickers 를 주면 그 종목만(상세 화면)."""
    return {r["ticker"]: r for r in db.latest_per_ticker("daily_price", "trade_date, market_cap, close", tickers)}


def mcap_now(ticker, price, mc_map):
    """시총을 '화면 현재가' 기준으로 환산(주식수 불변) — 장중/장후 가격과 시총이 어긋나지 않게."""
    from app.services import catalog
    r = mc_map.get(ticker)
    if r and r["market_cap"]:
        return int(r["market_cap"] * price / r["close"]) if r["close"] else int(r["market_cap"])
    return int(price * (catalog.shares_of(ticker) or 0))


_SORT = {"value": ("value", True), "volume": ("volume", True),
         "marcap": ("marcap", True), "rise": ("change_rate", True),
         "fall": ("change_rate", False)}


def stock_list(sort="value", limit=30):
    """종목 순위표 — 유니버스(stock 테이블) 전 종목의 현재가/등락률/거래량/거래대금/시총(정렬).
    성능: 최신 2봉·틱(quotes, 쿼리 2번) + 최신 시총(DISTINCT ON 1번) + 메타(1번) — 150종목이어도 조회는 상수 개.
    현재가 규칙은 quotes.effective: 장중 LIVE / 장 마감 후 '당일 종가' / 개장 전 '직전 종가'."""
    from app.services import catalog
    key, desc = _SORT.get(sort, _SORT["value"])
    universe = catalog.universe()
    qmap = quotes.kr_quotes(universe)
    mc_map = _mcap_map()
    metas = catalog.meta_map(universe)                       # 종목 메타는 쿼리 1번(N+1 제거)
    out = []
    for ticker in universe:
        q = qmap.get(ticker)
        if not q or q["prev_close"] is None:                 # 전일가 없으면(1봉뿐) 등락 계산 불가 → 제외
            continue
        m = metas[ticker]
        price = q["price"]
        out.append({"ticker": ticker, "name": m["name"], "market": m["market"], "sector": m["sector"],
                    "price": price, "change": q["change"], "change_rate": q["change_rate"],
                    "volume": q["volume"], "value": q["value"],
                    "marcap": mcap_now(ticker, price, mc_map),
                    "live": q["status"] == "live", "status": q["status"],
                    "session_date": q["session_date"]})
    out.sort(key=lambda r: r[key], reverse=desc)
    top = out[:limit]
    for i, r in enumerate(top, 1):
        r["rank"] = i
    st = quotes.status_payload([qmap.get(r["ticker"]) for r in top],
                               quotes.kr_phase(), quotes.market_source())
    return {"sort": sort, "live": st["live"], "as_of": st["session_date"],
            "status": st, "items": top}


def products(profile="안정형"):
    """(하위호환) 성향별 추천 상품 — 내용은 products.py(「OUTOFSTOCK_추천상품_선정.docx」)가 단일 출처."""
    from app.services import products as products_svc
    return products_svc.for_profile(profile)
