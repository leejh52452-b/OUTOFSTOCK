"""KRX 정보데이터시스템 OpenAPI 클라이언트 — 일별 시세/지수 실데이터.

  · base   : https://data-dbg.krx.co.kr/svc/apis/   (KRX_API_BASE 로 변경 가능)
  · auth   : 헤더 AUTH_KEY = <발급키>   (settings.KRX_API_KEY, .env)
             ※ 키 발급과 별개로 '쓰려는 API 마다' openapi.krx.co.kr 서비스 목록에서 이용신청·승인이 필요하다.
               주식(sto/*)만 신청하고 지수(idx/*)를 안 하면 지수 호출만 401 → KrxAuthError 로 구분해 올린다.
  · param  : basDd=YYYYMMDD  → 그 '거래일 하루'의 전 종목/지수 반환(비거래일은 빈 배열)
  · resp   : {"OutBlock_1": [ {...}, ... ]}
  · 한도   : 1일 10,000콜, '10년~ 데이터

시계열은 basDd 를 하루씩 돌며 모은다(전 종목이 한 번에 오므로 유니버스 채우기엔 효율적).
샌드박스(egress 차단)에선 호출이 막히니, 호출부에서 예외 시 생성 데이터로 폴백한다.
"""
import logging
import os
import time

import requests

from app.config import settings

log = logging.getLogger(__name__)

BASE = (os.getenv("KRX_API_BASE") or "https://data-dbg.krx.co.kr/svc/apis/").rstrip("/") + "/"
SVC_STK = "sto/stk_bydd_trd"      # 유가증권(코스피) 일별매매정보
SVC_KSQ = "sto/ksq_bydd_trd"      # 코스닥 일별매매정보
SVC_IDX_KOSPI = "idx/kospi_dd_trd"
SVC_IDX_KOSDAQ = "idx/kosdaq_dd_trd"


class KrxAuthError(RuntimeError):
    """401/403 — 키는 맞아도 그 API 서비스 이용신청(승인)이 안 된 경우가 대부분."""


def enabled():
    return bool(settings.KRX_API_KEY)


TRANSIENT_RETRIES = 2                          # 일시 오류(연결 끊김·시간 초과·5xx·429) 재시도 횟수


def _get(service, bas_dd):
    """한 서비스·한 거래일 조회 — 연결 끊김·시간 초과·5xx·429 는 1초·2초 쉬고 최대 2번 더 시도
    (예전엔 한 번 실패한 날을 '빈 날'로 보고 그날 봉을 지워 버렸다). HTTPS 만 — 인증서 오류여도 평문으로 바꾸지 않는다."""
    bas_dd = str(bas_dd).replace("-", "")      # KRX 는 YYYYMMDD(하이픈 없이)
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            r = requests.get(BASE + service, params={"basDd": bas_dd}, headers={"AUTH_KEY": settings.KRX_API_KEY},
                             timeout=15)
        except requests.exceptions.SSLError as e:
            # 예전엔 인증서 오류면 http(평문)로 다시 보냈다 → AUTH_KEY 헤더가 암호화 없이 네트워크에 노출.
            # 키를 평문으로 보내느니 실패시키고 원인을 알린다.
            raise RuntimeError("KRX HTTPS 인증서 확인 실패 — 키를 평문(http)으로 보내지 않으려고 중단했습니다. "
                               "서버의 인증서 저장소를 갱신하세요(우분투: sudo apt install -y ca-certificates, "
                               f"파이썬: pip install -U certifi). 원인: {str(e)[:120]}") from e
        except (requests.ConnectionError, requests.Timeout):
            if attempt == TRANSIENT_RETRIES:
                raise
            time.sleep(1.0 * (attempt + 1))
            continue
        if (r.status_code >= 500 or r.status_code == 429) and attempt < TRANSIENT_RETRIES:
            time.sleep(1.0 * (attempt + 1))
            continue
        break
    if r.status_code in (401, 403):
        raise KrxAuthError(f"KRX {service} {r.status_code} — openapi.krx.co.kr 에서 이 API 의 이용신청(승인) 여부를 확인하세요")
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or "OutBlock_1" not in j:
        # 비거래일은 {"OutBlock_1": []} 로 온다. 키 자체가 없으면 오류 응답(한도 초과 등)이다 — '휴장일'로 보면
        # 호출부가 그날의 기존 봉을 지운다.
        raise RuntimeError(f"KRX {service} 응답 형식 오류: {str(j)[:120]}")
    return j.get("OutBlock_1") or []


