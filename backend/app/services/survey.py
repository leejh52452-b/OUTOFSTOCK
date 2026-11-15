"""투자 성향 설문 — 「OUTOFSTOCK_투자성향설문.docx」 기준(문구·점수·등급 그대로).

  · 형식  : 객관식 4지 선다. 보기는 순서대로 0·1·2·3점(0 = 안정 지향 ~ 3 = 공격 지향).
  · 문항  : 5개 영역(A~E) × 4문항 = 20문항 은행(Q1~Q20).
  · 출제  : 영역별 2문항씩 무작위 → 10문항, 총점 0~30점. 어떤 조합이 걸려도 영역 균형이 유지된다.
  · 등급  : 총점 ÷ 최대 점수 — 1/3 이하 안정형 · 2/3 이하 중립형 · 그 초과 공격형
            (10문항이면 0~10 안정형 · 11~20 중립형 · 21~30 공격형).
  · 결과  : 등급 + 한 줄 설명 + 성향별 추천 상품 3개(「OUTOFSTOCK_추천상품_선정.docx」 → products.py).

제출 형식
  권장  {"answers": [{"id": "Q3", "answer": 2}, ...]}  — 출제된 10문항(영역별 2개)의 보기 번호(0~3)
  호환  {"answers": [2, 0, 3, ...]}                    — 보기 번호 10개(보기 번호 = 점수)
"""
import random

from app.services import products as products_svc

PER_DOMAIN = 2                     # 영역별 출제 문항 수
OPTION_COUNT = 4                   # 4지 선다(0~3점)

DOMAINS = [
    {"id": "A", "name": "투자 목적과 기대 수익",
     "measure": "투자로 얼마나 벌고 싶은지 · 안전과 수익 중 무엇이 먼저인지"},
    {"id": "B", "name": "손실 감내도", "measure": "손해를 얼마나, 어떤 마음으로 버틸 수 있는지"},
    {"id": "C", "name": "투자 자금 성격과 투자 기간", "measure": "투자하는 돈이 어떤 돈이고, 언제 써야 하는지"},
    {"id": "D", "name": "투자 경험과 지식", "measure": "투자 경험, 기간, 용어 이해도, 뉴스 보는 습관"},
    {"id": "E", "name": "종목 선택", "measure": "실제 상황에서 어떤 상품·회사를, 몇 개나 고르는지(상황형)"},
]
_DOMAIN_NAME = {d["id"]: d["name"] for d in DOMAINS}

