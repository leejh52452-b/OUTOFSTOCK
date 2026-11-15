"""양봉이 주간 추천 — 팀 알고리즘(yangbong_picks.py, 원본 그대로)을 우리 데이터·DB·배치·API 에 잇는다(2026-11-09).

알고리즘(바꾸지 않는다 — tests/test_yangbong.py 가 원본 파일 해시를 확인)
  매주 마지막 거래일 장 마감 기준: 코스피 보통주 시총 상위 200(상장 200거래일 이상·거래 중) → 최근 60거래일 흔들림 하위 30%
  → 200일선 위 → 시총 큰 순으로 고르되 5종목 모두 다른 업종. 채점: 다음 거래일 시가 매수 → 30일 뒤(휴장이면 직전 거래일)
  종가 매도, 5종목 평균 = 바구니 수익률(20%씩). 누적 성적은 매주 100만 원씩 담았다고 가정.
데이터(분석 계층 analytics.price_history — KRX 전 종목 일별매매정보, collectors/krx_history.py)
  · 수정주가는 팀 방식 그대로(Y.adjusted_close — 원 종가 + KRX 등락률 FLUC_RT) · 수정시가 = 시가 × 수정종가 ÷ 종가
  · 코스피에 있던 날의 행만(mkt='K') — 팀이 코스피 일별매매정보로 만든 표와 같다(이전상장 종목은 코스피에 온 뒤부터)
  · 보통주: 팀은 KRX 종목기본정보(증권구분=주권·주식종류=보통주)를 썼다. 우리 적재기는 그 API 를 받지 않으므로 같은 뜻의
           코드·이름 규칙 — 끝자리 0(우선주 제외) · 9로 시작하는 외국주권 제외 · 이름에 스팩·리츠·리얼티(부동산투자회사)·
           인프라(투자회사)·'○호'(선박투자회사) 제외. ETF·ETN 은 주식 일별매매정보에 없다.
  · 업종: DART 업종코드(KSIC) 앞 3자리 = KIND 상장회사 목록의 업종(팀이 쓴 FinanceDataReader KRX-DESC Industry 와 같은 분류).
          없으면 투자자용 업종, 그것도 없으면 종목마다 다른 업종(팀 규칙 — industry.get(code) or '?코드')
  · 비교 지수: index_daily(KOSPI) 매수일 시가 → 매도일 종가
배치(run — 매일 08:55·23:55, 따라잡기형·멱등)
  데이터가 들어온 '끝난 주'마다 바구니가 없으면 고르고, 30일이 지난 바구니를 채점한다. 금요일 확정치는 KRX 가 D+1 에 주므로
  토요일 밤 전체 파이프라인(23:00) 또는 월요일 08:40 적재 뒤에 계산된다 — 월요일 장 시작(매수) 전.
  안전장치: ① 그 주 마지막 거래일 시세가 아직 없으면 기다린다 ② 후보가 YANGBONG_MIN_UNIVERSE 보다 적으면 고르지 않는다
  (부분 적재·표본 DB 에서 몇 종목으로 고른 '추천'을 내보내지 않게) ③ 놓친 주는 YANGBONG_CATCHUP_WEEKS 주까지 거슬러 채운다
  ④ 같은 배치가 겹치면 늦게 온 쪽은 건너뛴다(advisory lock).
초기 데이터: 팀 백테스트 바구니 247개(2022-01~2026-09, app/data/yangbong/history_baskets_2022_2026.csv) — 첫 주부터 승률·누적 수익.
화면: GET /yangbong/weekly — 팀 api_example.weekly() 와 같은 JSON(+ source·model). 공개 데이터라 ETag/304, 서버 캐시 10분.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)

MODEL = "yangbong-v1"
SEED_CSV = Path(__file__).resolve().parents[1] / "data" / "yangbong" / "history_baskets_2022_2026.csv"
LOOKBACK_DAYS = 470                     # 신호일 앞으로 읽는 달력 일수(≈ 320거래일 — 200일선·상장 200거래일 판단에 넉넉히)
_NOT_COMMON = re.compile(r"스팩|기업인수목적|리츠|리얼티|REIT|인프라|\d+호$", re.IGNORECASE)
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "val": None}
CACHE_TTL = 600


class YangbongError(Exception):
    pass


# ── 종목 구분 ──────────────────────────────────────────────
def is_common(ticker: str, name: str) -> bool:
    """코스피 보통주인가(팀: 증권구분=주권·주식종류=보통주, 스팩 제외) — 코드·이름 규칙(모듈 설명)."""
    t, n = str(ticker or ""), str(name or "").strip()
    return len(t) == 6 and t.endswith("0") and not t.startswith("9") and not _NOT_COMMON.search(n)


def industry_key(ksic: str | None, sector: str | None) -> str | None:
    """업종 키 — KSIC 앞 3자리(소분류) → 투자자용 업종 → 없음(None = 종목마다 다른 업종)."""
    k = re.sub(r"\D", "", str(ksic or ""))
    if len(k) >= 3:
        return "ksic:" + k[:3]
    if k:
        return "ksic:" + k
    s = str(sector or "").strip()
    return ("sector:" + s) if s else None


# ── 데이터 ─────────────────────────────────────────────────
def latest_trade_date() -> str | None:
    """분석 계층에 적재된 가장 최근 코스피 거래일."""
    rows = db.query("SELECT max(trade_date) AS d FROM analytics.price_history WHERE mkt = 'K' "
                    "AND trade_date >= CAST(:s AS date)", {"s": (clock.today() - timedelta(days=400)).isoformat()})
    return rows[0]["d"] if rows and rows[0]["d"] else None


def trade_dates(since: str, until: str) -> list[str]:
    """since 이후 ~ until 까지 코스피 거래일(적재된 날)."""
    return [r["d"] for r in db.query(
        "SELECT DISTINCT trade_date AS d FROM analytics.price_history WHERE mkt = 'K' "
        "AND trade_date > CAST(:s AS date) AND trade_date <= CAST(:u AS date) ORDER BY 1", {"s": since, "u": until})]


def load_market_data(start: str, end: str) -> dict:
    """팀 api_example.load_market_data() 와 같은 모양 — start~end(포함) 코스피 '날짜 × 종목코드' 표들."""
    import pandas as pd
    from app.services import yangbong_picks as Y
    rows = db.query("SELECT ticker, trade_date, open, close, volume, mktcap, change_rate FROM analytics.price_history "
                    "WHERE mkt = 'K' AND trade_date BETWEEN CAST(:s AS date) AND CAST(:e AS date)", {"s": start, "e": end})
    if not rows:
        raise YangbongError(f"{start}~{end} 코스피 시세가 없습니다(analytics.price_history — KRX 적재 확인)")
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "close", "volume", "mktcap", "change_rate"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    piv = {c: df.pivot(index="trade_date", columns="ticker", values=c).sort_index()
           for c in ("open", "close", "volume", "mktcap", "change_rate")}
    raw_close = piv["close"]
    close = Y.adjusted_close(raw_close, piv["change_rate"])
    open_ = piv["open"] * (close / raw_close)
    tickers = list(raw_close.columns)
    names, industry, common = {}, {}, set()
    for m in db.query("SELECT ticker, name, ksic_code, sector FROM analytics.security_master WHERE ticker = ANY(:ts)",
                      {"ts": tickers}):
        names[m["ticker"]] = m["name"]
        ind = industry_key(m["ksic_code"], m["sector"])
        if ind:
            industry[m["ticker"]] = ind
        if is_common(m["ticker"], m["name"]):
            common.add(m["ticker"])
    k = db.query("SELECT trade_date, open, close FROM index_daily WHERE code = 'KOSPI' "
                 "AND trade_date BETWEEN CAST(:s AS date) AND CAST(:e AS date) ORDER BY trade_date", {"s": start, "e": end})
    kidx = pd.to_datetime([r["trade_date"] for r in k])
    return {"open": open_, "close": close, "volume": piv["volume"], "mktcap": piv["mktcap"], "common": common,
            "industry": industry, "names": names,
            "kospi_open": pd.Series([r["open"] for r in k], index=kidx, dtype="float64"),
            "kospi_close": pd.Series([r["close"] for r in k], index=kidx, dtype="float64")}


# ── 주(週) 계산 ────────────────────────────────────────────
def _calendar_last_day(d: date) -> date:
    """d 가 속한 주(월~일)의 마지막 거래일(달력 기준 — 금요일부터 주말·휴장일을 거슬러)."""
    from app.services import quotes
    x = d - timedelta(days=d.weekday()) + timedelta(days=4)
    while x.weekday() >= 5 or quotes.is_holiday("kr", x):
        x -= timedelta(days=1)
    return x


def completed_weeks(dates: list[str], latest: str) -> list[str]:
    """적재된 거래일 → 끝난 주마다 신호일(그 주에 적재된 마지막 거래일).
    끝난 주 = 뒤 주의 거래일이 이미 적재됐거나, 그 주 마지막 날(달력 기준)까지 적재됨. 달력에 없는 임시 휴장도 다음 주
    데이터가 들어오면 실제 마지막 거래일로 확정된다."""
    weeks: dict[tuple, str] = {}
    for s in dates:
        d = date.fromisoformat(s)
        weeks[d.isocalendar()[:2]] = max(weeks.get(d.isocalendar()[:2], s), s)
    out = []
    last_week = date.fromisoformat(latest).isocalendar()[:2] if latest else None
    for wk, s in sorted(weeks.items()):
        if wk != last_week or date.fromisoformat(s) >= _calendar_last_day(date.fromisoformat(s)):
            out.append(s)
    return out


# ── 저장 ───────────────────────────────────────────────────
def load_baskets() -> list[dict]:
    """저장된 바구니 전체(오래된 순) — 팀 api_example.load_baskets() 와 같은 모양."""
    return db.query("SELECT signal_date, buy_date, sell_date, codes, names, stock_returns, basket_return, kospi_return, source "
                    "FROM yangbong_basket ORDER BY signal_date")


def _save(conn, row: dict) -> None:
    conn.execute(text(
        "INSERT INTO yangbong_basket(signal_date, buy_date, sell_date, codes, names, stock_returns, basket_return, "
        "kospi_return, source, picked_at, scored_at) VALUES(:signal_date, :buy_date, :sell_date, :codes, :names, "
        ":stock_returns, :basket_return, :kospi_return, :source, now(), CASE WHEN :scored THEN now() END) "
        "ON CONFLICT(signal_date) DO NOTHING"),
        {**row, "scored": row.get("basket_return") is not None})


def _num(x: str):
    return float(x) if x not in (None, "") else None


def seed_rows(path: Path = SEED_CSV) -> list[dict]:
    """초기 데이터 CSV(값은 '|' 구분) → 행 목록 — 팀 api_example.seed_rows_from_csv() 와 같은 해석(pandas 없이)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{"signal_date": r["signal_date"], "buy_date": r["buy_date"] or None, "sell_date": r["sell_date"] or None,
                 "codes": r["codes"].split("|"), "names": r["names"].split("|"),
                 "stock_returns": [_num(x) for x in r["stock_returns"].split("|")],
                 "basket_return": _num(r["basket_return"]), "kospi_return": _num(r["kospi_return"])}
                for r in csv.DictReader(f)]


