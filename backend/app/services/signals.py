"""기술적 신호 라이브러리 + 오늘의 추천 종목 Top 5 API 진입점.

  · evaluate()/evaluate_at() — 기술적 신호(교육용). 추천 모델(reco_engine)의 '기술적 신호' 팩터로 쓰인다.
      골든크로스 발생  3  최근 3거래일 안에 SMA5 가 SMA20 을 위로 돌파(지금도 위)
      골든크로스 유력  2  SMA5 가 SMA20 아래 1% 이내로 좁혀지며 상승 중(곧 돌파 가능)
      RSI 과매도 반등  3  최근 5거래일 안에 RSI14 < 30, 오늘 30 이상으로 올라오며 상승
                      2  아직 30 아래지만 이틀 연속 상승(반등 초입)
      거래량 돌파 신호  3  거래량 ≥ 20일 평균의 2배 + 종가가 직전 20일 최고가 돌파
      박스권 하단 지지  2  60일 고저폭 30% 이내 박스권에서 종가가 하단 20% 구간 + 양봉
      주요 이평선 지지  2  상승 중인 SMA20/60 에 저가가 닿고(1.5% 이내) 종가는 그 위에서 마감
      상승 추세 유지    1  종가 > SMA20 > SMA60, SMA20 상승
  · today()/review() — 멀티팩터 모델(reco_engine: 모멘텀·기술적 신호·가치·재무 건전성·뉴스 심리·수급·안정성)로
    고른 Top N 과 '한 달 전 추천' 사후 검증. 기준일(as_of) 이후 데이터는 절대 쓰지 않는다(미래참조 방지).
    '한 달 전 추천'은 그날 실제로 보여준 스냅샷(reco_snapshot)이 있으면 그것을, 없으면 그날 기준으로 다시 계산.
  · rank() — (하위호환) 신호 강도 합만으로 고르던 예전 방식.
본 기능은 교육용 예시이며 투자 권유가 아니다.
"""
from datetime import date, timedelta

from app import db
from app.services import catalog, quotes
from app.services import indicators as ind

NOTE = "기술적 신호 기반 교육용 예시 · 투자 권유 아님"
_TTL = 300
_SAVED = set()                 # 이 프로세스에서 이미 스냅샷을 남긴 (기준일, 성향)


def _cached(key, fn):
    """추천 엔진과 같은 캐시(stale-while-revalidate + single-flight) — 키 공간만 나눔."""
    from app.services import reco_engine
    return reco_engine._cached(("signals",) + tuple(key), fn, ttl=_TTL)


def clear_cache():
    _SAVED.clear()
    try:
        from app.services import reco_engine
        reco_engine.clear_cache()
    except Exception:                    # noqa: BLE001
        pass


def evaluate(bars):
    """시간순 일봉 → [(신호, 강도)] (강한 순). 마지막 봉 = 기준일. 계산 불가(봉 부족)면 []."""
    n = len(bars)
    if n < 25:
        return []
    c = [float(b["close"]) for b in bars]

    def _or_close(b, k):                         # 없거나 0 이하(거래정지일 KRX 0 값)면 종가로
        v = b.get(k)
        return float(v) if v is not None and v > 0 else float(b["close"])
    h = [_or_close(b, "high") for b in bars]
    lo = [_or_close(b, "low") for b in bars]
    o = [_or_close(b, "open") for b in bars]
    v = [float(b["volume"] or 0) for b in bars]
    return evaluate_at(c, h, lo, o, v, ind.sma(c, 5), ind.sma(c, 20), ind.sma(c, 60), ind.rsi(c, 14), n - 1)


