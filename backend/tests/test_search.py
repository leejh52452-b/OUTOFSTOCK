"""종목 검색 — 이름·코드·초성·자모(입력 중 글자)·별칭·대소문자 무시 + 랭킹 + 자동완성 기록 정책."""
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.hangul import chosung, jamo, ko_reading, normalize
from app.services import search as sv

client = TestClient(app)


def _add(ticker, name, mcap=None):
    db.execute("INSERT INTO stock(ticker, name, market) VALUES(:t, :n, 'KOSPI') "
               "ON CONFLICT(ticker) DO UPDATE SET name=:n", {"t": ticker, "n": name})
    if mcap:
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate, market_cap) "
                   "VALUES(:t, '2026-09-30', 1000, 1, 0, :m) ON CONFLICT DO NOTHING", {"t": ticker, "m": mcap})
    sv.invalidate()


def test_chosung_util():
    assert chosung("삼성전자") == "ㅅㅅㅈㅈ"


def test_jamo_and_normalize():
    assert jamo("삼성젅").startswith(jamo("삼성")) and jamo("삼성전자").startswith(jamo("삼성젅"))   # 겹받침 분해
    assert jamo("과").startswith(jamo("고"))                                                     # 이중모음 분해
    assert normalize(" SK 하이닉스 ") == "sk하이닉스"


def test_search_by_name_code_chosung():
    r = sv.search_stocks("삼", log_it=False)
    assert any(x["ticker"] == "005930" for x in r)          # 이름 부분일치
    r2 = sv.search_stocks("ㅅㅅ", log_it=False)
    assert any(x["ticker"] == "005930" for x in r2)         # 초성 (삼성전자)
    r3 = sv.search_stocks("000", log_it=False)
    assert any(x["ticker"].startswith("000") for x in r3)   # 코드 접두


def test_case_space_insensitive_and_aliases():
    assert sv.search_stocks("sk", log_it=False)[0]["ticker"] == "000660"          # 예전: 0건
    assert sv.search_stocks("SK 하이", log_it=False)[0]["ticker"] == "000660"
    assert sv.search_stocks("naver", log_it=False)[0]["ticker"] == "035420"
    assert sv.search_stocks("네이버", log_it=False)[0]["ticker"] == "035420"      # 별칭
    assert sv.search_stocks("5930", log_it=False)[0]["ticker"] == "005930"        # 코드 일부


def test_typing_intermediate_hangul_keeps_candidates():
    """IME 조합 중 상태('삼서'·'삼성젅'·'삼ㅅ')에서도 후보가 끊기지 않는다."""
    for q in ("삼서", "삼성젅", "삼ㅅ", "하이닉"):
        r = sv.search_stocks(q, log_it=False)
        assert r, q
    assert sv.search_stocks("삼성젅", log_it=False)[0]["ticker"] == "005930"


def test_ranking_exact_then_prefix_then_market_cap():
    _add("005935", "삼성전자우", mcap=50)
    _add("028260", "삼성물산", mcap=300)
    _add("005930", "삼성전자", mcap=1000)
    r = sv.search_stocks("삼성", log_it=False)
    assert r[0]["ticker"] == "005930"                         # 접두 일치 중 시총 1위
    assert [x["ticker"] for x in r[:3]] == ["005930", "028260", "005935"]
    assert sv.search_stocks("삼성전자", log_it=False)[0]["match"] == "exact"


def test_suggest_does_not_log_but_commit_does():
    sv.suggest("삼")
    sv.suggest("삼성")
    assert db.query("SELECT COUNT(*) AS n FROM search_log")[0]["n"] == 0        # 타이핑 조각은 기록 안 함
    client.post("/search/log", json={"keyword": "삼성", "ticker": "005930"})
    client.post("/search/log", json={"keyword": "삼성전자", "ticker": "005930"})
    client.post("/search/log", json={"keyword": "하이닉스", "ticker": "000660"})
    pop = client.get("/search/popular?limit=3").json()
    assert pop["items"][0]["ticker"] == "005930" and pop["items"][0]["cnt"] == 2
    assert pop["basis"] in ("search", "mixed")


def test_suggest_api_shape_and_kind():
    j = client.get("/search/suggest", params={"q": "ㅅㅅㅈㅈ"}).json()
    assert j["items"] and j["items"][0]["ticker"] == "005930" and j["items"][0]["type"] == "kr"
    assert {"price", "change_rate", "match"} <= set(j["items"][0])
    us = client.get("/search/suggest", params={"q": "애플", "kind": "us"}).json()["items"]
    assert us and us[0]["ticker"] == "AAPL" and all(i["type"] == "us" for i in us)
    assert client.get("/search/suggest", params={"q": ""}).json()["items"] == []
    assert client.get("/search/suggest", params={"q": "x" * 500}).status_code == 200   # 긴 질의도 안전