def seed() -> int:
    """팀 백테스트 바구니 247개를 한 번 넣는다(있는 날은 건드리지 않음 — 멱등). 반환: 새로 넣은 수."""
    rows = seed_rows()
    with db.transaction() as conn:
        have = {r[0] for r in conn.execute(text("SELECT signal_date FROM yangbong_basket"))}
        n = 0
        for r in rows:
            if r["signal_date"] in have:
                continue
            _save(conn, {**r, "source": "seed"})
            n += 1
    if n:
        invalidate()
    return n


# ── 배치 ───────────────────────────────────────────────────
def run(now: datetime | None = None) -> dict:
    """끝난 주의 바구니를 고르고(없으면), 30일 지난 바구니를 채점한다. 반환: 한 일 요약(app_meta 'yangbong:last_run')."""
    with db.advisory_lock("yangbong") as got:
        if not got:
            return {"skipped": "다른 실행이 진행 중"}
        out = _run(now)
    db.set_meta("yangbong:last_run", json.dumps(out, ensure_ascii=False, default=str))
    invalidate()
    return out


def _run(now: datetime | None) -> dict:
    from app.services import yangbong_picks as Y
    now = now or clock.now()
    out = {"at": now.isoformat(timespec="seconds"), "picked": [], "scored": [], "skipped": []}
    latest = latest_trade_date()
    if not latest:
        out["skipped"].append("분석 계층 코스피 시세 없음(KRX 적재 전)")
        return out
    out["data_as_of"] = latest
    baskets = load_baskets()
    last_signal = baskets[-1]["signal_date"] if baskets else None
    since = last_signal or (date.fromisoformat(latest) - timedelta(weeks=settings.YANGBONG_CATCHUP_WEEKS)).isoformat()
    floor = (date.fromisoformat(latest) - timedelta(weeks=settings.YANGBONG_CATCHUP_WEEKS)).isoformat()
    done_week = date.fromisoformat(last_signal).isocalendar()[:2] if last_signal else (0, 0)
    targets = [s for s in completed_weeks(trade_dates(max(since, floor), latest), latest)
               if date.fromisoformat(s).isocalendar()[:2] > done_week]          # 이미 고른 주는 다시 고르지 않는다
    unscored = [b for b in baskets if b["basket_return"] is None]
    if not targets and not unscored:
        return out
    starts = [(date.fromisoformat(s) - timedelta(days=LOOKBACK_DAYS)).isoformat() for s in targets]
    starts += [b["signal_date"] for b in unscored]
    data = load_market_data(min(starts), latest)
    for s in targets:
        hist_close = data["close"].loc[:s]
        mask = Y.eligible(hist_close, data["volume"].loc[:s], data["mktcap"].loc[:s], data["common"])
        day = hist_close.index[-1].date().isoformat() if len(hist_close.index) else None
        pool = int(mask.iloc[-1].sum()) if len(mask.index) else 0
        if day != s:
            out["skipped"].append(f"{s}: 그날 시세 없음")
            continue
        if pool < settings.YANGBONG_MIN_UNIVERSE:
            out["skipped"].append(f"{s}: 후보 {pool}종목 < {settings.YANGBONG_MIN_UNIVERSE}(전 종목 적재 확인)")
            continue
        d, picks = Y.select_weekly(data["close"], data["volume"], data["mktcap"], data["common"],
                                   data["industry"], data["names"], s)
        if not picks:
            out["skipped"].append(f"{s}: 조건을 만족한 종목 없음")
            continue
        with db.transaction() as conn:
            _save(conn, {"signal_date": s, "buy_date": None, "sell_date": None, "codes": [p.code for p in picks],
                         "names": [p.name for p in picks], "stock_returns": None, "basket_return": None,
                         "kospi_return": None, "source": "batch"})
        out["picked"].append({"signal_date": s, "names": [p.name for p in picks], "pool": pool})
        unscored.append({"signal_date": s, "codes": [p.code for p in picks]})
    for b in unscored:
        sc = Y.score_basket(data["open"], data["close"], b["signal_date"], list(b["codes"]))
        if sc is None or sc["basket_return"] is None:
            continue
        kb = data["kospi_open"].get(_ts(sc["buy_date"]))
        ks = data["kospi_close"].get(_ts(sc["sell_date"]))
        kr = float(ks / kb - 1) if kb and ks and kb == kb and ks == ks else None
        rets = [sc["stock_returns"].get(c) for c in b["codes"]]
        with db.transaction() as conn:
            n = conn.execute(text(
                "UPDATE yangbong_basket SET buy_date = :b, sell_date = :s, stock_returns = :r, basket_return = :br, "
                "kospi_return = :kr, scored_at = now() WHERE signal_date = :d AND basket_return IS NULL"),
                {"b": sc["buy_date"], "s": sc["sell_date"], "r": rets, "br": sc["basket_return"], "kr": kr,
                 "d": b["signal_date"]}).rowcount
        if n:
            out["scored"].append({"signal_date": b["signal_date"], "basket_return": round(sc["basket_return"], 6)})
    return out


