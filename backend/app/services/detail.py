"""종목 상세 — 시가/고저/PER/PBR/시총/52주 등 펀더멘털 + 기간별 캔들(지표 오버레이).
캔들은 ohlc 테이블에서, 시총/거래대금은 카탈로그 상장주식수로 계산.
PER/PBR/배당수익률은 데모 상수(실서비스에선 DART 재무로 계산)."""
from app import db
from app.services import catalog
from app.services import indicators as ind
from app.services import quotes

_PERIOD_DAYS = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}


def _dart_pe_pb(ticker, mcap):
    """DART 재무로 PER(시총/순이익)·PBR(시총/자본총계) 산출 → (per, pbr, 재무 있음 여부).
    적자면 PER, 자본잠식이면 PBR 은 '의미 없음'(None) — 이건 데이터가 없는 게 아니라 값이 없는 것."""
    rows = db.query("SELECT net_income, total_equity FROM financial_statement "
                    "WHERE ticker=:t ORDER BY fiscal_year DESC LIMIT 1", {"t": ticker})
    if not rows or not mcap:
        return None, None, bool(rows)
    ni, eq = rows[0]["net_income"], rows[0]["total_equity"]
    per = round(mcap / ni, 2) if (ni and ni > 0) else None
    pbr = round(mcap / eq, 2) if (eq and eq > 0) else None
    return per, pbr, True

# 데모 펀더멘털(ticker → PER, PBR, 배당수익률%). 실서비스는 재무제표로 산출.
FUND = {
    "005930": (14.2, 1.4, 2.1), "000660": (11.8, 1.9, 1.2), "042700": (28.5, 4.1, 0.4),
    "357780": (18.9, 2.7, 0.6), "035420": (33.0, 1.8, 0.3), "035720": (41.5, 1.6, 0.2),
    "005380": (5.4, 0.6, 4.8), "000270": (4.9, 0.7, 5.1), "012330": (6.2, 0.7, 2.9),
    "051910": (16.1, 1.3, 1.1), "373220": (52.0, 3.4, 0.0), "006400": (22.7, 1.5, 0.5),
    "005490": (9.3, 0.5, 3.3), "207940": (62.3, 5.9, 0.0), "128940": (34.8, 3.2, 0.4),
    "000100": (12.9, 1.7, 1.9), "196170": (88.0, 9.1, 0.0),
}


def _last_bars(ticker, n, days=None):
    """최근 n개 일봉(시간순) — 단, 마지막 '연속 구간'만, days 를 주면 최신 봉 기준 그 달력일 이내만.
    같은 ohlc 표에 실습 시나리오 구간(2020~2023) 봉이 있어서, 날짜 제한 없이 '마지막 n개'를 읽으면
    최근 데이터가 짧을 때 52주 최고가·1Y 차트에 몇 년 전 봉이 섞였다."""
    if days:
        rows = db.query("SELECT trade_date,open,high,low,close,volume FROM ohlc WHERE ticker=:t "
                        "AND trade_date >= (SELECT MAX(trade_date) - CAST(:days AS integer) FROM ohlc WHERE ticker=:t) "
                        "ORDER BY trade_date DESC LIMIT :n", {"t": ticker, "n": n, "days": days})[::-1]
    else:
        rows = db.query("SELECT trade_date,open,high,low,close,volume FROM ohlc WHERE ticker=:t "
                        "ORDER BY trade_date DESC LIMIT :n", {"t": ticker, "n": n})[::-1]
    rows = quotes.recent_segment(rows)
    if rows:                                    # 수정주가 — 액면분할 날 차트 절벽·52주 최고가 왜곡 방지
        from app.services import adjust
        rates = adjust.rates_for([ticker], rows[0]["trade_date"], rows[-1]["trade_date"]).get(ticker, {})
        rows = adjust.adjust_bars(rows, rates)
    return rows


