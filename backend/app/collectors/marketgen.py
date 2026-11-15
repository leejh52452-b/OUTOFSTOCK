"""시세 데이터 계층 — 실습·종목상세·랭킹이 읽는 일봉 OHLC 를 채운다.

소스(settings.MARKET_SOURCE = krx|gen):
  · krx  — KRX 정보데이터시스템 OpenAPI(공식, AUTH_KEY). basDd 하루씩 돌며 전 종목 수집 →
           시총 상위 UNIVERSE_TOP_N 종목을 stock 테이블(=유니버스)에 적재.
  · gen  — 결정론적 생성기(패턴 내장). 폐쇄망/오프라인에서도 전 화면을 채운다(데모 17종목).

`python run.py demo`=gen, `real`/`fetch-*`=KRX 실데이터(접근 막히면 자동 gen 폴백). 네이버 미사용.
재무는 DART, 뉴스는 Google RSS, 실시간은 KIS 로 분리 수집.
"""
import math
import random
import logging
import zlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app import db
from app.config import settings
from app.services import catalog

log = logging.getLogger(__name__)

TICK = 10                     # 호가단위 근사(원)
KST = timezone(timedelta(hours=9))


def _today():
    """기준일 = 한국 시각 기준 '오늘'(서버가 UTC여도 KST). 호출 시점에 계산(장기 프로세스에서도 최신)."""
    return datetime.now(KST).date()


def _seed_of(key):
    """결정론적 시드 — hash() 는 프로세스마다 달라(PYTHONHASHSEED) 실행할 때마다 데모 가격이 바뀌었다."""
    return zlib.crc32(str(key).encode("utf-8")) & 0xFFFF


