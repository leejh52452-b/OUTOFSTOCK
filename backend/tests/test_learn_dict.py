"""학습하기(팀 용어 사전) — 사전 데이터 무결성 · 오늘의 용어 · '이해했어요' 계정 저장(학습 로그 이벤트)."""
from datetime import date

from fastapi.testclient import TestClient

from app.api import app
from app.services import learn, practice

client = TestClient(app)


def _login(u="learner@oos.io"):
    client.post("/auth/register", json={"username": u, "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": u, "password": "pass1234"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_dictionary_merges_team_terms_doc_and_ours():
    d = client.get("/learn/dictionary").json()
    items = {t["name"]: t for t in d["items"]}
    assert d["count"] == len(d["items"]) >= 141
    assert {t["src"] for t in d["items"]} == {"team", "doc", "ours"}
    # 팀 terms.js 카드는 문구 그대로, 난이도·주제는 정해진 값만
    gc = items["골든크로스"]
    assert gc["analogy"].startswith("단기 이동평균선이 장기 이동평균선을") and gc["practice"] == "golden"
    assert all(t["level"] in ("basic", "mid") and t["cat"] in d["categories"] for t in d["items"])
    # docx 병합: 같은 용어면 '더 알아보기' 로 붙고(원문 그대로), 없던 용어는 새 카드
    assert any(n["name"] == "주주총회" and n["howto"] for n in items["주주총회"]["notes"])
    assert items["현재가"]["src"] == "doc" and items["현재가"]["box"] == "정의"
    # 우리 기능 보존: 보조지표 계산·기간, 매매기법 신호·위험, 기존 용어(체결강도)
    assert [r[0] for r in items["RSI"]["detail"]["rows"]] == ["계산", "기간", "보는 것", "주의"]
    assert items["돌파"]["detail"]["title"] == "거래량 돌파 매매" and items["돌파"]["practice"] == "volume"
    assert "체결강도" in items and "평균매입가" in items["평균단가"]["alias"]


def test_related_and_practice_links_are_valid():
    d = learn.dictionary()
    names = {t["name"] for t in d["items"]}
    scenario_ids = {s["scenario_id"] for s in practice.SCENARIOS}
    for t in d["items"]:
        assert all(r in names and r != t["name"] for r in t["related"]), t["name"]
        if t["practice"]:
            p = d["practice"][t["practice"]]
            assert p["scenario"] is None or p["scenario"] in scenario_ids


def test_today_term_same_formula_as_team_page():
    # 웹 용어사전(learn.html?term=…)에서 열리는 팀 카드 중에서만 고른다(홈 '1분 용어 학습' 링크가 항상 열리게)
    items = [t for t in learn.dictionary()["items"] if t["src"] == "team"]
    day = date(2026, 9, 29)
    t = learn.today_learn(day)
    assert t["name"] == items[(day - date(1970, 1, 1)).days % len(items)]["name"]
    assert t["tab"] == "terms" and t["desc"] == t["analogy"]           # 홈 카드 호환 필드
    assert learn.today_learn(date(2026, 9, 30))["name"] != t["name"]


def test_progress_saved_to_account_as_log_events():
    h = _login()
    assert client.get("/learn/progress").status_code == 401            # 계정 저장은 로그인 필요
    assert client.get("/learn/progress", headers=h).json()["done"] == []
    client.post("/learn/progress", json={"term": "골든크로스", "done": True}, headers=h)
    client.post("/learn/progress", json={"term": "골든크로스", "done": True}, headers=h)   # 중복 클릭 무해
    p = client.post("/learn/progress", json={"term": "RSI", "done": True}, headers=h).json()
    assert p["done"] == ["RSI", "골든크로스"] and p["total"] >= 141
    p = client.post("/learn/progress", json={"term": "RSI", "done": False}, headers=h).json()
    assert p["done"] == ["골든크로스"]
    # 로그인 전 브라우저 기록 합치기(없는 용어는 무시)
    p = client.post("/learn/progress/sync", json={"terms": ["공매도", "골든크로스", "없는용어"]}, headers=h).json()
    assert p["done"] == ["골든크로스", "공매도"]
    assert client.post("/learn/progress", json={"term": "없는용어"}, headers=h).status_code == 404
    # 초기화 후엔 비고, 다시 표시하면 그것만
    assert client.post("/learn/progress/reset", headers=h).json()["done"] == []
    assert client.post("/learn/progress", json={"term": "PER"}, headers=h).json()["done"] == ["PER"]
    # 학습 로그(마이페이지)에도 남는다
    kinds = [it["kind"] for it in client.get("/me/logs/learning?limit=50", headers=h).json()["items"]]
    assert {"term_done", "term_undo", "term_reset"} <= set(kinds)
    assert kinds.count("term_done") == 4                                 # 골든크로스·RSI·공매도·PER


def test_progress_is_per_user():
    a, b = _login("a@oos.io"), _login("b@oos.io")
    client.post("/learn/progress", json={"term": "공매도"}, headers=a)
    assert client.get("/learn/progress", headers=b).json()["done"] == []