def _num(v):
    """'71,000' / '' / '-' → float."""
    s = str(v or "").replace(",", "").strip()
    if s in ("", "-", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _short(isu):
    """ISU_CD 가 단축코드(6)면 그대로, 표준코드(ISIN 12: KR7005930003)면 6자리 추출."""
    s = str(isu or "").strip()
    return s if len(s) == 6 else (s[3:9] if len(s) >= 9 else s)


MKT_SVC = {"K": SVC_STK, "Q": SVC_KSQ}
CACHE_MIN_AGE_DAYS = 3                          # 이보다 오래된 거래일은 KRX 가 더 고치지 않는다(확정) — DB 사본을 쓴다


def daily_raw(bas_dd, mk):
    """분석 계층 적재용 — 한 거래일·한 시장(K 코스피 | Q 코스닥)의 전 종목 원자료(가공 최소화).
    daily_stocks 와 달리 시가·고가·저가 0 을 채우지 않고 halted(매매거래정지 추정)로 표시한다.
    반환 [{ticker, name, market, sect_tp, open, high, low, close, volume, value, mktcap, shares, change_rate, halted}]."""
    out = []
    for row in _get(MKT_SVC[mk], bas_dd):
        code = _short(row.get("ISU_CD"))
        close = _num(row.get("TDD_CLSPRC"))
        if not code or close <= 0:
            continue
        o, h, lo = _num(row.get("TDD_OPNPRC")), _num(row.get("TDD_HGPRC")), _num(row.get("TDD_LWPRC"))
        vol = int(_num(row.get("ACC_TRDVOL")))
        fr = str(row.get("FLUC_RT") or "").strip()
        out.append({
            "ticker": code, "name": (row.get("ISU_NM") or "").strip() or code,
            "market": "KOSPI" if mk == "K" else "KOSDAQ",
            "sect_tp": (row.get("SECT_TP_NM") or "").strip() or None,
            "open": int(o) or None, "high": int(h) or None, "low": int(lo) or None, "close": int(close),
            "volume": vol, "value": int(_num(row.get("ACC_TRDVAL"))),
            "mktcap": int(_num(row.get("MKTCAP"))) or None, "shares": int(_num(row.get("LIST_SHRS"))) or None,
            "change_rate": _num(fr) if fr not in ("", "-") else None,
            "halted": o <= 0 and h <= 0 and lo <= 0 and vol == 0,
        })
    return out


def _cached_day(bas_dd, svcs):
    """분석 계층(analytics.price_history)에 그날 전 종목이 이미 적재돼 있으면 KRX 를 다시 부르지 않는다.
    유니버스 적재는 매일 180거래일 × 2시장 = 360번 호출했다 → 적재된 날은 DB 에서 읽어 하루 몇 번으로.
    확정된 날(CACHE_MIN_AGE_DAYS 이전)만, 요청한 시장이 모두 'ok' 또는 'empty'(휴장)일 때만. 아니면 None."""
    from datetime import date, timedelta
    from app import clock, db
    d = date.fromisoformat(str(bas_dd)[:10]) if "-" in str(bas_dd) else \
        date(int(str(bas_dd)[:4]), int(str(bas_dd)[4:6]), int(str(bas_dd)[6:8]))
    if d > clock.now().date() - timedelta(days=CACHE_MIN_AGE_DAYS):
        return None
    inv = {v: k for k, v in MKT_SVC.items()}
    keys = [f"{d.isoformat()}:{inv[s]}" for s in svcs if s in inv]
    try:
        st = {r["part_key"]: r["status"] for r in db.query(
            "SELECT part_key, status FROM analytics.load_log WHERE dataset = 'krx_daily' AND part_key = ANY(:k)",
            {"k": keys})}
        if len(st) < len(keys) or any(v not in ("ok", "empty") for v in st.values()):
            return None
        mks = [k.split(":")[1] for k, v in st.items() if v == "ok"]
        rows = db.query(
            "SELECT p.ticker, m.name, m.market, p.open, p.high, p.low, p.close, p.volume, p.value, p.mktcap, "
            "p.change_rate, m.listed_shares FROM analytics.price_history p "
            "JOIN analytics.security_master m ON m.ticker = p.ticker "
            "WHERE p.trade_date = :d AND p.mkt = ANY(:mk)", {"d": d, "mk": mks}) if mks else []
    except Exception:                               # noqa: BLE001 — 분석 계층이 없거나 DB 문제면 API 로
        return None
    out = {}
    for r in rows:
        c = float(r["close"])
        o, h, lo = (float(r[k] or 0) for k in ("open", "high", "low"))
        out[r["ticker"]] = {"name": r["name"], "market": r["market"],
                            "open": o if o > 0 else c, "high": max(h, c) if h > 0 else c,
                            "low": min(lo, c) if lo > 0 else c, "close": c,
                            "volume": int(r["volume"] or 0), "value": int(r["value"] or 0),
                            "mktcap": int(r["mktcap"] or 0), "shares": int(r["listed_shares"] or 0),
                            "change_rate": r["change_rate"]}
    return out


def daily_stocks(bas_dd, markets=None):
    """한 거래일의 전 종목 OHLCV → {단축코드: {...}}. 비거래일이면 빈 dict.
    markets=("KOSPI",)·("STK",) 처럼 주면 그 시장만 호출(실습 구간처럼 종목 시장을 알 때 호출 수 절반).
    거래정지일처럼 시가·고가·저가가 0 으로 오면 종가로 채운다(52주 최저 0원·꼬리 0 방지). 종가 0 인 행은 버림.
    분석 계층에 그날이 이미 적재돼 있으면(확정일) KRX 를 부르지 않고 DB 에서 같은 모양으로 만든다."""
    out = {}
    want = {"KOSPI": SVC_STK, "STK": SVC_STK, "KOSDAQ": SVC_KSQ, "KSQ": SVC_KSQ}
    svcs = list(dict.fromkeys(want[m] for m in markets if m in want)) if markets else [SVC_STK, SVC_KSQ]
    cached = _cached_day(bas_dd, svcs or [SVC_STK, SVC_KSQ])
    if cached is not None:
        return cached
    for svc in svcs or [SVC_STK, SVC_KSQ]:
        for row in _get(svc, bas_dd):
            code = _short(row.get("ISU_CD"))
            if not code:
                continue
            close = _num(row.get("TDD_CLSPRC"))
            if close <= 0:
                continue
            o, h, lo = _num(row.get("TDD_OPNPRC")), _num(row.get("TDD_HGPRC")), _num(row.get("TDD_LWPRC"))
            out[code] = {
                "name": (row.get("ISU_NM") or "").strip(),
                "market": (row.get("MKT_NM") or "").strip(),
                "open": o if o > 0 else close, "high": max(h, close) if h > 0 else close,
                "low": min(lo, close) if lo > 0 else close, "close": close,
                "volume": int(_num(row.get("ACC_TRDVOL"))),
                "value": int(_num(row.get("ACC_TRDVAL"))),
                "mktcap": int(_num(row.get("MKTCAP"))),
                "shares": int(_num(row.get("LIST_SHRS"))),
                # KRX 등락률(기준가 대비 — 액면분할·권리락이 반영된 값). 비어 있으면 None(직접 계산)
                "change_rate": _num(row.get("FLUC_RT"))
                if str(row.get("FLUC_RT") or "").strip() not in ("", "-") else None,
            }
    return out


# 홈 지수: 대표 종합지수만 추린다(IDX_NM 매칭). 이름 표기가 바뀌어도(공백·'지수' 접미·영문) 잡히게 정규화.
_IDX_PICK = {SVC_IDX_KOSPI: ("KOSPI", "코스피", ("코스피", "코스피지수", "kospi", "kospi지수")),
             SVC_IDX_KOSDAQ: ("KOSDAQ", "코스닥", ("코스닥", "코스닥지수", "kosdaq", "kosdaq지수"))}


def _norm_idx(nm):
    return "".join(str(nm or "").split()).lower()


def index_day(bas_dd):
    """한 거래일의 대표 지수(코스피/코스닥) → [{code,name,value,change,change_rate,open,high,low}]."""
    out = []
    for svc, (code, name, wanted) in _IDX_PICK.items():
        rows = _get(svc, bas_dd)
        pick = next((r for r in rows if _norm_idx(r.get("IDX_NM")) in wanted), None)
        if pick is None:                         # 예비: 숫자 없는 가장 짧은 이름(예: '코스피 200' 제외)
            cands = [r for r in rows if _norm_idx(r.get("IDX_NM")).startswith(name)
                     and not any(ch.isdigit() for ch in str(r.get("IDX_NM") or ""))]
            pick = min(cands, key=lambda r: len(str(r.get("IDX_NM") or "")), default=None)
        if pick:
            out.append({"code": code, "name": name,
                        "value": _num(pick.get("CLSPRC_IDX")),
                        "change": _num(pick.get("CMPPREVDD_IDX")),
                        "change_rate": _num(pick.get("FLUC_RT")),
                        "open": _num(pick.get("OPNPRC_IDX")) or None,
                        "high": _num(pick.get("HGPRC_IDX")) or None,
                        "low": _num(pick.get("LWPRC_IDX")) or None})
    return out
