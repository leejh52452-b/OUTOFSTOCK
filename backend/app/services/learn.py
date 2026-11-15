"""학습 콘텐츠 — 용어사전 · 보조지표 · 매매기법. 정적 콘텐츠라 코드 상수/JSON 으로 관리.
디자인: 용어를 익히고 → 지표·기법을 배우고 → 그 기법이 나타났던 과거 구간(실습)에서 매매."""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

# ── 주식 용어(카테고리별) ─────────────────────────────────
TERMS = {
    "가격": [
        ("시가", "장이 열린 뒤 처음 체결된 가격이에요.", False),
        ("종가", "장이 끝날 때 마지막으로 체결된 가격이에요.", False),
        ("고가", "하루 중 가장 높게 체결된 가격이에요.", False),
        ("저가", "하루 중 가장 낮게 체결된 가격이에요.", False),
        ("등락률", "전날 종가와 비교해 가격이 얼마나 움직였는지를 비율로 나타내요.", True),
        ("상한가", "하루에 오를 수 있는 가격 제한의 위쪽 끝이에요.", True),
        ("하한가", "하루에 내릴 수 있는 가격 제한의 아래쪽 끝이에요.", False),
        ("갭", "전날 종가와 다음 날 시가 사이에 생긴 가격 차이예요.", True),
    ],
    "거래": [
        ("거래량", "일정 기간 동안 손바뀜이 일어난 주식 수예요.", False),
        ("거래대금", "거래량에 거래 가격을 곱한 금액이에요.", False),
        ("호가", "팔거나 사려고 제시한 가격이에요.", False),
        ("체결", "매수 주문과 매도 주문의 가격이 맞아 거래가 성사된 것이에요.", False),
        ("체결강도", "같은 시간 매수 체결이 매도 체결보다 많은 정도를 나타내요.", True),
    ],
    "내 계좌": [
        ("손절", "손실을 확정하고 정리하는 매도를 말해요.", False),
        ("익절", "이익을 확정하고 정리하는 매도를 말해요.", False),
        ("평균매입가", "보유한 주식을 산 가격의 평균이에요.", True),
        ("평가손익", "지금 가격으로 계산한 이익이나 손실이에요.", True),
    ],
}

# ── 보조지표 ──────────────────────────────────────────────
INDICATORS = [
    {"key": "SMA", "name": "이동평균선(SMA)", "unit": "가격과 같은 단위",
     "desc": "일정 기간의 종가를 평균해 흐름을 매끄럽게 보여주는 지표예요.",
     "calc": "최근 N일 종가의 평균", "period": "SMA 5 · 20 · 60", "watch": "추세의 방향",
     "caution": "방향 전환을 늦게 알려줘요", "related": 3},
    {"key": "RSI", "name": "RSI 14", "unit": "0~100",
     "desc": "최근 14일의 상승폭과 하락폭을 비교해 과열과 침체를 0~100으로 보여줘요.",
     "calc": "상승폭 합 ÷ (상승폭+하락폭) 합", "period": "기본 14일",
     "watch": "70 위 과열 · 30 아래 침체", "caution": "강한 추세에선 오래 붙어 있어요", "related": 2},
    {"key": "MACD", "name": "MACD", "unit": "가격과 같은 단위",
     "desc": "단기·장기 이동평균의 차이로 추세 전환을 포착하는 지표예요.",
     "calc": "EMA12 − EMA26, 그리고 신호선(9)", "period": "12 · 26 · 9",
     "watch": "신호선 교차", "caution": "횡보장에서 자주 속여요", "related": 1},
    {"key": "VOL", "name": "거래량", "unit": "주",
     "desc": "하루 동안 손바뀜이 일어난 주식 수로, 관심과 힘을 확인해요.",
     "calc": "당일 체결 수량 합", "period": "당일 · 20일 평균 대비",
     "watch": "급증 여부", "caution": "숫자만으론 방향을 몰라요", "related": 2},
]

# ── 매매기법 ──────────────────────────────────────────────
TECHNIQUES = [
    {"key": "golden-cross", "name": "골든크로스 매매",
     "desc": "단기 이동평균(SMA5)이 장기(SMA20)를 아래에서 위로 뚫는 순간을 매수 신호로 봐요.",
     "signal": "SMA5가 SMA20을 상향 돌파", "risk": "횡보장에선 가짜 신호가 잦아요", "related": 2},
    {"key": "dead-cross", "name": "데드크로스 매도",
     "desc": "단기 이동평균이 장기를 위에서 아래로 뚫으면 추세 약화 신호로 봐요.",
     "signal": "SMA5가 SMA20을 하향 돌파", "risk": "이미 많이 빠진 뒤일 수 있어요", "related": 1},
    {"key": "volume-breakout", "name": "거래량 돌파 매매",
     "desc": "거래량이 급증하며 박스권 상단을 넘어서는 장면에서 진입 시점을 잡아요.",
     "signal": "평소 대비 거래량 급증 + 신고가", "risk": "돌파 실패 시 되돌림이 빨라요", "related": 1},
    {"key": "rsi-rebound", "name": "RSI 과매도 반등",
     "desc": "RSI가 30 아래로 내려간 뒤 다시 올라오는 반등 초입을 노려요.",
     "signal": "RSI 30 이탈 후 재상향", "risk": "하락 추세에선 더 빠질 수 있어요", "related": 2},
]

# 카테고리 라벨(홈 '최근 학습 콘텐츠' 태그)
_CAT = {"보조지표": [i["name"] for i in INDICATORS],
        "매매기법": [t["name"] for t in TECHNIQUES]}


def terms():
    return {"categories": [{"category": c, "count": len(v),
                            "items": [{"term": t, "def": d, "info": info} for t, d, info in v]}
                           for c, v in TERMS.items()]}