# ── 날짜 유틸 ─────────────────────────────────────────────
def business_days(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _parse(s):
    y, m, d = map(int, s.split("-"))
    return date(y, m, d)


# ── 결정론적 생성기 ───────────────────────────────────────
def _drift_vol(pattern, frac):
    if pattern == "golden_cross":
        return (-0.006 if frac < 0.45 else 0.011), 0.012
    if pattern == "rsi_oversold":
        return (-0.013 if frac < 0.40 else (0.013 if frac < 0.78 else 0.001)), 0.016
    if pattern == "volume_breakout":
        return (0.0 if frac < 0.62 else 0.013), (0.008 if frac < 0.62 else 0.015)
    if pattern == "ma_support_fail":
        return (0.009 if frac < 0.40 else -0.011), 0.014
    if pattern == "rsi_divergence":
        return (-0.005 if frac < 0.62 else 0.009), 0.013
    if pattern == "sideways_cross":
        return 0.0, 0.010
    return 0.0004, 0.012


def gen_closes(n, base, seed, pattern=None, anchor_last=None):
    rng = random.Random(seed)
    closes, p = [], float(base)
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 0.0
        drift, vol = _drift_vol(pattern, frac)
        g = (rng.random() + rng.random() + rng.random() - 1.5) / 1.5
        step = drift + vol * g
        if pattern == "sideways_cross":
            step += 0.006 * math.sin(i / 3.0)
        p = max(1000.0, p * (1 + step))
        closes.append(p)
    if anchor_last is not None and closes:
        scale = anchor_last / closes[-1]
        closes = [c * scale for c in closes]
    return closes


def ohlc_from_closes(dates, closes, seed, pattern=None):
    rng = random.Random(seed ^ 0x9E3779B9)
    bars, prev = [], closes[0]
    n = len(closes)
    for i, (d, c) in enumerate(zip(dates, closes)):
        o = prev * (1 + rng.uniform(-0.006, 0.006))
        hi = max(o, c) * (1 + abs(rng.uniform(0, 0.010)))
        lo = min(o, c) * (1 - abs(rng.uniform(0, 0.010)))
        vbase = rng.randint(300_000, 4_000_000)
        frac = i / (n - 1) if n > 1 else 0
        if pattern == "volume_breakout" and frac > 0.60:
            vbase = int(vbase * rng.uniform(2.5, 5.0))
        elif pattern in ("rsi_oversold", "golden_cross") and 0.38 < frac < 0.55:
            vbase = int(vbase * rng.uniform(1.6, 3.0))
        bars.append({"date": d, "open": round(o / TICK) * TICK, "high": round(hi / TICK) * TICK,
                     "low": round(lo / TICK) * TICK, "close": round(c / TICK) * TICK, "volume": vbase})
        prev = c
    return bars


def clean_bar(b):
    """시가·고가·저가가 0/빈 값인 봉(거래정지일 등 — KRX 는 체결이 없으면 0 으로 준다) → 종가로 채우고
    고가 ≥ 시가·종가 ≥ 저가가 되게 보정. 종가가 없거나 0 이하면 None(저장하지 않음).
    예전: 0원 저가가 그대로 저장돼 52주 최저가 0원·캔들이 0까지 늘어난 차트."""
    def pos(v):
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None
    c = pos(b.get("close"))
    if c is None:
        return None
    o = pos(b.get("open")) or c
    h = max(pos(b.get("high")) or c, o, c)
    lo = min(pos(b.get("low")) or c, o, c)
    return {**b, "open": o, "high": h, "low": lo, "close": c}


def store_ohlc(conn, ticker, bars):
    for b in filter(None, map(clean_bar, bars)):
        conn.execute(text(
            "INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) "
            "VALUES(:t,:d,:o,:h,:l,:c,:v) "
            "ON CONFLICT(ticker,trade_date) DO UPDATE SET open=:o,high=:h,low=:l,close=:c,volume=:v"),
            {"t": ticker, "d": b["date"], "o": b["open"], "h": b["high"],
             "l": b["low"], "c": b["close"], "v": b["volume"]})


# ── 소스 결정 ─────────────────────────────────────────────
def _resolve_source(real):
    """demo(=오프라인)만 gen. 실데이터는 KRX(실패 시 내부에서 gen 폴백)."""
    if not real:
        return "gen"
    return "gen" if (settings.MARKET_SOURCE or "krx").lower() == "gen" else "krx"


# ── 오케스트레이션: 유니버스 최근 일봉 ────────────────────
GEN_WARNING = ("KRX 실데이터를 받지 못해 표본(생성) 데이터로 채웠습니다 — 화면 가격은 실제 시세가 아닙니다. "
               ".env 에 KRX_API_KEY 를 넣고(데이터 서비스 신청 포함) 다시 실행하세요.")


def seed_market(days=180, real=False):
    """유니버스 최근 일봉 적재. 반환 source 는 '실제로 적재된' 출처(krx|gen|kept).
    이전 버그: KRX 실패로 생성 데이터로 폴백해도 source='krx' 로 보고 → 표본 가격을 실데이터로 오인."""
    src = _resolve_source(real)
    warning = None
    with db.transaction() as conn:
        # 같은 적재가 동시에 두 번 돌지 않게(08:40 정기 실행과 수동 fetch-market·보충 실행이 겹치면) — 늦게 온 쪽은
        # 앞의 것이 끝날 때까지 기다린다. 예전엔 둘 다 랭킹을 지우고 다시 넣다가 PK 충돌로 한쪽 적재 전체가 롤백됐다.
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_SEED_MARKET})
        if src == "krx":
            src = _krx_seed_recent(conn, days)   # 'krx' | 'gen'(폴백) | 'kept'(기존 실데이터 유지)
            if src != "krx":
                warning = GEN_WARNING if src == "gen" else (
                    "KRX 접근 실패 — 기존 실데이터를 그대로 유지했습니다(표본으로 덮어쓰지 않음). "
                    "KRX_API_KEY 를 확인하세요.")
        else:
            catalog.seed_stocks(conn)            # 데모 유니버스(17) 시드
            _gen_seed_recent(conn, days)
        if src != "kept":
            db.set_meta("market_source", src, conn)
        asof = conn.execute(text("SELECT MAX(trade_date) FROM ohlc")).scalar()
        db.set_meta("market_asof", asof, conn)
    # 지수(코스피·코스닥)는 시세와 독립된 다중 소스로 — 네트워크 호출이라 트랜잭션 밖에서(DB 잠금 최소화)
    idx = _refresh_indices(real)
    index_warning = None
    if real and idx:
        bad = [c for c in ("KOSPI", "KOSDAQ") if (idx.get(c) or {}).get("source") in ("none", "kept")]
        if bad:
            index_warning = (f"지수 {', '.join(bad)} 최신값을 받지 못했습니다 — "
                             + ("직전 실데이터를 유지합니다." if all(idx[c]["source"] == "kept" for c in bad)
                                else "KIS 키 또는 인터넷 연결(야후), KRX 지수 API 이용신청을 확인하세요."))
            log.warning(index_warning)
    try:                                         # 유니버스가 바뀌었을 수 있으니 검색 색인·추천 캐시 비움
        from app.services import search, signals
        search.invalidate()
        signals.clear_cache()
    except Exception:                            # noqa: BLE001
        pass
    if warning:
        log.warning(warning)
    out = {"stocks": len(catalog.universe()), "source": src, "as_of": asof,
           "indices": {c: (idx.get(c) or {}).get("source") for c in ("KOSPI", "KOSDAQ")} if idx else None}
    if warning:
        out["warning"] = warning
    if index_warning:
        out["index_warning"] = index_warning          # 시세 출처 경고(warning)와 분리 — 지수만의 문제
    return out


