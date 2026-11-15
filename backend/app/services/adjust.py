"""수정주가 — 액면분할·무상증자·감자 같은 권리 이벤트가 낀 기간의 가격을 같은 기준으로 맞춘다(화면용 표).

왜: 원 종가로 기간 수익률·변동성·최대낙폭을 계산하면 액면분할(5:1) 날이 '−80% 폭락'으로 들어간다.
    종목 상세 차트에 절벽이 생기고, 추천 모델의 모멘텀·변동성·낙폭이 틀려 몇 달 동안 그 종목이 부당하게 빠지며,
    '한 달 전 추천 결과'가 −80% 로 보인다.
원리(분석 계층 V007 analytics.price_adj_event 와 같은 규칙):
    KRX 등락률은 기준가(권리락이 반영된 전일 가격) 대비라 이벤트 날에도 실제 수익률이다.
    이벤트 날 = 원 종가 비율 c_t/c_{t-1} 과 1 + 등락률/100 이 TOL(0.5%) 넘게 다른 날.
    조정계수 f = (c_t/c_{t-1}) / (1 + r_t/100) — 그 날 '이전' 가격에 곱하고 거래량은 f 로 나눈다.
    평소에는 두 비율이 반올림 오차 안에서 같으므로 원 가격을 그대로 둔다.
원 가격은 표에 그대로 두고 읽을 때만 맞춘다(재적재와 충돌 없음, 언제든 원래 값 확인 가능).
"""
from __future__ import annotations

TOL = 0.005


def _skipped_trading_day(d0, d1):
    """d0 와 d1 사이에 (주말·휴장일이 아닌) 거래일이 끼어 있나 — 봉이 빠진 날을 '권리 이벤트'로 오인하지 않게.
    빠진 날의 등락이 0.5% 를 넘으면 원 비율과 공식 등락률이 달라 보이는데, 그건 이벤트가 아니라 결측이다."""
    from datetime import date, timedelta
    from app.services import quotes
    try:
        a, b = date.fromisoformat(str(d0)[:10]), date.fromisoformat(str(d1)[:10])
    except ValueError:
        return False
    d = a + timedelta(days=1)
    for _ in range(40):                        # 연휴라도 40일을 넘게 비는 일은 없다 — 넘으면 결측으로 본다
        if d >= b:
            return False
        if d.weekday() < 5 and not quotes.is_holiday("kr", d):
            return True
        d += timedelta(days=1)
    return True


def max_events(n):
    """한 시계열에서 믿을 수 있는 권리 이벤트 수의 상한 — 실제로는 1년에 많아야 한두 번.
    이보다 많이 '어긋나면' 등락률 자료가 종가와 맞지 않는 것(잘못 적재·임의 값)이라 조정하지 않는다."""
    return max(2, int(n * 0.02))


def event_factors(dates, closes, rates, tol=TOL, check_gap=True):
    """시간순 dates·closes 와 {날짜: 공식 등락률(%)} → {이벤트 날짜: f}. 바로 앞 거래일 봉이 있는 날만 판정.
    어긋난 날이 너무 많으면(max_events 초과) 등락률을 믿을 수 없다고 보고 빈 dict(원 가격 그대로)."""
    out = {}
    for i in range(1, len(closes)):
        r = rates.get(dates[i])
        c0, c1 = closes[i - 1], closes[i]
        if r is None or not c0 or not c1 or c0 <= 0 or c1 <= 0 or r <= -100:
            continue
        f = (float(c1) / float(c0)) / (1 + float(r) / 100)
        if abs(f - 1) > tol and not (check_gap and _skipped_trading_day(dates[i - 1], dates[i])):
            out[dates[i]] = f
    return out if len(out) <= max_events(len(closes)) else {}


