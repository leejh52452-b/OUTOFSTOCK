"""장기 실전 모의투자 — 현재가로 모의 매수한 뒤, '실제 최신가' 대비 평가/실현손익을 추적.

핵심(추가 아이디어): "그때 샀다면 지금 실제로 얼마?" — 과거 사례 실습(미래 은닉)과 달리
실데이터의 현재 가격으로 사고, 시간이 흐른 뒤 실제 시세로 평가한다(장기·실제 예측).
지갑과 연동 — 매수 시 지갑에서 출금, 매도 시 지갑으로 입금(원장 기록).
평가액은 최신 종가(장중이면 KIS 실시간 오버레이) 기준으로 mark-to-market.
"""
from datetime import datetime, timedelta, timezone, date

from sqlalchemy import text

from app import clock
from app import db
from app.services import wallet, catalog, quotes

KST = timezone(timedelta(hours=9))


class InvestError(Exception):
    """장기 투자 규칙 위반(수량 오류·현재가 없음·타인 포지션 등)."""


def _now():
    return clock.iso_now()


def _current_price(ticker, live=None):
    """현재가 — quotes 규칙(장중 LIVE / 장 마감 후 당일 종가 / 개장 전 직전 종가).
    live: 미리 계산한 {ticker: 유효시세} 맵(목록 평가 시 쿼리 1번으로 재사용)."""
    q = live.get(ticker) if live is not None else quotes.kr_quote(ticker)
    return int(q["price"]) if q and q.get("price") else 0


def _pending_factor(ticker, q, since):
    """오늘 생긴 권리 이벤트(장 마감 뒤 apply_corporate_actions 가 일봉으로 확정하기 전).
    시세의 기준가(prev_close — 거래소가 권리락·분할을 반영해 고친 전일 가격)와 저장된 전일 종가(원 가격)의 비율이
    0.5% 넘게 다르면 그 비율 f. 5:1 분할 첫날 장중에 보유분이 '−80%'로 평가되거나, 그 값으로 매도돼 손실이 굳지 않게.
    since = 매수일·마지막 반영일 — 그 뒤 날짜의 이벤트만. 전일 봉이 빠졌으면(결측) 판단하지 않는다."""
    from app.services import adjust
    sd = (q or {}).get("session_date")
    pc = (q or {}).get("prev_close")
    if not sd or not pc or sd <= (since or ""):
        return 1.0
    row = db.query("SELECT trade_date, close FROM ohlc WHERE ticker = :t AND trade_date < :d AND close > 0 "
                   "ORDER BY trade_date DESC LIMIT 1", {"t": ticker, "d": sd})
    if not row or adjust._skipped_trading_day(row[0]["trade_date"], sd):
        return 1.0
    f = float(pc) / float(row[0]["close"])
    return f if abs(f - 1) > adjust.TOL else 1.0


def _apply_factor(conn, position_id, f, d):
    """포지션에 계수 f 반영(수량 ÷ f, 매수가 × f) — 원가·지갑·실현손익은 그대로. adj_through 로 한 번만."""
    return conn.execute(text(
        "UPDATE invest_position SET quantity = GREATEST(1, round(quantity / :f)), buy_price = buy_price * :f, "
        "sold_qty = CASE WHEN sold_qty IS NULL THEN NULL ELSE round(sold_qty / :f) END, adj_through = :d "
        "WHERE position_id = :p AND status = 'open' AND (adj_through IS NULL OR adj_through < :d)"),
        {"f": f, "d": d, "p": position_id}).rowcount or 0


def _latest_date(ticker):
    q = quotes.kr_quote(ticker)
    return q["session_date"] if q and q.get("session_date") else datetime.now(KST).date().isoformat()


def _days_held(bday):
    try:
        return (datetime.now(KST).date() - date.fromisoformat(bday)).days
    except (ValueError, TypeError):
        return 0


def buy(username, ticker, quantity):
    """현재가로 모의 매수 — 지갑에서 매수대금 출금 + 포지션 개설."""
    quantity = int(quantity)
    if quantity <= 0:
        raise InvestError("수량은 1 이상이어야 합니다")
    price = _current_price(ticker)
    if price <= 0:
        raise InvestError("현재가를 확인할 수 없는 종목입니다")
    meta = catalog.meta(ticker)
    cost = price * quantity
    bday = _latest_date(ticker)
    with db.transaction() as conn:
        wallet.ensure(conn, username)
        wallet.debit(conn, username, cost, "invest_buy", ref=ticker,
                     memo=f"{meta['name']} {quantity}주 매수 @ {price:,}")
        pid = conn.execute(db.invest_position.insert().values(
            username=username, ticker=ticker, quantity=quantity, buy_price=price,
            buy_date=bday, cost=cost, status="open", created_at=_now())).inserted_primary_key[0]
        bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                           {"u": username}).scalar()
    return {"ok": True, "position_id": pid, "ticker": ticker, "name": meta["name"],
            "quantity": quantity, "buy_price": price, "cost": cost, "balance": int(bal)}


