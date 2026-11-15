"""뉴스 홈 — 팀 뉴스 화면(news.html · assets/news-api.js)의 데이터 계약을 실데이터로 채운다.

  GET /api/news/home → {status, message, data: {updatedAt, todayTop, themes, rankings, latest, aiPicks, …}}

조회수(views)에 대하여
  국내 포털(네이버·다음)과 주요 언론사 기사 페이지는 기사별 조회수를 공개하지 않는다(2026-09 직접 확인).
  그래서 '많이 읽힌'을 '많이 보도된'으로 바꿔 계산한다 — 같은 이슈(종목)를 다룬 기사 수와 언론사 수.
  views 는 계약 호환용으로 두되 항상 null 이고, 대신 coverage{articles, outlets} 를 준다.

정의
  · 이슈 = 같은 종목으로 태깅된 기사 묶음(종목 없는 시장 기사는 기사 1건이 곧 이슈)
  · 오늘의 이슈 TOP = 최근 24시간 이슈를 (언론사 수, 기사 수, 최신순)으로 — 5개가 안 되면 최근 3일
  · 핵심 테마 = 테마 사전(키워드 + 대표 종목)에 걸린 기사 수 · 대표 종목 평균 등락률 · 7일 기사 추이
  · 인기 기사 랭킹 = 주간(최근 7일)·월간(이번 달) 이슈 순위, 직전 기간 순위와 비교해 ▲▼/NEW
"""
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app import db

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
TTL = 120                                   # 초 — 결과 캐시(뉴스가 새로 쌓이면 키가 바뀌어 즉시 갱신)
LATEST_N = 60

# 테마 사전 — tags: 화면 태그(최대 3) · keywords: 제목 매칭(exclude 단어가 있으면 무시) ·
#            members: 대표 종목(유니버스에 있는 것만 쓰임)
# 키워드는 오인을 줄이려고 구체적으로(예: '칩'은 '블루칩', '조선'은 '조선비즈'에 걸려서 제외).
THEMES = [
    {"name": "반도체", "tags": ["메모리", "HBM", "장비"],
     "keywords": ["반도체", "HBM", "메모리", "D램", "디램", "낸드", "파운드리"],
     "members": ["005930", "000660", "042700", "000990", "058470", "403870", "357780"]},
    {"name": "2차전지", "tags": ["배터리", "양극재", "리튬"],
     "keywords": ["2차전지", "이차전지", "배터리", "양극재", "음극재", "리튬"],
     "members": ["373220", "006400", "051910", "003670", "247540", "086520", "066970"]},
    {"name": "자동차", "tags": ["완성차", "부품", "전기차"],
     "keywords": ["자동차", "완성차", "전기차", "車"],
     "members": ["005380", "000270", "012330", "011210", "204320"]},
    {"name": "바이오·제약", "tags": ["신약", "임상", "바이오시밀러"],
     "keywords": ["바이오", "제약", "신약", "임상", "FDA", "바이오시밀러", "항체"],
     "members": ["207940", "068270", "000100", "128940", "196170", "326030", "028300", "302440"]},
    {"name": "방산·조선", "tags": ["방산", "조선", "수출"],
     "keywords": ["방산", "방위산업", "K방산", "조선업", "조선주", "조선사", "선박", "LNG선", "함정"],
     "members": ["012450", "047810", "064350", "079550", "272210", "329180", "009540", "042660", "010140",
                 "010620"]},
    {"name": "전력·원전", "tags": ["원전", "전력기기", "변압기"],
     "keywords": ["원전", "원자력", "전력기기", "변압기", "전력망", "SMR", "송전"],
     "members": ["034020", "010120", "267260", "298040", "015760", "006260"]},
    {"name": "인터넷·게임", "tags": ["플랫폼", "게임", "콘텐츠"],
     "keywords": ["게임", "포털", "웹툰", "신작"],
     "members": ["035420", "035720", "259960", "036570", "251270", "377300"]},
    {"name": "금융", "tags": ["은행", "보험", "배당"],
     "keywords": ["은행", "금융지주", "보험", "카드사"], "exclude": ["한국은행", "중앙은행", "세계은행", "한은"],
     "members": ["105560", "055550", "086790", "316140", "024110", "138040", "032830", "000810", "323410",
                 "006800", "071050"]},
    {"name": "엔터·미디어", "tags": ["K팝", "공연", "콘텐츠"],
     "keywords": ["엔터", "아이돌", "K팝", "케이팝", "음반", "콘서트"],
     "members": ["352820", "035900", "041510", "122870", "035760"]},
    {"name": "철강·소재", "tags": ["철강", "비철금속", "소재"],
     "keywords": ["철강", "제철", "비철", "아연", "구리"],
     "members": ["005490", "004020", "010130"]},
    {"name": "정유·화학", "tags": ["정유", "석유화학", "유가"],
     "keywords": ["정유", "석유화학", "유가", "원유", "나프타"],
     "members": ["096770", "010950", "011170", "009830"]},
    {"name": "통신", "tags": ["통신", "5G", "요금"],
     "keywords": ["통신사", "이통사", "5G", "6G"],
     "members": ["017670", "030200", "032640"]},
    {"name": "건설·부동산", "tags": ["건설", "주택", "해외수주"],
     "keywords": ["건설", "부동산", "분양", "재건축"],
     "members": ["028260", "000720", "006360", "047040"]},
    {"name": "로봇", "tags": ["로봇", "휴머노이드", "자동화"],
     "keywords": ["로봇", "휴머노이드"],
     "members": ["454910", "277810", "108490"]},
    {"name": "소비재·음식료", "tags": ["화장품", "K푸드", "유통"],
     "keywords": ["화장품", "식품", "라면", "음식료", "K푸드"],
     "members": ["090430", "051900", "003230", "097950", "271560", "033780"]},
    {"name": "항공·해운", "tags": ["항공", "해운", "운임"],
     "keywords": ["항공사", "해운", "운임", "컨테이너선", "항공편"],
     "members": ["003490", "011200"]},
]

