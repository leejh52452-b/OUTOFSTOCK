"""뉴스 홈(팀 news.html 계약) — 보도량 기준 이슈·테마·주간/월간 랭킹, 발행 시각 저장, 감성 집계 기간."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.api import app
from app.collectors.news import parse_rss, _store, clean_title
from app.services import news_home, themes

client = TestClient(app)
KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=KST)

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>삼성전자, HBM 수주 확대 - 샘플일보</title><link>http://ex.com/1</link>
<pubDate>Mon, 28 Sep 2026 23:30:00 GMT</pubDate><source url="http://s.com">샘플일보</source></item>
</channel></rss>"""


def _add(i, title, when, source="샘플일보", tickers=(), label=None):
    with db.transaction() as conn:
        nid = conn.execute(text(
            "INSERT INTO news(title, url, source, published_at, published_ts) VALUES(:t,:u,:s,:p,:ts) "
            "RETURNING news_id"), {"t": title, "u": f"https://ex.com/{i}", "s": source,
                                   "p": when.date().isoformat(), "ts": when.isoformat()}).scalar()
        for t in tickers:
            conn.execute(text("INSERT INTO news_stock(news_id, ticker) VALUES(:n,:t)"), {"n": nid, "t": t})
        if label:
            conn.execute(text("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) "
                              "VALUES(:n,:l,:sc,'test',:c)"),
                         {"n": nid, "l": label, "sc": {"POS": 1, "NEG": -1}.get(label, 0),
                          "c": when.replace(tzinfo=None).isoformat(timespec="seconds")})
    return nid


def test_rss_title_suffix_removed_and_time_kept():
    it = parse_rss(RSS)[0]
    assert it["title"] == "삼성전자, HBM 수주 확대"                  # ' - 매체명' 꼬리 제거
    assert it["published_ts"] == "2026-09-29T08:30:00+09:00"         # 한국시간으로 저장
    assert it["published_at"] == "2026-09-29"
    assert clean_title("제목 - 다른매체", "샘플일보") == "제목 - 다른매체"   # 다른 매체면 그대로


def test_store_backfills_time_for_old_rows():
    with db.transaction() as conn:
        conn.execute(text("INSERT INTO news(title,url,source,published_at) VALUES('옛 기사','http://ex.com/1','샘플일보','2026-09-29')"))
    _store(parse_rss(RSS), "005930")
    row = db.query("SELECT published_ts FROM news WHERE url='http://ex.com/1'")[0]
    assert row["published_ts"].isoformat() == "2026-09-29T08:30:00+09:00"     # timestamptz → 한국시간 datetime


def test_today_top_ranks_by_coverage_not_views():
    for i, src in enumerate(["A일보", "B경제", "C뉴스"]):          # 삼성전자: 3개 언론사
        _add(i, f"삼성전자 소식 {i}", NOW - timedelta(hours=i + 1), src, ["005930"], "POS")
    for i in range(3, 7):                                          # 카카오: 4건이지만 1개 언론사
        _add(i, f"카카오 소식 {i}", NOW - timedelta(hours=2), "A일보", ["035720"])
    _add(9, "어제 이전 기사", NOW - timedelta(days=3), "D신문", ["000660"])
    d = news_home.build(NOW)
    top = d["todayTop"]
    assert top[0]["stocks"][0]["name"] == "삼성전자" and top[0]["coverage"] == {"articles": 3, "outlets": 3}
    assert top[1]["coverage"] == {"articles": 4, "outlets": 1}
    assert all(a["views"] is None for a in top) and d["metric"]["basis"] == "coverage"
    assert top[0]["title"] == "삼성전자 소식 0"                         # 대표 기사 = 최신
    assert d["todayLabel"] == "최근 3일"                                # 24시간 이슈가 5개 미만 → 3일로 넓힘
    assert d["sample"] is False and d["latest"][0]["sentiment"] == "호재"


def test_themes_keywords_members_and_exclusion():
    _add(1, "반도체 업황 회복 기대", NOW - timedelta(hours=1))           # 키워드
    _add(2, "SK하이닉스 신고가", NOW - timedelta(hours=2), tickers=["000660"])   # 대표 종목
    _add(3, "한국은행 기준금리 동결", NOW - timedelta(hours=3))          # '은행'이지만 금융 테마 아님
    _add(4, "블루칩 중심 장세", NOW - timedelta(hours=3))                # '칩' 오인 없음
    _add(5, "반도체 수출 증가", NOW - timedelta(days=3))                 # 7일 추이에만
    _add(6, "코스피 보합 마감", NOW - timedelta(hours=4))                # 24시간 이슈 5개 → '오늘 기준' 유지
    d = news_home.build(NOW)
    th = {t["name"]: t for t in d["themes"]}
    assert d["todayLabel"] == "오늘 기준"
    assert th["반도체"]["articleCount"] == 2 and th["반도체"]["rank"] == 1
    assert len(th["반도체"]["trend"]) == 7 and th["반도체"]["trend"][-1] == 2 and th["반도체"]["trend"][-4] == 1
    assert "금융" not in th and len(th["반도체"]["keywords"]) <= 3