def evaluate_at(c, h, lo, o, v, s5, s20, s60, r, i):
    """미리 계산한 배열(종가·고가·저가·시가·거래량·SMA5/20/60·RSI14, 없는 값은 None)에서 i 번째 봉 기준 신호.
    추천 모델(reco_engine)이 종목당 지표를 한 번만 계산해 여러 기준일(백테스트)에 재사용하려고 분리했다.
    i 이후 값은 읽지 않는다(미래참조 없음)."""
    if i < 24:
        return []
    sig = []
    # 골든크로스
    if s5[i] is not None and s20[i] is not None and s5[i - 1] is not None and s20[i - 1] is not None:
        crossed = any(s5[j - 1] is not None and s20[j - 1] is not None and s5[j] is not None and s20[j] is not None
                      and s5[j - 1] <= s20[j - 1] and s5[j] > s20[j] for j in range(max(1, i - 2), i + 1))
        gap, gap_prev = s20[i] - s5[i], s20[i - 1] - s5[i - 1]
        if crossed and s5[i] > s20[i]:
            sig.append(("골든크로스 발생", 3))
        elif 0 < gap <= s20[i] * 0.01 and s5[i] > s5[i - 1] and gap < gap_prev:
            sig.append(("골든크로스 유력", 2))
    # RSI 과매도 반등
    if r[i] is not None and r[i - 1] is not None:
        recent = [x for x in r[max(0, i - 5):i] if x is not None]
        if recent and min(recent) < 30 <= r[i] and r[i] > r[i - 1]:
            sig.append(("RSI 과매도 반등", 3))
        elif r[i] < 30 and r[i - 2] is not None and r[i] > r[i - 1] > r[i - 2]:
            sig.append(("RSI 과매도 반등", 2))
    # 거래량 돌파
    if i >= 20:
        avg_v = sum(v[i - 20:i]) / 20
        if avg_v > 0 and v[i] >= 2 * avg_v and c[i] > max(h[i - 20:i]):
            sig.append(("거래량 돌파 신호", 3))
    # 박스권 하단 지지
    if i >= 59:
        hi, low = max(h[i - 59:i + 1]), min(lo[i - 59:i + 1])
        if low > 0 and hi > low and (hi - low) / low <= 0.30 and (c[i] - low) / (hi - low) <= 0.2 and c[i] >= o[i]:
            sig.append(("박스권 하단 지지", 2))
    # 주요 이평선 지지
    for s in (s20, s60):
        if i >= 5 and s[i] is not None and s[i - 5] is not None and s[i] > s[i - 5] \
                and lo[i] <= s[i] * 1.015 and c[i] >= s[i]:
            sig.append(("주요 이평선 지지", 2))
            break
    # 상승 추세 유지
    if s60[i] is not None and s20[i] is not None and s20[i - 5] is not None \
            and c[i] > s20[i] > s60[i] and s20[i] > s20[i - 5]:
        sig.append(("상승 추세 유지", 1))
    sig.sort(key=lambda x: -x[1])
    return sig


def _distinct_tag(sig, used, freq):
    """아직 안 쓴 대표 신호 고르기 — 가장 강한 신호들 중 후보 전체에서 드문 것 우선, 없으면 남은 것 중 가장 강한 것."""
    top_w = sig[0][1]
    free_top = [t for t, w in sig if w == top_w and t not in used]
    if free_top:
        return min(free_top, key=lambda t: (freq[t], t))
    return next((t for t, _ in sig if t not in used), None)


STALE_DAYS = 7                 # 기준일보다 이만큼(달력일) 넘게 오래된 마지막 봉 = 거래 안 되는 종목(상폐·정지)
GAP_DAYS = 20                  # 봉 사이가 이만큼 벌어지면 다른 구간(예: 실습 시나리오의 몇 년 전 봉)


def _usable(bars, as_of):
    """한 종목의 봉(시간순) 중 신호 계산에 쓸 부분 — 마지막 봉이 기준일 근처(STALE_DAYS 이내)여야 하고,
    뒤에서부터 끊김 없이 이어진 구간만(quotes.recent_segment). 추천 엔진(reco_engine.score_universe)의
    '최근 시세 없음'·연속 구간 규칙과 같은 취지 — 몇 달·몇 년 전 봉(상폐 종목·옛 실습 구간)으로 신호를 내지 않게."""
    if not bars:
        return []
    try:
        last = date.fromisoformat(str(bars[-1]["trade_date"])[:10])
        if as_of and (date.fromisoformat(str(as_of)[:10]) - last).days > STALE_DAYS:
            return []
    except (TypeError, ValueError):
        return list(bars)
    return quotes.recent_segment(list(bars), GAP_DAYS)