def indicators():
    return {"items": INDICATORS}


def techniques():
    return {"items": TECHNIQUES}


# ── 용어 사전(팀 학습하기 화면) ─────────────────────────────
#  app/data/learn_terms.json — tools/build_learn_terms.py 가 팀 terms.js(169 — 웹 화면 web/assets/terms.js 와 같은 파일)
#  + 팀 「주식 기초 용어 사전」 docx(100, 겹치는 건 '더 알아보기'로 병합) + 위 보조지표·매매기법 상세를 합쳐 만든다.
_DICT_PATH = Path(__file__).resolve().parents[1] / "data" / "learn_terms.json"
LEVELS = {"basic": "초급", "mid": "중급"}
# 용어 카드의 '실습하러 가기' → 실습 시나리오(practice.SCENARIOS). scenario 가 없으면 실습 목록으로.
PRACTICE = {
    "golden": {"label": "골든크로스 실습하러 가기", "scenario": "samsung-golden"},
    "rsi": {"label": "RSI 실습하러 가기", "scenario": "naver-rsi"},
    "volume": {"label": "거래량 돌파 실습하러 가기", "scenario": "hyundai-volume"},
    "ma": {"label": "이동평균 지지 실습하러 가기", "scenario": "kakao-ma-fail"},
    "sideways": {"label": "횡보 구간 실습하러 가기", "scenario": "lgchem-sideways"},
    "divergence": {"label": "다이버전스 실습하러 가기", "scenario": "skhynix-divergence"},
    "any": {"label": "실습하러 가기", "scenario": None},
}
# 학습 로그(learning_log.kind) — '이해했어요' 상태는 로그를 이벤트로 쌓고 마지막 상태로 복원한다
KIND_DONE, KIND_UNDO, KIND_RESET = "term_done", "term_undo", "term_reset"


@lru_cache(maxsize=1)
def _dictionary():
    data = json.loads(_DICT_PATH.read_text(encoding="utf-8"))
    names = [it["name"] for it in data["items"]]
    return data, set(names)


def dictionary():
    """학습하기 화면 전체 데이터 — 용어 카드 + 주제 순서 + 실습 연결."""
    data, _ = _dictionary()
    return {"version": data["version"], "count": data["count"], "categories": data["categories"],
            "levels": LEVELS, "practice": PRACTICE, "sources": data["sources"], "items": data["items"]}


def term_names():
    return _dictionary()[1]


def today_term(day=None):
    """오늘의 용어 — 날짜(한국)마다 한 개(1970-01-01 기준 일수 % 개수). 웹 용어사전(learn.html?term=…)에 있는
    팀 카드(src=team) 중에서만 고른다 — 홈 '1분 용어 학습'을 누르면 그 용어가 바로 열리게."""
    from app.services import quotes
    d = day or quotes.now_kst().date()
    data = _dictionary()[0]["items"]
    items = [it for it in data if it.get("src") == "team"] or data
    return items[(d - date(1970, 1, 1)).days % len(items)]


def today_learn(day=None):
    """홈 '양봉이의 1분 용어 학습' + 학습하기 '오늘의 용어' — 같은 날엔 모두 같은 용어.
    (기존 필드 kind·name·desc·tab 유지 + en·analogy·level·cat·term)"""
    t = today_term(day)
    return {"kind": "주식 용어", "name": t["name"], "desc": t["analogy"], "tab": "terms",
            "term": t["name"], "en": t["en"], "analogy": t["analogy"], "box": t.get("box", "비유"),
            "level": t["level"], "cat": t["cat"]}


def progress(user_id):
    """'이해했어요' 표시한 용어 — 학습 로그를 이벤트로 보고 용어별 마지막 상태(마지막 초기화 이후)만."""
    from app import db
    rows = db.query(
        "SELECT item_key FROM ("
        "  SELECT DISTINCT ON (item_key) item_key, kind FROM learning_log"
        "  WHERE user_id=:u AND kind IN (:d, :x) AND log_id > COALESCE("
        "    (SELECT MAX(log_id) FROM learning_log WHERE user_id=:u AND kind=:r), 0)"
        "  ORDER BY item_key, log_id DESC) s WHERE kind=:d",
        {"u": user_id, "d": KIND_DONE, "x": KIND_UNDO, "r": KIND_RESET})
    valid = term_names()
    done = sorted(r["item_key"] for r in rows if r["item_key"] in valid)
    return {"done": done, "count": len(done), "total": len(valid)}


def set_learned(user_id, term, done=True):
    from app.services import account
    if term not in term_names():
        raise KeyError(term)
    now = set(progress(user_id)["done"])
    if done != (term in now):                            # 상태가 바뀔 때만 기록(중복 클릭 무해)
        account.record_learning(user_id, KIND_DONE if done else KIND_UNDO, term, term)
    return progress(user_id)


def sync_learned(user_id, terms):
    """로그인 전(브라우저)에 표시한 용어를 계정으로 옮김 — 이미 완료인 건 건너뜀."""
    from app.services import account
    valid, now = term_names(), set(progress(user_id)["done"])
    for t in dict.fromkeys(terms or []):
        if t in valid and t not in now:
            account.record_learning(user_id, KIND_DONE, t, t)
    return progress(user_id)


def reset_learned(user_id):
    from app.services import account
    account.record_learning(user_id, KIND_RESET, "", "학습 현황 초기화")
    return progress(user_id)


def recent_contents():
    """홈 '최근 학습 콘텐츠' 목록."""
    return {"items": [
        {"title": "골든크로스 매매", "category": "매매기법"},
        {"title": "이동평균선(SMA)", "category": "보조지표"},
        {"title": "거래량 돌파 매매", "category": "매매기법"},
        {"title": "거래량", "category": "보조지표"},
    ]}
