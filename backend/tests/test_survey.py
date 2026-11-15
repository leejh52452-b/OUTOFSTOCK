"""투자 성향 설문 — OUTOFSTOCK_투자성향설문.docx 규칙(20문항 은행·영역별 2문항·0~3점·0~30점·3등급)."""
import random

from fastapi.testclient import TestClient

from app.api import app
from app import db
from app.services import survey as sv

client = TestClient(app)


def _answers(qs, choice):
    return [{"id": q["id"], "answer": choice(q)} for q in qs]


def test_bank_matches_document():
    assert len(sv.BANK) == 20
    for d in "ABCDE":                                            # 영역마다 4문항
        assert [b["no"] for b in sv.BANK if b["domain"] == d] == \
            list(range("ABCDE".index(d) * 4 + 1, "ABCDE".index(d) * 4 + 5))
    assert all(len(b["options"]) == 4 for b in sv.BANK)          # 4지 선다
    assert sv.BY_ID["Q1"]["q"] == "투자를 하는 가장 큰 이유는 무엇인가요?"
    assert sv.BY_ID["Q8"]["options"][3] == "20% 확률로 80만 원, 80% 확률로 0원"
    assert sv.BY_ID["Q20"]["options"][0] == "여러 회사를 묶은 상품(ETF) 하나로 한 번에 나눠 담아요"


def test_questions_two_per_domain_random_ten():
    seen = set()
    for seed in range(30):
        d = sv.questions(seed=seed)
        qs = d["questions"]
        assert d["count"] == 10 and d["max_score"] == 30
        for dom in "ABCDE":
            assert sum(1 for q in qs if q["domain"] == dom) == 2  # 영역별 정확히 2개
        assert len({q["id"] for q in qs}) == 10
        assert [o["score"] for o in qs[0]["options"]] == [0, 1, 2, 3]
        seen.add(tuple(sorted(q["id"] for q in qs)))
    assert len(seen) > 1                                          # 무작위 출제
    assert sv.questions(all_items=True)["count"] == 20


def test_grade_boundaries_follow_document():
    assert sv.classify(0) == sv.classify(10) == "안정형"
    assert sv.classify(11) == sv.classify(20) == "중립형"
    assert sv.classify(21) == sv.classify(30) == "공격형"
    # 비율 규칙(총점÷최대점수): 1/3 이하·2/3 이하·초과
    assert sv.classify(4, 12) == "안정형" and sv.classify(5, 12) == "중립형" and sv.classify(9, 12) == "공격형"


def test_submit_by_ids_scores_and_recommends():
    qs = sv.questions(seed=7)["questions"]
    low = sv.submit(_answers(qs, lambda q: 0))
    assert low["type"] == "안정형" and low["score"] == 0 and low["max_score"] == 30
    assert [p["name"] for p in low["products"]] == ["PLUS KOFR금리", "KODEX TRF3070", "PLUS 금채권혼합"]
    assert low["product_rule"] == "저위험 2 + 중위험 1"
    mid = sv.submit(_answers(qs, lambda q: 1 if q["domain"] != "A" else 2))     # 8×1 + 2×2 = 12
    assert mid["type"] == "중립형" and mid["score"] == 12
    assert [p["name"] for p in mid["products"]][0] == "KODEX 200미국채혼합50"
    high = sv.submit(_answers(qs, lambda q: 3))
    assert high["type"] == "공격형" and high["score"] == 30
    assert [p["name"] for p in high["products"]] == ["KODEX 삼성전자채권혼합", "TIGER 미국테크TOP10 INDXX",
                                                     "SK하이닉스"]
    assert {b["domain"]: b["score"] for b in high["breakdown"]} == {d: 6 for d in "ABCDE"}


def test_submit_legacy_ten_indices():
    assert sv.submit([1] * 10)["type"] == "안정형"               # 10점 → 안정형(경계 포함)
    assert sv.submit([1] * 9 + [2])["type"] == "중립형"          # 11점
    assert sv.submit([2] * 10 + [])["type"] == "중립형"          # 20점
    assert sv.submit([3, 3, 3, 3, 3, 3, 3, 0, 0, 0])["type"] == "공격형"   # 21점


def test_submit_validation():
    qs = sv.questions(seed=3)["questions"]
    good = _answers(qs, lambda q: 1)
    bad_cases = [
        [],                                                        # 빈 답
        good[:9],                                                  # 9문항
        good[:9] + [good[0]],                                      # 같은 문항 두 번
        good[:9] + [{"id": "Q99", "answer": 1}],                   # 없는 문항
        good[:9] + [{"id": good[9]["id"], "answer": 4}],           # 보기 범위 밖
        [1] * 9, [1] * 9 + [5],                                    # 호환 형식 오류
    ]
    # 영역 불균형(A 3개, E 1개)
    a_extra = next(b["id"] for b in sv.BANK if b["domain"] == "A" and b["id"] not in {q["id"] for q in qs})
    e_ids = [q["id"] for q in qs if q["domain"] == "E"]
    bad_cases.append([x for x in good if x["id"] != e_ids[0]] + [{"id": a_extra, "answer": 1}])
    for bad in bad_cases:
        r = client.post("/survey/submit", json={"answers": bad})
        assert r.status_code in (400, 422), bad


def test_api_flow_saves_to_account():
    client.post("/auth/register", json={"username": "sv@oos.io", "password": "pass1234"})
    tok = client.post("/auth/login", json={"username": "sv@oos.io", "password": "pass1234"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    qs = client.get("/survey/questions").json()["questions"]
    assert len(qs) == 10 and qs[0]["domain_name"]
    r = client.post("/survey/submit", headers=h, json={"answers": _answers(qs, lambda q: 3)}).json()
    assert r["type"] == "공격형" and r["grade"] == "공격형 · 고위험" and len(r["products"]) == 3
    me = client.get("/me", headers=h).json()
    assert me["survey"]["type"] == "공격형" and me["survey"]["score"] == 30
    anon = client.post("/survey/submit", json={"answers": [0] * 10}).json()      # 비로그인도 결과는 받음
    assert anon["type"] == "안정형"
    assert db.query("SELECT COUNT(*) AS n FROM survey_result")[0]["n"] == 1        # 저장은 로그인만


def test_random_picks_always_balanced():
    rng = random.Random(42)
    for _ in range(200):
        qs = sv.pick_questions(rng)
        assert sorted(q["domain"] for q in qs) == sorted("AABBCCDDEE")