def rank(bars_by_ticker, limit=5, as_of=None):
    """종목별 신호 → 점수순 Top N(대표 신호가 겹치지 않게 먼저 채우고, 모자라면 점수순으로 보충).
    as_of(없으면 입력 중 가장 늦은 봉 날짜) 근처에 거래가 없는 종목·끊긴 앞 구간은 빼고 계산한다."""
    from collections import Counter
    ref = as_of or max((str(v[-1]["trade_date"]) for v in bars_by_ticker.values() if v), default=None)
    cands = []
    for t, bars in bars_by_ticker.items():
        bars = _usable(bars, ref)
        if not bars:
            continue
        sig = evaluate(bars)
        if not sig:
            continue
        last = bars[-1]
        cands.append({"ticker": t, "tag": sig[0][0], "tags": [s for s, _ in sig], "_sig": sig,
                      "score": sum(w for _, w in sig), "value": float(last["close"]) * float(last["volume"] or 0),
                      "as_of": last["trade_date"], "close": float(last["close"])})
    cands.sort(key=lambda x: (-x["score"], -x["value"], x["ticker"]))
    freq = Counter(t for cd in cands for t in cd["tags"])
    picked, used = [], set()
    for cd in cands:
        if len(picked) >= limit:
            break
        tag = _distinct_tag(cd["_sig"], used, freq)
        if tag:
            cd["tag"] = tag
            picked.append(cd)
            used.add(tag)
    for cd in cands:
        if len(picked) >= limit:
            break
        if not any(cd is p for p in picked):
            picked.append(cd)
    picked.sort(key=lambda x: (-x["score"], -x["value"], x["ticker"]))
    for cd in picked:
        cd.pop("_sig", None)
    return picked


def _latest_date(universe):
    r = db.query("SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY(:ts)", {"ts": list(universe)})
    return r[0]["d"] if r else None


def today(limit=5, profile=None):
    """오늘의 추천 종목 Top N — 멀티팩터 모델 점수 + 지금 가격(장중 LIVE / 장 마감 후 당일 종가).
    profile(안정형·중립형·공격형)을 주면 적합성 필터(성향 대비 변동성)를 적용한다.
    처음 계산된 결과는 reco_snapshot 에 남긴다(같은 날·같은 성향은 한 번만, 이후 불변)."""
    from app.services import reco_engine as R
    limit = max(1, min(int(limit or 5), 10))
    prof = profile if profile in R.PROFILES else None
    universe = catalog.universe()
    latest = _latest_date(universe)
    if not latest:
        return {"as_of": None, "items": [], "note": R.NOTE, "source": quotes.market_source(), "model": None}
    # 장중(또는 개장 전)에는 KIS 가 오늘 '진행 중 봉'을 넣어 둘 수 있다 → 순위는 마지막 '확정' 일봉 기준으로
    # (가격·등락률은 아래에서 실시간으로 붙인다). 장중에 순위가 수시로 바뀌며 깜빡이는 것도 막는다.
    phase = quotes.kr_phase()
    if latest >= quotes.kr_session_date() and phase in ("open", "pre"):
        prev = db.query("SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY(:ts) AND trade_date < :t",
                        {"ts": list(universe), "t": quotes.kr_session_date()})
        latest = (prev[0]["d"] if prev and prev[0]["d"] else latest)
    res = R._cached(("today", latest, len(universe), limit, prof),
                    lambda: R.recommend(limit, prof, as_of=latest))
    # 감사 추적 스냅샷 — 확정된 일봉 기준일 때만(장중 미확정 데이터로 '공개 기록'을 남기지 않음).
    # 실데이터는 '확정 종가 기록'으로 판정 — 예전엔 15:35(장 마감 판정) 직후 ~ 15:40(KIS 종가 갱신) 사이 조회가
    # 종가 단일가 전 가격(15:25 무렵 장중 갱신 봉)으로 스냅샷을 남겨, 한 달 뒤 사후 검증이 틀린 매수가로 계산됐다.
    final = quotes.close_is_final(latest)
    key = (latest, prof or "all", limit)
    if final and res["items"] and key not in _SAVED and limit >= 5:
        R.save_snapshot(res, prof)
        _SAVED.add(key)
    qmap = quotes.kr_quotes([it["ticker"] for it in res["items"]])
    items = []
    for it in res["items"]:
        q = qmap.get(it["ticker"]) or {}
        items.append({**it, "price": q.get("price", it["entry_price"]), "change_rate": q.get("change_rate"),
                      "status": q.get("status")})
    return {"as_of": latest, "items": items, "note": R.NOTE, "source": quotes.market_source(),
            "profile": prof, "model": res["model"]}