_cache = {"key": None, "data": None, "busy": False}
_lock = threading.Lock()                    # _cache 읽기/쓰기
_build_lock = threading.Lock()              # 요청 쪽 다시 계산은 한 번에 하나
_clock = time.time                          # 테스트에서 시각을 바꿔 끼운다


# ── 기본 도구 ─────────────────────────────────────────────
def _when(row):
    """(발행 시각 KST, 날짜만 있는지). published_ts 가 없으면 날짜 00:00 으로 본다."""
    ts = row.get("published_ts")
    if ts:
        try:
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))   # timestamptz → datetime(KST)
            return (dt if dt.tzinfo else dt.replace(tzinfo=KST)).astimezone(KST), False
        except ValueError:
            pass
    d = str(row.get("published_at") or "")[:10]
    try:
        return datetime.fromisoformat(d).replace(tzinfo=KST), True
    except ValueError:
        return None, True


def _http(url):
    return url if url and url.lower().startswith(("http://", "https://")) else ""


# 발행 시각 식(timestamptz) — 시각이 없는 예전 기사는 그 날짜 00:00(한국시간)으로 본다(_when 과 같은 규칙).
# db.NEWS_TS 한 곳에서 정의하고 ix_news_ts 가 같은 식으로 인덱스를 만든다(식이 다르면 인덱스를 못 쓴다).
_TS = db.NEWS_TS.format(p="n.")
# 순위 동률 정렬용 글자 키 — 발행 시각(한국시간, 고정 폭)#id 12자리. 바이트 순 = 시간 순(세션 시간대와 무관하게 명시)
_TS_KEY = f"to_char({_TS} AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD\"T\"HH24:MI:SS')"
_TAGS = "ARRAY(SELECT t.ticker FROM news_stock t WHERE t.news_id = {id} ORDER BY t.ticker) AS tickers"
_COLS = ("SELECT n.news_id, n.title, n.url, n.source, n.published_at, n.published_ts, s.label, "
         + _TAGS.format(id="n.news_id") + " FROM news n LEFT JOIN news_sentiment s ON s.news_id = n.news_id ")


def _iso(dt):
    return dt.astimezone(KST).isoformat(timespec="seconds")


def _win(start, end):
    """SQL 기간 조건 [start, end) — 발행 시각 식 인덱스(ix_news_ts)로 그 기간만 바로 찾는다."""
    return f"{_TS} >= :t0 AND {_TS} < :t1", {"t0": _iso(start), "t1": _iso(end)}


def _arts(rows):
    """DB 행 → 기사 dict(발행 시각·날짜·종목 태그). 시각 내림차순(같으면 id 큰 것 먼저)."""
    arts = []
    for r in rows:
        when, date_only = _when(r)
        if when is None:
            continue
        arts.append({"id": r["news_id"], "title": r["title"] or "", "url": r["url"] or "",
                     "source": r["source"] or "", "when": when, "day": when.date(), "date_only": date_only,
                     "label": r["label"], "tickers": list(r["tickers"] or [])})
    arts.sort(key=lambda a: (a["when"], a["id"]), reverse=True)
    return arts


