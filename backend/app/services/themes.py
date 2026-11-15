"""뉴스 감성(호재/악재) — 배치 분류(classify_news) + 종목 감성 집계(오늘의 AI 뉴스 픽·종목 관련 뉴스).

배치(pipeline)에서 classify_news() 로 결과를 news_sentiment 에 남기고, API 는 저장된 결과만 읽는다
(웹서버엔 무거운 모델 불필요). 테마 랭킹(theme_reco)은 팀 결정으로 제거(2026-10).
"""
import logging
from datetime import datetime

from sqlalchemy import text

from app import clock
from app import db
from app.services.sentiment import get_classifier

log = logging.getLogger(__name__)
# 발행 시각 식(시각 없으면 그 날짜 00:00 한국시간) — db.NEWS_TS 한 곳에서 정의, 인덱스 ix_news_ts 를 탄다
_TS = db.NEWS_TS.format(p="n.")

# 종목 → 테마 (기존 CURATED 및 대표 종목을 포괄)

def _now():
    return clock.iso_now()


# ── 배치 단계 ─────────────────────────────────────────────
def classify_news(prefer_model=False):
    """뉴스 제목을 호재/악재로 분류해 news_sentiment 에 저장(멱등).
    대상: 아직 분류 안 된 기사 + (사전을 쓸 때) 예전 사전 버전으로 매긴 기사 — 사전을 고치면 다음 배치에서
    전부 새 규칙으로 다시 매긴다. 모델(KR-FinBert) 결과는 건드리지 않는다."""
    clf = get_classifier(prefer_model)
    relabel = clf.name.startswith("lexicon")
    rows = db.query("SELECT n.news_id, n.title FROM news n LEFT JOIN news_sentiment s ON s.news_id = n.news_id "
                    "WHERE s.news_id IS NULL OR (:relabel AND s.model LIKE 'lexicon%' AND s.model <> :m)",
                    {"relabel": relabel, "m": clf.name})
    if not rows:
        return {"classified": 0, "model": clf.name}
    sents = clf.classify([r["title"] for r in rows])
    now = _now()
    with db.transaction() as conn:
        conn.execute(text(
            "INSERT INTO news_sentiment(news_id, label, score, model, classified_at) "
            "VALUES(:n, :l, :sc, :m, :c) ON CONFLICT(news_id) DO UPDATE SET "
            "label=EXCLUDED.label, score=EXCLUDED.score, model=EXCLUDED.model, classified_at=EXCLUDED.classified_at"),
            [{"n": r["news_id"], "l": s.label, "sc": s.score, "m": clf.name, "c": now} for r, s in zip(rows, sents)])
    log.info("뉴스 감성 분류: %d건 (%s)", len(rows), clf.name)
    return {"classified": len(rows), "model": clf.name}


AGG_DAYS = 7          # 감성 집계 기간 — '오늘의 AI 뉴스 픽'이 몇 달 치 누적 평균이 되지 않도록 최근 7일만


def _agg_since(days=AGG_DAYS):
    """집계 시작일 — 최근 기사 날짜를 포함해 딱 days 일(예: 7일이면 기준일-6일부터, `>=` 로 비교).
    배치가 며칠 멈춰도 마지막 데이터 기준으로 계산. (예전엔 기준일-7일이라 실제로는 8일이었다)"""
    from datetime import date, timedelta
    last = db.query("SELECT MAX(published_at) AS d FROM news")[0]["d"]
    try:
        ref = date.fromisoformat(str(last)[:10])
    except (TypeError, ValueError):
        return ""
    return (ref - timedelta(days=days - 1)).isoformat()           # 기준일 포함 days 일(예전: days+1 일 — '최근 7일'이 8일)