def adjust_bars(bars, rates, keys=("open", "high", "low", "close"), vol_key="volume", date_key="trade_date"):
    """시간순 봉(dict) → 마지막 봉 기준 수정주가 봉(새 dict 목록). 이벤트가 없으면 받은 목록 그대로.
    rates: {날짜: 공식 등락률(%)} — 없으면 그 날은 이벤트로 보지 않는다(원 가격 유지)."""
    if not bars or not rates:
        return bars
    dates = [b[date_key] for b in bars]
    closes = [b.get("close") for b in bars]
    ev = event_factors(dates, closes, rates)
    if not ev:
        return bars
    out, F = [], 1.0
    for b in reversed(bars):
        nb = dict(b)
        if F != 1.0:
            for k in keys:
                if nb.get(k) is not None:
                    nb[k] = round(float(nb[k]) * F, 4)
            if vol_key and nb.get(vol_key) is not None:
                nb[vol_key] = int(round(float(nb[vol_key]) / F))
        out.append(nb)
        F *= ev.get(b[date_key], 1.0)          # 이 날 이벤트 → 이 날 '이전' 봉부터 계수를 곱한다
    return out[::-1]


def adjust_closes(dates, closes, rates):
    """종가 목록만 — risk 처럼 (날짜, 종가) 두 목록을 쓰는 곳."""
    bars = adjust_bars([{"trade_date": d, "close": c} for d, c in zip(dates, closes)], rates, keys=("close",),
                       vol_key=None)
    return [b["close"] for b in bars]


def factor_between(dates, closes, rates, start, end=None):
    """start(포함 안 함) ~ end(포함) 사이 이벤트 계수의 곱 — start 날 가격 × 이 값 = end 기준 가격."""
    F = 1.0
    for d, f in event_factors(dates, closes, rates).items():
        if d > start and (end is None or d <= end):
            F *= f
    return F


# ── DB(화면용 표 daily_price 의 KRX 공식 등락률) ──────────────────────────
def rates_for(tickers, start=None, end=None):
    """{종목: {날짜: 공식 등락률}} — daily_price.change_rate(KRX FLUC_RT·KIS). 비어 있으면 그 날은 원 가격."""
    from app import db
    tickers = list(tickers or [])
    if not tickers:
        return {}
    sql = "SELECT ticker, trade_date, change_rate FROM daily_price WHERE ticker = ANY(:ts) AND change_rate IS NOT NULL"
    params = {"ts": tickers}
    if start:
        sql += " AND trade_date >= :s"
        params["s"] = start
    if end:
        sql += " AND trade_date <= :e"
        params["e"] = end
    out = {}
    for r in db.query(sql, params):
        out.setdefault(r["ticker"], {})[r["trade_date"]] = float(r["change_rate"])
    return out


def events_since(tickers, start, end=None):
    """종목마다 start(포함 안 함) 이후 ~ end 의 권리 이벤트 {종목: {날짜: f}} — 화면용 표(ohlc 종가 + daily_price 등락률)."""
    from app import db
    tickers = list(tickers or [])
    if not tickers:
        return {}
    params = {"ts": tickers, "s": start}
    sql = ("SELECT o.ticker, o.trade_date, o.close FROM ohlc o WHERE o.ticker = ANY(:ts) AND o.trade_date >= :s "
           "AND o.close > 0")
    if end:
        sql += " AND o.trade_date <= :e"
        params["e"] = end
    by = {}
    for r in db.query(sql + " ORDER BY o.ticker, o.trade_date", params):
        by.setdefault(r["ticker"], ([], []))
        by[r["ticker"]][0].append(r["trade_date"])
        by[r["ticker"]][1].append(float(r["close"]))
    rates = rates_for(tickers, start, end)
    return {t: {d: f for d, f in event_factors(ds, cs, rates.get(t, {})).items() if d > start and (end is None or d <= end)}
            for t, (ds, cs) in by.items()}


def factors_since(tickers, start, end=None):
    """종목마다 start 이후(~end) 권리 이벤트 계수의 곱 — '그날 산 가격'을 지금 기준으로 바꿀 때(start 가격 × F)."""
    out = {}
    for t, ev in events_since(tickers, start, end).items():
        F = 1.0
        for f in ev.values():
            F *= f
        out[t] = F
    return out
