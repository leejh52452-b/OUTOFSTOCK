"""해외(미국) 데이터 적재 — 일봉·종목정보·미국 지수·환율. (국내 marketgen 의 해외판)

  demo(real=False): 표본(gen) 일봉 — 네트워크 불필요
  real(real=True) : · 일봉      FinanceDataReader(야후 차트, 키 불필요)
                    · 발행주식수 SEC EDGAR(키 불필요, SEC_USER_AGENT 연락처만) → 시가총액 계산
                    · 시세 스냅샷 Finnhub /quote (FINNHUB_API_KEY 있으면) → 장 마감 후에도 '당일 종가' 즉시 반영
                    · 지수       FDR US500(S&P500)·IXIC(나스닥)·DJI(다우)
                    · 환율       수출입은행(EXIM_API_KEY) → 없으면 FDR USD/KRW
  종목별로 실데이터가 실패하면 그 종목만 표본으로 채우고 출처(source)를 남긴다 → 화면에 '표본' 표기.
실행:  python run.py fetch-overseas   (real 에도 포함)
"""
import logging
import time
from datetime import timedelta

from sqlalchemy import text

from app import db
from app.config import settings
from app.services import quotes
from app.services.overseas import US_STOCKS, US_INDEX, gen_bars

log = logging.getLogger(__name__)
GEN_DAYS = 320                      # 1년 차트 + 지표 워밍업
FDR_LOOKBACK_DAYS = 480
FDR_TIMEOUT = 60                    # FinanceDataReader 는 내부에서 시간 제한 없이 요청 → 이 시간 넘으면 실패 처리

_UPSERT_BAR = text(
    "INSERT INTO overseas_ohlc(symbol,trade_date,open,high,low,close,volume) "
    "VALUES(:s,:d,:o,:h,:l,:c,:v) ON CONFLICT(symbol,trade_date) DO UPDATE SET "
    "open=:o,high=:h,low=:l,close=:c,volume=:v")


def replace_bars(conn, symbol, bars):
    """종목 일봉을 통째로 교체(표본↔실데이터 전환 시 휴장일·미래 날짜 표본 봉이 남지 않게)."""
    conn.execute(text("DELETE FROM overseas_ohlc WHERE symbol=:s"), {"s": symbol})
    if bars:
        conn.execute(_UPSERT_BAR, [{"s": symbol, "d": b["trade_date"], "o": b["open"], "h": b["high"],
                                    "l": b["low"], "c": b["close"], "v": b["volume"]} for b in bars])


def upsert_meta(conn, symbol, source, shares=None, market_cap=None, name=None):
    en, ko, ex, sec, _base, approx_shares = US_STOCKS[symbol]
    conn.execute(text(
        "INSERT INTO overseas_stock(symbol,name,name_ko,exchange,sector,shares,market_cap,source,updated_at) "
        "VALUES(:s,:n,:k,:e,:sec,:sh,:mc,:src,:t) ON CONFLICT(symbol) DO UPDATE SET "
        "name=:n, name_ko=:k, exchange=:e, sector=:sec, "
        "shares=COALESCE(:sh, overseas_stock.shares), "
        "market_cap=COALESCE(:mc, overseas_stock.market_cap), source=:src, updated_at=:t"),
        {"s": symbol, "n": name or en, "k": ko, "e": ex, "sec": sec,
         "sh": shares if shares else (approx_shares if source == "gen" else None),
         "mc": market_cap, "src": source, "t": quotes.iso_now()})


