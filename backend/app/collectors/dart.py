"""① 재무 수집기 — DART(OpenDART) → financial_statement. corp_code 매핑·CFS우선·부채비율 계산
+ 부실예측(Altman Z'')용 대차대조표 항목(자산총계·유동자산·유동부채·부채총계·이익잉여금)."""
import io
import re
import zipfile
import logging

import requests

from app import clock
from app.config import settings
from app.retry import retry
from app.services import catalog
from app import db
from app.logging_conf import safe_error

log = logging.getLogger(__name__)

def _xml(data):
    """외부 XML 파싱 — defusedxml 이 있으면 그것으로(엔티티 확장·외부 엔티티 공격 차단, bandit B314).
    없으면 표준 라이브러리(파이썬 3.7.1+ 의 expat 은 '억만 개의 웃음' 확장 공격을 막는다)."""
    try:
        from defusedxml.ElementTree import fromstring
    except ImportError:
        from xml.etree.ElementTree import fromstring       # nosec B405
    # 아래 nosec: defusedxml 이 없을 때만 표준 파서를 쓴다(위 설명)
    return fromstring(data)  # nosec B314

BASE = "https://opendart.fss.or.kr/api"
DART_MAX_CONSECUTIVE_FAILS = 5


@retry(times=3, delay=1.0, exceptions=(requests.RequestException,))
def _get(url, params):
    # 연결 10초·읽기 60초 — corpCode.xml(zip, 수 MB)은 느린 망에서 30초를 넘기기도 한다
    r = requests.get(url, params=params, timeout=(10, 60))
    r.raise_for_status()
    return r


# OpenDART 응답 상태 코드(개발가이드). 치명(FATAL) 코드는 몇 번을 다시 불러도 같으므로 그 회차를 바로 멈춘다.
STATUS_MEANING = {
    "000": "정상", "010": "등록되지 않은 키", "011": "사용할 수 없는 키(일시 중지 등)", "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음", "014": "파일이 존재하지 않음", "020": "요청 제한 초과(키당 하루 약 2만 건)",
    "021": "조회 가능한 회사 개수 초과(최대 100개)", "100": "필드의 부적절한 값", "101": "부적절한 접근",
    "800": "시스템 점검 중", "900": "정의되지 않은 오류", "901": "개인정보 보유기간 만료로 사용할 수 없는 키",
}
FATAL_STATUS = {"010", "011", "012", "020", "100", "101", "800", "901"}
CORP_CACHE_DAYS = 7                           # 회사 코드 목록은 하루에도 거의 안 바뀐다 — 매 배치 3MB 를 받을 이유가 없다


class DartError(RuntimeError):
    """DART 가 zip 대신 오류(JSON/XML)를 돌려줌 — 키 오류·일일 한도 초과 020·점검 800 등(메시지에 키는 안 담음)."""


def status_text(code):
    return f"status={code}({STATUS_MEANING.get(code, '알 수 없는 코드')})"


def explain(e):
    """오류 → 사람이 바로 조치할 수 있는 한 줄(원문에는 키가 섞일 수 있어 safe_error 로 가린다)."""
    if isinstance(e, requests.exceptions.SSLError):
        hint = "TLS 인증서 검증 실패 — 학교망 프록시가 HTTPS 를 가로채거나 VM 시계가 틀림(timedatectl 확인)"
    elif isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError)):
        hint = "opendart.fss.or.kr 에 연결 못함 — VM 외부망(방화벽·프록시) 확인: curl -I https://opendart.fss.or.kr"
    elif isinstance(e, requests.exceptions.ReadTimeout):
        hint = "DART 응답 지연(시간 초과) — 잠시 뒤 다시"
    elif isinstance(e, requests.exceptions.HTTPError):
        hint = "DART HTTP 오류"
    elif isinstance(e, DartError):
        return safe_error(e)
    else:
        hint = "DART 처리 오류"
    return f"{hint} [{safe_error(e, 160)}]"


class CorpMap(dict):
    """종목코드→corp_code + 회사명(names) — 캐시 표에 이름까지 남기려고."""
    names: dict


def _status_of(body):
    status = re.search(r"<status>\s*(\w+)\s*</status>|\"status\"\s*:\s*\"(\w+)\"", body)
    message = re.search(r"<message>\s*([^<]+?)\s*</message>|\"message\"\s*:\s*\"([^\"]+)\"", body)
    code = next((g for g in (status.groups() if status else ()) if g), "?")
    text_ = next((g for g in (message.groups() if message else ()) if g), body[:80])
    return code, text_