def _load_window(start, end):
    """[start, end) 기사 전부 — 최근 7일(오늘의 이슈·테마용, 수천~1만여 건)."""
    where, p = _win(start, end)
    return _arts(db.query(_COLS + "WHERE " + where, p))


def _latest(end, n=LATEST_N):
    """end 이전 최신 기사 n건 — 인덱스를 거꾸로 n건만 읽는다(데이터가 오래 멈춰 있어도 그대로)."""
    return _arts(db.query(_COLS + f"WHERE {_TS} < :t1 ORDER BY {_TS} DESC, n.news_id DESC LIMIT :lim",
                          {"t1": _iso(end), "lim": n}))


def _newest(before):
    """before 이전 가장 최근 기사 시각(없으면 None)."""
    m = db.query(f"SELECT MAX({_TS}) AS m FROM news n WHERE {_TS} <= :t1", {"t1": _iso(before)})[0]["m"]
    return _when({"published_ts": m, "published_at": m})[0] if m else None


def _article(a, names, cov=None, **extra):
    """팀 계약의 기사 객체 + 우리 확장(sentiment·stocks·coverage·dateOnly)."""
    out = {"id": str(a["id"]), "title": a["title"], "summary": "", "source": a["source"],
           "publishedAt": a["when"].date().isoformat() if a["date_only"] else a["when"].isoformat(timespec="seconds"),
           "url": _http(a["url"]), "views": None, "dateOnly": a["date_only"],
           "sentiment": {"POS": "호재", "NEG": "악재"}.get(a["label"]),
           "stocks": [{"ticker": t, "name": names.get(t, t)} for t in a["tickers"][:3]]}
    if cov is not None:
        out["coverage"] = cov
    out.update(extra)
    return out


def _clusters(arts, start, end):
    """[start, end) 기사 → 이슈(종목) 묶음, (언론사 수, 기사 수, 최신) 내림차순. (오늘의 이슈 — 적은 양이라 파이썬에서)
    같으면 최신 기사가 더 새것(같은 시각이면 id 큰 것)인 이슈, 그것도 같으면 종목코드 순 — _agg 와 같은 규칙."""
    groups = defaultdict(list)
    for a in arts:
        if not (start <= a["when"] < end):
            continue
        if a["tickers"]:
            for t in a["tickers"]:
                groups[("s", t)].append(a)
        else:
            groups[("n", a["id"])].append(a)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda x: (x["when"], x["id"]), reverse=True)
        outlets = len({x["source"] for x in items if x["source"]}) or 1
        out.append({"key": key, "items": items, "articles": len(items), "outlets": outlets,
                    "latest": items[0]["when"]})
    out.sort(key=lambda c: (c["outlets"], c["articles"], c["latest"]), reverse=True)
    return out


def _pick(cluster, names, used):
    """이슈의 대표 기사 — 종목명이 제목에 들어간 최신 기사 우선, 이미 쓴 기사는 건너뜀."""
    kind, val = cluster["key"]
    nm = names.get(val) if kind == "s" else None
    cands = [a for a in cluster["items"] if a["id"] not in used]
    if not cands:
        return None
    if nm:
        named = [a for a in cands if nm in a["title"]]
        if named:
            return named[0]
    return cands[0]


def _ranked(clusters, names, n, used=None):
    used = set() if used is None else used
    out = []
    for c in clusters:
        a = _pick(c, names, used)
        if a is None:
            continue
        used.add(a["id"])
        out.append((c, a))
        if len(out) >= n:
            break
    return out


def _day(dt):
    return dt.astimezone(KST).date()


# ── 섹션 ──────────────────────────────────────────────────
def _today_top(arts, names, ref):
    for hours, label in ((24, "오늘 기준"), (72, "최근 3일")):
        cl = _clusters(arts, ref - timedelta(hours=hours), ref + timedelta(seconds=1))
        if len(cl) >= 5 or hours == 72:
            break
    rows = _ranked(cl, names, 5)
    items = [_article(a, names, {"articles": c["articles"], "outlets": c["outlets"]},
                      related=[_brief(x) for x in c["items"] if x["id"] != a["id"]][:3]) for c, a in rows]
    return items, label, (24 if label == "오늘 기준" else 72)