def frame_to_bars(df):
    """FDR DataFrame(Open/High/Low/Close/Volume, 날짜 인덱스) → 봉 리스트. NaN·0 행은 건너뜀."""
    bars = []
    if df is None:
        return bars
    for idx, r in df.iterrows():
        try:
            c = float(r["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if c != c or c <= 0:                                   # NaN/0
            continue

        def g(k, dflt):
            try:
                v = float(r[k])
                return v if v == v and v > 0 else dflt
            except (KeyError, TypeError, ValueError):
                return dflt
        try:
            vol = float(r["Volume"])
            vol = int(vol) if vol == vol else 0
        except (KeyError, TypeError, ValueError):
            vol = 0
        bars.append({"trade_date": idx.strftime("%Y-%m-%d"), "open": round(g("Open", c), 4),
                     "high": round(g("High", c), 4), "low": round(g("Low", c), 4),
                     "close": round(c, 4), "volume": vol})
    return bars


def drop_unfinished(bars, now=None):
    """미국 정규장이 끝나기 전(프리마켓·장중)에 받은 '오늘' 봉은 아직 확정 전(진행 중)이므로 버린다.
    예) 한국시간 23:00(run.py real 정기 실행) = 뉴욕 오전 9~10시 — 그때의 오늘 봉을 '종가'로 저장하면 안 된다.
    장중 가격은 실시간(Finnhub)이 따로 보여 주고, 확정 종가는 다음 수집(화~토 07:30)에서 들어온다."""
    if bars and quotes.us_phase(now) in ("pre", "open") and bars[-1]["trade_date"] >= quotes.us_session_date(now):
        return bars[:-1]
    return bars


def fetch_fdr_bars(symbol, days=FDR_LOOKBACK_DAYS, finished_only=True):
    import FinanceDataReader as fdr
    from app.retry import call_with_timeout
    start = (quotes.to_et().date() - timedelta(days=days)).isoformat()
    bars = frame_to_bars(call_with_timeout(fdr.DataReader, symbol, start, timeout=FDR_TIMEOUT))
    return drop_unfinished(bars) if finished_only else bars


def _upsert_index(conn, code, name, value, change, rate, source=None, as_of=None):
    """미국 지수·환율 1행. 출처·기준일도 함께 — 예전엔 안 써서, 한 번이라도 데모를 돌린 DB 는 실데이터 환율에도
    source='gen' 이 남아 화면에 '표본'으로 보였다."""
    conn.execute(text(
        "INSERT INTO market_index(code,name,value,change,change_rate,ts,source,as_of) "
        "VALUES(:c,:n,:v,:ch,:r,:ts,:src,:a) ON CONFLICT(code) DO UPDATE SET name=:n,value=:v,change=:ch,"
        "change_rate=:r,ts=:ts,source=COALESCE(:src, market_index.source),as_of=COALESCE(:a, market_index.as_of)"),
        {"c": code, "n": name, "v": value, "ch": change, "r": rate, "ts": quotes.iso_now(), "src": source,
         "a": as_of})


def _index_from_bars(bars):
    if len(bars) < 2:
        return None
    last, prev = bars[-1]["close"], bars[-2]["close"]
    return last, round(last - prev, 2), round((last / prev - 1) * 100, 2) if prev else 0.0


def fetch_index_bars(real):
    """미국 3대 지수 일봉(네트워크 — 트랜잭션 밖에서). {code: bars 또는 None}."""
    out = {}
    for code, (_name, fdr_code, _base) in US_INDEX.items():
        out[code] = None
        if real:
            try:
                out[code] = fetch_fdr_bars(fdr_code, days=20)
            except Exception as e:                             # noqa: BLE001
                log.warning("미국 지수 %s 수집 실패: %s", code, e)
    return out


def seed_indices(conn, real, fetched=None):
    """미국 3대 지수(SPX·NASDAQ·DOW). real 이면 FDR. 실패하면 실데이터가 이미 있으면 그대로 두고(예전: 6800·23000·
    47500 같은 표본 상수로 덮음), 한 번도 없었을 때만 표본(결정론적 등락)."""
    fetched = fetch_index_bars(real) if fetched is None else fetched
    ok = kept = 0
    for code, (name, _fdr_code, base) in US_INDEX.items():
        bars = fetched.get(code) or []
        vals = _index_from_bars(bars) if bars else None
        if vals:
            ok += 1
            _upsert_index(conn, code, name, *vals, source="fdr", as_of=bars[-1]["trade_date"])
            continue
        if real:
            prev = conn.execute(text("SELECT source FROM market_index WHERE code=:c"), {"c": code}).scalar()
            if prev and prev != "gen":
                kept += 1
                continue
        import random                                          # 표본: 지수·날짜별 결정론적 등락(지수마다 다르게)
        import zlib
        rng = random.Random(zlib.crc32(code.encode()) ^ int(quotes.us_session_date().replace("-", "")))
        rate = round(rng.uniform(-1.2, 1.2), 2)
        _upsert_index(conn, code, name, base, round(base * rate / 100, 2), rate, source="gen")
    if ok + kept == len(US_INDEX):        # 이번에 받은 값 + 유지한 예전 실데이터 = 모두 실데이터(표본 없음)
        return "fdr"
    return "mixed" if (ok or kept) else "gen"


def fetch_fx():
    """원/달러(네트워크 — 트랜잭션 밖에서). 수출입은행(키) → FDR(USD/KRW). 실패하면 None.
    등락은 '직전 영업일 고시 환율' 대비 — 예전엔 마지막으로 저장한 값과 비교해, 같은 날을 다시 받으면(화~토 07:30)
    등락이 0.00 으로 보였다."""
    from app.collectors import forex
    q = forex.usd_krw_quote()
    if q and q.get("rate"):
        rate, pv = q["rate"], q.get("prev") or q["rate"]
        return {"vals": (rate, round(rate - pv, 2), round((rate / pv - 1) * 100, 2) if pv else 0.0),
                "source": "koreaexim", "as_of": q.get("date")}
    try:
        bars = fetch_fdr_bars("USD/KRW", days=15, finished_only=False)   # 환율은 24시간 거래 → 최신값
    except Exception as e:                                     # noqa: BLE001
        log.warning("환율(FDR) 수집 실패: %s", e)
        return None
    vals = _index_from_bars(bars)
    return {"vals": vals, "source": "fdr", "as_of": bars[-1]["trade_date"]} if vals else None


def seed_fx(conn, fetched=None):
    """원/달러 저장. 실패하면 기존 값 유지. 반환 출처(또는 None)."""
    fx = fetch_fx() if fetched is None else fetched
    if not fx:
        return None
    _upsert_index(conn, "USDKRW", "원/달러 환율", *fx["vals"], source=fx["source"], as_of=fx.get("as_of"))
    return fx["source"]


def snapshot_quotes(symbols=None, pace=1.05):
    """Finnhub /quote 스냅샷 → overseas_quote (장 마감 후에도 '당일 종가/직전 체결가' 반영). 무료 60회/분 준수."""
    from app.collectors import finnhub
    if not finnhub.enabled():
        return 0
    n = 0
    for s in symbols or list(US_STOCKS):
        q = finnhub.quote(s)
        if q:
            save_quote(s, q, source="finnhub", trade_ts=q.get("t"))
            n += 1
        time.sleep(pace)
    return n


def save_quote(symbol, q, source="finnhub", session_date=None, trade_ts=None):
    """Finnhub 형식 시세 → overseas_quote upsert. 거래일 = session_date 지정값 > 시세 시각(t)의 미국 날짜 > 오늘(ET).
    trade_ts(유닉스 초) = 그 가격이 체결된 시각 — 신선도(LIVE) 판정 기준. 예전엔 저장한 시각을 써서 장이 닫힌 주말에도
    10분마다 폴링할 때마다 2분씩 'LIVE' 로 보였다."""
    from datetime import datetime, timezone
    t = q.get("t")
    sdate = session_date or (quotes.to_et(datetime.fromtimestamp(t, timezone.utc)).date().isoformat()
                             if t else quotes.us_session_date())
    db.execute(
        "INSERT INTO overseas_quote(symbol,price,change,change_rate,prev_close,open,high,low,volume,"
        "session_date,source,ts) VALUES(:s,:p,:ch,:r,:pc,:o,:h,:l,:v,:sd,:src,:ts) "
        "ON CONFLICT(symbol) DO UPDATE SET price=:p,change=:ch,change_rate=:r,prev_close=:pc,"
        "open=:o,high=:h,low=:l,volume=:v,session_date=:sd,source=:src,ts=:ts",
        {"s": symbol, "p": q["price"], "ch": q.get("change"), "r": q.get("change_rate"),
         "pc": q.get("prev_close"), "o": q.get("open"), "h": q.get("high"), "l": q.get("low"),
         "v": q.get("volume"), "sd": sdate, "src": source,
         "ts": (datetime.fromtimestamp(float(trade_ts), timezone.utc).isoformat(timespec="seconds")
                if trade_ts else quotes.iso_now())})


def seed_overseas(real=False, symbols=None):
    """해외 일봉·종목정보·지수·환율 적재. 반환 요약(실데이터/표본 종목 수, 경고)."""
    syms = symbols or list(US_STOCKS)
    use_real = real and (settings.OVERSEAS_SOURCE or "auto").lower() != "gen"
    got_fdr, got_gen = [], []
    shares_map, fetched = {}, {}
    got_kept = []
    if use_real:
        from app.collectors import edgar
        for s in syms:                                         # SEC 공정사용: 초당 10회 이하
            shares_map[s] = edgar.shares_outstanding(s)
            time.sleep(0.12)
        # 네트워크는 전부 트랜잭션 밖에서 — 예전엔 DB 트랜잭션(행 잠금)을 연 채 FDR 을 불러, 응답 없는 연결 하나에
        # 23:00 배치와 그 잠금을 기다리는 07:30 작업까지 무기한 멈췄다(FDR 은 시간 제한이 없어 FDR_TIMEOUT 으로 끊음).
        for s in syms:
            try:
                fetched[s] = fetch_fdr_bars(s)
            except Exception as e:                             # noqa: BLE001 — 한 종목 실패가 전체를 막지 않게
                log.warning("해외 일봉 %s 실패: %s", s, e)
                fetched[s] = []
    idx_fetched = fetch_index_bars(use_real)
    fx_fetched = fetch_fx() if use_real else None
    with db.transaction() as conn:
        real_src = {r[0]: r[1] for r in conn.execute(text("SELECT symbol, source FROM overseas_stock")).all()}
        for s in syms:
            bars = fetched.get(s) or []
            if len(bars) >= 2:
                replace_bars(conn, s, bars)
                upsert_meta(conn, s, "fdr", shares=shares_map.get(s))
                got_fdr.append(s)
            elif use_real and real_src.get(s) == "fdr":       # 일시 실패 — 가진 실데이터 유지(예전: 표본 320봉으로 교체)
                got_kept.append(s)
            else:
                replace_bars(conn, s, gen_bars(s, GEN_DAYS))
                upsert_meta(conn, s, "gen")
                got_gen.append(s)
        idx_src = seed_indices(conn, use_real, idx_fetched)
        fx_src = seed_fx(conn, fx_fetched) if use_real else None
        src = "fdr" if not got_gen else ("gen" if not (got_fdr or got_kept) else "mixed")
        db.set_meta("overseas_source", src, conn)
        db.set_meta("us_index_source", idx_src, conn)
        if fx_src:
            db.set_meta("fx_source", fx_src, conn)
        asof = conn.execute(text("SELECT MAX(trade_date) FROM overseas_ohlc")).scalar()
        db.set_meta("overseas_asof", asof, conn)
    if not use_real:                                           # 데모: 이전 실시간 시세는 비워 표본과 섞이지 않게
        db.execute("DELETE FROM overseas_quote WHERE source <> 'sim' OR source IS NULL")
    snap = snapshot_quotes(got_fdr) if (use_real and got_fdr) else 0
    out = {"symbols": len(syms), "real": len(got_fdr), "kept": len(got_kept), "sample": len(got_gen), "source": src,
           "indices": idx_src, "fx": fx_src or "unchanged", "quotes": snap, "as_of": asof}
    warns = []
    if got_kept:
        warns.append(f"해외 {len(got_kept)}종목은 이번에 일봉을 받지 못해 이전 실데이터를 그대로 뒀습니다 "
                     f"({', '.join(got_kept[:6])}{'…' if len(got_kept) > 6 else ''}).")
    if real and got_gen:
        warns.append(f"해외 {len(got_gen)}종목은 실데이터(야후/FDR)를 받지 못해 표본으로 채웠습니다 "
                     f"({', '.join(got_gen[:6])}{'…' if len(got_gen) > 6 else ''}). "
                     "서버의 인터넷 연결을 확인하세요 — 화면엔 '표본'으로 표시됩니다.")
    if warns:
        out["warning"] = " ".join(warns)
        log.warning(out["warning"])
    return out