def get_corp_code_map(api_key):
    """종목코드 → DART corp_code (corpCode.xml 은 zip).
    DART 는 오류(키 오류·일일 한도 초과 020·점검 800)도 HTTP 200 + XML/JSON 상태로 준다 — zip 이 아니면 그 상태를
    읽어 분명한 오류로 올린다(예전: BadZipFile 로 원인이 안 보였다)."""
    r = _get(f"{BASE}/corpCode.xml", {"crtfc_key": api_key})
    if not r.content.startswith(b"PK"):
        code, text_ = _status_of(r.content[:400].decode("utf-8", "replace"))
        raise DartError(f"DART corpCode 응답 오류 status={code}: {text_} — {STATUS_MEANING.get(code, '')}".rstrip(" —"))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    root = _xml(zf.read(zf.namelist()[0]))
    m = CorpMap()
    m.names, m.modified = {}, {}
    for item in root.iter("list"):
        sc = (item.findtext("stock_code") or "").strip()
        cc = (item.findtext("corp_code") or "").strip()
        if sc:
            m[sc] = cc
            m.names[sc] = (item.findtext("corp_name") or "").strip()
            m.modified[sc] = (item.findtext("modify_date") or "").strip()
    return m


def _cached_corps():
    try:
        return db.query("SELECT stock_code, corp_code, fetched_at FROM dart_corp WHERE stock_code IS NOT NULL")
    except Exception:                                   # noqa: BLE001 — 표가 없는 구버전 DB
        return []


def _store_corps(m):
    from sqlalchemy import text
    now = clock.iso_now()
    names, mod = getattr(m, "names", {}) or {}, getattr(m, "modified", {}) or {}
    rows = [{"cc": cc, "nm": names.get(sc) or None, "sc": sc, "md": mod.get(sc) or None, "at": now}
            for sc, cc in m.items() if cc]
    with db.transaction() as conn:
        conn.execute(text("DELETE FROM dart_corp"))
        if rows:
            conn.execute(text("INSERT INTO dart_corp(corp_code, corp_name, stock_code, modify_date, fetched_at) "
                              "VALUES(:cc, :nm, :sc, :md, :at) ON CONFLICT(corp_code) DO NOTHING"), rows)


def corp_codes(api_key, max_age_days=CORP_CACHE_DAYS):
    """종목코드 → corp_code, dart_corp 표에 CORP_CACHE_DAYS 일 캐시.
    새로 받기에 실패하면 오래된 목록이라도 쓴다(장애 완화: DART 가 잠깐 막혀도 재무 수집은 계속)."""
    from datetime import datetime, timedelta
    rows = _cached_corps()
    def kst(x):                                         # timestamptz(datetime) — 예전 문자열(오프셋 유무 섞임)도 허용
        d = x if isinstance(x, datetime) else datetime.fromisoformat(str(x))
        return d.astimezone(clock.KST).replace(tzinfo=None) if d.tzinfo else d
    newest, fresh = "", False
    try:
        stamps = [kst(r["fetched_at"]) for r in rows if r["fetched_at"]]
        if stamps:
            newest = max(stamps).isoformat()
            fresh = max(stamps) >= clock.now() - timedelta(days=max_age_days)
    except ValueError:
        fresh = False
    if rows and fresh:
        return {r["stock_code"]: r["corp_code"] for r in rows}
    try:
        m = get_corp_code_map(api_key)
    except Exception as e:                              # noqa: BLE001
        if rows:
            log.warning("DART 회사 코드 목록 받기 실패 — 저장된 목록(%s) 사용: %s", newest[:10], explain(e))
            return {r["stock_code"]: r["corp_code"] for r in rows}
        raise
    try:
        _store_corps(m)
    except Exception as e:                              # noqa: BLE001 — 캐시 저장 실패는 수집을 막지 않는다
        log.warning("dart_corp 캐시 저장 실패: %s", type(e).__name__)
    return dict(m)


def fetch_financials(api_key, corp_code):
    r = _get(f"{BASE}/fnlttSinglAcnt.json",
             {"crtfc_key": api_key, "corp_code": corp_code,
              "bsns_year": settings.DART_BSNS_YEAR, "reprt_code": settings.DART_REPRT_CODE})
    return r.json()


def _to_int(s):
    if not s or str(s).strip() in ("-", ""):
        return None
    return int(str(s).replace(",", ""))


def _disclosed_at(rows):
    """공시 접수번호(rcept_no) 앞 8자리(YYYYMMDD) → 공시일. 우리가 '알게 된' 시점."""
    rcept = (rows[0].get("rcept_no") if rows else "") or ""
    if len(rcept) >= 8 and rcept[:8].isdigit():
        return f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}"
    return None


