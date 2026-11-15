from app import db
from app.collectors.news import parse_rss, _store

MOCK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>삼성전자 신제품 공개</title><link>http://ex.com/a</link>
<pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate><source url="http://y.com">연합뉴스</source></item>
<item><title>삼성전자 실적 발표</title><link>http://ex.com/b</link>
<pubDate>Wed, 03 Sep 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_parse_rss():
    items = parse_rss(MOCK_RSS, limit=5)
    assert len(items) == 2
    assert items[0]["title"] == "삼성전자 신제품 공개"
    assert items[0]["url"] == "http://ex.com/a"
    assert items[0]["source"] == "연합뉴스"


def test_rss_date_normalized_to_iso():
    """RFC822 pubDate → 'YYYY-MM-DD'(프론트가 slice(0,10) 로 바로 쓰는 형식)."""
    items = parse_rss(MOCK_RSS, limit=5)
    assert items[0]["published_at"] == "2026-09-03"


def test_store_tag_and_dedup():
    items = parse_rss(MOCK_RSS)
    _store(items, "005930")
    tagged = db.query("SELECT n.title FROM news n JOIN news_stock ns ON ns.news_id=n.news_id "
                      "WHERE ns.ticker='005930'")
    assert len(tagged) == 2
    _store(items, "005930")                       # 재실행 — url unique 로 중복 방지
    assert db.query("SELECT COUNT(*) AS c FROM news")[0]["c"] == 2


# ── 수집 안전장치(하루 1번 · 요청 간격 · 차단 시 즉시 중단 · 저장 오류 격리 · 동시 실행 방지) ──
import requests
from app.collectors import news as news_col
from app.config import settings


def _fake_fetch(plan):
    """plan: 호출 순서대로 'ok' | 'block' | 'fail' — 호출 기록을 남긴다."""
    calls = []

    def fetch(query):
        calls.append(query)
        kind = plan[min(len(calls), len(plan)) - 1]
        if kind == "block":
            raise news_col.Blocked("HTTP 429")
        if kind == "fail":
            raise RuntimeError("parse error")
        return MOCK_RSS.replace("http://ex.com/", f"http://ex.com/{len(calls)}/")
    return fetch, calls


