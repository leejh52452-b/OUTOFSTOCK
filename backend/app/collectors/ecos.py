"""한국은행 경제통계시스템(ECOS) Open API — 금리·환율의 공식 시계열(10년 백필 + 매일 증분).

  키: ECOS_API_KEY(.env) — https://ecos.bok.or.kr 에서 무료·즉시 발급. 없으면 이 수집기는 건너뛴다
      (그때 금리는 한국투자증권 '금리 종합'의 당일 값만 쌓인다).
  요청: /api/StatisticSearch/{키}/json/kr/{시작}/{끝}/{통계표}/{주기}/{시작일}/{종료일}/{항목}
  항목 코드는 통계표 개편 때 바뀔 수 있어, 처음에 StatisticItemList 로 '이름'을 보고 코드를 찾는다
  (이름 매칭이 실패하면 알려진 기본 코드로). 찾은 코드는 app_meta 에 캐시.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import requests
from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)
BASE = "https://ecos.bok.or.kr/api"
# (통계표, 주기, 시리즈, 이름에 있어야 할 글자들, 알려진 기본 항목코드)
SPECS = [
    ("817Y002", "D", "CALL", ("콜금리", "전체"), "010101000"),
    ("817Y002", "D", "CD91", ("CD", "91"), "010502000"),
    ("817Y002", "D", "KTB3Y", ("국고채", "3년"), "010200000"),
    ("817Y002", "D", "KTB10Y", ("국고채", "10년"), "010210000"),
    ("817Y002", "D", "KDB1Y", ("산금채",), "010260000"),
    ("817Y002", "D", "CORP_AA3Y", ("회사채", "AA-"), "010300000"),
    ("817Y002", "D", "CORP_BBB3Y", ("회사채", "BBB-"), "010320000"),
    ("731Y001", "D", "USDKRW", ("미국달러",), "0000001"),
    ("731Y001", "D", "JPYKRW", ("일본엔",), "0000002"),
    ("731Y001", "D", "EURKRW", ("유로",), "0000003"),
    ("731Y001", "D", "CNYKRW", ("위안",), "0000053"),
    ("722Y001", "D", "BASE", ("기준금리",), "0101000"),
]


class EcosError(RuntimeError):
    pass


def enabled():
    return bool(getattr(settings, "ECOS_API_KEY", ""))


def _get(path):
    r = requests.get(f"{BASE}/{path}", timeout=(5, 30))
    r.raise_for_status()
    j = r.json()
    if "RESULT" in j:                                    # 오류·데이터 없음은 {"RESULT": {"CODE": "INFO-200", ...}}
        code = j["RESULT"].get("CODE", "")
        if code == "INFO-200":                           # 해당 데이터 없음
            return []
        raise EcosError(f"ECOS {code}: {j['RESULT'].get('MESSAGE', '')}")
    for v in j.values():
        if isinstance(v, dict) and "row" in v:
            return v["row"]
    return []


def resolve_items(fetch=None):
    """통계표별 항목 목록에서 이름으로 코드를 찾는다 → {시리즈: (통계표, 주기, 코드)}."""
    cached = db.get_meta("ecos_items")
    if cached:
        try:
            return {k: tuple(v) for k, v in json.loads(cached).items()}
        except ValueError:
            pass
    fetch = fetch or _get
    found = {}
    for stat in sorted({s[0] for s in SPECS}):
        try:
            items = fetch(f"StatisticItemList/{settings.ECOS_API_KEY}/json/kr/1/500/{stat}")
        except Exception as e:                           # noqa: BLE001 — 목록 실패면 기본 코드
            log.warning("ECOS 항목 목록 실패 %s: %s", stat, type(e).__name__)
            items = []
        for st, cyc, series, keys, default in (s for s in SPECS if s[0] == stat):
            hit = next((i for i in items if all(k in str(i.get("ITEM_NAME", "")) for k in keys)
                        and str(i.get("CYCLE", cyc)) in (cyc, "")), None)
            found[series] = (st, cyc, (hit or {}).get("ITEM_CODE") or default)
    db.set_meta("ecos_items", json.dumps(found))
    return found


def collect(start: date | None = None, end: date | None = None, fetch=None):
    """시리즈마다 기간 조회(한 번에 최대 1만 행) → analytics.indicator_daily. 반환 {시리즈: 행 수}."""
    if not enabled():
        return {"skipped": "ECOS_API_KEY 없음"}
    end = end or clock.now().date()
    start = start or end - timedelta(days=settings.ANALYTICS_YEARS * 365 + 10)
    fetch = fetch or _get
    out, errors = {}, {}
    for series, (stat, cyc, item) in resolve_items(fetch).items():
        try:
            rows = fetch(f"StatisticSearch/{settings.ECOS_API_KEY}/json/kr/1/10000/{stat}/{cyc}/"
                         f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/{item}")
        except Exception as e:                           # noqa: BLE001
            from app.logging_conf import safe_error
            errors[series] = safe_error(e, 120)
            continue
        n = 0
        with db.transaction() as conn:
            for r in rows:
                t, v = str(r.get("TIME") or ""), str(r.get("DATA_VALUE") or "").replace(",", "")
                if len(t) != 8 or v in ("", "-"):
                    continue
                conn.execute(text("INSERT INTO analytics.indicator_daily(series, trade_date, value, source) "
                                  "VALUES(:s, :d, :v, 'ecos') ON CONFLICT (series, trade_date) DO UPDATE SET "
                                  "value = EXCLUDED.value, source = EXCLUDED.source"),
                             {"s": series, "d": f"{t[:4]}-{t[4:6]}-{t[6:]}", "v": float(v)})
                n += 1
        out[series] = n
    from app.collectors.krx_history import record_load
    with db.transaction() as conn:
        record_load(conn, end.isoformat(), "ok" if out and not errors else ("failed" if not out else "ok"),
                    dataset="ecos", rows_in=sum(out.values()), rows_upserted=sum(out.values()), source="ecos",
                    message=json.dumps({"errors": errors}, ensure_ascii=False) if errors else None)
    return {"rows": out, "errors": errors}
