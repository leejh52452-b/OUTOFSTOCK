"""관심종목(즐겨찾기) — 로그인 계정에 귀속. 등록/해제/목록.
목록은 현재가·등락률(장중이면 실시간 오버레이)까지 얹어 종목 순위표처럼 바로 렌더되게 돌려준다."""

from app import clock
from app import db
from app.services import catalog


def _now():
    return clock.iso_now()


def _known(ticker):
    """유니버스(stock 테이블)·대표종목(STOCKS)·해외 유니버스에 존재하는 코드인지."""
    if ticker in catalog.STOCKS:
        return True
    from app.services import overseas
    if overseas.is_symbol(ticker):
        return overseas.known(ticker)
    return bool(db.query("SELECT 1 FROM stock WHERE ticker=:t", {"t": ticker}))


def add(user_id, ticker):
    """관심종목 등록(이미 있으면 무시)."""
    if not user_id or not ticker:
        return {"ok": False}
    if not _known(ticker):                               # 유니버스에 없는 코드는 거부
        return {"ok": False, "reason": "unknown_ticker"}
    db.execute("INSERT INTO watchlist(user_id, ticker, created_at) VALUES(:u, :t, :c) "
               "ON CONFLICT (user_id, ticker) DO NOTHING",
               {"u": user_id, "t": ticker, "c": _now()})
    return {"ok": True, "ticker": ticker, "watched": True}


def remove(user_id, ticker):
    """관심종목 해제."""
    if not user_id:
        return {"ok": False}
    db.execute("DELETE FROM watchlist WHERE user_id=:u AND ticker=:t", {"u": user_id, "t": ticker})
    return {"ok": True, "ticker": ticker, "watched": False}


def tickers(user_id):
    """이 유저가 담은 종목코드 목록(프론트 별표 표시용)."""
    if not user_id:
        return []
    return [r["ticker"] for r in db.query(
        "SELECT ticker FROM watchlist WHERE user_id=:u ORDER BY id DESC", {"u": user_id})]


def items(user_id):
    """관심종목 + 현재가/등락률(quotes 규칙: 장중 LIVE / 장후 당일 종가). 해외 심볼(AAPL 등)도 지원."""
    if not user_id:
        return []
    from app.services import quotes, overseas
    tks = tickers(user_id)
    kr = [t for t in tks if not overseas.is_symbol(t)]
    us = [t for t in tks if overseas.is_symbol(t)]
    qmap = quotes.kr_quotes(kr)
    umap = overseas.quotes_map(us) if us else {}
    out = []
    for t in tks:
        if t in umap:
            u = umap[t]
            out.append({"ticker": t, "name": u["name_ko"] or u["name"], "market": u["exchange"],
                        "price": u["price"], "change_rate": u["change_rate"],
                        "live": u["status"] == "live", "status": u["status"],
                        "overseas": True, "currency": "USD"})
            continue
        m = catalog.meta(t)
        q = qmap.get(t) or {}                          # 시세가 아직 없어도 목록에서 빠지지 않게(가격은 null)
        out.append({"ticker": t, "name": m["name"], "market": m["market"],
                    "price": q.get("price"), "change_rate": q.get("change_rate"),
                    "live": q.get("status") == "live", "status": q.get("status"),
                    "overseas": False, "currency": "KRW"})
    return out