# (문항 번호, 영역, 질문, [0점, 1점, 2점, 3점 보기]) — 문서 문구 그대로
_BANK = [
    (1, "A", "투자를 하는 가장 큰 이유는 무엇인가요?", [
        "넣은 돈을 지키면서 은행 이자보다 조금이라도 더 벌고 싶어요",
        "물가가 오르는 만큼은 내 돈도 따라가면 좋겠어요",
        "은행 이자의 몇 배는 벌고 싶어요",
        "크게 오르내려도 괜찮으니 높은 수익을 노리고 싶어요"]),
    (2, "A", "100만 원을 넣었을 때, 1년 뒤에 얼마쯤 벌고 싶나요?", [
        "은행 이자만큼(3만 원 정도)이면 충분해요",
        "5만~10만 원 정도",
        "10만~20만 원 정도",
        "20만 원 이상, 두 배가 되는 것도 기대해요"]),
    (3, "A", "100만 원을 넣었을 때, 가장 마음에 드는 결과는?", [
        "매년 3만 원씩 꾸준히 벌고, 손해는 거의 없음",
        "보통 5만~8만 원 벌고, 가끔 조금 손해",
        "잘되면 20만 원 벌고, 안되면 10만 원 손해",
        "잘되면 50만 원 벌고, 안되면 30만 원 손해"]),
    (4, "A", "다음 중 내 생각과 가장 가까운 문장은?", [
        "잃지 않는 것이 버는 것이다",
        "천천히, 꾸준히 가는 게 최고다",
        "위험 없이는 수익도 없다",
        "크게 벌려면 크게 걸어야 한다"]),
    (5, "B", "100만 원을 넣었다면, 얼마까지 줄어도 견딜 수 있나요?", [
        "조금이라도 줄면 안 돼요",
        "90만 원까지는 괜찮아요",
        "70만 원까지는 견딜 수 있어요",
        "50만 원 아래로 내려가도 기다릴 수 있어요"]),
    (6, "B", "100만 원을 넣었는데 한 달 만에 80만 원이 됐어요. 어떻게 하시겠어요?", [
        "더 잃기 전에 전부 팔아요",
        "일부를 팔아서 손해를 줄여요",
        "그대로 두고 다시 오르길 기다려요",
        "싸졌으니 오히려 더 사요"]),
    (7, "B", "손해가 났을 때 나의 마음 상태는?", [
        "잠이 안 올 만큼 스트레스를 받아요",
        "신경은 쓰이지만 일상에 지장은 없어요",
        "투자하면 있을 수 있는 일이라 담담해요",
        "오히려 다음 투자 기회로 느껴져요"]),
    (8, "B", "둘 중 하나를 고를 수 있다면 무엇을 고르겠어요?", [
        "확실하게 10만 원 받기",
        "70% 확률로 20만 원, 30% 확률로 0원",
        "50% 확률로 40만 원, 50% 확률로 0원",
        "20% 확률로 80만 원, 80% 확률로 0원"]),
    (9, "C", "투자한 돈을 언제쯤 쓸 계획인가요?", [
        "3개월 안에 써야 해요",
        "6개월~1년 뒤에 쓸 것 같아요",
        "1~2년 뒤에 쓸 것 같아요",
        "2년 이상 쓸 계획이 없어요"]),
    (10, "C", "투자에 쓰는 돈은 어떤 돈인가요?", [
        "없어지면 생활이 어려워지는 돈이에요",
        "급할 때 쓸 돈(비상금)은 따로 두고, 남은 돈의 일부예요",
        "당장 쓸 데 없는 돈의 대부분이에요",
        "없어도 생활에 전혀 지장 없는 돈이에요"]),
    (11, "C", "매달 들어오는 돈(용돈·알바비·월급) 중 투자에 넣을 수 있는 비율은?", [
        "거의 없어요",
        "10% 이하",
        "10~30%",
        "30% 이상"]),
    (12, "C", "앞으로 매달 들어오는 돈은 어떻게 될 것 같나요?", [
        "줄어들거나 불안정할 것 같아요",
        "지금과 비슷할 것 같아요",
        "조금씩 늘어날 것 같아요",
        "크게 늘어날 것 같아요"]),
    (13, "D", "지금까지 해 본 것 중 가장 위험한 투자는?", [
        "예금·적금뿐이에요",
        "여러 회사를 묶은 상품(펀드·ETF)이나 채권",
        "주식을 직접 사고팔기",
        "해외 주식, 코인, 선물·옵션 같은 고위험 상품"]),
    (14, "D", "투자 경험은 얼마나 되나요?", [
        "아직 없어요",
        "3개월 미만",
        "3개월 ~ 1년",
        "1년 이상"]),
    (15, "D", "\"PER\", \"배당수익률\" 같은 주식 용어를 보면?", [
        "처음 들어봐요",
        "들어는 봤지만 설명은 못 해요",
        "대략 무슨 뜻인지 알아요",
        "직접 계산해서 회사끼리 비교해 봐요"]),
    (16, "D", "주식 시세나 경제 뉴스를 얼마나 자주 보나요?", [
        "거의 안 봐요",
        "큰 뉴스가 있을 때만 봐요",
        "일주일에 몇 번은 챙겨봐요",
        "매일 확인해요"]),
    (17, "E", "방학 동안 알바해서 200만 원을 모았어요. 등록금은 장학금으로 해결됐고, 당장 쓸 데는 없어요. "
              "이 돈을 어디에 넣으시겠어요?", [
        "예금·적금에 넣어요",
        "반은 예금에, 반은 큰 회사 여러 개를 묶은 상품(ETF)에 넣어요",
        "대부분 회사 주식에 넣어요, 이름을 아는 큰 회사 위주로",
        "전부 크게 오를 것 같은 회사 주식에 넣어요"]),
    (18, "E", "두 회사 중 하나만 살 수 있어요. A회사는 매년 번 돈의 일부를 주주에게 나눠 주고(배당) "
              "주식 값은 거의 안 움직여요. B회사는 나눠 주는 돈은 없지만 매출이 매년 빠르게 늘고, "
              "주식 값도 크게 오르내려요. 어느 쪽을 고르시겠어요?", [
        "A회사, 배당을 이자처럼 받는 게 좋아요",
        "A회사를 더 많이, B회사는 조금만",
        "B회사를 더 많이, A회사는 조금만",
        "B회사, 크게 오를 가능성이 중요해요"]),
    (19, "E", "요즘 '우주항공' 관련 주식이 며칠째 크게 오르고 있어요. 그런데 관련 회사 중에는 아직 "
              "매출이 거의 없는 작은 회사도 많아요. 주식을 산다면 무엇을 고르시겠어요?", [
        "유행하는 주식은 안 사요",
        "관련 회사를 묶은 ETF로 조금만 사요",
        "그중 실제로 돈을 버는 큰 회사를 사요",
        "크게 오를 수 있는 작은 회사를 사요"]),
    (20, "E", "300만 원으로 주식을 처음 시작해요. 사고 싶은 회사를 적어 보니 10개나 돼요. "
              "어떻게 투자하시겠어요?", [
        "여러 회사를 묶은 상품(ETF) 하나로 한 번에 나눠 담아요",
        "5~10개 회사에 골고루 나눠요",
        "그중 확신 있는 2~3개에 집중해요",
        "가장 좋아 보이는 한 회사에 전부 넣어요"]),
]
BANK = [{"id": f"Q{no}", "no": no, "domain": d, "q": q, "options": opts} for no, d, q, opts in _BANK]
BY_ID = {b["id"]: b for b in BANK}
QUESTION_COUNT = PER_DOMAIN * len(DOMAINS)            # 10
MAX_SCORE = QUESTION_COUNT * (OPTION_COUNT - 1)       # 30

