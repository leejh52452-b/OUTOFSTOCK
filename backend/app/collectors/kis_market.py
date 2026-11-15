"""한국투자증권 시세분석·예탁원정보 → 분석 계층 — 토스·네이버 증권의 수급·증시자금·금리·경보·일정 기능의 원천.

  investor_market()   시장별 투자자매매동향(일별)      → analytics.investor_flow_market (개인·외국인·기관 + 세부 10종)
  investor_rank()     국내기관·외국인 매매종목 가집계   → analytics.investor_rank (외국인·기관 순매수/순매도 상위)
  investor_stocks()   주식현재가 투자자(종목별 30일)   → analytics.investor_flow_stock
  market_funds()      국내 증시자금 종합(금융투자협회) → analytics.market_funds (고객예탁금·신용융자·미수금·펀드)
  interest_rates()    금리 종합                       → analytics.indicator_daily (국고채·회사채·CD·콜·산금채 …)
  stock_status()      주식현재가 시세의 상태 코드       → analytics.stock_status (관리·정지·시장경고·단기과열 …)
  dividends()/agm()/ipo()  예탁원 배당·주주총회·공모 일정 → analytics.dividend · analytics.corp_event

금액 단위: KIS 의 수급 '거래대금'은 화면마다 원/백만원이 섞여 있어, 받은 값의 크기를 수량×가격과 비교해
단위를 판정한 뒤 원으로 바꿔 저장한다(_unit_factor). 판정 근거는 적재 이력(load_log.message)에 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from datetime import date, timedelta

from sqlalchemy import text

from app import clock, db
from app.collectors import kis_api
from app.collectors.kis_api import num, ymd

log = logging.getLogger(__name__)

INVESTORS = {   # 출력 필드 접두어 → 저장 코드
    "prsn": "prsn", "frgn": "frgn", "orgn": "orgn", "scrt": "scrt", "ivtr": "ivtr", "pe_fund": "pe_fund",
    "bank": "bank", "insu": "insu", "mrbn": "mrbn", "fund": "fund", "etc_orgt": "etc_orgt", "etc_corp": "etc_corp",
    "etc": "etc",
}
INVESTOR_NAMES = {"prsn": "개인", "frgn": "외국인", "orgn": "기관계", "scrt": "금융투자", "ivtr": "투신",
                  "pe_fund": "사모", "bank": "은행", "insu": "보험", "mrbn": "종금", "fund": "연기금 등",
                  "etc_orgt": "기타단체", "etc_corp": "기타법인", "etc": "기타"}
MARKET_CODES = {"KOSPI": ("0001", "KSP"), "KOSDAQ": ("1001", "KSQ")}


def enabled():
    return kis_api.enabled()


def _record(dataset, key, status, rows=None, message=None):
    from app.collectors.krx_history import record_load
    with db.transaction() as conn:
        record_load(conn, key, status, dataset=dataset, rows_in=rows, rows_upserted=rows, source="kis",
                    message=message)


def _unit_factor(pairs):
    """[(금액, 수량×가격 또는 수량)] → 금액을 원으로 바꾸는 배수(1 또는 1,000,000).
    금액이 원이면 금액/(수량×가격) ≈ 1, 백만원이면 ≈ 0.000001 — 중앙값으로 판정(데이터 없으면 백만원 가정)."""
    ratios = [abs(v) / abs(ref) for v, ref in pairs if v and ref]
    if not ratios:
        return 1_000_000
    return 1 if statistics.median(ratios) > 0.01 else 1_000_000


# ── 투자자별 매매동향(시장) ─────────────────────────────────
def parse_investor_market(rows, market):
    """출력 행들 → [(trade_date, market, investor, net_qty, net_value_raw)] + 단위 배수."""
    out, pairs = [], []
    for r in rows:
        d = ymd(r.get("stck_bsop_date"))
        if not d:
            continue
        for prefix, code in INVESTORS.items():
            q = num(r.get(f"{prefix}_ntby_qty"), None)
            if q is None:
                q = num(r.get(f"{prefix}_ntby_vol"), None)            # 사모·기타단체·기타법인은 _vol
            v = num(r.get(f"{prefix}_ntby_tr_pbmn"), None)
            if q is None and v is None:
                continue
            out.append((d, market, code, None if q is None else int(q), v))
            if code in ("prsn", "frgn", "orgn") and q and v:
                pairs.append((v, q * 10000))             # 시장 평균 체결가를 1만원 안팎으로 보고 단위만 판정
    return out, _unit_factor(pairs)


def investor_market(on: date | None = None, fetch=None):
    """코스피·코스닥 투자자별 순매수(일별, 응답에 담긴 여러 날 전부) → 저장. 반환 {rows, days, unit}."""
    on = on or clock.now().date()
    total, days, units = 0, set(), {}
    for market, (iscd, iscd1) in MARKET_CODES.items():
        params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd, "FID_INPUT_DATE_1": on.strftime("%Y%m%d"),
                  "FID_INPUT_ISCD_1": iscd1, "FID_INPUT_DATE_2": on.strftime("%Y%m%d"), "FID_INPUT_ISCD_2": iscd}
        pages = (fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
                                       "FHPTJ04040000", params)
        parsed, factor = parse_investor_market(kis_api.rows(pages), market)
        units[market] = "원" if factor == 1 else "백만원"
        with db.transaction() as conn:
            for d, mk, inv, q, v in parsed:
                conn.execute(text(
                    "INSERT INTO analytics.investor_flow_market(trade_date, market, investor, net_qty, net_value, source) "
                    "VALUES(:d, :m, :i, :q, :v, 'kis') ON CONFLICT (trade_date, market, investor) DO UPDATE SET "
                    "net_qty = EXCLUDED.net_qty, net_value = EXCLUDED.net_value, source = EXCLUDED.source"),
                    {"d": d, "m": mk, "i": inv, "q": q, "v": None if v is None else int(v * factor)})
                days.add(d)
        total += len(parsed)
    _record("investor_market", on.isoformat(), "ok" if total else "empty", total,
            message=json.dumps({"unit": units}, ensure_ascii=False))
    return {"rows": total, "days": len(days), "unit": units}


# ── 외국인·기관 순매수 상위(가집계) ───────────────────────────
def investor_rank(n=30, fetch=None, as_of: date | None = None):
    """외국인·기관 × 순매수·순매도 상위 n(금액순) → analytics.investor_rank. 장중엔 가집계, 장 마감 후 확정에 가까움."""
    as_of = as_of or clock.now().date()
    saved = 0
    for inv, etc in (("frgn", "1"), ("orgn", "2")):
        for side, sort in (("buy", "0"), ("sell", "1")):
            params = {"FID_COND_MRKT_DIV_CODE": "V", "FID_COND_SCR_DIV_CODE": "16449", "FID_INPUT_ISCD": "0000",
                      "FID_DIV_CLS_CODE": "1", "FID_RANK_SORT_CLS_CODE": sort, "FID_ETC_CLS_CODE": etc}
            rows = kis_api.rows((fetch or kis_api.get)(
                "/uapi/domestic-stock/v1/quotations/foreign-institution-total", "FHPTJ04400000", params))[:n]
            vkey = f"{inv}_ntby_tr_pbmn"
            factor = _unit_factor([(num(r.get(vkey)), num(r.get(f"{inv}_ntby_qty"), 0) * num(r.get("stck_prpr"), 0))
                                   for r in rows])
            with db.transaction() as conn:
                conn.execute(text("DELETE FROM analytics.investor_rank WHERE as_of = :d AND investor = :i AND side = :s"),
                             {"d": as_of, "i": inv, "s": side})
                for k, r in enumerate(rows, 1):
                    code = str(r.get("mksc_shrn_iscd") or "").strip()
                    if not code:
                        continue
                    v = num(r.get(vkey))
                    conn.execute(text(
                        "INSERT INTO analytics.investor_rank(as_of, investor, side, rank, ticker, name, net_qty, net_value, "
                        "price, change_rate, source) VALUES(:d, :i, :s, :k, :t, :nm, :q, :v, :p, :cr, 'kis')"),
                        {"d": as_of, "i": inv, "s": side, "k": k, "t": code, "nm": (r.get("hts_kor_isnm") or "").strip(),
                         "q": int(num(r.get(f"{inv}_ntby_qty"), 0)), "v": None if v is None else int(v * factor),
                         "p": int(num(r.get("stck_prpr"), 0)) or None,
                         "cr": _signed(num(r.get("prdy_ctrt")), r.get("prdy_vrss_sign"))})
                    saved += 1
    _record("investor_rank", as_of.isoformat(), "ok" if saved else "empty", saved)
    return {"rows": saved, "as_of": as_of.isoformat()}


def _signed(v, sign):
    """KIS 전일대비부호 4·5(하락)면 음수."""
    if v is None:
        return None
    return -abs(v) if str(sign) in ("4", "5") else v


# ── 종목별 투자자(30일) ─────────────────────────────────────
def investor_stocks(tickers, fetch=None):
    """종목별 개인·외국인·기관 순매수 최근 약 30일 → analytics.investor_flow_stock. 실패 종목은 건너뜀."""
    ok = failed = 0
    for t in tickers:
        try:
            rows = kis_api.rows((fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/inquire-investor",
                                                        "FHKST01010900",
                                                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": t}))
        except Exception as e:                                # noqa: BLE001 — 한 종목 실패가 전체를 막지 않게
            failed += 1
            log.warning("종목 수급 실패 %s: %s", t, type(e).__name__)
            if failed >= 5 and ok == 0:
                raise
            continue
        factor = _unit_factor([(num(r.get("frgn_ntby_tr_pbmn")), num(r.get("frgn_ntby_qty"), 0) *
                                num(r.get("stck_clpr"), 0)) for r in rows])
        with db.transaction() as conn:
            for r in rows:
                d = ymd(r.get("stck_bsop_date"))
                if not d or all(num(r.get(f"{p}_ntby_qty")) is None for p in ("prsn", "frgn", "orgn")):
                    continue                                    # 당일분은 장 마감 후에야 채워진다
                vals = {p: num(r.get(f"{p}_ntby_tr_pbmn")) for p in ("prsn", "frgn", "orgn")}
                conn.execute(text(
                    "INSERT INTO analytics.investor_flow_stock(ticker, trade_date, prsn_net_qty, frgn_net_qty, "
                    "orgn_net_qty, prsn_net_value, frgn_net_value, orgn_net_value, source) "
                    "VALUES(:t, :d, :pq, :fq, :oq, :pv, :fv, :ov, 'kis') ON CONFLICT (ticker, trade_date) DO UPDATE SET "
                    "prsn_net_qty = EXCLUDED.prsn_net_qty, frgn_net_qty = EXCLUDED.frgn_net_qty, "
                    "orgn_net_qty = EXCLUDED.orgn_net_qty, prsn_net_value = EXCLUDED.prsn_net_value, "
                    "frgn_net_value = EXCLUDED.frgn_net_value, orgn_net_value = EXCLUDED.orgn_net_value"),
                    {"t": t, "d": d, "pq": _int(r.get("prsn_ntby_qty")), "fq": _int(r.get("frgn_ntby_qty")),
                     "oq": _int(r.get("orgn_ntby_qty")),
                     **{k: None if vals[p] is None else int(vals[p] * factor)
                        for k, p in (("pv", "prsn"), ("fv", "frgn"), ("ov", "orgn"))}})
        ok += 1
    _record("investor_stock", clock.now().date().isoformat(), "ok" if ok else "failed", ok,
            message=f"실패 {failed}종목" if failed else None)
    return {"ok": ok, "failed": failed}


def _int(v):
    x = num(v)
    return None if x is None else int(x)


# ── 증시자금 ────────────────────────────────────────────────
FUND_FIELDS = {"customer_deposit": "cust_dpmn_amt", "credit_balance": "crdt_loan_rmnd", "uncollected": "uncl_amt",
               "futures_deposit": "futs_tfam_amt", "equity_fund": "sttp_amt", "mixed_fund": "mxtp_amt",
               "bond_fund": "bntp_amt", "mmf": "mmf_amt", "collateral_loan": "secu_lend_amt"}


def market_funds(on: date | None = None, fetch=None):
    """국내 증시자금 종합(억원, 금융투자협회 자료) — 응답에 담긴 날짜 전부 저장."""
    on = on or clock.now().date()
    rows = kis_api.rows((fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/mktfunds", "FHKST649100C0",
                                               {"FID_INPUT_DATE_1": on.strftime("%Y%m%d")}))
    n = 0
    with db.transaction() as conn:
        for r in rows:
            d = ymd(r.get("bsop_date"))
            if not d:
                continue
            vals = {k: _int(r.get(f)) for k, f in FUND_FIELDS.items()}
            if all(v is None for v in vals.values()):
                continue
            cols = ", ".join(FUND_FIELDS)
            conn.execute(text(
                f"INSERT INTO analytics.market_funds(trade_date, {cols}, source) "
                f"VALUES(:d, {', '.join(':' + k for k in FUND_FIELDS)}, 'kis') ON CONFLICT (trade_date) DO UPDATE SET "
                + ", ".join(f"{k} = EXCLUDED.{k}" for k in FUND_FIELDS)), {"d": d, **vals})
            n += 1
    _record("market_funds", on.isoformat(), "ok" if n else "empty", n)
    return {"rows": n}


# ── 금리 종합 ───────────────────────────────────────────────
RATE_NAMES = (   # (이름에 들어 있는 글자들(공백·괄호 제거 후), 시리즈) — 위에서부터 먼저 맞는 것
    (("국고채", "10"), "KTB10Y"), (("국고채", "3"), "KTB3Y"), (("회사채", "BBB"), "CORP_BBB3Y"),
    (("회사채", "AA"), "CORP_AA3Y"), (("CD",), "CD91"), (("콜",), "CALL"), (("산금채",), "KDB1Y"),
    (("미국", "10"), "US10Y"), (("미국채", "10"), "US10Y"), (("기준금리",), "BASE"),
)


def rate_series(name):
    s = "".join(ch for ch in str(name or "") if ch not in " ()[]·,-").upper()
    for keys, series in RATE_NAMES:
        if all(k.upper() in s for k in keys):
            if series == "KTB3Y" and ("30" in s or "13" in s):          # 국고채 30년·13주 등은 제외
                return None
            return series
    return None


def interest_rates(fetch=None):
    """금리 종합(국내·해외) → 이름으로 시리즈를 맞춰 지표 표에 저장. 반환 {saved: {시리즈: 값}, unknown: [이름]}."""
    pages = (fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/comp-interest", "FHPST07020000",
                                   {"FID_COND_MRKT_DIV_CODE": "I", "FID_COND_SCR_DIV_CODE": "20702",
                                    "FID_DIV_CLS_CODE": "1", "FID_DIV_CLS_CODE1": ""})
    saved, unknown = {}, []
    today = clock.now().date().isoformat()
    with db.transaction() as conn:
        for r in kis_api.rows(pages, "output1") + kis_api.rows(pages, "output2") + kis_api.rows(pages, "output"):
            name = r.get("hts_kor_isnm")
            v = num(r.get("bond_mnrt_prpr"))
            series = rate_series(name)
            if v is None:
                continue
            if not series:
                unknown.append(str(name))
                continue
            d = ymd(r.get("stck_bsop_date")) or today
            conn.execute(text("INSERT INTO analytics.indicator_daily(series, trade_date, value, source) "
                              "VALUES(:s, :d, :v, 'kis') ON CONFLICT (series, trade_date) DO UPDATE SET "
                              "value = EXCLUDED.value, source = EXCLUDED.source"), {"s": series, "d": d, "v": v})
            saved[series] = v
    _record("rates_kis", today, "ok" if saved else "empty", len(saved),
            message=("모르는 이름: " + ", ".join(unknown[:10])) if unknown else None)
    return {"saved": saved, "unknown": unknown}


# ── 종목 상태(관리·정지·경고) ───────────────────────────────
STATUS_CODES = {"51": "관리종목", "52": "투자위험", "53": "투자경고", "54": "투자주의", "55": "신용가능",
                "57": "증거금 100%", "58": "거래정지", "59": "단기과열"}


def parse_status(o):
    st = str(o.get("iscd_stat_cls_code") or "").strip()
    warn = {"01": 1, "02": 2, "03": 3}.get(str(o.get("mrkt_warn_cls_code") or "").strip(), 0)
    warn = max(warn, {"54": 1, "53": 2, "52": 3}.get(st, 0))
    yn = lambda k: str(o.get(k) or "").strip().upper() == "Y"               # noqa: E731
    vi = str(o.get("vi_cls_code") or "").strip().upper()
    return {"managed": yn("mang_issu_cls_code") or st == "51", "halted": yn("temp_stop_yn") or st == "58",
            "warn_level": warn, "caution": yn("invt_caful_yn"), "short_overheat": yn("short_over_yn") or st == "59",
            "liquidation": yn("sltr_yn"), "vi": vi not in ("", "N", "0"), "status_code": st or None,
            "sector_name": (o.get("bstp_kor_isnm") or "").strip() or None,
            "per": num(o.get("per")), "pbr": num(o.get("pbr")), "eps": num(o.get("eps")), "bps": num(o.get("bps")),
            "w52_high": _int(o.get("w52_hgpr")), "w52_low": _int(o.get("w52_lwpr")),
            "foreign_ratio": num(o.get("hts_frgn_ehrt"))}


def stock_status(tickers, fetch=None):
    """종목 현재가 시세의 상태 코드 → analytics.stock_status. 반환 {ok, failed, flagged}."""
    ok = failed = flagged = 0
    today = clock.now().date()
    for t in tickers:
        try:
            rows = kis_api.rows((fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/inquire-price",
                                                        "FHKST01010100",
                                                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": t}))
        except Exception as e:                                # noqa: BLE001
            failed += 1
            log.warning("종목 상태 실패 %s: %s", t, type(e).__name__)
            if failed >= 5 and ok == 0:
                raise
            continue
        if not rows:
            continue
        s = parse_status(rows[0])
        flagged += bool(s["managed"] or s["halted"] or s["warn_level"] or s["liquidation"] or s["short_overheat"])
        db.execute(
            "INSERT INTO analytics.stock_status(ticker, as_of, managed, halted, warn_level, caution, short_overheat, "
            "liquidation, vi, status_code, sector_name, per, pbr, eps, bps, w52_high, w52_low, foreign_ratio, source, "
            "captured_at) VALUES(:t, :d, :managed, :halted, :warn_level, :caution, :short_overheat, :liquidation, :vi, "
            ":status_code, :sector_name, :per, :pbr, :eps, :bps, :w52_high, :w52_low, :foreign_ratio, 'kis', now()) "
            "ON CONFLICT (ticker) DO UPDATE SET as_of = EXCLUDED.as_of, managed = EXCLUDED.managed, "
            "halted = EXCLUDED.halted, warn_level = EXCLUDED.warn_level, caution = EXCLUDED.caution, "
            "short_overheat = EXCLUDED.short_overheat, liquidation = EXCLUDED.liquidation, vi = EXCLUDED.vi, "
            "status_code = EXCLUDED.status_code, sector_name = EXCLUDED.sector_name, per = EXCLUDED.per, "
            "pbr = EXCLUDED.pbr, eps = EXCLUDED.eps, bps = EXCLUDED.bps, w52_high = EXCLUDED.w52_high, "
            "w52_low = EXCLUDED.w52_low, foreign_ratio = EXCLUDED.foreign_ratio, source = 'kis', captured_at = now()",
            {"t": t, "d": today, **s})
        ok += 1
    _record("stock_status", today.isoformat(), "ok" if ok else "failed", ok,
            message=f"위험 신호 {flagged}종목, 실패 {failed}")
    return {"ok": ok, "failed": failed, "flagged": flagged}


# ── 예탁원 일정(배당·주주총회·공모) ──────────────────────────
def _event(conn, key, d, ticker, kind, title, detail, source="kis"):
    conn.execute(text(
        "INSERT INTO analytics.corp_event(event_key, event_date, ticker, kind, title, detail, source) "
        "VALUES(:k, :d, :t, :kind, :title, CAST(:detail AS jsonb), :src) ON CONFLICT (event_key) DO UPDATE SET "
        "event_date = EXCLUDED.event_date, title = EXCLUDED.title, detail = EXCLUDED.detail, captured_at = now()"),
        {"k": key[:120], "d": d, "t": ticker, "kind": kind, "title": title,
         "detail": json.dumps(detail, ensure_ascii=False), "src": source})


def _ksd(path, tr_id, start, end, extra=None, fetch=None, max_pages=20):
    """예탁원정보 공통 — 기간 조회(연속 조회 max_pages 쪽까지). 응답 키는 output1(문서) 또는 output."""
    params = {"CTS": "", "F_DT": start.strftime("%Y%m%d"), "T_DT": end.strftime("%Y%m%d"), "SHT_CD": "", **(extra or {})}
    pages = fetch(path, tr_id, params) if fetch else kis_api.get(path, tr_id, params, max_pages=max_pages)
    return kis_api.rows(pages, "output1") or kis_api.rows(pages)


def _kind(s):
    s = str(s or "")
    return "중간" if "중간" in s else "분기" if "분기" in s else "결산"


def dividends(start: date, end: date, fetch=None):
    """예탁원 배당 일정(전 종목) → analytics.dividend + 일정(배당기준일·배당지급일). 반환 {rows}."""
    rows = _ksd("/uapi/domestic-stock/v1/ksdinfo/dividend", "HHKDB669102C0", start, end,
                {"GB1": "0", "HIGH_GB": ""}, fetch)
    n = 0
    with db.transaction() as conn:
        for r in rows:
            t, rd = str(r.get("sht_cd") or "").strip(), ymd(r.get("record_date"))
            if not t or not rd:
                continue
            kind = _kind(r.get("divi_kind"))
            dps, pay = num(r.get("per_sto_divi_amt")), ymd(r.get("divi_pay_dt"))
            conn.execute(text(
                "INSERT INTO analytics.dividend(ticker, record_date, kind, dps, div_rate, stock_div_rate, pay_date, source) "
                "VALUES(:t, :rd, :k, :dps, :dr, :sdr, :pay, 'kis') ON CONFLICT (ticker, record_date, kind) DO UPDATE SET "
                "dps = EXCLUDED.dps, div_rate = EXCLUDED.div_rate, stock_div_rate = EXCLUDED.stock_div_rate, "
                "pay_date = EXCLUDED.pay_date"),
                {"t": t, "rd": rd, "k": kind, "dps": dps, "dr": num(r.get("divi_rate")), "sdr": num(r.get("stk_divi_rate")),
                 "pay": pay})
            title = f"{kind}배당 기준일" + (f" · 주당 {dps:,.0f}원" if dps else "")
            _event(conn, f"ksd:div:{t}:{rd}:{kind}", rd, t, "배당기준일", title, {"dps": dps, "kind": kind})
            if pay:
                _event(conn, f"ksd:divpay:{t}:{pay}:{kind}", pay, t, "배당지급일", f"{kind}배당 지급", {"dps": dps})
            n += 1
    _record("ksd_dividend", f"{start.isoformat()}~{end.isoformat()}", "ok" if n else "empty", n)
    return {"rows": n}


def agm(start: date, end: date, fetch=None):
    rows = _ksd("/uapi/domestic-stock/v1/ksdinfo/sharehld-meet", "HHKDB669111C0", start, end, None, fetch)
    n = 0
    with db.transaction() as conn:
        for r in rows:
            t, d = str(r.get("sht_cd") or "").strip(), ymd(r.get("gen_meet_dt"))
            if not t or not d:
                continue
            kind = (r.get("gen_meet_type") or "").strip()
            _event(conn, f"ksd:agm:{t}:{d}", d, t, "주주총회", f"{kind or '정기'} 주주총회",
                   {"agenda": (r.get("agenda") or "").strip()[:300], "record_date": ymd(r.get("record_date"))})
            n += 1
    _record("ksd_agm", f"{start.isoformat()}~{end.isoformat()}", "ok" if n else "empty", n)
    return {"rows": n}


def ipo(start: date, end: date, fetch=None):
    rows = _ksd("/uapi/domestic-stock/v1/ksdinfo/pub-offer", "HHKDB669108C0", start, end, None, fetch)
    n = 0
    with db.transaction() as conn:
        for r in rows:
            t = str(r.get("sht_cd") or "").strip()
            sub = str(r.get("subscr_dt") or "")
            first = ymd(sub.split("~")[0]) if sub else None
            listed = ymd(r.get("list_dt"))
            price = num(r.get("fix_subscr_pri"))
            info = {"price": price, "lead_manager": (r.get("lead_mgr") or "").strip(), "subscription": sub.strip()}
            if first:
                _event(conn, f"ksd:ipo:{t}:{first}", first, t or None, "공모청약",
                       "공모주 청약" + (f" · 공모가 {price:,.0f}원" if price else ""), info)
                n += 1
            if listed:
                _event(conn, f"ksd:list:{t}:{listed}", listed, t or None, "신규상장", "신규 상장", info)
                n += 1
    _record("ksd_ipo", f"{start.isoformat()}~{end.isoformat()}", "ok" if n else "empty", n)
    return {"rows": n}


def opinion_key(r):
    return hashlib.sha256(f"{r.get('invt_opnn')}|{r.get('rgbf_invt_opnn')}|{r.get('hts_goal_prc')}".encode()).hexdigest()[:16]


def invest_opinions(ticker, days=365, fetch=None):
    """증권사 투자의견·목표가(종목) → analytics.analyst_opinion. 반환 저장 건수."""
    end = clock.now().date()
    start = end - timedelta(days=days)
    rows = kis_api.rows((fetch or kis_api.get)("/uapi/domestic-stock/v1/quotations/invest-opinion", "FHKST663300C0",
                                               {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "16633",
                                                "FID_INPUT_ISCD": ticker, "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                                                "FID_INPUT_DATE_2": end.strftime("%Y%m%d")}))
    n = 0
    with db.transaction() as conn:
        for r in rows:
            d = ymd(r.get("stck_bsop_date"))
            if not d:
                continue
            conn.execute(text(
                "INSERT INTO analytics.analyst_opinion(ticker, opinion_date, row_key, opinion, prev_opinion, target_price, "
                "source) VALUES(:t, :d, :k, :o, :p, :tp, 'kis') ON CONFLICT DO NOTHING"),
                {"t": ticker, "d": d, "k": opinion_key(r), "o": (r.get("invt_opnn") or "").strip() or None,
                 "p": (r.get("rgbf_invt_opnn") or "").strip() or None, "tp": _int(r.get("hts_goal_prc")) or None})
            n += 1
    return n