def test_popular_searches():
    sv.search_stocks("삼성")
    sv.search_stocks("삼성")
    sv.search_stocks("네이버")
    pop = sv.popular_searches()
    assert pop[0]["keyword"] == "삼성"
    assert pop[0]["cnt"] == 2


# ── 2026-10-03 팀 요청: '검색이 해외 주식만 된다' → 국내 포함 + 한국어 자동완성 ─────────────
def _kr_items(q, **kw):
    return [i for i in sv.suggest(q, **kw)["items"] if i["type"] == "kr"]


def test_kr_search_works_before_first_collection():
    """새 설치 직후(첫 수집이 커밋되기 전)·run.py demo 전 — 국내 유니버스(stock)가 비어도 국내 검색이 된다.
    예전: 해외만 표본이 자동으로 깔려 '삼성'은 0건, '애플'만 나와 '검색이 해외 주식만 된다'로 보였다."""
    db.execute("DELETE FROM stock")
    sv.invalidate()
    assert any(i["ticker"] == "005930" for i in _kr_items("삼성"))
    assert sv.suggest("ㅅㅅㅈㅈ")["items"][0]["ticker"] == "005930"                  # 초성
    assert sv.suggest("삼서")["items"][0]["ticker"] == "005930"                       # 조합 중 글자
    assert _kr_items("알테오젠")[0]["ticker"] == "196170"                              # 큐레이션 17종목
    assert sv.suggest("애플")["items"][0]["ticker"] == "AAPL"                          # 해외도 그대로
    j = client.get("/search/suggest", params={"q": "삼성전자"}).json()
    assert j["items"][0]["ticker"] == "005930" and j["items"][0]["type"] == "kr"
    # 유니버스가 채워지면(실데이터·데모) 그 표가 기준 — 큐레이션 대체 목록은 빠진다
    _add("005930", "삼성전자")
    assert _kr_items("알테오젠") == [] and _kr_items("삼성")[0]["ticker"] == "005930"


def test_overseas_screen_search_includes_korean_and_prefers_us():
    """해외 화면 검색창(kind=all&prefer=us) — 국내도 찾고, 같은 매칭 등급이면 해외가 먼저."""
    _add("090430", "아모레퍼시픽")
    us_first = sv.suggest("아", kind="all", prefer="us")["items"]
    assert us_first[0]["type"] == "us" and us_first[0]["ticker"] == "AMZN"           # 아마존
    assert any(i["ticker"] == "090430" for i in us_first)                             # 국내도 함께
    kr_first = sv.suggest("아")["items"]                                              # 기본(국내 먼저)
    assert kr_first[0]["ticker"] == "090430"
    j = client.get("/search/suggest", params={"q": "삼성", "kind": "all", "prefer": "us"}).json()
    assert j["items"] and j["items"][0]["ticker"] == "005930"                         # 해외 결과가 없으면 국내 전부
    assert client.get("/search/suggest", params={"q": "삼성", "prefer": "zz"}).status_code == 200


def test_each_market_keeps_its_candidates():
    """한쪽 시장 후보가 많아도 다른 쪽이 잘리지 않는다(시장별로 따로 매칭).
    예전: 국내 상위 후보 14개가 먼저 잘려 해외 '아마존'이 빠졌다."""
    for i in range(15):
        _add(f"9{i:05d}", f"아이{i:02d}", mcap=10 ** 17)
    items = sv.suggest("아")["items"]
    assert sum(i["type"] == "kr" for i in items) >= 5
    assert any(i["ticker"] == "AMZN" for i in items)
    assert sv.suggest("아", kind="all", prefer="us")["items"][0]["ticker"] == "AMZN"


def test_korean_reading_of_acronym_names():
    """'LG화학'을 '엘지', 'SK하이닉스'를 '에스케이'로 — 영문 약자 이름도 한국어로 자동완성."""
    assert ko_reading("LG화학") == "엘지화학" and ko_reading("SK하이닉스") == "에스케이하이닉스"
    assert ko_reading("KT&G") == "케이티앤지" and ko_reading("HD현대") == "에이치디현대"
    assert ko_reading("NAVER") is None and ko_reading("POSCO홀딩스") is None           # 5자 이상은 단어
    assert ko_reading("삼성전자") is None and ko_reading("S-Oil") == "에스-Oil"
    assert _kr_items("엘지")[0]["ticker"] == "051910"
    assert _kr_items("에스케이")[0]["ticker"] == "000660"
    assert _kr_items("엘ㅈ")[0]["ticker"] == "051910"                                # 조합 중 글자
    assert _kr_items("에스케")[0]["ticker"] == "000660"
    assert sv.suggest("에이엠디", kind="us")["items"][0]["ticker"] == "AMD"           # 해외 약자도