# 등급 — 결과 화면 문구(문서 3절 한 줄 설명 + 팀 UI 보충 문장)
TYPES = {
    "안정형": {"key": "stable", "risk": "저위험", "grade": "안정형 · 저위험", "range": "0~10점",
            "summary": "원금 변동을 줄이는 쪽이에요",
            "description": "원금 변동을 줄이는 쪽이에요. 저위험 자산 위주로 천천히 배워가요."},
    "중립형": {"key": "neutral", "risk": "중위험", "grade": "중립형 · 중위험", "range": "11~20점",
            "summary": "수익과 안정 사이에서 균형을 잡는 쪽이에요",
            "description": "수익과 안정 사이에서 균형을 잡는 쪽이에요. 중위험 자산을 중심으로 조금씩 경험을 넓혀가요."},
    "공격형": {"key": "aggressive", "risk": "고위험", "grade": "공격형 · 고위험", "range": "21~30점",
            "summary": "변동을 감수하고 수익을 노리는 쪽이에요",
            "description": "변동을 감수하고 수익을 노리는 쪽이에요. 고위험 자산 비중을 높여 실전 감각을 키워요."},
}


class SurveyError(ValueError):
    """제출 형식 오류(문항 수·영역 균형·보기 번호 범위 등)."""


def classify(score, max_score=MAX_SCORE):
    """총점 ÷ 최대 점수: 1/3 이하 안정형 · 2/3 이하 중립형 · 초과 공격형(정수 비교 — 부동소수 오차 없음)."""
    if 3 * score <= max_score:
        return "안정형"
    if 3 * score <= 2 * max_score:
        return "중립형"
    return "공격형"