def _brief(a):
    """같은 이슈의 다른 보도(대표 기사 카드 아래 목록)."""
    return {"id": str(a["id"]), "title": a["title"], "source": a["source"], "url": _http(a["url"]),
            "publishedAt": a["when"].date().isoformat() if a["date_only"] else a["when"].isoformat(timespec="seconds"),
            "dateOnly": a["date_only"]}


def _theme_index():
    """테마 역색인 — 종목 → 테마 번호들, (키워드, 테마 번호) 목록, 테마 번호 → 제외어."""
    by_ticker, kw_pairs, excl = defaultdict(list), [], {}
    for i, th in enumerate(THEMES):
        for t in th["members"]:
            by_ticker[t].append(i)
        kw_pairs += [(k, i) for k in th["keywords"]]
        if th.get("exclude"):
            excl[i] = tuple(th["exclude"])
    return by_ticker, kw_pairs, excl


def _themes(arts, names, ref, hours, quotes_map):
    """arts = 최근 7일 기사(_load_window). 기사를 한 번만 훑어 걸리는 테마를 모은다.
    테마에 걸림 = 대표 종목 태그가 있거나, 제목에 키워드가 있고 제외어는 없을 때."""
    start = ref - timedelta(hours=hours)
    days = [_day(ref) - timedelta(days=i) for i in range(6, -1, -1)]
    by_ticker, kw_pairs, excl = _theme_index()
    hits = [[] for _ in THEMES]
    for a in arts:
        if a["day"] < days[0] or a["when"] > ref:
            continue
        title = a["title"]
        got = {i for t in a["tickers"] for i in by_ticker.get(t, ())}
        for i in {i for k, i in kw_pairs if k in title} - got:
            if not any(x in title for x in excl.get(i, ())):
                got.add(i)
        for i in got:
            hits[i].append(a)
    out = []
    for th, week in zip(THEMES, hits):
        today = [a for a in week if a["when"] >= start]
        if not today:
            continue
        members = set(th["members"])
        per_day = Counter(a["day"] for a in week)
        stock_cnt = Counter(t for a in today for t in a["tickers"] if t in members)
        rates = [quotes_map[t]["change_rate"] for t in th["members"] if t in quotes_map]
        sent = Counter(a["label"] for a in today)
        out.append({
            "name": th["name"], "summary": today[0]["title"], "articleCount": len(today),
            "changeRate": round(sum(rates) / len(rates), 2) if rates else None,
            "keywords": th["tags"][:3], "trend": [per_day.get(d, 0) for d in days],
            "stocks": [{"ticker": t, "name": names.get(t, t),
                        "changeRate": quotes_map.get(t, {}).get("change_rate")} for t, _ in stock_cnt.most_common(3)],
            "sentiment": {"pos": sent.get("POS", 0), "neg": sent.get("NEG", 0)},
            "memberCount": len(rates),
        })
    out.sort(key=lambda t: (t["articleCount"], t["trend"][-1]), reverse=True)
    out = out[:5]
    for i, t in enumerate(out, start=1):
        t["rank"] = i
    return out


def _agg(start, end, limit=None, keys=None):
    """기간 [start, end) 이슈별 보도량 — DB 에서 집계(기사를 불러오지 않음). 반환 [{k, articles, outlets, rk}].
    이슈 키 k = 종목코드(종목 없는 기사는 'n:<id>'), rk = 전체 순위.
    순위 규칙은 _clusters 와 같다: (언론사 수, 기사 수, 최신 기사 시각, 그 기사 id) 내림차순 → 종목코드 순.
    (이슈, 언론사)별로 먼저 묶고 다시 이슈별로 묶어 COUNT(DISTINCT) 정렬 없이 해시 집계로 끝낸다.
    top = '발행시각#id(12자리)' 의 최댓값 — 최신 기사 시각과 그 id 를 한 값으로(바이트 순 비교)."""
    where, p = _win(start, end)
    sql = ("SELECT k, articles, outlets, ROW_NUMBER() OVER (ORDER BY outlets DESC, articles DESC, "
           "top COLLATE \"C\" DESC, k COLLATE \"C\") AS rk "
           "FROM (SELECT k, SUM(c)::int AS articles, GREATEST(COUNT(src), 1)::int AS outlets, "
           "MAX(top COLLATE \"C\") AS top FROM ("
           "SELECT COALESCE(ns.ticker, 'n:' || n.news_id) AS k, NULLIF(n.source, '') AS src, COUNT(*) AS c, "
           f"MAX(({_TS_KEY} || '#' || lpad(n.news_id::text, 12, '0')) COLLATE \"C\") AS top "
           "FROM news n LEFT JOIN news_stock ns ON ns.news_id = n.news_id WHERE " + where + " GROUP BY 1, 2) a "
           "GROUP BY k) g")
    if keys is not None:
        if not keys:
            return []
        return db.query("SELECT k, articles, outlets, rk FROM (" + sql + ") r WHERE k = ANY(:keys)",
                        {**p, "keys": list(keys)})
    return db.query(sql + " ORDER BY rk LIMIT :lim", {**p, "lim": limit or 30})