def _account_key(acc):
    """계정명 → 내부 키. 공백·괄호 꼬리('영업이익(손실)'·'이익잉여금(결손금)'·'당기순이익(손실)')를 떼고 비교."""
    raw = (acc or "").replace(" ", "")
    base = re.sub(r"\(.*?\)", "", raw)
    if base in ("매출액", "영업수익") or "매출액" in raw:
        return "revenue"
    if base == "영업이익":
        return "operating_profit"
    if base.startswith("당기순이익"):
        return "net_income"
    return {"부채총계": "total_liab", "자본총계": "total_equity", "자산총계": "total_assets",
            "유동자산": "current_assets", "유동부채": "current_liab"}.get(base) or \
        ("retained" if base.startswith("이익잉여금") or base.startswith("결손금") else None)


def parse_financials(data):
    """계정 매칭 + 부채비율 직접 계산 + 공시일 추출. 실패/무데이터는 None.
    한 회사 안에서 연결(CFS)과 별도(OFS)를 섞지 않는다 — 연결재무제표가 있으면 연결만, 없으면 별도만.
    (예전엔 계정마다 'CFS 우선'이라 연결에 없는 계정은 별도 값으로 채워져, 자산·부채가 서로 다른 기준이 될 수 있었다)"""
    if data.get("status") != "000":
        return None
    rows = data.get("list", [])
    basis = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
    picked = {}
    for row in rows:
        if (row.get("fs_div") or "OFS") != basis:
            continue
        acc = row.get("account_nm", "").replace(" ", "")
        amt = _to_int(row.get("thstrm_amount"))
        if amt is None:
            continue
        key = _account_key(acc)
        if key == "retained" and re.sub(r"\(.*?\)", "", acc).startswith("결손금") and amt > 0:
            amt = -amt                                   # '결손금' 단독 계정은 양수로 공시되기도 함 → 음수로
        if key and key not in picked:
            picked[key] = amt

    debt_ratio = None
    if picked.get("total_liab") and picked.get("total_equity"):
        debt_ratio = round(picked["total_liab"] / picked["total_equity"] * 100, 2)
    return {"revenue": picked.get("revenue"), "operating_profit": picked.get("operating_profit"),
            "net_income": picked.get("net_income"), "total_equity": picked.get("total_equity"),
            "debt_ratio": debt_ratio, "disclosed_at": _disclosed_at(rows),
            "total_assets": picked.get("total_assets"), "current_assets": picked.get("current_assets"),
            "current_liabilities": picked.get("current_liab"), "total_liabilities": picked.get("total_liab"),
            "retained_earnings": picked.get("retained"), "fs_basis": basis}


# ── 데모(표본) 재무 ── 오프라인 데모에서도 가치·재무 팩터와 PER/PBR 이 보이게 넣는 '역산 값'(DART 실데이터 아님).
def demo_statement(ticker, year):
    """데모 재무 1건(결정적) — detail.FUND(데모 PER·PBR)와 카탈로그 기준가·주식수에서 역산. 해당 없으면 None."""
    import random
    import zlib
    from app.services import detail
    f, s = detail.FUND.get(ticker), catalog.STOCKS.get(ticker)
    if not f or not s:
        return None
    per, pbr, _div = f
    _n, _m, _s, base, shares = s
    rng = random.Random(zlib.crc32(("fin" + ticker).encode()))
    mcap = base * shares
    ni = int(mcap / per) if per else None
    eq = int(mcap / pbr) if pbr else None
    opm = rng.uniform(0.05, 0.25)
    rev = int(ni / (opm * 0.75)) if ni else None
    op = int(rev * opm) if rev else None
    debt = round(rng.uniform(25, 160), 2)
    tl = int(eq * debt / 100) if eq else None                  # 부채총계 = 자본 × 부채비율
    ta = (eq or 0) + (tl or 0)
    ca = int(ta * rng.uniform(0.30, 0.55))
    cl = int((tl or 0) * rng.uniform(0.45, 0.75))
    re_ = int((eq or 0) * rng.uniform(0.55, 0.90))
    return {"ticker": ticker, "fiscal_year": year, "revenue": rev, "operating_profit": op, "net_income": ni,
            "total_equity": eq, "debt_ratio": debt, "disclosed_at": f"{year + 1}-03-15",
            "total_assets": ta, "current_assets": ca, "current_liabilities": cl, "total_liabilities": tl,
            "retained_earnings": re_}


def purge_demo_statements(conn=None):
    """실데이터로 바뀌면 데모 재무를 지운다 — demo_statement() 와 순이익·자본총계가 정확히 같은 행만(실제 DART 행 보존).
    남겨 두면 DART 가 못 채운 종목(키 없음·수집 실패·유니버스 밖)에서 표본 재무로 PER·PBR·Z''·Merton PD 가
    실데이터처럼 계산된다. 반환: 지운 행 수."""
    from sqlalchemy import text
    from app.services import detail
    removed = 0

    def run(c):
        nonlocal removed
        for t in detail.FUND:
            d = demo_statement(t, 0)                          # 순이익·자본은 연도와 무관(기준가·주식수·데모 PER/PBR)
            if not d or d["net_income"] is None or d["total_equity"] is None:
                continue
            removed += c.execute(text("DELETE FROM financial_statement WHERE ticker=:t AND net_income=:ni "
                                      "AND total_equity=:eq"),
                                 {"t": t, "ni": d["net_income"], "eq": d["total_equity"]}).rowcount or 0
    if conn is not None:
        run(conn)
    else:
        with db.transaction() as c:
            run(c)
    if removed:
        log.info("실데이터 모드: 데모(표본) 재무 %d건 제거", removed)
    return removed