def pick_questions(rng=None, per_domain=PER_DOMAIN):
    """영역(A~E)마다 4문항 중 per_domain 개를 무작위로 → 영역 순서대로 반환(영역 안은 무작위 순서)."""
    rng = rng or random.SystemRandom()
    out = []
    for d in DOMAINS:
        pool = [b for b in BANK if b["domain"] == d["id"]]
        out.extend(rng.sample(pool, per_domain))
    return out


def _public(b):
    return {"id": b["id"], "no": b["no"], "domain": b["domain"], "domain_name": _DOMAIN_NAME[b["domain"]],
            "q": b["q"], "options": [{"text": t, "score": i} for i, t in enumerate(b["options"])]}


def questions(all_items=False, seed=None):
    """출제 — 기본은 영역별 2문항 무작위 10문항. all_items=True 면 20문항 은행 전체(문서 확인용)."""
    if all_items:
        items = BANK
    else:
        items = pick_questions(random.Random(seed) if seed is not None else None)
    return {"questions": [_public(b) for b in items], "count": len(items),
            "max_score": len(items) * (OPTION_COUNT - 1), "per_domain": PER_DOMAIN,
            "domains": DOMAINS,
            "grades": [{"type": t, "grade": v["grade"], "range": v["range"], "summary": v["summary"]}
                       for t, v in TYPES.items()]}


def _field(a, name):
    return a.get(name) if isinstance(a, dict) else getattr(a, name, None)


def _valid_choice(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < OPTION_COUNT


def score_answers(answers):
    """제출 → (총점, 최대점수, 영역별 점수 | None). 형식이 틀리면 SurveyError."""
    answers = list(answers or [])
    if not answers:
        raise SurveyError("답변이 없어요")
    if all(isinstance(a, int) and not isinstance(a, bool) for a in answers):      # 호환: 보기 번호 10개
        if len(answers) != QUESTION_COUNT:
            raise SurveyError(f"{QUESTION_COUNT}문항 모두 답해 주세요")
        if not all(_valid_choice(a) for a in answers):
            raise SurveyError("보기 번호는 0~3 이에요")
        return sum(answers), MAX_SCORE, None
    seen, per_domain, total = set(), {d["id"]: 0 for d in DOMAINS}, 0
    dom_score = {d["id"]: 0 for d in DOMAINS}
    for a in answers:
        qid, choice = _field(a, "id"), _field(a, "answer")
        b = BY_ID.get(str(qid or "").upper())
        if b is None:
            raise SurveyError(f"알 수 없는 문항이에요: {qid}")
        if b["id"] in seen:
            raise SurveyError(f"같은 문항에 두 번 답했어요: {b['id']}")
        if not _valid_choice(choice):
            raise SurveyError("보기 번호는 0~3 이에요")
        seen.add(b["id"])
        per_domain[b["domain"]] += 1
        dom_score[b["domain"]] += choice
        total += choice
    if any(n != PER_DOMAIN for n in per_domain.values()):
        raise SurveyError(f"영역마다 {PER_DOMAIN}문항씩, 모두 {QUESTION_COUNT}문항에 답해 주세요")
    breakdown = [{"domain": d["id"], "name": d["name"], "score": dom_score[d["id"]],
                  "max": PER_DOMAIN * (OPTION_COUNT - 1)} for d in DOMAINS]
    return total, MAX_SCORE, breakdown


def result_for(score, max_score=MAX_SCORE, breakdown=None):
    inv_type = classify(score, max_score)
    t = TYPES[inv_type]
    reco = products_svc.for_profile(inv_type)
    return {"type": inv_type, "key": t["key"], "risk": t["risk"], "grade": t["grade"],
            "score": score, "max_score": max_score,
            "summary": t["summary"], "description": t["description"],
            "breakdown": breakdown,
            "products": reco["items"], "product_rule": reco["rule"], "notice": reco["notice"]}


def submit(answers):
    total, max_score, breakdown = score_answers(answers)
    return result_for(total, max_score, breakdown)