def _stock_agg(days=AGG_DAYS):
    """종목별 감성 집계(최근 days 일): {ticker: {sentiment, pos_ratio, news_count}}."""
    rows = db.query("SELECT ns.ticker AS ticker, s.label AS label, s.score AS score "
                    "FROM news_sentiment s JOIN news_stock ns ON ns.news_id=s.news_id "
                    "JOIN news n ON n.news_id=s.news_id WHERE n.published_at >= :since",
                    {"since": _agg_since(days)})
    agg = {}
    for r in rows:
        a = agg.setdefault(r["ticker"], {"sum": 0.0, "pos": 0, "n": 0})
        a["sum"] += r["score"]
        a["n"] += 1
        if r["label"] == "POS":
            a["pos"] += 1
    out = {}
    for tk, a in agg.items():
        n = a["n"] or 1
        out[tk] = {"sentiment": round(a["sum"] / n, 3),
                   "pos_ratio": round(a["pos"] / n, 3), "news_count": a["n"]}
    return out



# ── API 조회 ──────────────────────────────────────────────

def _evidence(ticker, limit):
    ev = db.query("SELECT n.title, n.url, n.source, s.label FROM news_sentiment s "
                  "JOIN news n ON n.news_id=s.news_id "
                  "JOIN news_stock ns ON ns.news_id=s.news_id "
                  "WHERE ns.ticker=:tk AND s.label<>'NEU' AND n.published_at >= :since "
                  f"ORDER BY {_TS} DESC, n.news_id DESC LIMIT :l",
                  {"tk": ticker, "l": limit, "since": _agg_since()})
    return [{"title": e["title"], "type": "호재" if e["label"] == "POS" else "악재",
             "url": e["url"] if str(e["url"] or "").startswith("http") else "", "source": e["source"]}
            for e in ev]


def today_picks():
    """오늘의 추천 종목(감성 최상위, 호재 우세일 때만) / 비추천 종목(감성 최하위, 악재 우세일 때만).
    종목이 하나뿐이면 같은 종목이 추천·비추천에 동시에 뜨지 않게, 모두 호재면 비추천은 비운다."""
    agg = _stock_agg()
    if not agg:
        return {"recommend": None, "avoid": None}
    names = {r["ticker"]: r["name"] for r in db.query("SELECT ticker, name FROM stock")}
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["sentiment"], reverse=True)

    def pack(item):
        tk, a = item
        return {"ticker": tk, "name": names.get(tk, tk), "sentiment": a["sentiment"],
                "pos_ratio": a["pos_ratio"], "news_count": a["news_count"],
                "evidence": _evidence(tk, 2)}

    best, worst = ranked[0], ranked[-1]
    rec = pack(best) if best[1]["sentiment"] > 0 else None
    avoid = pack(worst) if worst[1]["sentiment"] < 0 and (rec is None or worst[0] != best[0]) else None
    return {"recommend": rec, "avoid": avoid}



def stock_news(ticker, limit=10, offset=0):
    """종목 상세 '관련 뉴스' — 최신순 목록 + 최근 AGG_DAYS 일 호재/악재/중립 개수(AI 뉴스 픽과 같은 기간).
    원문 링크는 http(s) 만 돌려준다(데모 링크 등은 빈 문자열)."""
    items = db.query("SELECT n.title, n.url, n.source, n.published_at, n.published_ts, s.label "
                     "FROM news n JOIN news_stock ns ON ns.news_id = n.news_id "
                     "LEFT JOIN news_sentiment s ON s.news_id = n.news_id "
                     f"WHERE ns.ticker = :t ORDER BY {_TS} DESC, n.news_id DESC LIMIT :n OFFSET :o",
                     {"t": ticker, "n": max(1, min(int(limit), 50)), "o": max(0, min(int(offset), 1_000_000))})
    for it in items:
        lab = it.pop("label", None)
        it["type"] = "호재" if lab == "POS" else "악재" if lab == "NEG" else "중립"
        url = it.get("url") or ""
        it["url"] = url if url.lower().startswith(("http://", "https://")) else ""
    since = _agg_since()
    cnt = {r["label"]: r["c"] for r in db.query(
        "SELECT COALESCE(s.label, 'NEU') AS label, COUNT(*) AS c FROM news n "
        "JOIN news_stock ns ON ns.news_id = n.news_id LEFT JOIN news_sentiment s ON s.news_id = n.news_id "
        "WHERE ns.ticker = :t AND n.published_at >= :since GROUP BY 1", {"t": ticker, "since": since})}
    summary = {"days": AGG_DAYS, "since": since or None,
               "pos": cnt.get("POS", 0), "neg": cnt.get("NEG", 0), "neu": cnt.get("NEU", 0)}
    summary["total"] = summary["pos"] + summary["neg"] + summary["neu"]
    return {"ticker": ticker, "items": items, "summary": summary}