def _refresh_indices(real):
    from app.collectors import indexes
    try:
        return indexes.refresh(real=real)
    except Exception as e:                       # noqa: BLE001 — 지수 실패가 시세 적재를 막지 않게
        log.warning("지수 갱신 실패: %s", e)
        return None


_LOCK_SEED_MARKET = 0x4F4F5301            # seed_market 트랜잭션 잠금 키(임의 상수)
KRX_MAX_CONSECUTIVE_FAILS = 3             # 수집 도중 KRX 가 연속으로 이만큼 실패하면 받은 데까지만 반영하고 멈춤

# 실습 시나리오 종목은 유니버스 크기와 무관하게 항상 포함(상세에서 열람 가능하도록)
_SCENARIO_TICKERS = {"005930", "035420", "035720", "000660", "005380", "051910"}


def _top_n():
    return max(1, int(getattr(settings, "UNIVERSE_TOP_N", 50)))


def _scenario_windows():
    """{ticker: [(워밍업 시작일, 종료일), …]} — 같은 ohlc 표에 있는 실습 구간(과거 2020~2023, SMA60 워밍업 포함)은
    최근 시세 적재가 건드리지 않는다(실습 구간은 seed_scenario_ohlc 가 따로 관리)."""
    from app.services import practice
    out = {}
    for sc in practice.SCENARIOS:
        out.setdefault(sc["ticker"], []).append((practice.warmup_start(sc), sc["end_date"]))
    return out


def _outside_windows_sql(ticker, windows, prefix="w"):
    """ticker 의 실습 구간 밖만 고르는 SQL 조건 + 파라미터."""
    cond, params = [], {}
    for i, (a, b) in enumerate(windows.get(ticker, [])):
        cond.append(f"NOT (trade_date BETWEEN :{prefix}a{i} AND :{prefix}b{i})")
        params[f"{prefix}a{i}"], params[f"{prefix}b{i}"] = a, b
    return (" AND " + " AND ".join(cond)) if cond else "", params


