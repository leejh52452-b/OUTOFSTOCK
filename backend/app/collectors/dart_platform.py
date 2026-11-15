"""DART → 분석 계층 — 시장조치·주요사항 공시(경보·신용 사건), 업종코드(KSIC), 다년 재무(다중회사), 종목 공시 피드.

  disclosures()      공시검색(list.json) 시장조치/안내(I003)·주요사항보고서(B001) — 날짜가 있어 '그때 알 수 있었던'
                     관리종목 지정·투자경고·거래정지·부도·회생 이력을 만든다(4번 신용위험 모형의 라벨, 경보 화면)
  company_sectors()  기업개황(company.json)의 업종코드 → 표준산업분류 기반 업종(지금 뜨는 산업·업종 집계)
  financials()       다중회사 주요계정(fnlttMultiAcnt, 한 번에 100개 회사) — 전 종목 × 10년 연간 재무
  gross_profit()     단일회사 전체 재무제표(fnlttSinglAcntAll)에서 매출총이익 — 골라보기 '돈 잘버는 회사' 후보만
  company_feed()     종목별 최근 공시(리서치 대체: 원문 링크는 DART 뷰어)

DART 일일 한도(키당 약 2만 건)를 다른 수집과 나눠 쓰므로 모든 함수에 호출 예산(budget)이 있다.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from sqlalchemy import text

from app import clock, db
from app.collectors import dart
from app.config import settings

log = logging.getLogger(__name__)
BASE = dart.BASE

ACTION_RULES = [                     # (정규식, 분류) — 위에서부터 먼저 맞는 것. '해제'를 먼저 본다.
    (r"관리종목.*해제", "관리종목해제"), (r"관리종목", "관리종목지정"),
    (r"투자주의환기종목.*해제", "환기종목해제"), (r"투자주의환기종목", "환기종목지정"),
    (r"투자위험종목.*해제", "경고해제"), (r"투자위험종목", "투자위험"),
    (r"투자경고종목.*해제", "경고해제"), (r"투자경고종목", "투자경고"),
    (r"투자주의종목", "투자주의"),
    (r"매매거래정지.*해제|매매거래재개|거래재개", "거래재개"), (r"매매거래정지", "거래정지"),
    (r"정리매매", "정리매매"), (r"상장폐지|상장적격성\s*실질심사", "상장폐지사유"),
    (r"불성실공시법인", "불성실공시"), (r"단기과열", "단기과열"), (r"공매도\s*과열", "공매도과열"),
    (r"부도발생", "부도"), (r"회생절차", "회생절차"), (r"영업정지", "영업정지"), (r"해산사유", "해산"),
    (r"채권은행.*관리절차", "채권은행관리"), (r"감자\s*결정", "감자"),
]
_RULES = [(re.compile(p), a) for p, a in ACTION_RULES]
CATEGORY = {"I003": "시장조치", "B001": "주요사항"}
TY_CATEGORY = {"A": "정기", "B": "주요사항", "C": "발행", "D": "지분", "E": "기타", "F": "감사", "I": "거래소"}


def classify(report_nm):
    """보고서명 → 조치/사건 분류(없으면 None). '[기재정정]' 같은 앞머리는 무시."""
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(report_nm or "")).replace(" ", "")
    for rx, action in _RULES:
        if rx.search(s):
            return action
    return None


def _call(path, params):
    r = dart._get(f"{BASE}/{path}", {"crtfc_key": settings.DART_API_KEY, **params})
    j = r.json()
    code = str(j.get("status") or "")
    if code in dart.FATAL_STATUS:
        raise dart.DartError(f"DART {dart.status_text(code)}")
    return j if code == "000" else {"list": [], "status": code}


def _store_disclosures(rows, category=None):
    n = 0
    with db.transaction() as conn:
        for r in rows:
            rn = str(r.get("rcept_no") or "")
            if len(rn) != 14:
                continue
            d = str(r.get("rcept_dt") or rn[:8])
            cat = category or TY_CATEGORY.get(str(r.get("pblntf_ty") or ""), "기타")
            conn.execute(text(
                "INSERT INTO analytics.disclosure(rcept_no, corp_code, ticker, corp_name, corp_cls, report_nm, rcept_dt, "
                "flr_nm, rm, category, action, source) VALUES(:rn, :cc, :t, :nm, :cls, :rpt, :d, :flr, :rm, :cat, :act, "
                "'dart') ON CONFLICT (rcept_no) DO UPDATE SET report_nm = EXCLUDED.report_nm, action = EXCLUDED.action, "
                "rm = EXCLUDED.rm"),
                {"rn": rn, "cc": r.get("corp_code"), "t": (r.get("stock_code") or "").strip() or None,
                 "nm": r.get("corp_name"), "cls": (r.get("corp_cls") or None), "rpt": r.get("report_nm") or "",
                 "d": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "flr": r.get("flr_nm"), "rm": (r.get("rm") or "")[:10] or None,
                 "cat": cat, "act": classify(r.get("report_nm"))})
            n += 1
    return n


def disclosures(start: date, end: date, kinds=("I003", "B001"), budget=400, call=None):
    """기간을 3개월(회사 미지정 검색 한도) 이하 창으로 나눠 상세유형별로 전 쪽을 받는다. 반환 {rows, calls, stopped}."""
    call = call or _call
    rows = calls = 0
    stopped = None
    w_end = end
    while w_end >= start and not stopped:
        w_start = max(start, w_end - timedelta(days=88))
        for kind in kinds:
            page = 1
            while True:
                if calls >= budget:
                    stopped = f"호출 예산 {budget}회 소진"
                    break
                j = call("list.json", {"bgn_de": w_start.strftime("%Y%m%d"), "end_de": w_end.strftime("%Y%m%d"),
                                       "pblntf_detail_ty": kind, "page_no": page, "page_count": 100})
                calls += 1
                rows += _store_disclosures(j.get("list") or [], CATEGORY.get(kind))
                if page >= int(j.get("total_page") or 1):
                    break
                page += 1
            if stopped:
                break
        w_end = w_start - timedelta(days=1)
    from app.collectors.krx_history import record_load
    with db.transaction() as conn:
        record_load(conn, f"{start.isoformat()}~{end.isoformat()}", "ok", dataset="dart_disclosure", rows_in=rows,
                    rows_upserted=rows, source="dart", message=stopped)
    return {"rows": rows, "calls": calls, "stopped": stopped}


def company_feed(ticker, days=90, call=None):
    """종목 한 개의 최근 공시(모든 유형) — 화면의 '공시' 탭. 반환 저장 건수."""
    corp = dart.corp_codes(settings.DART_API_KEY).get(ticker)
    if not corp:
        return 0
    end = clock.now().date()
    j = (call or _call)("list.json", {"corp_code": corp, "bgn_de": (end - timedelta(days=days)).strftime("%Y%m%d"),
                                      "end_de": end.strftime("%Y%m%d"), "page_count": 100})
    return _store_disclosures(j.get("list") or [])


# ── 업종(KSIC) ─────────────────────────────────────────────
def company_sectors(budget=500, call=None):
    """업종코드가 없는 종목만 기업개황을 조회(한 번 채우면 신규 상장 종목만). 반환 {updated, calls, missing}."""
    call = call or _call
    corp = dart.corp_codes(settings.DART_API_KEY)
    todo = [r["ticker"] for r in db.query("SELECT ticker FROM analytics.security_master WHERE ksic_code IS NULL "
                                          "ORDER BY last_seen DESC, ticker")]
    updated = calls = missing = 0
    for t in todo:
        cc = corp.get(t)
        if not cc:
            missing += 1
            continue
        if calls >= budget:
            break
        j = call("company.json", {"corp_code": cc})
        calls += 1
        code = str(j.get("induty_code") or "").strip()
        if not code:
            continue
        db.execute("UPDATE analytics.security_master SET corp_code = :cc, ksic_code = CAST(:k AS varchar), "
                   "sector = analytics.sector_of(CAST(:k AS text)), ksic_seen_at = COALESCE(ksic_seen_at, :d), "
                   "updated_at = now() WHERE ticker = :t",
                   {"cc": cc, "k": code, "t": t, "d": clock.today()})
        updated += 1
    return {"updated": updated, "calls": calls, "missing": missing, "remaining": max(0, len(todo) - missing - calls)}


# ── 다년 재무(다중회사) ─────────────────────────────────────
def parse_multi(rows):
    """fnlttMultiAcnt 행 → {종목코드: {계정: 값, basis, rcept_no}} — 회사마다 연결(CFS)이 있으면 연결만."""
    by = {}
    for r in rows:
        sc = (r.get("stock_code") or "").strip()
        if sc:
            by.setdefault(sc, []).append(r)
    out = {}
    for sc, rs in by.items():
        basis = "CFS" if any(x.get("fs_div") == "CFS" for x in rs) else "OFS"
        acc = {"basis": basis, "rcept_no": rs[0].get("rcept_no")}
        for x in rs:
            if (x.get("fs_div") or "OFS") != basis:
                continue
            nm = (x.get("account_nm") or "").replace(" ", "")
            key = "capital_stock" if re.sub(r"\(.*?\)", "", nm) == "자본금" else dart._account_key(nm)
            v = dart._to_int(x.get("thstrm_amount"))
            if key and v is not None and key not in acc:
                acc[key] = v
        out[sc] = acc
    return out


def financials(years=None, budget=300, call=None, reprt_code="11011"):
    """전 종목 연간 재무(사업보고서) — 연도마다 100개 회사씩 묶어 호출. 이미 받은 (종목, 연도)는 건너뜀."""
    call = call or _call
    corp = dart.corp_codes(settings.DART_API_KEY)
    tickers = [r["ticker"] for r in db.query("SELECT ticker FROM analytics.security_master")]
    end_year = clock.now().date().year - 1
    years = years or list(range(end_year, end_year - settings.ANALYTICS_YEARS, -1))
    have = {(r["ticker"], r["fiscal_year"]) for r in db.query(
        "SELECT ticker, fiscal_year FROM analytics.fin_annual WHERE source = 'dart'")}
    saved = calls = 0
    stopped = None
    for y in years:
        need = [(t, corp[t]) for t in tickers if t in corp and (t, y) not in have]
        for i in range(0, len(need), 100):
            if calls >= budget:
                stopped = f"호출 예산 {budget}회 소진"
                break
            chunk = need[i:i + 100]
            j = call("fnlttMultiAcnt.json", {"corp_code": ",".join(cc for _, cc in chunk), "bsns_year": str(y),
                                             "reprt_code": reprt_code})
            calls += 1
            parsed = parse_multi(j.get("list") or [])
            with db.transaction() as conn:
                for sc, a in parsed.items():
                    rn = str(a.get("rcept_no") or "")
                    conn.execute(text(
                        "INSERT INTO analytics.fin_annual(ticker, fiscal_year, fs_basis, revenue, operating_profit, "
                        "net_income, total_assets, total_liabilities, total_equity, capital_stock, current_assets, "
                        "current_liabilities, retained_earnings, disclosed_at, rcept_no, source) VALUES(:t, :y, :b, "
                        ":revenue, :operating_profit, :net_income, :total_assets, :total_liab, :total_equity, "
                        ":capital_stock, :current_assets, :current_liab, :retained, :disc, :rn, 'dart') "
                        "ON CONFLICT (ticker, fiscal_year) DO UPDATE SET fs_basis = EXCLUDED.fs_basis, "
                        "revenue = EXCLUDED.revenue, operating_profit = EXCLUDED.operating_profit, "
                        "net_income = EXCLUDED.net_income, total_assets = EXCLUDED.total_assets, "
                        "total_liabilities = EXCLUDED.total_liabilities, total_equity = EXCLUDED.total_equity, "
                        "capital_stock = EXCLUDED.capital_stock, current_assets = EXCLUDED.current_assets, "
                        "current_liabilities = EXCLUDED.current_liabilities, retained_earnings = EXCLUDED.retained_earnings, "
                        "disclosed_at = EXCLUDED.disclosed_at, rcept_no = EXCLUDED.rcept_no, source = 'dart'"),
                        {"t": sc, "y": y, "b": a["basis"], "rn": rn or None,
                         "disc": f"{rn[:4]}-{rn[4:6]}-{rn[6:8]}" if len(rn) >= 8 else None,
                         **{k: a.get(k) for k in ("revenue", "operating_profit", "net_income", "total_assets",
                                                  "total_liab", "total_equity", "capital_stock", "current_assets",
                                                  "current_liab", "retained")}})
                    saved += 1
        if stopped:
            break
    return {"saved": saved, "calls": calls, "stopped": stopped}


def gross_profit(tickers, year=None, budget=200, call=None):
    """매출총이익(전체 재무제표) — 주어진 종목만(골라보기 후보). 반환 {updated, calls}."""
    call = call or _call
    corp = dart.corp_codes(settings.DART_API_KEY)
    year = year or clock.now().date().year - 1
    updated = calls = 0
    for t in tickers:
        cc = corp.get(t)
        if not cc or calls >= budget:
            continue
        gp = None
        for fs in ("CFS", "OFS"):
            j = call("fnlttSinglAcntAll.json", {"corp_code": cc, "bsns_year": str(year), "reprt_code": "11011",
                                                "fs_div": fs})
            calls += 1
            for x in j.get("list") or []:
                if (x.get("account_nm") or "").replace(" ", "") == "매출총이익":
                    gp = dart._to_int(x.get("thstrm_amount"))
                    break
            if gp is not None or j.get("list"):
                break
        if gp is not None:
            db.execute("UPDATE analytics.fin_annual SET gross_profit = :g WHERE ticker = :t AND fiscal_year = :y",
                       {"g": gp, "t": t, "y": year})
            updated += 1
    return {"updated": updated, "calls": calls}
