"""종목 카탈로그 — 유니버스 관리.
STOCKS 는 데모/오프라인용 '알려진' 종목(섹터·기준가·상장주식수 enrich). 실데이터 수집 시엔
KRX/네이버가 상위 N종목을 `stock` 테이블에 적재하고, 읽기 계층은 그 테이블(=유니버스)을 본다.
→ 유니버스 크기는 settings.UNIVERSE_TOP_N 으로 확장(6개 고정이 아니라 상위 150 등)."""
from sqlalchemy import text

from app import db

# ticker → (이름, 시장, 섹터, 기준가, 상장주식수(억 단위 아님 실수량 근사))
STOCKS = {
    "005930": ("삼성전자",        "KOSPI",  "반도체",     71000,  5_969_782_550),
    "000660": ("SK하이닉스",      "KOSPI",  "반도체",     173000,   728_002_365),
    "042700": ("한미반도체",      "KOSPI",  "반도체장비", 102500,    97_000_000),
    "357780": ("솔브레인",        "KOSDAQ", "반도체소재", 317000,    15_600_000),
    "035420": ("NAVER",           "KOSPI",  "인터넷",     233000,   146_000_000),
    "035720": ("카카오",          "KOSPI",  "인터넷",      45200,   445_000_000),
    "005380": ("현대차",          "KOSPI",  "자동차",     246000,   209_000_000),
    "000270": ("기아",            "KOSPI",  "자동차",     110000,   400_000_000),
    "012330": ("현대모비스",      "KOSPI",  "자동차부품", 304500,    94_000_000),
    "051910": ("LG화학",          "KOSPI",  "화학",       262500,    70_600_000),
    "373220": ("LG에너지솔루션",  "KOSPI",  "2차전지",    379000,   234_000_000),
    "006400": ("삼성SDI",         "KOSPI",  "2차전지",    324000,    68_700_000),
    "005490": ("POSCO홀딩스",     "KOSPI",  "철강",       394000,    82_500_000),
    "207940": ("삼성바이오로직스","KOSPI",  "바이오",     713000,    71_100_000),
    "128940": ("한미약품",        "KOSPI",  "제약",       399500,    12_600_000),
    "000100": ("유한양행",        "KOSPI",  "제약",        78000,    76_000_000),
    "196170": ("알테오젠",        "KOSDAQ", "바이오",     357500,    53_000_000),
}

# 실습 시나리오에서 쓰는 종목(과거 구간)도 유니버스에 포함되어야 함 → 전부 위에 있음.



def seed_stocks(conn):
    """stock 테이블에 '알려진' 종목 upsert(이름·시장·섹터). 데모/시드용."""
    for t, (name, market, sector, _p, _s) in STOCKS.items():
        upsert_stock(conn, t, name, market, sector)


def upsert_stock(conn, ticker, name, market, sector=None):
    """stock 테이블 1행 upsert — 실데이터 수집 시 발견한 종목도 여기로."""
    conn.execute(text(
        "INSERT INTO stock(ticker, name, market, sector) VALUES(:t,:n,:m,:s) "
        "ON CONFLICT(ticker) DO UPDATE SET name=:n, market=:m, "
        "sector=COALESCE(:s, stock.sector)"),
        {"t": ticker, "n": name, "m": market, "s": sector})


def universe():
    """현재 유니버스 = stock 테이블의 종목들(적재된 것). 없으면 STOCKS 로 폴백."""
    rows = db.query("SELECT ticker FROM stock ORDER BY ticker")
    return [r["ticker"] for r in rows] or list(STOCKS.keys())


def exists(ticker):
    """알려진(큐레이션) 종목이거나 stock 테이블에 있는 종목인가."""
    if not ticker:
        return False
    return ticker in STOCKS or bool(db.query("SELECT 1 FROM stock WHERE ticker=:t", {"t": str(ticker)[:20]}))


def meta(ticker):
    """이름/시장/섹터 — 알려진 종목은 STOCKS(섹터 품질↑), 나머지는 stock 테이블."""
    if ticker in STOCKS:
        n, m, s, _p, _sh = STOCKS[ticker]
        return {"ticker": ticker, "name": n, "market": m, "sector": s}
    rows = db.query("SELECT name, market, sector FROM stock WHERE ticker=:t", {"t": ticker})
    if rows:
        return {"ticker": ticker, "name": rows[0]["name"],
                "market": rows[0]["market"], "sector": rows[0]["sector"] or ""}
    return {"ticker": ticker, "name": ticker, "market": "", "sector": ""}


def meta_map(tickers=None):
    """여러 종목의 이름/시장/섹터를 쿼리 1번으로 — 목록 화면에서 종목마다 meta() 를 부르던 N+1 쿼리 제거.
    (150종목 순위표가 5초마다 갱신될 때 요청당 150번 → 1번)"""
    params, flt = {}, ""
    if tickers is not None:
        tickers = list(tickers)
        if not tickers:
            return {}
        flt, params = " WHERE ticker = ANY(:ts)", {"ts": tickers}
    rows = {r["ticker"]: r for r in db.query("SELECT ticker, name, market, sector FROM stock" + flt, params)}
    out = {}
    for t in (tickers if tickers is not None else list(rows)):
        if t in STOCKS:
            n, m, s, _p, _sh = STOCKS[t]
            out[t] = {"ticker": t, "name": n, "market": m, "sector": s}
        elif t in rows:
            r = rows[t]
            out[t] = {"ticker": t, "name": r["name"], "market": r["market"], "sector": r["sector"] or ""}
        else:
            out[t] = {"ticker": t, "name": t, "market": "", "sector": ""}
    return out


def shares_of(ticker):
    """알려진 종목의 상장주식수(시총 계산용). 모르면 None."""
    return STOCKS[ticker][4] if ticker in STOCKS else None