def test_weekly_ranking_change_and_new():
    week = NOW - timedelta(days=1)
    prev = NOW - timedelta(days=9)
    for i in range(3):
        _add(10 + i, f"삼성전자 {i}", week - timedelta(hours=i), f"S{i}", ["005930"])
    for i in range(2):
        _add(20 + i, f"카카오 {i}", week, f"K{i}", ["035720"])
    for i in range(3):                                             # 지난주엔 카카오 1위, 삼성 2위
        _add(30 + i, f"카카오 지난주 {i}", prev, f"K{i}", ["035720"])
    _add(40, "삼성전자 지난주", prev, "S0", ["005930"])
    r = news_home.build(NOW)["rankings"]
    wk = {a["stocks"][0]["name"]: a for a in r["week"]}
    assert wk["삼성전자"]["rank"] == 1 and wk["삼성전자"]["rankChange"] == 1 and not wk["삼성전자"]["isNew"]
    assert wk["카카오"]["rankChange"] == -1
    assert r["weekLabel"] == "9.23 ~ 9.29" and r["monthLabel"] == "9월"


def test_stale_data_uses_last_article_day():
    _add(1, "삼성전자 오래된 소식", NOW - timedelta(days=4), tickers=["005930"])
    d = news_home.build(NOW)
    assert d["todayTop"] and d["todayLabel"] == "9.25 기준"


def test_news_home_api_contract():
    themes.seed_demo_news()
    j = client.get("/api/news/home").json()
    assert j["status"] == "success"
    data = j["data"]
    assert {"updatedAt", "todayTop", "themes", "rankings", "latest"} <= set(data)
    assert isinstance(data["todayTop"], list) and isinstance(data["latest"], list)   # news-api.js 검증 조건
    assert data["sample"] is True                                 # demo:// 링크뿐 → 샘플 표시
    assert client.get("/news/home").json()["data"]["latest"]


def test_ai_picks_use_recent_week_only():
    old = datetime.now(KST) - timedelta(days=40)
    new = datetime.now(KST) - timedelta(hours=2)
    _add(1, "삼성전자 호재", new, tickers=["005930"], label="POS")
    _add(2, "카카오 악재", new, tickers=["035720"], label="NEG")
    for i in range(5):                                             # 40일 전 기사는 집계에서 빠져야 함
        _add(10 + i, f"카카오 옛 호재 {i}", old, tickers=["035720"], label="POS")
    p = themes.today_picks()
    assert p["recommend"]["ticker"] == "005930" and p["avoid"]["ticker"] == "035720"
    assert p["avoid"]["news_count"] == 1


def test_ranking_tie_break_same_in_python_and_sql():
    """보도량·최신 시각이 같으면: 최신 기사 id 큰 이슈 → 종목코드 순 — 오늘의 이슈(파이썬)와 주간 랭킹(DB 집계)이 같게."""
    t = NOW - timedelta(hours=1)
    _add(1, "카카오 소식", t, "A일보", ["035720"])
    _add(2, "NAVER 소식", t, "B경제", ["035420"])                   # 같은 시각 — id 가 더 큼
    _add(3, "현대차·삼성전자 동반 강세", t - timedelta(hours=1), "C뉴스", ["005930", "005380"])   # 한 기사, 두 종목
    for i in range(4, 7):                                          # 24시간 이슈 5개 이상 → '오늘 기준'
        _add(i, f"시장 소식 {i}", t - timedelta(hours=i))
    d = news_home.build(NOW)
    top = [a["stocks"][0]["ticker"] if a["stocks"] else a["id"] for a in d["todayTop"]]
    week = [a["stocks"][0]["ticker"] if a["stocks"] else a["id"] for a in d["rankings"]["week"]]
    assert top[:3] == ["035420", "035720", "005380"]               # 삼성전자 이슈의 대표는 이미 현대차에 쓰였다
    assert week[:len(top)] == top


def test_index_on_publish_time_exists():
    names = {r["indexname"] for r in db.query("SELECT indexname FROM pg_indexes WHERE tablename IN ('news','news_stock')")}
    assert {"ix_news_ts", "ix_news_published_at", "ix_news_stock_ticker"} <= names


def test_home_cache_serves_stale_and_refreshes_in_background(monkeypatch):
    import threading
    calls = []
    monkeypatch.setattr(news_home, "build", lambda now=None: calls.append(1) or {"n": len(calls)})
    clock = [1_000_000.0]
    monkeypatch.setattr(news_home, "_clock", lambda: clock[0])
    news_home.clear_cache()
    _add(1, "삼성전자 소식", NOW, tickers=["005930"])
    assert news_home.home() == {"n": 1}
    assert news_home.home() == {"n": 1} and len(calls) == 1        # 같은 데이터·같은 시간 → 캐시
    clock[0] += news_home.TTL                                      # 시간만 지남 → 옛 결과를 바로, 뒤에서 다시 계산
    assert news_home.home() == {"n": 1}
    for th in [t for t in threading.enumerate() if t.name == "news-home-refresh"]:
        th.join(5)
    assert len(calls) == 2 and news_home.home() == {"n": 2}
    _add(2, "카카오 소식", NOW, tickers=["035720"])                  # 새 기사 → 그 자리에서 다시 계산
    assert news_home.home() == {"n": 3}
    news_home.clear_cache()
