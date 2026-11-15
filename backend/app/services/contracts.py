"""데이터 계약(data contract) · 품질 지표 — 데이터셋마다 '지켜야 할 약속'을 선언하고 매일 측정해 남긴다.

BCBS 239(리스크 데이터 집계·보고 원칙)의 품질 차원으로 묶는다:
  completeness(완전성) · accuracy(정확성) · consistency(일관성) · timeliness(적시성) · validity(유효성)
계약 = (데이터셋, 검사 이름, 차원, 측정 SQL → 숫자 하나, 비교 연산, 기준값, 중요도). 측정값을 기준과 비교해
pass/fail 을 dq_result 에 쌓는다 → 추세(어제보다 나빠졌는가)와 데이터셋별 점수를 볼 수 있다.
critical 실패는 운영 알림. 기존 품질 게이트(quality.py — 적재 직후 막기)와 역할이 다르다: 여기는 '매일 측정·기록'.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from app import clock, db


@dataclass(frozen=True)
class Check:
    dataset: str
    name: str
    dimension: str
    sql: str
    op: str            # '<=' | '>=' | '=='
    threshold: float
    severity: str = "warn"      # warn | critical
    desc: str = ""


_LATEST = "(SELECT MAX(trade_date) FROM daily_price)"
# 한국 날짜 — CURRENT_DATE 는 DB 서버 시간대 기준이라 UTC 서버에선 00~09시(KST)에 하루 전 날짜였다(결함 29: 07:50 측정 때
# 그날 새벽 기사가 '발행일이 미래'로 잡히고, 신선도는 하루 덜 늦게 계산됐다). 저장 문자열(KST)과 같은 기준으로 맞춘다.
_KST_TODAY = "(now() AT TIME ZONE 'Asia/Seoul')::date"

CONTRACTS: tuple[Check, ...] = (
    Check("daily_price", "universe_coverage", "completeness",
          f"SELECT COALESCE(COUNT(DISTINCT d.ticker)::float / NULLIF((SELECT COUNT(*) FROM stock), 0), 0) "
          f"FROM daily_price d WHERE d.trade_date = {_LATEST}", ">=", 0.95, "critical",
          "최근 거래일에 유니버스 종목의 95% 이상 시세가 있어야 한다"),
    Check("daily_price", "invalid_price", "accuracy",
          "SELECT COUNT(*) FROM daily_price WHERE close IS NULL OR close <= 0 OR volume < 0", "==", 0, "critical",
          "종가 결측·0 이하·음수 거래량 없음"),
    Check("daily_price", "price_limit_breach", "validity",
          "SELECT COUNT(*) FROM daily_price WHERE change_rate IS NOT NULL AND ABS(change_rate) > 30.5", "==", 0, "warn",
          "등락률이 가격제한폭(±30%) 밖인 행 없음"),
    Check("daily_price", "staleness_days", "timeliness",
          f"SELECT COALESCE({_KST_TODAY} - {_LATEST}::date, 999)", "<=", 4, "critical",
          "가장 최근 시세가 4일(주말 감안) 이내"),
    Check("ohlc", "candle_inconsistent", "consistency",
          "SELECT COUNT(*) FROM ohlc WHERE high < GREATEST(open, close) OR low > LEAST(open, close) OR low <= 0",
          "==", 0, "warn", "고가 ≥ 시가·종가, 저가 ≤ 시가·종가, 저가 > 0"),
    Check("overseas_ohlc", "candle_inconsistent", "consistency",
          "SELECT COUNT(*) FROM overseas_ohlc WHERE high < GREATEST(open, close) OR low > LEAST(open, close)",
          "==", 0, "warn", "해외 일봉 고저가 일관성"),
    Check("financial_statement", "pit_violation", "consistency",
          "SELECT COUNT(*) FROM financial_statement WHERE disclosed_at IS NOT NULL AND ingested_at IS NOT NULL "
          "AND disclosed_at > ingested_at", "==", 0, "critical",
          "공시일이 적재일보다 늦은 행 없음(시점 조회의 미래 참조 방지)"),
    Check("news", "future_published", "validity",
          f"SELECT COUNT(*) FROM news WHERE published_at > {_KST_TODAY} + 1", "==", 0, "warn",
          "발행일이 미래인 기사 없음"),
    Check("news", "sentiment_coverage_7d", "completeness",
          "SELECT COALESCE(AVG((s.news_id IS NOT NULL)::int), 1) FROM news n LEFT JOIN news_sentiment s "
          f"ON s.news_id = n.news_id WHERE n.published_at >= {_KST_TODAY} - 7",
          ">=", 0.9, "warn", "최근 7일 기사의 90% 이상 감성 분류 완료"),
    Check("wallet", "negative_balance", "validity",
          "SELECT COUNT(*) FROM wallet WHERE balance < 0", "==", 0, "critical", "음수 잔액 없음"),
    Check("wallet", "ledger_mismatch", "consistency",
          "SELECT COUNT(*) FROM wallet w LEFT JOIN (SELECT username, SUM(amount) AS s FROM wallet_ledger "
          "GROUP BY username) l ON l.username = w.username WHERE COALESCE(w.balance, 0) <> COALESCE(l.s, 0)",
          "==", 0, "critical", "잔액 = 원장 합"),
    Check("wallet", "transfer_unscreened", "completeness",
          "SELECT COUNT(*) FROM wallet_ledger w WHERE w.reason = 'transfer_out' "
          "AND w.created_at >= (now() AT TIME ZONE 'Asia/Seoul') - interval '30 days' "
          "AND w.created_at >= COALESCE((SELECT date_trunc('second', MIN(d.at) AT TIME ZONE 'Asia/Seoul') "   # 원장 시각은 초 단위
          "FROM fds_decision d WHERE d.ref IS NOT NULL), 'infinity') "
          "AND NOT EXISTS (SELECT 1 FROM fds_decision d WHERE d.ref = substr(w.idem_key, 4))", "==", 0, "critical",
          "최근 30일 실행된 송금마다 FDS 판정 기록이 있다(통제 완전성 — 판정을 거치지 않은 송금 경로가 생기면 잡힌다)"),
    Check("journal_entry", "trial_balance", "consistency",
          "SELECT ABS(COALESCE(SUM(amount), 0)) FROM journal_entry", "==", 0, "critical", "모든 분개 합 = 0"),
    Check("reco_snapshot", "score_range", "validity",
          "SELECT COUNT(*) FROM reco_snapshot WHERE score < 0 OR score > 100", "==", 0, "warn", "추천 점수 0~100"),
)


def _ok(value: float, op: str, threshold: float) -> bool:
    return {"<=": value <= threshold, ">=": value >= threshold, "==": value == threshold}[op]


def run(record: bool = True) -> dict:
    """계약 전체 측정 → {score, datasets{}, failed[]}. record=True 면 dq_result 에 남기고 critical 실패는 알림."""
    now = clock.utc()
    results = []
    for c in CONTRACTS:
        try:
            v = db.query(c.sql)
            value = float(next(iter(v[0].values())) or 0)
            ok = _ok(value, c.op, c.threshold)
            err = None
        except Exception as e:                       # noqa: BLE001 — 측정 실패도 결과(실패)로 남긴다
            value, ok, err = None, False, f"{type(e).__name__}"
        results.append({"dataset": c.dataset, "check": c.name, "dimension": c.dimension, "value": value,
                        "op": c.op, "threshold": c.threshold, "severity": c.severity,
                        "status": "pass" if ok else "fail", "error": err})
    if record:
        with db.transaction() as conn:
            from sqlalchemy import text
            conn.execute(text("INSERT INTO dq_result(run_at, dataset, check_name, dimension, value, op, threshold, "
                              "severity, status) VALUES(:at, :ds, :ck, :dm, :v, :op, :th, :sv, :st)"),
                         [{"at": now, "ds": r["dataset"], "ck": r["check"], "dm": r["dimension"], "v": r["value"],
                           "op": r["op"], "th": r["threshold"], "sv": r["severity"], "st": r["status"]} for r in results])
    by_ds: dict = {}
    for r in results:
        d = by_ds.setdefault(r["dataset"], {"pass": 0, "total": 0})
        d["total"] += 1
        d["pass"] += r["status"] == "pass"
    critical = [r for r in results if r["status"] == "fail" and r["severity"] == "critical"]
    if record and critical:
        from app.services import alerts
        alerts.notify("dq:critical", "[데이터 품질] 핵심 계약 위반",
                      "\n".join(f"· {r['dataset']}.{r['check']} = {r['value']} (기준 {r['op']} {r['threshold']})"
                                for r in critical), level="error")
    passed = sum(1 for r in results if r["status"] == "pass")
    return {"at": now.isoformat(timespec="seconds"), "score": round(passed / len(results), 4),
            "datasets": {k: {**v, "score": round(v["pass"] / v["total"], 4)} for k, v in sorted(by_ds.items())},
            "failed": [r for r in results if r["status"] == "fail"], "results": results}


def trend(days: int = 14) -> list[dict]:
    """일별 통과율(최근 days 일) — 품질이 나빠지는 추세를 본다."""
    rows = db.query("SELECT (run_at AT TIME ZONE 'Asia/Seoul')::date AS d, AVG((status = 'pass')::int) AS score, "
                    "COUNT(*) AS n FROM dq_result WHERE run_at >= :t GROUP BY 1 ORDER BY 1",   # 한국 날짜로 묶는다
                    {"t": clock.utc() - timedelta(days=days)})
    return [{"date": str(r["d"])[:10], "score": round(float(r["score"]), 4), "checks": int(r["n"])}
            for r in rows]


def catalog() -> list[dict]:
    return [{"dataset": c.dataset, "check": c.name, "dimension": c.dimension, "rule": f"{c.op} {c.threshold}",
             "severity": c.severity, "desc": c.desc} for c in CONTRACTS]


def as_json() -> str:
    return json.dumps(catalog(), ensure_ascii=False, indent=1)
