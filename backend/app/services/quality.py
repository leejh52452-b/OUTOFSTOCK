"""데이터 품질 검증 — 수집한 데이터의 결측·이상치·참조무결성·신선도를 검사하는 '품질 게이트'.

배경: 은행/금융의 데이터 거버넌스(예: BCBS 239 리스크데이터 원칙)는 '믿을 수 있는 데이터만
하류로 흘려보낼 것'을 요구한다. 수집(collectors)과 활용(ranking/recommend/API) 사이에
검증 관문을 두고, 규칙에 걸린 행은 data_quality_issue 로 격리(quarantine)해 추적한다.

규칙은 (이름, 설명, '불량 행을 뽑는 SQL') 튜플로 선언 — 규칙 추가가 쉽고 SQL 로 검증이 투명."""
import logging
from datetime import datetime
from sqlalchemy import text
from app import clock
from app import db

log = logging.getLogger(__name__)

PRICE_LIMIT = 30.0        # 한국 주식 가격제한폭 ±30% — 이를 넘는 등락률은 오류 데이터
_TOL = 0.5                # 부동소수 여유 — 30.0 정확히(상한가)는 정상, 30.5 초과만 불량

# (규칙명, 설명, 위반 행 SELECT) — SQL 이 돌려주는 행 = 불량 데이터
_RULES = [
    ("null_close", "종가 결측(NULL) 금지",
     "SELECT ticker, trade_date, close AS val FROM daily_price WHERE close IS NULL"),
    ("positive_price", "종가는 0보다 커야 함",
     "SELECT ticker, trade_date, close AS val FROM daily_price WHERE close <= 0"),
    ("nonneg_volume", "거래량은 음수 불가",
     "SELECT ticker, trade_date, volume AS val FROM daily_price WHERE volume < 0"),
    ("price_limit", f"등락률 절댓값은 가격제한폭(+-{PRICE_LIMIT}%) 이내",
     f"SELECT ticker, trade_date, change_rate AS val FROM daily_price "
     f"WHERE change_rate IS NOT NULL AND ABS(change_rate) > {PRICE_LIMIT + _TOL}"),
    ("orphan_ticker", "daily_price 종목은 stock 에 존재해야 함(참조 무결성)",
     "SELECT d.ticker, d.trade_date, d.ticker AS val FROM daily_price d "
     "LEFT JOIN stock s ON s.ticker = d.ticker WHERE s.ticker IS NULL"),
]
_RULE_SQL = {name: sql for name, _desc, sql in _RULES}


def check_daily_prices(samples=5):
    """규칙별 위반 건수 + 표본. samples 는 규칙당 예시 행 개수."""
    checks, total = [], 0
    for name, desc, sql in _RULES:
        bad = db.query(sql)
        checks.append({"rule": name, "desc": desc, "ok": not bad,
                       "violations": len(bad), "samples": bad[:samples]})
        total += len(bad)
    return {"ok": total == 0, "total_issues": total, "checks": checks}


def check_freshness(max_age_days=3, as_of=None):
    """최신 시세가 max_age_days 이내인가 — 파이프라인이 실제로 돌고 있는지(신선도)."""
    row = db.query("SELECT MAX(trade_date) AS latest FROM daily_price")
    latest = row[0]["latest"] if row else None
    ref = as_of or clock.today_str()
    age_days, ok = None, False
    if latest:
        d0 = datetime.strptime(latest, "%Y-%m-%d")
        d1 = datetime.strptime(ref, "%Y-%m-%d")
        age_days = (d1 - d0).days
        ok = 0 <= age_days <= max_age_days
    return {"rule": "freshness", "ok": ok, "latest": latest, "as_of": ref,
            "age_days": age_days, "max_age_days": max_age_days}


INDEX_LIMIT = 20.0        # 코스피·코스닥 서킷브레이커 최종 단계(−20%) — 이를 넘는 하루 등락은 오류 데이터


def check_indices(samples=5):
    """지수 이력 품질 — 값은 양수, 하루 등락은 ±20% 이내(서킷브레이커), 출처 기록 필수."""
    rules = [
        ("index_positive", "지수 값은 0보다 커야 함",
         "SELECT code AS ticker, trade_date, close AS val FROM index_daily WHERE close IS NULL OR close <= 0"),
        ("index_daily_move", f"지수 하루 등락은 ±{INDEX_LIMIT:.0f}% 이내(서킷브레이커)",
         f"SELECT code AS ticker, trade_date, change_rate AS val FROM index_daily "
         f"WHERE change_rate IS NOT NULL AND ABS(change_rate) > {INDEX_LIMIT}"),
        ("index_lineage", "지수 값에는 출처(source)·기준일(as_of)이 있어야 함",
         "SELECT code AS ticker, as_of AS trade_date, value AS val FROM market_index "
         "WHERE code IN ('KOSPI','KOSDAQ') AND (source IS NULL OR as_of IS NULL)"),
    ]
    checks, total = [], 0
    for name, desc, sql in rules:
        try:
            bad = db.query(sql)
        except Exception:                             # noqa: BLE001 — 구버전 DB(표 없음)
            bad = []
        checks.append({"rule": name, "desc": desc, "ok": not bad, "violations": len(bad), "samples": bad[:samples]})
        total += len(bad)
    return {"ok": total == 0, "total_issues": total, "checks": checks}