def _reps(keys, start, end, per):
    """이슈별 대표 기사 후보 per 건(선호 순: 제목에 종목명 포함 → 최신 → id) — _pick 과 같은 순서."""
    where, p = _win(start, end)
    tks = [k for k in keys if not k.startswith("n:")]
    ids = [int(k[2:]) for k in keys if k.startswith("n:")]
    rows = []
    if tks:
        rows += db.query(
            "SELECT x.*, " + _TAGS.format(id="x.news_id") + " FROM ("
            "SELECT n.news_id, n.title, n.url, n.source, n.published_at, n.published_ts, s.label, ns.ticker AS k, "
            "ROW_NUMBER() OVER (PARTITION BY ns.ticker ORDER BY "
            "(COALESCE(st.name, '') <> '' AND strpos(COALESCE(n.title, ''), st.name) > 0) DESC, "
            f"{_TS} DESC, n.news_id DESC) AS rn "
            "FROM news n JOIN news_stock ns ON ns.news_id = n.news_id LEFT JOIN stock st ON st.ticker = ns.ticker "
            "LEFT JOIN news_sentiment s ON s.news_id = n.news_id "
            "WHERE ns.ticker = ANY(:tks) AND " + where + ") x WHERE rn <= :per ORDER BY k, rn",
            {**p, "tks": tks, "per": per})
    if ids:
        rows += db.query(_COLS.replace("SELECT ", "SELECT 'n:' || n.news_id AS k, 1 AS rn, ", 1) +
                         "WHERE n.news_id = ANY(:ids)", {"ids": ids})
    by_id = {a["id"]: a for a in _arts(rows)}
    out = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["k"], r["rn"])):
        a = by_id.get(r["news_id"])
        if a is not None:
            out[r["k"]].append(a)
    return out


def _period(names, start, end, prev_start, prev_end, n=10):
    """주간·월간 랭킹 — 집계는 DB, 대표 기사는 상위 이슈 것만 불러온다. 직전 기간 순위와 비교(▲▼/NEW).
    이미 대표로 쓴 기사는 많아야 n-1건 → 이슈당 후보 n건, 건너뛰는 이슈도 많아야 n-1개 → 상위 3n 이슈면
    기사를 전부 불러와 고르던 방식(_ranked)과 결과가 같다."""
    cur = _agg(start, end, limit=n * 3)
    if not cur:
        return []
    reps = _reps([c["k"] for c in cur], start, end, per=n)
    used, chosen = set(), []
    for c in cur:
        a = next((x for x in reps.get(c["k"], []) if x["id"] not in used), None)
        if a is None:
            continue
        used.add(a["id"])
        chosen.append((c, a))
        if len(chosen) >= n:
            break
    prev_rank = {r["k"]: r["rk"] for r in _agg(prev_start, prev_end, keys=[c["k"] for c, _ in chosen])}
    out = []
    for i, (c, a) in enumerate(chosen, start=1):
        p = prev_rank.get(c["k"])
        out.append(_article(a, names, {"articles": c["articles"], "outlets": c["outlets"]},
                            rank=i, rankChange=(p - i) if p else 0, isNew=p is None))
    return out


def _md(d):
    return f"{d.month}.{d.day}"