def collect_financials(tickers=None):
    if not settings.DART_API_KEY:
        log.warning("DART_API_KEY 없음 — 재무 수집 건너뜀 (https://opendart.fss.or.kr 발급)")
        return {"ok": 0, "skipped": 0}

    tickers = tickers or catalog.universe()             # KRX 로 적재된 유니버스(top-N)
    try:
        corp_map = corp_codes(settings.DART_API_KEY)
    except Exception as e:                              # noqa: BLE001 — 재무 단계만 실패로 남기고 배치는 계속
        err = explain(e)
        log.error("DART corp_code 목록 실패 — 재무 수집 건너뜀: %s", err)
        return {"ok": 0, "skipped": len(tickers), "error": err}
    ok = skipped = streak = 0
    stopped = fatal = None

    for ticker in tickers:
        cc = corp_map.get(ticker)
        if not cc:
            log.warning("corp_code 없음: %s", ticker); skipped += 1; continue
        try:
            data = fetch_financials(settings.DART_API_KEY, cc)
            code = str((data or {}).get("status") or "")
            if code in FATAL_STATUS:
                # 키·IP·한도·점검 오류는 다음 종목도 똑같다 — 150번 헛걸음하지 않고 바로 멈춘다
                fatal = f"DART {status_text(code)} — 이번 회차 중단"
                log.error("%s", fatal)
                skipped += 1
                break
            fin = parse_financials(data)
            streak = 0
        except requests.RequestException as e:
            # 네트워크·서버 장애가 이어지면 그 회차는 멈춘다 — 예전엔 종목마다 30초×3번을 끝까지(150종목이면 수 시간)
            log.error("재무 실패 %s: %s", ticker, explain(e)); skipped += 1; streak += 1
            if streak >= DART_MAX_CONSECUTIVE_FAILS:
                stopped = f"연속 {streak}번 연결 실패 — 이번 회차 중단(다음 실행 때 다시): {explain(e)}"
                log.error("DART %s", stopped)
                break
            continue
        except Exception as e:
            log.error("재무 실패 %s: %s", ticker, e); skipped += 1; continue
        if not fin:
            log.warning("재무 데이터 없음: %s", ticker); skipped += 1; continue

        db.execute("""
            INSERT INTO financial_statement
                (ticker, fiscal_year, report_code, revenue, operating_profit, net_income,
                 total_equity, debt_ratio, disclosed_at, ingested_at,
                 total_assets, current_assets, current_liabilities, total_liabilities, retained_earnings, fs_basis)
            VALUES(:t, :y, :rc, :rev, :op, :net, :eq, :debt, :disc, :ing, :ta, :ca, :cl, :tl, :re, :basis)
            ON CONFLICT(ticker, fiscal_year, report_code) DO UPDATE SET
              revenue=excluded.revenue, operating_profit=excluded.operating_profit,
              net_income=excluded.net_income, total_equity=excluded.total_equity,
              debt_ratio=excluded.debt_ratio,
              disclosed_at=excluded.disclosed_at, ingested_at=excluded.ingested_at,
              total_assets=excluded.total_assets, current_assets=excluded.current_assets,
              current_liabilities=excluded.current_liabilities, total_liabilities=excluded.total_liabilities,
              retained_earnings=excluded.retained_earnings, fs_basis=excluded.fs_basis
        """, {"t": ticker, "y": int(settings.DART_BSNS_YEAR), "rc": settings.DART_REPRT_CODE,
              "rev": fin["revenue"], "op": fin["operating_profit"],
              "net": fin["net_income"], "eq": fin.get("total_equity"), "debt": fin["debt_ratio"],
              "disc": fin.get("disclosed_at"),
              "ing": clock.iso_now(),
              "ta": fin.get("total_assets"), "ca": fin.get("current_assets"),
              "cl": fin.get("current_liabilities"), "tl": fin.get("total_liabilities"),
              "re": fin.get("retained_earnings"), "basis": fin.get("fs_basis")})
        ok += 1

    log.info("재무 수집 완료: %d종목 / 스킵 %d", ok, skipped)
    out = {"ok": ok, "skipped": skipped}
    if stopped:
        out["stopped"] = stopped
    if fatal:
        out["error"] = fatal                            # run.py 가 'DART 재무' 단계 실패로 기록·알림
    return out