def _ts(s: str):
    import pandas as pd
    return pd.Timestamp(s)


# ── 화면 API ───────────────────────────────────────────────
def invalidate() -> None:
    with _cache_lock:
        _cache.update(at=0.0, val=None)


def weekly() -> dict:
    """GET /yangbong/weekly — 팀 api_example.weekly() 와 같은 JSON(+ source·model). 10분 캐시."""
    with _cache_lock:
        if _cache["val"] is not None and time.monotonic() - _cache["at"] < CACHE_TTL:
            return _cache["val"]
    val = _weekly()
    with _cache_lock:
        _cache.update(at=time.monotonic(), val=val)
    return val


def _weekly() -> dict:
    from app.services import yangbong_picks as Y
    baskets = load_baskets()
    if not baskets:
        return {"as_of": None, "picks": [], "last_basket": None,
                "stats": {"weeks": 0, "wins": 0, "win_rate": None, "avg_return": None, "total_won": 0, "since": None},
                "model": MODEL, "source": None}
    latest = baskets[-1]
    scored = [b for b in baskets if b["basket_return"] is not None]
    last = scored[-1] if scored else None
    st = Y.stats(scored)
    return {
        "as_of": latest["signal_date"],
        "picks": [{"rank": i + 1, "code": c, "name": n} for i, (c, n) in enumerate(zip(latest["codes"], latest["names"]))],
        "last_basket": None if last is None else {
            "buy_date": last["buy_date"], "sell_date": last["sell_date"],
            "basket_return": last["basket_return"], "kospi_return": last["kospi_return"],
            "stocks": [{"code": c, "name": n, "return": r}
                       for c, n, r in zip(last["codes"], last["names"], last["stock_returns"] or [])],
        },
        "stats": {**st, "since": scored[0]["signal_date"] if scored else None},
        "model": MODEL,
        "source": latest["source"],            # seed = 팀 백테스트(과거 시뮬레이션) · batch = 우리 배치가 고른 주
    }


def status() -> dict:
    """운영 확인(관리 화면·run.py yangbong) — 바구니 수·최근 고른 주·채점 대기·마지막 배치."""
    rows = db.query("SELECT count(*) AS n, count(*) FILTER (WHERE basket_return IS NULL) AS pending, "
                    "count(*) FILTER (WHERE source = 'batch') AS batch, max(signal_date) AS last_signal "
                    "FROM yangbong_basket")
    r = rows[0] if rows else {}
    try:
        last_run = json.loads(db.get_meta("yangbong:last_run") or "null")
    except ValueError:
        last_run = None
    return {"baskets": r.get("n", 0), "pending": r.get("pending", 0), "batch": r.get("batch", 0),
            "last_signal": r.get("last_signal"), "last_run": last_run, "model": MODEL}