def _krx_since():
    """종목별 'KRX 실데이터가 시작된 날' {ticker: YYYY-MM-DD} — 이 날보다 오래된(실습 구간 밖) 봉은
    예전 데모(생성) 데이터가 남은 것이다. 이 날짜는 앞으로만 당겨지고(더 과거로) 뒤로 밀리지 않아서,
    실데이터가 1년 넘게 쌓여도 지워지지 않는다."""
    import json
    try:
        v = json.loads(db.get_meta("krx_since") or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _gen_seed_recent(conn, days):
    """생성(오프라인): 데모 유니버스(catalog.STOCKS) 고정."""
    today = _today()
    start = today - timedelta(days=int(days * 1.5) + 10)
    dates = business_days(start, today)
    for ticker, (_n, _m, _s, base, shares) in catalog.STOCKS.items():
        seed = _seed_of(ticker)
        bars = ohlc_from_closes(dates, gen_closes(len(dates), base, seed, anchor_last=base), seed)
        store_ohlc(conn, ticker, bars)
        _derive(conn, ticker, bars, int(bars[-1]["close"] * shares))
    db.set_meta("krx_since", "{}", conn)          # 표본으로 덮었으니 '실데이터 시작일' 기록도 초기화
    _build_rankings(conn)


def _krx_seed_recent(conn, want_days):
    """KRX: 최근 거래일을 하루씩 뒤로 돌며 시총 상위 N종목의 일봉을 채운다.

    지우는 규칙(실데이터를 잃지 않게):
      · 예전 출처가 표본(gen)이면 → 받은 종목의 실습 구간(워밍업 포함) 밖 봉을 전부 지우고 실데이터로 채운다
        (데모 봉이 KRX 구간 앞뒤·휴장일에 남아 가짜 급등락·52주값을 만들던 문제).
      · 이미 실데이터면 → '이번에 받은 날'과 '확인된 휴장일'만 지우고 다시 넣는다. 재시도 끝에도 실패한 날은
        기존 봉을 그대로 두고(예전엔 그날 봉이 통째로 사라졌다), KRX 최신일 이후 봉(KIS 가 넣은 오늘 봉)도 둔다.
      · 종목별 'KRX 시작일'(krx_since)보다 오래된 봉(실습 구간 밖)은 데모 잔여물이므로 정리.
        기록이 없는 종목(이 기능 이전에 쌓인 실데이터 DB)은 DB 의 가장 이른 봉을 시작일로 삼는다 —
        처음 한 번에 수백 일치 실데이터를 지우지 않게.
      · KRX 가 연속으로 실패하면(KRX_MAX_CONSECUTIVE_FAILS) 받은 데까지만 반영하고 멈춘다."""
    import json
    from app.collectors import krx
    had_real = db.get_meta("market_source") == "krx" and \
        bool(conn.execute(text("SELECT 1 FROM ohlc LIMIT 1")).first())

    def _fallback(reason):
        log.warning("KRX 사용 불가(%s)", reason)
        if had_real:                                             # 실데이터가 이미 있으면 표본으로 덮지 않는다
            return "kept"
        catalog.seed_stocks(conn)
        _gen_seed_recent(conn, want_days)
        return "gen"

    if not krx.enabled():
        return _fallback("KRX_API_KEY 미설정")
    topn = _top_n()
    per, mcap = {}, {}
    selected = None
    fetched, empty, failed = set(), set(), set()                # 받은 날 · 빈 응답(휴장) · 실패(기존 봉 유지)
    good = calls = streak = 0
    d = _today()
    cap = want_days * 2 + 40
    while good < want_days and calls < cap:
        if d.weekday() < 5:
            calls += 1
            try:
                day = krx.daily_stocks(d.isoformat())
                streak = 0
            except Exception as e:                               # noqa: BLE001 (재시도 뒤에도 실패)
                log.warning("KRX 호출 실패(%s): %s", d, type(e).__name__)
                if good == 0 and calls <= 2:                     # 초반부터 막히면 폴백
                    return _fallback(f"호출 실패: {type(e).__name__}")
                failed.add(d.isoformat())
                streak += 1
                if streak >= KRX_MAX_CONSECUTIVE_FAILS:          # 예전: 끝까지(최대 400회 × 재시도) 돌아 몇 시간 정지
                    log.warning("KRX 연속 %d회 실패 — 받은 %d거래일까지만 반영하고 중단", streak, good)
                    break
                day = None
            if day:
                good += 1
                fetched.add(d.isoformat())
                if selected is None:                             # 최신 거래일 기준 상위 N 선정
                    ranked = sorted(day.items(), key=lambda kv: kv[1]["mktcap"], reverse=True)
                    existing = {r[0] for r in conn.execute(text("SELECT ticker FROM stock")).all()}
                    # 이미 유니버스에 있던 종목(데모 시드·관심종목·장기투자 보유)도 실데이터로 교체
                    selected = {c for c, _ in ranked[:topn]} | (_SCENARIO_TICKERS & set(day)) \
                        | (existing & set(day))
                    for c in selected:
                        info = day[c]
                        catalog.upsert_stock(conn, c, info["name"] or c, info["market"] or "KOSPI", None)
                        mcap[c] = info["mktcap"]
                for c in selected:
                    if c in day:
                        b = day[c]
                        per.setdefault(c, []).append({
                            "date": d.isoformat(), "open": b["open"], "high": b["high"],
                            "low": b["low"], "close": b["close"], "volume": b["volume"],
                            "change_rate": b.get("change_rate")})
            elif day is not None:
                empty.add(d.isoformat())                         # 평일인데 빈 응답 = 휴장일(또는 아직 미제공)
        d -= timedelta(days=1)
    if not per:                                                  # 응답은 왔지만 데이터 0건
        return _fallback("수집된 일봉 0건")
    newest = max(fetched)                                        # KRX 가 실제로 데이터를 준 가장 최근 거래일
    holidays_ = {x for x in empty if x < newest}                 # 최신 거래일보다 앞의 빈 날 = 확인된 휴장일
    windows = _scenario_windows()
    since = _krx_since()
    for code, bars in per.items():
        bars = list(reversed(bars))                              # desc → asc
        first = bars[0]["date"]
        outside, wparams = _outside_windows_sql(code, windows)
        if not had_real:                                         # 표본 → 실데이터 전환: 실습 구간 밖 봉 전부 교체
            for tbl in ("ohlc", "daily_price"):
                conn.execute(text(f"DELETE FROM {tbl} WHERE ticker=:t" + outside), {"t": code, **wparams})
            since[code] = first
        else:
            redo = sorted(fetched | holidays_)                   # 실패한 날·KRX 최신일 이후(KIS 오늘 봉)는 제외
            for tbl in ("ohlc", "daily_price"):
                conn.execute(text(f"DELETE FROM {tbl} WHERE ticker=:t AND trade_date = ANY(CAST(:ds AS date[]))"),
                             {"t": code, "ds": redo})
            known = since.get(code) or conn.execute(text(
                "SELECT MIN(trade_date) FROM daily_price WHERE ticker=:t"), {"t": code}).scalar() or first
            start = min(known, first)
            for tbl in ("ohlc", "daily_price"):                  # KRX 시작일보다 오래된 데모 잔여 봉 정리
                conn.execute(text(f"DELETE FROM {tbl} WHERE ticker=:t AND trade_date < :s" + outside),
                             {"t": code, "s": start, **wparams})
            since[code] = start
        store_ohlc(conn, code, bars)
        _derive(conn, code, bars, int(mcap.get(code, 0)))
    db.set_meta("krx_since", json.dumps(since, sort_keys=True), conn)
    if failed:
        log.warning("KRX %d일 조회 실패 — 그날 기존 봉은 그대로 둠: %s", len(failed), ", ".join(sorted(failed)[:5]))
    _build_rankings(conn)
    purge_demo_artifacts(conn)                                   # 데모 재무·뉴스가 실데이터처럼 쓰이지 않게
    if newest > (db.get_meta("kr_close_final") or ""):           # KRX 가 준 날짜까지는 확정 종가
        db.set_meta("kr_close_final", newest, conn)
    log.info("KRX 최근 일봉 수집: %d거래일 × %d종목", good, len(per))
    return "krx"


def purge_demo_artifacts(conn=None):
    """실데이터로 바뀐 DB 에서 데모 전용 데이터를 지운다 — 표본 재무(PER 역산 값)·가상 뉴스·실데이터 구간보다 오래된 생성 봉.
    한 번이라도 'run.py demo' 를 돌린 DB 는 이것들이 남아 실데이터 화면·모형 입력에 섞였다.
    KRX 적재 성공 때(같은 트랜잭션)·서버 시작·initdb 때(실데이터 모드면) 호출.
    반환: {'financials': n, 'news': m, 'price_bars': k}."""
    from app.collectors import dart
    from app.services import themes
    if conn is None:
        with db.transaction() as c:
            return purge_demo_artifacts(c)
    return {"financials": dart.purge_demo_statements(conn), "news": themes.purge_demo_news(conn),
            "price_bars": purge_demo_price_bars(conn)}


def purge_demo_price_bars(conn):
    """데모(생성) 시세 봉 중 '실데이터 첫 적재 구간'보다 오래된 것을 지운다.
    데모는 약 280일치를 만들고 KRX 첫 적재는 그보다 짧은 구간만 바꿔 써서, 그 앞에 생성 봉이 이어 붙은 채 남았다
    (공백이 없어 최근 구간 자르기에도 안 걸림) → 변동성·VaR·최대낙폭·누적수익률·52주·1년 차트가 가짜 가격을 포함.
    기준일 = 데모에 없는 종목(KRX 로만 들어온 종목)의 가장 이른 거래일. 실습 시나리오(과거 사례) 구간은
    SMA60 워밍업 봉까지 보존(예전엔 시작일부터만 남겨, 서버를 켤 때마다 워밍업 봉이 지워졌다)."""
    demo = list(catalog.STOCKS)
    first = conn.execute(text("SELECT MIN(trade_date) FROM daily_price WHERE NOT (ticker = ANY(:d))"),
                         {"d": demo}).scalar()
    if not first:
        return 0
    windows = _scenario_windows()
    removed = 0
    for t in demo:
        cond, params = _outside_windows_sql(t, windows)
        removed += conn.execute(text("DELETE FROM ohlc WHERE ticker=:t AND trade_date < :f" + cond),
                                {"t": t, "f": first, **params}).rowcount or 0
        removed += conn.execute(text("DELETE FROM daily_price WHERE ticker=:t AND trade_date < :f"),
                                {"t": t, "f": first}).rowcount or 0
    if removed:
        log.info("실데이터 모드: 실데이터 구간보다 오래된 데모(생성) 시세 봉 %d건 제거", removed)
    return removed


def _derive(conn, ticker, bars, mktcap):
    """최근 일봉 → daily_price(종가·거래량·등락률·시총).
    시총은 최신일 값을 기준으로 날짜별 종가 비율로 환산(주식수 불변 가정).
    등락률은 KRX 공식 값(FLUC_RT, 봉의 change_rate)이 있으면 그것 — 종가로 다시 계산하면 액면분할일에 −80% 처럼
    틀리고, 앞날을 못 받은 날은 이틀 치 변화가 하루로 잡혔다. 없을 때만(생성 데이터) 직전 종가로 계산.
    첫 봉의 '직전 종가'는 DB 에 남아 있는 그 전 거래일 종가(예전엔 매번 0.0 으로 덮였다).
    ※ 예전엔 여기서 realtime_price 에 '가짜 틱'(ts=날짜)을 써서 실시간 판정과 섞였다 → 제거."""
    if not bars:
        return
    last_close = bars[-1]["close"] or 0
    prev = conn.execute(text("SELECT close FROM daily_price WHERE ticker=:t AND trade_date < :d "
                             "ORDER BY trade_date DESC LIMIT 1"), {"t": ticker, "d": bars[0]["date"]}).scalar()
    for b in bars:
        if b.get("change_rate") is not None:
            rate = round(float(b["change_rate"]), 2)
        else:
            rate = round((b["close"] / prev - 1) * 100, 2) if prev else 0.0
        mc = int(mktcap * b["close"] / last_close) if last_close else int(mktcap)
        conn.execute(text(
            "INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
            "VALUES(:t,:d,:c,:v,:r,:m) ON CONFLICT(ticker,trade_date) DO UPDATE SET "
            "close=:c,volume=:v,change_rate=:r,market_cap=:m"),
            {"t": ticker, "d": b["date"], "c": b["close"], "v": b["volume"], "r": rate, "m": mc})
        prev = b["close"]


def _build_rankings(conn):
    """랭킹(거래대금·거래량·시총·상승·하락 상위 15) — 같은 트랜잭션(conn)에서 읽는다.
    예전엔 유니버스를 다른 연결로 읽어, 새 DB 첫 적재 때는 방금 넣은(아직 커밋 전) 종목이 안 보여 랭킹이 비었고
    종목마다 쿼리 2번(150종목 = 300번)을 돌았다.
    상승·하락은 같은 날짜의 daily_price 등락률(KRX 공식 FLUC_RT)을 우선 — 종가 두 개로 계산하면 액면분할일에
    −80% 같은 값이 '하락 1위'로 올라온다. 없을 때만(생성 데이터·날짜 불일치) 종가로 계산."""
    conn.execute(text("DELETE FROM ranking"))
    q = conn.execute(text(
        "SELECT s.ticker, b0.close AS c0, b0.volume AS v0, b0.trade_date AS d0, b1.close AS c1, "
        "       dp.market_cap, dp.change_rate AS dr, dp.trade_date AS dd "
        "FROM stock s "
        "JOIN LATERAL (SELECT close, volume, trade_date FROM ohlc WHERE ticker = s.ticker "
        "              ORDER BY trade_date DESC LIMIT 1) b0 ON TRUE "
        "JOIN LATERAL (SELECT close FROM ohlc WHERE ticker = s.ticker "
        "              ORDER BY trade_date DESC OFFSET 1 LIMIT 1) b1 ON TRUE "
        "LEFT JOIN LATERAL (SELECT market_cap, change_rate, trade_date FROM daily_price WHERE ticker = s.ticker "
        "                   ORDER BY trade_date DESC LIMIT 1) dp ON TRUE ORDER BY s.ticker")).mappings().all()
    rows = []
    for r in q:
        if r["c0"] is None or r["c1"] is None:
            continue
        if r["dr"] is not None and r["dd"] == r["d0"]:
            chg = round(float(r["dr"]), 2)
        else:
            chg = round((r["c0"] / r["c1"] - 1) * 100, 2) if r["c1"] else 0.0
        mc = r["market_cap"] or r["c0"] * (catalog.shares_of(r["ticker"]) or 0)
        vol = r["v0"] or 0
        rows.append({"t": r["ticker"], "value": r["c0"] * vol, "volume": vol,
                     "marcap": mc, "rise": chg, "fall": chg})
    for rtype, desc in {"value": True, "volume": True, "marcap": True, "rise": True, "fall": False}.items():
        for i, r in enumerate(sorted(rows, key=lambda r: r[rtype], reverse=desc)[:15], 1):
            conn.execute(text("INSERT INTO ranking(ranking_type,rank,ticker,metric_value,as_of) "
                              "VALUES(:t,:r,:tk,:v,:d)"),
                         {"t": rtype, "r": i, "tk": r["t"], "v": r[rtype], "d": _today().isoformat()})


# ── 오케스트레이션: 실습 시나리오 과거 구간 ──────────────
def _gen_scenario_bars(scenario):
    """생성 데이터 — 시나리오 구간(학습 패턴) + 그 앞 워밍업 봉(패턴 없이, 첫 봉 가격에 이어지게)."""
    from app.services import practice
    ticker = scenario["ticker"]
    s, e = _parse(scenario["start_date"]), _parse(scenario["end_date"])
    dates = business_days(s, e)
    base = catalog.STOCKS.get(ticker, (None, None, None, 50000))[3]
    seed = _seed_of(scenario["scenario_id"])
    closes = gen_closes(len(dates), base, seed, pattern=scenario["pattern"])
    bars = ohlc_from_closes(dates, closes, seed, pattern=scenario["pattern"])
    wdates = business_days(_parse(practice.warmup_start(scenario)), s - timedelta(days=1))
    if wdates:
        wseed = _seed_of(scenario["scenario_id"] + ":warmup")
        wcloses = gen_closes(len(wdates), base, wseed, anchor_last=closes[0])
        bars = ohlc_from_closes(wdates, wcloses, wseed) + bars
    return bars


def _window_count(ticker, a, b):
    return int(db.query("SELECT COUNT(*) AS n FROM ohlc WHERE ticker=:t AND trade_date BETWEEN :a AND :b",
                        {"t": ticker, "a": a, "b": b})[0]["n"])


def _scenario_rows(ticker, start, end):          # 예전 이름(호환)
    return _window_count(ticker, start, end)


def _warmup_covered(scenario):
    """워밍업 구간까지 받아 둔 실데이터인가 — 워밍업 도입 전에 받은 구간(시작일부터만)은 한 번 더 받는다."""
    from app.services import practice
    a = practice.warmup_start(scenario)
    if (db.get_meta(f"scenario_from:{scenario['scenario_id']}") or "9999") <= a:
        return True
    day_before = (_parse(scenario["start_date"]) - timedelta(days=1)).isoformat()
    return _window_count(scenario["ticker"], a, day_before) > 0       # 스냅샷·이전 판에서 받은 워밍업 봉


def seed_scenario_ohlc(scenario, real=False, force=False):
    """실습 시나리오 구간(+SMA60 워밍업 봉) 적재. 반환: 그 구간(워밍업 포함) 봉 수.
      · 실데이터(KRX)로 한 번 받은 구간은 다시 받지 않는다(과거는 바뀌지 않음, force 로만) — 예전엔 매일 23:00 마다
        구간 전체(약 1,200회 호출)를 다시 받아 KRX 하루 한도를 갉아먹었다.
      · KRX 가 하루라도 실패하면 아무것도 쓰지 않고 기존 봉을 그대로 둔다 — 예전엔 생성(가짜) 가격으로 실데이터
        구간을 덮었고, 생성 데이터는 평일 공휴일에도 봉을 만들어 그 날짜들이 계속 남았다.
      · KRX 로 받으면 구간을 지우고 한 트랜잭션에 새로 넣는다(예전 생성 데이터의 휴장일 봉 제거).
      · 생성 데이터는 데모이거나, 실데이터를 못 받았는데 구간이 비어 있을 때만(실습이 열리게)."""
    from app.services import practice
    src = _resolve_source(real)
    ticker, sid = scenario["ticker"], scenario["scenario_id"]
    a, b = practice.warmup_start(scenario), scenario["end_date"]
    key = f"scenario_src:{sid}"
    if src == "krx":
        if not force and db.get_meta(key) == "krx" and _window_count(ticker, scenario["start_date"], b) \
                and _warmup_covered(scenario):
            return _window_count(ticker, a, b)                   # 이미 실데이터 — KRX 호출 0회
        bars = _krx_scenario(scenario)
        if bars:
            with db.transaction() as conn:
                conn.execute(text("DELETE FROM ohlc WHERE ticker=:t AND trade_date BETWEEN :a AND :b"),
                             {"t": ticker, "a": a, "b": b})
                store_ohlc(conn, ticker, bars)
                db.set_meta(key, "krx", conn)
                db.set_meta(f"scenario_from:{sid}", a, conn)
            return len(bars)
        have = _window_count(ticker, scenario["start_date"], b)
        if have:                                                 # 실패 — 가진 봉 유지(생성 데이터로 덮지 않음)
            log.warning("실습 '%s' KRX 수집 실패 — 기존 봉 %d개 유지", scenario.get("name", sid), have)
            return _window_count(ticker, a, b)
    bars = _gen_scenario_bars(scenario)                          # gen 또는 '비어 있는' 구간의 실데이터 실패
    with db.transaction() as conn:
        conn.execute(text("DELETE FROM ohlc WHERE ticker=:t AND trade_date BETWEEN :a AND :b"),
                     {"t": ticker, "a": a, "b": b})
        store_ohlc(conn, ticker, bars)
        db.set_meta(key, "gen", conn)
    return len(bars)


def _krx_scenario(scenario):
    """KRX: 시나리오 구간(워밍업 포함)을 하루씩 돌며 해당 종목만 뽑는다(종목 시장만 호출).
    하나라도 실패하거나 시나리오 구간에 봉이 없으면 None → 아무것도 안 씀."""
    from app.collectors import krx
    from app.services import practice
    tk = scenario["ticker"]
    market = (catalog.STOCKS.get(tk) or (None, None))[1]
    markets = (market,) if market in ("KOSPI", "KOSDAQ") else None
    d, end = _parse(practice.warmup_start(scenario)), _parse(scenario["end_date"])
    bars = []
    while d <= end:
        if d.weekday() < 5:
            try:
                day = krx.daily_stocks(d.isoformat(), markets=markets)
            except Exception as ex:                              # noqa: BLE001
                log.warning("KRX 실습 호출 실패(%s): %s — 이번엔 건너뜀", d, type(ex).__name__)
                return None
            if tk in day:
                b = day[tk]
                bars.append({"date": d.isoformat(), "open": b["open"], "high": b["high"],
                             "low": b["low"], "close": b["close"], "volume": b["volume"]})
        d += timedelta(days=1)
    if not any(x["date"] >= scenario["start_date"] for x in bars):
        return None
    return bars