def sell(username, position_id, quantity=None):
    """현재가로 모의 매도 — 지갑으로 매도대금 입금. 전량이면 포지션 종료, 일부면 축소.
    동시성: 포지션 행을 FOR UPDATE 로 잠근다 — 같은 포지션에 매도 요청이 겹쳐도(더블클릭·재시도)
    두 번째 요청은 첫 요청이 끝난 뒤의 상태(청산됨·남은 수량)를 보고 판단한다(매도대금 이중 입금 방지).
    부분 매도: 그 몫의 실현손익·매도대금·매도수량을 누계로 남긴다(예전엔 사라졌다).
    quantity 는 '남은 보유 수량' — 전량 매도하면 0(판 수량은 sold_qty 누계)."""
    if quantity is not None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise InvestError("매도 수량이 올바르지 않습니다") from None
        if quantity <= 0:                                # 예전엔 0 도 '전량'으로 처리했다
            raise InvestError("매도 수량은 1 이상이어야 합니다")
    with db.transaction() as conn:
        wallet.hold(conn, username)                      # 잠금 순서: 계정 → 포지션 → 지갑(탈퇴와 교착하지 않게)
        p = conn.execute(text("SELECT * FROM invest_position WHERE position_id=:p FOR UPDATE"),
                         {"p": position_id}).mappings().first()
        if not p:
            raise InvestError("포지션을 찾을 수 없습니다")
        if p["username"] != username:
            raise InvestError("본인 포지션이 아닙니다")
        if p["status"] != "open":
            raise InvestError("이미 청산된 포지션입니다")
        price = _current_price(p["ticker"])
        if price <= 0:
            raise InvestError("현재가를 확인할 수 없습니다")
        # 오늘 분할·무상증자가 있었는데 아직 반영 전이면 먼저 반영(분할 전 수량을 분할 뒤 가격에 파는 손실 방지)
        q = quotes.kr_quote(p["ticker"])
        f = _pending_factor(p["ticker"], q, max(p["buy_date"] or "", p["adj_through"] or ""))
        if f != 1.0 and _apply_factor(conn, position_id, f, q["session_date"]):
            p = conn.execute(text("SELECT * FROM invest_position WHERE position_id=:p"), {"p": position_id}).mappings().first()
        qty = p["quantity"] if quantity is None else quantity          # 생략=전량
        if qty > p["quantity"]:
            raise InvestError(f"보유 수량({p['quantity']:,}주)보다 많이 팔 수 없습니다")
        proceeds = price * qty
        sold_cost = p["cost"] if qty == p["quantity"] else round(p["cost"] * qty / p["quantity"])
        sday = _latest_date(p["ticker"])
        wallet.credit(conn, username, proceeds, "invest_sell", ref=p["ticker"],
                      memo=f"{catalog.meta(p['ticker'])['name']} {qty}주 매도 @ {price:,}")
        full = qty == p["quantity"]
        realized = proceeds - sold_cost
        r = conn.execute(text(
            "UPDATE invest_position SET quantity=quantity-:q, cost=cost-:c, "
            "status=CASE WHEN :closing THEN 'closed' ELSE status END, "
            "sell_price=:sp, sell_date=:sd, proceeds=COALESCE(proceeds,0)+:pr, "
            "realized_pnl=COALESCE(realized_pnl,0)+:rp, sold_qty=COALESCE(sold_qty,0)+:q "
            "WHERE position_id=:p AND status='open' AND quantity>=:q"),
            {"q": qty, "c": sold_cost, "closing": full, "sp": price, "sd": sday, "pr": proceeds,
             "rp": realized, "p": position_id})
        if r.rowcount != 1:                              # 잠금 덕에 일어나지 않지만, 어긋나면 입금까지 롤백
            raise InvestError("포지션이 방금 바뀌었어요. 다시 시도해 주세요")
        bal = conn.execute(text("SELECT balance FROM wallet WHERE username=:u"),
                           {"u": username}).scalar()
    return {"ok": True, "position_id": position_id, "sold": qty, "sell_price": price,
            "proceeds": proceeds, "realized_pnl": realized, "closed": full, "balance": int(bal)}