def test_collect_paces_requests_and_stops_on_block(monkeypatch):
    fetch, calls = _fake_fetch(["ok", "ok", "block"])
    sleeps = []
    monkeypatch.setattr(news_col, "_fetch_rss", fetch)
    monkeypatch.setattr(news_col.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(settings, "NEWS_REQUEST_GAP", 0.5)
    r = news_col.collect_news()
    assert r["ok"] == 2 and r["tickers"] == 6 and "차단" in r["stopped"]
    assert len(calls) == 3                                   # 막힌 뒤로는 한 번도 더 보내지 않음
    assert sleeps == [0.5, 0.5]                              # 요청 사이 간격
    assert db.get_meta("news_collected_at")                  # 받은 게 있으니 갱신


def test_collect_stops_after_consecutive_failures_and_keeps_old_timestamp(monkeypatch):
    fetch, calls = _fake_fetch(["fail"])
    monkeypatch.setattr(news_col, "_fetch_rss", fetch)
    monkeypatch.setattr(settings, "NEWS_REQUEST_GAP", 0)
    db.set_meta("news_collected_at", "2026-09-28T23:05:00+09:00")
    r = news_col.collect_news()
    assert r["ok"] == 0 and r["failed"] == settings.NEWS_MAX_FAILS and len(calls) == settings.NEWS_MAX_FAILS
    assert "연속" in r["stopped"]
    assert db.get_meta("news_collected_at") == "2026-09-28T23:05:00+09:00"   # 전부 실패 → '방금 갱신' 아님
    from app.services import monitoring
    h = monitoring.health()
    assert "news_collection_stopped" in h["issues"] and h["news"]["last_run"]["ok"] == 0


def test_store_error_skips_only_that_stock(monkeypatch):
    fetch, _ = _fake_fetch(["ok"])
    real_store = news_col._store

    def store(items, ticker):
        if ticker == "000660":
            raise RuntimeError("deadlock detected")
        real_store(items, ticker)
    monkeypatch.setattr(news_col, "_fetch_rss", fetch)
    monkeypatch.setattr(news_col, "_store", store)
    monkeypatch.setattr(settings, "NEWS_REQUEST_GAP", 0)
    r = news_col.collect_news()
    assert r["failed"] == 1 and r["ok"] == 5 and r["stopped"] is None    # 예외가 밖으로 새지 않음


def test_collect_skips_when_another_run_holds_lock(monkeypatch):
    fetch, calls = _fake_fetch(["ok"])
    monkeypatch.setattr(news_col, "_fetch_rss", fetch)
    with db.advisory_lock("news") as got:
        assert got
        r = news_col.collect_news()
    assert r.get("skipped") and calls == []


class _Resp:
    def __init__(self, status=200, url="https://news.google.com/rss/search?q=x", body=MOCK_RSS):
        self.status_code, self.url, self.text = status, url, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_fetch_rss_block_is_not_retried(monkeypatch):
    for resp in (_Resp(429), _Resp(503), _Resp(200, url="https://www.google.com/sorry/index?continue=x")):
        calls = []
        monkeypatch.setattr(news_col.requests, "get", lambda *a, _r=resp, **k: calls.append(1) or _r)
        try:
            news_col._fetch_rss("삼성전자 주식")
            raise AssertionError("차단인데 통과함")
        except news_col.Blocked:
            pass
        assert len(calls) == 1


def test_fetch_rss_retries_connection_error_once(monkeypatch):
    import app.retry
    seq = [requests.ConnectionError("reset"), _Resp(200)]
    monkeypatch.setattr(app.retry.time, "sleep", lambda s: None)

    def get(*a, **k):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(news_col.requests, "get", get)
    assert "<rss" in news_col._fetch_rss("삼성전자 주식") and seq == []


def test_store_does_not_rewrite_unchanged_rows():
    items = parse_rss(MOCK_RSS)
    _store(items, "005930")
    before = db.query("SELECT xmin::text AS x FROM news ORDER BY news_id")
    _store(items, "005930")                                  # 다음 날 같은 기사를 또 받아도
    assert db.query("SELECT xmin::text AS x FROM news ORDER BY news_id") == before   # 행을 새로 쓰지 않음


def test_stock_news_list_summary_and_paging():
    from fastapi.testclient import TestClient
    from app.api import app
    rows = [(1, "삼성전자 수주", "https://ex.com/1", "2026-09-30", "2026-09-30T09:00:00+09:00", "POS"),
            (2, "삼성전자 급락", "demo://news/2", "2026-09-29", "2026-09-29T15:00:00+09:00", "NEG"),
            (3, "삼성전자 주총", "https://ex.com/3", "2026-09-28", None, None),
            (4, "삼성전자 옛 소식", "https://ex.com/4", "2026-09-01", "2026-09-01T10:00:00+09:00", "POS")]
    for nid, t, u, d, ts, lab in rows:
        db.execute("INSERT INTO news(news_id, title, url, source, published_at, published_ts) "
                   "VALUES(:i, :t, :u, 'A일보', :d, :ts)", {"i": nid, "t": t, "u": u, "d": d, "ts": ts})
        db.execute("INSERT INTO news_stock(news_id, ticker) VALUES(:i, '005930')", {"i": nid})
        if lab:
            db.execute("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) "
                       "VALUES(:i, :l, 0, 'x', '2026-10-01T09:00:00')", {"i": nid, "l": lab})
    c = TestClient(app)
    d = c.get("/stocks/005930/news?limit=3").json()
    assert [x["title"] for x in d["items"]] == ["삼성전자 수주", "삼성전자 급락", "삼성전자 주총"]   # 최신순
    assert [x["type"] for x in d["items"]] == ["호재", "악재", "중립"]
    assert d["items"][1]["url"] == ""                                                        # http(s) 만
    assert d["summary"] == {"days": 7, "since": "2026-09-24", "pos": 1, "neg": 1, "neu": 1, "total": 3}
    page2 = c.get("/stocks/005930/news?limit=3&offset=3").json()["items"]
    assert [x["title"] for x in page2] == ["삼성전자 옛 소식"]
    assert c.get("/stocks/000660/news").json() == {"ticker": "000660", "items": [],
                                                    "summary": {"days": 7, "since": "2026-09-24", "pos": 0,
                                                                "neg": 0, "neu": 0, "total": 0}}