def _closes_at(tickers, as_of):
    """각 종목의 as_of 이하 마지막 종가."""
    if not tickers:
        return {}
    return {r["ticker"]: float(r["close"]) for r in db.latest_per_ticker(
        "ohlc", "trade_date, close", list(tickers), where="trade_date <= :a AND close > 0", params={"a": as_of})}


def review(days=30, limit=5, today_date=None):
    """한 달 전 추천 결과 — (오늘-days) 이전 마지막 거래일의 Top N 을 그날 종가에 샀다면 지금 몇 %인지.
    · 그날 실제로 보여준 스냅샷이 있으면 그대로(사후에 바꿀 수 없음), 없으면 그날까지의 데이터로 다시 계산.
    · 1위 종목·Top N 평균과 함께 같은 기간 '유니버스 동일가중 평균'(벤치마크)·초과수익도 준다(좋은 것만 고르지 않음)."""
    from app.services import reco_engine as R
    days = max(7, min(int(days or 30), 365))
    limit = max(1, min(int(limit or 5), 10))
    universe = catalog.universe()
    base = today_date or quotes.now_kst().date()
    target = (base - timedelta(days=days)).isoformat() if isinstance(base, date) else base
    r = db.query("SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY(:ts) AND trade_date <= :t",
                 {"ts": list(universe), "t": target})
    as_of = r[0]["d"] if r else None
    if not as_of:
        return {"available": False, "days": days, "note": R.NOTE}
    snap = R.snapshot(as_of)
    version = None
    if snap:
        version = snap[0]["model_version"]
        picks = [{"ticker": x["ticker"], "name": x["name"], "tag": x["tag"], "entry_price": x["entry_price"],
                  "entry_date": as_of} for x in snap if x["model_version"] == version][:limit]
    else:
        res = R._cached(("review", as_of, len(universe), limit), lambda: R.recommend(limit, None, as_of=as_of))
        version = (res.get("model") or {}).get("version")
        picks = [{"ticker": it["ticker"], "name": it["name"], "tag": it["tag"], "entry_price": it["entry_price"],
                  "entry_date": it["entry_date"]} for it in res["items"]]
    if not picks:
        return {"available": False, "days": days, "as_of": as_of, "note": R.NOTE}
    qmap = quotes.kr_quotes(universe)
    # 권리 이벤트(액면분할·무상증자) 계수 — 그날 가격 × F = 지금 기준 가격(분할이 끼면 −80% 로 보이던 것)
    from app.services import adjust
    F = adjust.factors_since(universe, as_of)
    items = []
    for i, p in enumerate(picks, 1):
        q = qmap.get(p["ticker"])
        if not q or not p["entry_price"]:
            continue
        base = p["entry_price"] * F.get(p["ticker"], 1.0)
        ret = (q["price"] - base) / base * 100
        items.append({"rank": i, "ticker": p["ticker"], "name": p["name"] or catalog.meta(p["ticker"])["name"],
                      "tag": p["tag"], "entry_date": p["entry_date"], "entry_price": p["entry_price"],
                      "price": q["price"], "price_date": q.get("session_date"), "return_pct": round(ret, 2),
                      **({"entry_price_adj": round(base, 2)} if abs(base - p["entry_price"]) > 1e-9 else {})})
    if not items:
        return {"available": False, "days": days, "as_of": as_of, "note": R.NOTE}
    avg = sum(x["return_pct"] for x in items) / len(items)
    entry = _closes_at(universe, as_of)
    rets = [(qmap[t]["price"] / (c * F.get(t, 1.0)) - 1) * 100 for t, c in entry.items() if t in qmap and c]
    bench = sum(rets) / len(rets) if rets else None
    return {"available": True, "days": days, "as_of": as_of, "items": items, "top": items[0],
            "avg_return": round(avg, 2), "wins": sum(1 for x in items if x["return_pct"] > 0),
            "count": len(items), "bench_return": round(bench, 2) if bench is not None else None,
            "excess_return": round(avg - bench, 2) if bench is not None else None,
            "bench_count": len(rets), "from_snapshot": bool(snap), "model_version": version,
            "note": R.NOTE, "source": quotes.market_source()}