def run_quality_checks(max_age_days=3, as_of=None, quarantine=False):
    """전체 품질 검사 실행 → 요약 리포트. quarantine=True 면 불량 행을 격리소에 기록."""
    report = check_daily_prices()
    fresh = check_freshness(max_age_days, as_of)
    report["freshness"] = fresh
    report["indices"] = check_indices()
    report["ok"] = report["ok"] and fresh["ok"]

    if quarantine:
        now = clock.iso_now()
        with db.transaction() as conn:
            # 매 실행마다 daily_price 격리분을 새로 씀(중복 누적 방지 = 멱등). 깨끗하면 비워짐.
            conn.execute(text("DELETE FROM data_quality_issue WHERE table_name='daily_price'"))
            for chk in report["checks"]:
                if chk["ok"]:
                    continue
                for row in db.query(_RULE_SQL[chk["rule"]]):   # 전체 불량 행 재조회
                    conn.execute(text(
                        "INSERT INTO data_quality_issue"
                        "(table_name, ticker, trade_date, rule, detail, detected_at) "
                        "VALUES('daily_price', :t, :d, :r, :det, :ts)"),
                        {"t": row.get("ticker"), "d": row.get("trade_date"),
                         "r": chk["rule"], "det": str(row.get("val")), "ts": now})

    log.info("데이터 품질 검사: %s (불량 %d건, 신선도 %s)",
             "통과" if report["ok"] else "실패", report["total_issues"],
             "OK" if fresh["ok"] else "STALE")
    return report


# ── 적재 후 품질 게이트(국내 시세) ─────────────────────────────────────────
# 2026-10-10 장애: KRX 적재가 매번 롤백돼도 '실패'가 로그에만 남아 1주일 동안 아무도 몰랐다
# (시세 출처 없음 · 유니버스 17 · 종목마다 일봉 8일). 적재 직후 결과를 기대치와 비교해 실패면 기록·알림한다.
GATE_META = "quality_gate:"


def last_kr_trading_day(before):
    """before(날짜) 직전의 국내 거래일(주말·휴장일 제외)."""
    from datetime import timedelta
    from app.services import quotes
    d = before - timedelta(days=1)
    while d.weekday() >= 5 or quotes.is_holiday("kr", d):
        d -= timedelta(days=1)
    return d


def _trading_days_after(a, b):
    """a 다음 날부터 b 까지(포함)의 국내 거래일 수 — 최신일이 기대보다 며칠(거래일) 늦었나."""
    from datetime import timedelta
    from app.services import quotes
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5 and not quotes.is_holiday("kr", d):
            n += 1
        d += timedelta(days=1)
    return n