def build(now=None):
    from app.services import quotes, themes as themes_svc
    now = (now or quotes.now_kst()).astimezone(KST)
    names = {r["ticker"]: r["name"] for r in db.query("SELECT ticker, name FROM stock")}
    newest = _newest(now)
    # 데이터가 하루 넘게 멈춰 있으면(배치 미실행) 마지막 기사 시각을 '오늘'의 기준으로 삼는다
    ref = now if newest is None or (now - newest) <= timedelta(hours=24) else newest
    end = ref + timedelta(seconds=1)
    week_start = datetime.combine(_day(ref) - timedelta(days=6), datetime.min.time(), KST)
    month_start = datetime(ref.year, ref.month, 1, tzinfo=KST)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

    recent = _load_window(week_start, end)                   # 최근 7일(오늘의 이슈·테마)
    top, top_label, hours = _today_top(recent, names, ref)
    if ref.date() != now.date():
        top_label = f"{_md(ref)} 기준"

    member_set = {t for th in THEMES for t in th["members"] if t in names}
    try:
        qmap = quotes.kr_quotes(sorted(member_set)) if member_set else {}
    except Exception:                                   # 시세가 없어도 뉴스 화면은 떠야 한다
        qmap = {}
    theme_rows = _themes(recent, names, ref, hours, qmap)

    rank_week = _period(names, week_start, end, week_start - timedelta(days=7), week_start)
    rank_month = _period(names, month_start, end, prev_month_start, month_start)

    latest_arts = _latest(end)
    latest = [_article(a, names) for a in latest_arts]
    collected = db.get_meta("news_collected_at")
    try:
        picks = themes_svc.today_picks()
    except Exception:
        picks = {"recommend": None, "avoid": None}
    sample = bool(latest_arts) and not any(_http(a["url"]) for a in latest_arts)
    return {
        "updatedAt": collected or (newest.isoformat(timespec="seconds") if newest else now.isoformat(timespec="seconds")),
        "todayTop": top, "todayLabel": top_label,
        "themes": theme_rows,
        "rankings": {"week": rank_week, "month": rank_month,
                     "weekLabel": f"{_md(week_start)} ~ {_md(ref)}", "monthLabel": f"{ref.month}월",
                     "basis": "보도량(관련 기사·언론사 수)"},
        "latest": latest,
        "aiPicks": picks,
        "metric": {"views": None, "basis": "coverage",
                   "note": "기사 조회수는 포털·언론사가 공개하지 않아, 같은 이슈를 다룬 기사·언론사 수(보도량)로 순위를 매겨요."},
        "sample": sample,
    }


def home(now=None):
    """결과 캐시 — 데이터 키(최신 기사 id·기사 수·감성 수)와 2분 단위 시각으로 관리.
    · 데이터가 바뀌면(수집 직후) 그 자리에서 다시 계산 — 새 기사가 바로 보이게(동시에 온 요청은 한 번만 계산).
    · 데이터는 같고 시간만 지났으면 가진 결과를 바로 주고 뒤에서 한 번만 다시 계산(요청이 기다리지 않음).
    now 를 주면(테스트) 항상 새로 계산."""
    if now is not None:
        return build(now)
    k = db.query("SELECT COALESCE(MAX(news_id), 0) AS m, COUNT(*) AS c, "
                 "(SELECT COUNT(*) FROM news_sentiment) AS s FROM news")[0]
    dkey, bucket = (k["m"], k["c"], k["s"]), int(_clock() // TTL)
    hit = _cached(dkey, bucket)
    if hit is not None:
        return hit
    with _build_lock:
        hit = _cached(dkey, bucket, refresh=False)         # 기다리는 동안 다른 요청이 계산해 뒀으면 그것
        if hit is not None:
            return hit
        data = build()
        with _lock:
            _cache.update(key=(dkey, bucket), data=data)
        return data


def _cached(dkey, bucket, refresh=True):
    """같은 데이터로 계산해 둔 결과(없으면 None). 시간만 지났으면 뒤에서 한 번 다시 계산을 건다."""
    with _lock:
        key, data = _cache["key"], _cache["data"]
        if data is None or key[0] != dkey:
            return None
        if refresh and key[1] != bucket and not _cache["busy"]:
            _cache["busy"] = True
            threading.Thread(target=_refresh, args=(dkey, bucket), name="news-home-refresh", daemon=True).start()
        return data


def _refresh(dkey, bucket):
    """뒤에서 다시 계산 — 그사이 새 데이터로 이미 다시 계산됐으면(키가 바뀜) 덮어쓰지 않는다."""
    try:
        data = build()
        with _lock:
            if _cache["key"] is not None and _cache["key"][0] == dkey:
                _cache.update(key=(dkey, bucket), data=data)
    except Exception:                                    # noqa: BLE001 — 실패해도 가진 결과로 계속 서비스
        log.exception("뉴스 화면 백그라운드 갱신 실패")
    finally:
        with _lock:
            _cache["busy"] = False


def clear_cache():
    with _lock:
        _cache.update(key=None, data=None)