def apply_corporate_actions():
    """보유 중 포지션에 권리 이벤트(액면분할·무상증자·감자) 반영 — 증권사가 분할 때 주식 수를 늘려 주는 것과 같다.
    수량 ÷ f, 매수가 × f(5:1 분할이면 수량 ×5, 매수가 ÷5). 원가·지갑·실현손익은 그대로라 손익이 '−80%'로 보이지 않는다.
    adj_through(반영한 마지막 이벤트 날짜)로 같은 이벤트를 두 번 반영하지 않는다(멱등). 단주는 반올림(교육용 단순화).
    매수일 당일 이벤트는 반영하지 않는다(이미 분할 뒤 가격에 산 것). 반환 {checked, adjusted}."""
    from app.services import adjust
    rows = db.query("SELECT position_id, ticker, quantity, buy_price, buy_date, sold_qty, adj_through "
                    "FROM invest_position WHERE status = 'open'")
    adjusted = 0
    for p in rows:
        start = max(p["buy_date"] or "", p["adj_through"] or "")
        if not start:
            continue
        ev = adjust.events_since([p["ticker"]], start).get(p["ticker"]) or {}
        if not ev:
            continue
        F = 1.0
        for f in ev.values():
            F *= f
        last = max(ev)
        with db.transaction() as conn:
            adjusted += _apply_factor(conn, p["position_id"], F, last)
    return {"checked": len(rows), "adjusted": adjusted}


def positions(username):
    """내 장기 투자 — 보유(평가손익, mark-to-market) + 청산(실현손익) + 합계."""
    rows = db.query("SELECT * FROM invest_position WHERE username=:u ORDER BY position_id DESC",
                    {"u": username})
    live = quotes.kr_quotes([p["ticker"] for p in rows if p["status"] == "open"])
    open_list, closed_list = [], []
    tot_cost = tot_value = realized_sum = 0
    for p in rows:
        meta = catalog.meta(p["ticker"])
        realized_sum += p["realized_pnl"] or 0            # 부분 매도분(보유 중 포지션)도 실현손익에 포함
        if p["status"] == "open":
            cur = _current_price(p["ticker"], live)
            # 오늘 권리 이벤트(장 마감 뒤 확정 반영 전) — 화면 평가는 반영된 수량·매수가로(저장은 매도·장 마감 때)
            f = _pending_factor(p["ticker"], live.get(p["ticker"]), max(p["buy_date"] or "", p["adj_through"] or ""))
            qty = max(1, round(p["quantity"] / f)) if f != 1.0 else p["quantity"]
            bp = p["buy_price"] * f if f != 1.0 else p["buy_price"]
            value = cur * qty
            pnl = value - p["cost"]
            pnl_pct = round((cur / bp - 1) * 100, 2) if bp else 0.0
            open_list.append({
                "position_id": p["position_id"], "ticker": p["ticker"], "name": meta["name"],
                "market": meta["market"], "quantity": qty, "buy_price": bp,
                "buy_date": p["buy_date"], "cost": p["cost"], "current_price": cur,
                "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
                "realized_pnl": p["realized_pnl"] or 0, "sold_qty": p.get("sold_qty") or 0,
                "days_held": _days_held(p["buy_date"]), "pending_adjustment": f != 1.0,
                "live": (live.get(p["ticker"]) or {}).get("status") == "live"})
            tot_cost += p["cost"]
            tot_value += value
        else:
            # 여러 번 나눠 판 포지션은 '판 수량 합계·평균 매도가·누적 실현손익'으로 보여준다.
            # sold_qty 가 없는 예전 행은 마지막 매도분만 기록돼 있으므로 그 값 그대로(서로 일관됨).
            sold = p.get("sold_qty") or p["quantity"]
            rp, pr = p["realized_pnl"] or 0, p["proceeds"] or 0
            basis = pr - rp                                    # 매도한 주식의 원가 합(= 매도대금 − 실현손익)
            avg_sell = round(pr / sold) if (p.get("sold_qty") and sold and pr) else p["sell_price"]
            closed_list.append({
                "position_id": p["position_id"], "ticker": p["ticker"], "name": meta["name"],
                "quantity": sold, "buy_price": p["buy_price"], "buy_date": p["buy_date"],
                "sell_price": avg_sell, "sell_date": p["sell_date"],
                "realized_pnl": rp,
                "realized_pct": round(rp / basis * 100, 2) if basis > 0 else
                (round((p["sell_price"] / p["buy_price"] - 1) * 100, 2) if p["buy_price"] and p["sell_price"] else 0.0)})
    summary = {
        "open_count": len(open_list), "closed_count": len(closed_list),
        "cost": tot_cost, "value": tot_value, "pnl": tot_value - tot_cost,
        "pnl_pct": round((tot_value / tot_cost - 1) * 100, 2) if tot_cost else 0.0,
        "realized_pnl": realized_sum}
    return {"summary": summary, "open": open_list, "closed": closed_list}
