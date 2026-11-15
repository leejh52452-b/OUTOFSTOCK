# -*- coding: utf-8 -*-
"""OUTOFSTOCK 콘텐츠·실습 작성 템플릿 (복붙용).

이 파일은 '양식'이다. 여기서 블록을 복사해 실제 파일에 붙여넣어 채운다.
  · 용어/지표/기법  →  app/services/learn.py  의 TERMS / INDICATORS / TECHNIQUES
  · 실습 시나리오    →  app/services/practice.py 의 SCENARIOS

붙여넣고 저장한 뒤 (로컬에서 확인):
  · 콘텐츠(용어/지표/기법)  →  uvicorn 재시작이면 끝(정적)
  · 시나리오               →  python run.py demo (생성 데이터) 또는 python run.py fetch-practice (실데이터)
※ 라이브(VM) 반영: 백엔드는 학교 VM에 컨테이너로 떠 있어서, 완성한 변경은 주원이 VM에서
   `git pull && ./deploy/deploy.sh`(재빌드) + 필요 시 `... exec app python run.py demo` 로 올린다.
   즉 너는 로컬에서 채우고 확인만 하면 된다(CONTENT_ALGO_GUIDE.md 6번 참고).
각 항목은 [예시](실제 채운 것)와 [빈칸](양식)을 같이 뒀다. 빈칸을 채워 쓰면 된다.
"""

# ══════════════════════════════════════════════════════════
# ① 학습 콘텐츠 — app/services/learn.py
# ══════════════════════════════════════════════════════════

# ── 1-A. 용어(TERMS): "카테고리" 안에 (용어, 설명, 중요여부) 튜플 ──
#   중요여부 True 면 화면에서 강조(응답 info=True). 초보 눈높이의 한 줄 설명 권장.
TERMS_EXAMPLE = {
    "가격": [
        ("시가", "장이 열린 뒤 처음 체결된 가격이에요.", False),
        ("등락률", "전날 종가 대비 얼마나 움직였는지 비율로 나타내요.", True),
    ],
}
TERMS_BLANK = {
    "카테고리명": [
        ("용어",  "쉬운 한 줄 설명.", False),   # 중요하면 False → True
        # ("용어2", "설명2.", True),
    ],
}

# ── 1-B. 보조지표(INDICATORS): 아래 8개 키를 모두 채운 dict ──
INDICATOR_EXAMPLE = {
    "key": "RSI",                       # 영문 유일 식별자
    "name": "RSI 14",                   # 화면 표기명
    "unit": "0~100",                    # 값의 단위/범위
    "desc": "최근 14일 상승·하락폭을 비교해 과열/침체를 0~100으로 보여줘요.",  # 초보용 한 줄
    "calc": "상승폭 합 ÷ (상승폭+하락폭) 합",   # 계산식 요약
    "period": "기본 14일",              # 대표 기간
    "watch": "70 위 과열 · 30 아래 침체",  # 무엇을 보나
    "caution": "강한 추세에선 오래 붙어 있어요",  # 주의점/함정
    "related": 2,                       # 관련 실습 개수(표시용 숫자)
}
INDICATOR_BLANK = {
    "key": "", "name": "", "unit": "",
    "desc": "", "calc": "", "period": "",
    "watch": "", "caution": "", "related": 0,
}

# ── 1-C. 매매기법(TECHNIQUES): 아래 6개 키 dict ──
TECHNIQUE_EXAMPLE = {
    "key": "golden-cross",              # 영문-하이픈 유일 식별자
    "name": "골든크로스 매매",
    "desc": "단기 이동평균(SMA5)이 장기(SMA20)를 아래에서 위로 뚫는 순간을 매수 신호로 봐요.",
    "signal": "SMA5가 SMA20을 상향 돌파",   # 진입/청산 신호 조건
    "risk": "횡보장에선 가짜 신호가 잦아요",   # 리스크/함정
    "related": 2,                       # 관련 실습 개수(표시용)
}
TECHNIQUE_BLANK = {
    "key": "", "name": "", "desc": "", "signal": "", "risk": "", "related": 0,
}


# ══════════════════════════════════════════════════════════
# ② 실습 시나리오 — app/services/practice.py 의 SCENARIOS
# ══════════════════════════════════════════════════════════
# pattern(생성 데이터용) 유효 키:
#   golden_cross · rsi_oversold · ma_support_fail · sideways_cross · rsi_divergence · volume_breakout
#   (새 패턴이 필요하면 app/collectors/marketgen.py 의 _drift_vol() 에 분기 추가)
# indicators 가능 값: "SMA5" · "SMA20" · "SMA60" · "RSI14" · "VOL" (원하는 것만 배열로)
# difficulty: "초급" · "중급" · "고급"

SCENARIO_EXAMPLE = {
    "scenario_id": "samsung-golden",    # 유일 식별자(영문-하이픈)
    "ticker": "005930", "stock": "삼성전자",
    "name": "삼성전자 골든크로스 구간", "difficulty": "초급",
    "start_date": "2020-04-01", "end_date": "2020-08-04",  # YYYY-MM-DD (거래일 40일 이상 권장)
    "pattern": "golden_cross",
    "indicators": ["SMA5", "SMA20"],
    "objective": "SMA 5가 SMA 20을 위로 통과하는 시점을 직접 찾아 매수·매도 판단을 연습해요.",
}
SCENARIO_BLANK = {
    "scenario_id": "",
    "ticker": "", "stock": "",
    "name": "", "difficulty": "초급",
    "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
    "pattern": "golden_cross",
    "indicators": ["SMA5", "SMA20"],
    "objective": "",
}


# ══════════════════════════════════════════════════════════
# ③ (참고) 추천 알고리즘 손보는 자리 — 파일만 안내(여기 붙여넣기 아님)
# ══════════════════════════════════════════════════════════
# · 감성 단어사전  →  app/services/sentiment.py 의 POS_WORDS / NEG_WORDS (+ POS_TH/NEG_TH)
#                    반전 구절(예: '적자 폭 축소'=호재)은 같은 파일의 RULES — 확인: python tools/eval_sentiment.py
# · 테마 매핑      →  app/services/themes.py 의 THEME_MAP  (예: "반도체": {"005930": "삼성전자", ...})
# · 테마 랭킹식    →  app/services/themes.py 의 build_theme_reco()  (현재: 감성 × log(1+뉴스수))
# · 데모 뉴스 문장 →  app/services/themes.py 의 DEMO_NEWS
# · 유사종목 피처  →  app/services/recommend.py 의 _load_features()
# · 추천상품       →  app/services/products.py 의 _P(12개)·PICKS(성향별 3개) — 추천상품_선정.docx 기준
# · 투자성향 설문   →  app/services/survey.py 의 _BANK(20문항)·TYPES — 투자성향설문.docx 기준
# 수정 후 반영(로컬):  python run.py demo   (오프라인 재계산)   /   python run.py fetch-news (실데이터)
# 라이브(VM) 반영:     주원이 재배포 후  <compose> exec app python run.py demo  (CONTENT_ALGO_GUIDE.md 6번)

THEME_MAP_ADD_EXAMPLE = {
    # themes.py 의 THEME_MAP 에 이런 식으로 테마 한 줄 추가
    "방산": {"012450": "한화에어로스페이스", "047810": "한국항공우주"},
}
SENTIMENT_WORDS_ADD_EXAMPLE = {
    "POS_WORDS_추가": ["어닝서프라이즈", "최대 수주", "점유율 확대"],
    "NEG_WORDS_추가": ["어닝쇼크", "공급과잉", "점유율 하락"],
}