# ── 오프라인 데모용 뉴스 시드 ─────────────────────────────
DEMO_NEWS = [
    ("005930", "삼성전자 역대 최대 실적, HBM 수주 확대"),
    ("005930", "삼성전자 목표가 상향 조정"),
    ("000660", "SK하이닉스 HBM 대규모 수주로 흑자전환"),
    ("042700", "한미반도체 신규 계약 체결"),
    ("035420", "NAVER 대형 클라우드 수주 기대"),
    ("035720", "카카오 자사주 매입 결정"),
    ("005380", "현대차 수출 급증에 강세"),
    ("000270", "기아 제품 리콜 사태 발생"),
    ("051910", "LG화학 대규모 유상증자 발표로 약세"),
    ("006400", "삼성SDI 실적 부진, 목표가 하향"),
    ("000100", "유한양행 신약 임상 성공, 허가 기대"),
    ("128940", "한미약품 임상 실패 소식에 급락"),
]
# 뉴스 화면(오늘의 이슈·테마·주간/월간 랭킹)이 오프라인에서도 채워지도록 기간을 넓게 분산한 추가 표본.
# (종목, 제목, 며칠 전, 시각) — 언론사명은 실제 매체로 오인되지 않게 가상 이름을 쓴다.
DEMO_NEWS_MORE = [
    ("005930", "삼성전자, 차세대 HBM 공급 협상 순항…수주 기대 확대", 0, "08:40"),
    ("000660", "SK하이닉스, 고대역폭 메모리 생산라인 증설 발표", 0, "10:15"),
    ("373220", "LG에너지솔루션, 북미 배터리 공장 증설", 0, "09:50"),
    ("005380", "현대차, 하이브리드 판매 호조…역대 최대 분기 판매", 0, "07:55"),
    ("196170", "알테오젠, 기술수출 계약 기대감에 급등", 0, "13:45"),
    ("005930", "삼성전자 외국인 순매수 이어져…반도체 대형주 강세", 1, "15:40"),
    ("006400", "삼성SDI, 전기차 배터리 수요 둔화 우려에 약세", 1, "14:20"),
    ("207940", "삼성바이오로직스, 대형 위탁생산 계약 체결", 1, "10:00"),
    ("035420", "NAVER, AI 검색 서비스 공개…플랫폼 경쟁력 강화", 1, "09:35"),
    ("357780", "솔브레인, 반도체 소재 수출 호조", 2, "13:05"),
    ("000270", "기아, 전기차 신차 출시 앞두고 목표가 상향", 2, "08:30"),
    ("005490", "POSCO홀딩스, 철강 업황 부진에 실적 부진", 2, "16:30"),
    ("042700", "한미반도체, 신규 장비 공급 계약 체결", 3, "09:20"),
    ("128940", "한미약품, 비만 신약 임상 결과 발표 앞두고 관망", 3, "11:20"),
    ("051910", "LG화학, 양극재 사업 수익성 개선 기대", 4, "10:40"),
    ("035720", "카카오, 계열사 구조 개편 발표", 4, "15:00"),
    ("000660", "SK하이닉스, 메모리 가격 반등에 실적 개선 기대", 5, "11:30"),
    ("012330", "현대모비스, 전동화 부품 수주 확대", 6, "09:10"),
    ("373220", "2차전지 업종, 리튬 가격 안정에 반등 시도", 9, "16:10"),
    ("000100", "유한양행, 폐암 신약 해외 허가 절차 순항", 12, "09:00"),
    ("005930", "삼성전자, 파운드리 고객사 확대 기대", 15, "10:00"),
    ("000660", "SK하이닉스, 분기 최대 실적 전망", 18, "09:30"),
    ("005380", "현대차, 미국 관세 부담 우려", 20, "14:00"),
    ("373220", "LG에너지솔루션, 수주 잔고 증가", 22, "11:00"),
    ("207940", "삼성바이오로직스, 신규 공장 가동", 25, "10:30"),
    ("042700", "한미반도체, 장비 수출 급증", 27, "13:00"),
]
DEMO_SOURCES = ["샘플일보", "가상경제TV", "예시증권뉴스", "데모경제", "모의투자신문"]
_DEMO_STOCKS = {"042700": "한미반도체", "000270": "기아", "006400": "삼성SDI",
                "373220": "LG에너지솔루션", "000100": "유한양행", "128940": "한미약품"}