def gate_market_load(load=None, today=None):
    """국내 시세 적재 직후 게이트 — 출처·유니버스 크기·최근 일봉 수·최신일·가격 상식을 확인.
    load = marketgen.seed_market 반환값(없으면 DB 상태만 본다). 결과는 app_meta 에 남겨 /health 가 보여 준다.
    반환 {name, level: ok|warn|fail, ok, checks:[{name, ok, level, detail}], summary, checked_at}."""
    import json
    from datetime import date, timedelta
    from app.config import settings
    checks = []

    def add(name, ok, detail, level="fail"):
        checks.append({"name": name, "ok": bool(ok), "level": "ok" if ok else level, "detail": detail})

    real = bool(settings.KRX_API_KEY) and (settings.MARKET_SOURCE or "auto").lower() != "gen"
    src = (load or {}).get("source") or db.get_meta("market_source")
    if real:
        add("source", src == "krx", f"시세 출처 {src or '없음'}(기대: krx)",
            level="warn" if src == "kept" else "fail")        # kept = KRX 실패, 기존 실데이터 유지
    else:
        add("source", True, f"시세 출처 {src or '없음'} — KRX 키가 없거나 표본 모드(실데이터 기준 검사 생략)")
    n_uni = db.query("SELECT COUNT(*) AS n FROM stock")[0]["n"]
    if real:
        need = int(settings.UNIVERSE_TOP_N * settings.QUALITY_MIN_UNIVERSE_RATIO)
        add("universe", n_uni >= need, f"유니버스 {n_uni}종목(기준 {need} 이상 = 상위 {settings.UNIVERSE_TOP_N}의 "
                                       f"{settings.QUALITY_MIN_UNIVERSE_RATIO:.0%})")
    latest = db.query("SELECT MAX(o.trade_date) AS d FROM ohlc o JOIN stock s ON s.ticker = o.ticker")[0]["d"]
    if not latest:
        add("history", False, "유니버스 종목의 일봉이 하나도 없음")
    else:
        since = (date.fromisoformat(latest) - timedelta(days=100)).isoformat()
        r = db.query("SELECT COUNT(*) FILTER (WHERE n >= :m) AS good, COUNT(*) AS total FROM ("
                     " SELECT s.ticker, COUNT(o.trade_date) AS n FROM stock s LEFT JOIN ohlc o"
                     " ON o.ticker = s.ticker AND o.trade_date >= :s AND o.trade_date <= :l GROUP BY s.ticker) x",
                     {"m": settings.QUALITY_MIN_HISTORY_BARS, "s": since, "l": latest})[0]
        share = (r["good"] / r["total"]) if r["total"] else 0.0
        add("history", share >= settings.QUALITY_MIN_HISTORY_SHARE,
            f"최근 100일 일봉 {settings.QUALITY_MIN_HISTORY_BARS}개 이상 종목 {r['good']}/{r['total']}({share:.0%}, "
            f"기준 {settings.QUALITY_MIN_HISTORY_SHARE:.0%})")
        if real:
            want = last_kr_trading_day(today or clock.now().date())
            got = date.fromisoformat(latest)
            lag = 0 if got >= want else _trading_days_after(got, want)
            add("freshness", lag == 0, f"최신 일봉 {latest}(기대 {want.isoformat()} 이후, {lag}거래일 늦음)",
                level="warn" if lag <= 1 else "fail")
        bad = db.query("SELECT COUNT(*) FILTER (WHERE close IS NULL OR close <= 0) AS nonpos, "
                       "COUNT(*) FILTER (WHERE change_rate IS NOT NULL AND ABS(change_rate) > :lim) AS jump "
                       "FROM daily_price WHERE trade_date = :d", {"d": latest, "lim": PRICE_LIMIT + _TOL})[0]
        add("sanity", not (bad["nonpos"] or bad["jump"]),
            f"최신일 종가 0 이하 {bad['nonpos']}건 · 가격제한폭 초과 등락 {bad['jump']}건")
    levels = [c["level"] for c in checks]
    level = "fail" if "fail" in levels else "warn" if "warn" in levels else "ok"
    bad_ones = [c["detail"] for c in checks if not c["ok"]]
    out = {"name": "kr_market", "level": level, "ok": level != "fail", "checks": checks,
           "summary": " · ".join(bad_ones) if bad_ones else "기대치 충족", "checked_at": clock.iso_now()}
    try:
        db.set_meta(GATE_META + "kr_market", json.dumps(out, ensure_ascii=False))
    except Exception as e:                            # noqa: BLE001 — 기록 실패가 배치를 막지 않게
        log.warning("품질 게이트 결과 기록 실패: %s", type(e).__name__)
    return out


def gate_and_alert(load=None, today=None):
    """게이트 + 실패면 알림(같은 문제는 ALERT_DEDUP_HOURS 안에 한 번)."""
    g = gate_market_load(load, today)
    if g["level"] == "fail":
        from app.services import alerts
        lines = [f"- {c['detail']}" for c in g["checks"] if not c["ok"]]
        alerts.notify("quality:kr_market", "국내 시세 적재 검증 실패",
                      "적재 결과가 기대치와 다릅니다(화면의 시세·추천이 비거나 오래될 수 있음).\n" + "\n".join(lines)
                      + "\n진단: .venv/bin/python tools/reco_check.py", level="error")
    return g


def gates():
    """마지막 게이트 결과들 {이름: {level, summary, checked_at}} — /health 용(상세 checks 는 제외)."""
    import json
    out = {}
    try:
        rows = db.query("SELECT key, value FROM app_meta WHERE key LIKE :p", {"p": GATE_META + "%"})
    except Exception:                                 # noqa: BLE001
        return out
    for r in rows:
        try:
            g = json.loads(r["value"] or "{}")
        except ValueError:
            continue
        out[r["key"][len(GATE_META):]] = {k: g.get(k) for k in ("level", "summary", "checked_at")}
    return out