def stock_detail(ticker):
    """종목 상세. 현재가·시/고/저·거래량은 quotes.effective(장중 LIVE / 장후 당일 종가 / 개장 전 직전 종가).
    전일 대비는 '틱이 속한 영업일의 전일 종가' 기준(이전: 장중에 2거래일 전 종가와 비교하던 버그 수정)."""
    from app.services import market
    year = _last_bars(ticker, 300, 365)             # 52주 = 최신 봉 기준 1년(달력일)
    q = quotes.kr_quote(ticker)
    if not year and not q:
        return None
    m = catalog.meta(ticker)
    price = q["price"]
    mcap = market.mcap_now(ticker, price, market._mcap_map([ticker]))
    per, pbr, has_fin = _dart_pe_pb(ticker, mcap)    # 실데이터: DART 재무로 산출
    # 데모 상수(알려진 17종목)는 '표본 시세' 모드에서만, 그리고 재무가 아예 없을 때만 쓴다.
    # (예전엔 실데이터 모드에서도 적자 PER·자본잠식 PBR·배당수익률 자리에 데모 상수가 실제 값처럼 표시됐다)
    real = quotes.market_source() == "krx"
    fund = FUND.get(ticker) if not real else None
    if not has_fin and fund:
        per, pbr = fund[0], fund[1]
    div = fund[2] if fund else None                 # 배당수익률은 DART fnlttSinglAcnt 밖 → 표본 모드 상수만
    # 'dart' = 실시세 × 실제 DART 재무. 표본 시세 모드는 재무 행이 있어도(데모 재무·예전 수집분) 시총이 생성 값이라 'sample'
    fund_source = ("dart" if real else "sample") if has_fin else ("sample" if fund else None)
    year = quotes.merge_bar(year, q)                 # 당일 봉(틱) 반영 → 52주 고/저도 최신화
    if year:                                          # 52주 = 최근 봉 기준 365일 이내만
        from datetime import date as _date, timedelta as _td
        cutoff = (_date.fromisoformat(str(year[-1]["trade_date"])[:10]) - _td(days=365)).isoformat()
        year = [b for b in year if str(b["trade_date"])[:10] >= cutoff]
    highs = [b["high"] for b in year if b["high"] and b["high"] > 0]
    lows = [b["low"] for b in year if b["low"] and b["low"] > 0]      # 거래정지일 0원 저가 제외
    hi52 = max(highs) if highs else q["high"]
    lo52 = min(lows) if lows else q["low"]
    return {
        "ticker": ticker, "name": m["name"], "market": m["market"], "sector": m["sector"],
        "price": price, "change": round(q["change"], 2), "change_rate": q["change_rate"],
        "live": q["status"] == "live", "status": q["status"], "session_date": q["session_date"],
        "phase": quotes.kr_phase(), "source": quotes.market_source(),
        "fundamentals": {
            "open": q["open"], "high": q["high"], "low": q["low"],
            "volume": q["volume"], "value": q["value"],
            "market_cap": int(mcap), "per": per, "pbr": pbr,
            "dividend_yield": div, "high_52w": hi52, "low_52w": lo52,
            "prev_close": q["prev_close"],
            "fundamentals_source": fund_source,          # dart(실재무) · sample(데모 상수) · null(없음)
        },
    }


def ohlc_series(ticker, period="6M", indicators=None):
    n = _PERIOD_DAYS.get(period, 126)
    bars = _last_bars(ticker, n + 60, int((n + 60) * 1.6) + 14)   # 지표 워밍업분 더 읽고 뒤에서 자른다
    bars = quotes.merge_bar(bars, quotes.kr_quote(ticker))   # 장 마감 후에도 마지막 캔들 = 당일
    if not bars:
        return {"ticker": ticker, "period": period, "series": [], "overlays": {}}
    closes = [b["close"] for b in bars]
    want = [i.strip().upper() for i in (indicators or "").split(",") if i.strip()]
    overlays = {}
    if "SMA5" in want:
        overlays["SMA5"] = ind.sma(closes, 5)
    if "SMA20" in want:
        overlays["SMA20"] = ind.sma(closes, 20)
    if "SMA60" in want:
        overlays["SMA60"] = ind.sma(closes, 60)
    if "RSI14" in want:
        overlays["RSI14"] = ind.rsi(closes, 14)
    cut = max(0, len(bars) - n)                # 표시 구간만 반환(워밍업 절단)
    series = bars[cut:]
    overlays = {k: v[cut:] for k, v in overlays.items()}
    return {"ticker": ticker, "period": period, "series": series, "overlays": overlays}