def seed_demo_news():
    """네트워크 없이 UI 를 채우기 위한 데모 뉴스 + 종목 보강(발행 시각까지 — 'N분 전'·기간 집계용)."""
    from datetime import timedelta, timezone
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).replace(second=0, microsecond=0)
    rows = []
    for i, (tk, title) in enumerate(DEMO_NEWS):                          # 최근 8일에 분산
        rows.append((f"demo://news/{i}", tk, title, now - timedelta(days=i % 8, minutes=25 + i * 41)))
    for j, (tk, title, days, hm) in enumerate(DEMO_NEWS_MORE):
        h, m = (int(x) for x in hm.split(":"))
        at = (now - timedelta(days=days)).replace(hour=h, minute=m)
        if at > now:                                                    # 아직 안 온 시각이면 '방금 전' 쪽으로
            at = now - timedelta(minutes=12 + j * 7)
        rows.append((f"demo://news/more/{j}", tk, title, at))
    with db.transaction() as conn:
        for tk, nm in _DEMO_STOCKS.items():
            conn.execute(text("INSERT INTO stock(ticker, name) VALUES(:t, :n) "
                              "ON CONFLICT(ticker) DO NOTHING"), {"t": tk, "n": nm})
        for i, (url, tk, title, at) in enumerate(rows):
            conn.execute(text("INSERT INTO news(title, url, source, published_at, published_ts) "
                              f"VALUES(:t, :u, :s, :p, :ts) {db.NEWS_URL_CONFLICT} DO UPDATE SET "
                              "published_ts=EXCLUDED.published_ts, published_at=EXCLUDED.published_at, "
                              "source=EXCLUDED.source"),
                         {"t": title, "u": url, "s": DEMO_SOURCES[i % len(DEMO_SOURCES)],
                          "p": at.date().isoformat(), "ts": at.isoformat(timespec="seconds")})
            nid = conn.execute(text(db.NEWS_ID_BY_URL), {"u": url}).scalar()
            if nid:
                conn.execute(text("INSERT INTO news_stock(news_id, ticker) VALUES(:n, :t) "
                                  "ON CONFLICT(news_id, ticker) DO NOTHING"), {"n": nid, "t": tk})
    return {"seeded": len(rows)}


_DEMO_NEWS_WHERE = "url LIKE 'demo://%' OR url LIKE 'news://market/%'"     # seed_demo_news · run._seed_market_news


def purge_demo_news(conn=None):
    """실데이터로 바뀐 DB 에서 데모 뉴스(가상 언론사·demo:// 주소)를 지운다 — 실제 뉴스와 섞여 '실시간 금융 뉴스'·
    호재/악재 감성·테마·AI 뉴스 픽에 쓰이지 않게(종목 매핑·감성 행 포함). 반환: 지운 기사 수."""
    def run(c):
        ids = f"SELECT news_id FROM news WHERE {_DEMO_NEWS_WHERE}"
        c.execute(text(f"DELETE FROM news_stock WHERE news_id IN ({ids})"))
        c.execute(text(f"DELETE FROM news_sentiment WHERE news_id IN ({ids})"))
        return c.execute(text(f"DELETE FROM news WHERE {_DEMO_NEWS_WHERE}")).rowcount or 0
    if conn is not None:
        n = run(conn)
    else:
        with db.transaction() as c:
            n = run(c)
    if n:
        log.info("실데이터 모드: 데모 뉴스 %d건 제거", n)
    return n



