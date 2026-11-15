"""뉴스 호재/악재 감성 분류 (순수 로직 — DB 의존 없음).

- Tier 1 (사전): 무료/무설치/설명가능. 기본값.
- Tier 2 (KR-FinBert-SC): transformers/torch 설치 시 자동 사용, 없으면 사전으로 폴백.
  무거운 모델은 배치(pipeline)에서만 돌리고, API 는 저장된 결과만 읽는다(비용 최소화).

사전 v2 (lexicon-v2)
  v1 은 단어가 들어 있는지만 셌다 → '적자 폭 축소'를 악재로, '유상증자 철회'를 악재 둘로, 'FDA 승인 불발'을
  호재로 읽었다. v2 는 '구절 규칙'을 우선순위대로 적용하고, 먼저 맞은 구절이 차지한 글자는 뒤 규칙이 다시 못 쓴다.
    ① 반전 구절(적자 축소·철회·승소·불발·해소·반등 실패 …) → ② 구체 구절(어닝쇼크·목표가 상향 …)
    → ③ 한 단어(급등·약세·손실 …) → ④ POS_WORDS / NEG_WORDS(팀원이 단어를 더하는 자리, 가중치 1)
  점수 = Σ가중치 / Σ|가중치| (−1 ~ +1), ±0.15 를 넘으면 호재/악재. 제목에 호재와 악재가 같이 있으면 중립 쪽.
  평가: tests/test_sentiment.py 의 라벨 달린 제목 120개(python tools/eval_sentiment.py 로 v1 과 비교 출력).
  한계: 제목만 본다(본문·주가 반응 X), 문맥 판단 X('주가 조정'·'감산' 같은 말은 모름)."""
import re
from dataclasses import dataclass

# 팀원이 단어를 더하는 자리(가중치 ±1, 규칙 뒤에 적용) — 구절 규칙이 이미 먹은 글자에는 다시 걸리지 않는다.
POS_WORDS = ["최대 실적", "실적 개선", "수주", "계약 체결", "신약", "임상 성공", "승인", "허가",
             "목표가 상향", "상향", "자사주", "흑자전환", "수혜", "특허", "역대 최대",
             "돌파", "급등", "강세", "호실적", "증설"]
NEG_WORDS = ["쇼크", "실적 부진", "부진", "적자", "적자전환", "임상 실패", "리콜", "소송",
             "유상증자", "목표가 하향", "하향", "급락", "약세", "손실", "제재", "철회", "무산",
             "횡령"]

POS_TH = 0.15
NEG_TH = -0.15

_S = r"\s*"                      # 띄어쓰기 유무 모두
# (정규식, 가중치) — 위에서부터 적용. 반전 구절이 단어보다 먼저 와야 한다.
RULES = [
    # ① 반전 — 나쁜 말 + 좋아지는 말 → 호재 / 좋은 말 + 나빠지는 말 → 악재
    (r"적자" + _S + r"(폭" + _S + r")?(축소|감소|줄|개선)", +1),
    (r"적자" + _S + r"(폭" + _S + r")?(확대|지속|누적)", -1),
    (r"(흑자|이익|순이익|영업이익)" + _S + r"(폭" + _S + r")?(축소|감소|급감|반토막|뒷걸음|하락)", -1),
    (r"매출" + _S + r"(감소|급감|반토막|뒷걸음|부진)", -1),
    (r"(유상" + _S + r"증자|증자)" + _S + r"(계획" + _S + r")?(철회|취소|무산|연기)", +1),
    (r"(소송|분쟁|특허소송)" + _S + r"(승소|합의|종결|기각)", +1),
    (r"(제재|규제|관세)" + _S + r"(해제|완화|풀려|유예|철폐)", +1),
    (r"(우려|불확실성|악재|리스크|오버행)" + _S + r"(해소|완화|걷혀|덜어|불식)", +1),
    (r"(부진|침체|적자|바닥)" + _S + r"(탈출|털어|벗어|터널\s*끝)", +1),
    (r"(하락|약세|급락)" + _S + r"(세" + _S + r")?(멈추|멈춰|멈춤|제동|진정|폭\s*축소|마감\s*후\s*반등)", +1),
    (r"(상승|반등|강세|랠리)" + _S + r"(세" + _S + r")?(제한|반납|실패|주춤|꺾|멈추|멈춰|둔화)", -1),
    (r"(승인|허가|인가)" + _S + r"(거부|불발|반려|취소|철회|지연|실패|무산)", -1),
    (r"임상" + _S + r"(\d상" + _S + r")?(실패|중단|중지|좌초|보류|철회|불발)", -2),
    (r"임상" + _S + r"(\d상" + _S + r")?(성공|순항|통과|긍정|유효성\s*입증)", +2),
    (r"수주" + _S + r"(잔고" + _S + r")?(감소|부진|취소|급감|가뭄|절벽|해지|무산)", -1),
    (r"수출" + _S + r"(감소|부진|급감|규제|제한|둔화|금지|타격)", -1),
    (r"수출" + _S + r"(호조|증가|급증|확대|최대|호황|돌풍)", +1),
    (r"(기대치|컨센서스|예상치|시장\s*예상|전망치)" + _S + r"(를\s*|을\s*)?(하회|밑돌|미달|못\s*미쳐)", -1),
    (r"(기대치|컨센서스|예상치|시장\s*예상|전망치)" + _S + r"(를\s*|을\s*)?(상회|웃돌|넘어)", +1),
    (r"기대" + _S + r"(이하|에\s*못\s*미|미달)", -1),
    # ② 구체 구절
    (r"어닝" + _S + r"쇼크|실적" + _S + r"쇼크|쇼크", -2),
    (r"어닝" + _S + r"서프라이즈|깜짝" + _S + r"실적|서프라이즈", +2),
    (r"(사상|역대)" + _S + r"최대|최대" + _S + r"실적", +2),
    (r"흑자" + _S + r"전환|흑전", +2),
    (r"적자" + _S + r"전환|적전", -2),
    (r"(목표" + _S + r"주?가|투자" + _S + r"의견|눈높이)[^,…·]{0,12}?(상향|올려|올린|올렸)", +1),
    (r"(목표" + _S + r"주?가|투자" + _S + r"의견|눈높이)[^,…·]{0,12}?(하향|내려|내린|낮춰|낮춘|낮췄)", -1),
    (r"(실적|업황|수익성|이익|수요|경기|가동률)" + _S + r"(개선|회복|호조|반등|정상화)", +1),
    (r"(실적|업황|수익성|수요|경기)" + _S + r"(부진|악화|둔화|감소|위축|침체)", -1),
    (r"(영업이익|순이익|이익|매출|판매)" + _S + r"(증가|급증|확대|성장|호조)", +1),
    (r"(성장|매출)" + _S + r"(가속|확대|급증)", +1),
    (r"(공급|납품|수출|판매|라이선스|기술이전|기술수출|수주)" + _S + r"계약|계약" + _S + r"(체결|성사)", +1),
    (r"기술" + _S + r"수출|라이선스" + _S + r"아웃", +1),
    (r"자사주" + _S + r"(매입|소각|취득)", +1),
    (r"(배당|주주환원|주주" + _S + r"환원)" + _S + r"(확대|증액|상향|강화|늘)", +1),
    (r"주주" + _S + r"가치" + _S + r"제고", +1),
    (r"무상" + _S + r"증자", +1),
    (r"(무상|유상)?" + _S + r"감자(?!칩|튀김|탕|전분|농사|가격)", -1),
    (r"(유상" + _S + r"증자|전환사채|CB" + _S + r"발행)", -1),
    (r"(외국인|기관)" + _S + r"(순매수|매수세|사자)", +1),
    (r"(외국인|기관)" + _S + r"(순매도|매도세|팔자)", -1),
    (r"순매수|매수세", +1),
    (r"순매도|매도세|매도" + _S + r"폭탄", -1),
    (r"(상승|오름)" + _S + r"폭" + _S + r"(축소|둔화|반납)|상승분" + _S + r"반납", -1),
    (r"(하락|내림)" + _S + r"폭" + _S + r"(축소|둔화)|낙폭" + _S + r"(축소|만회|회복)", +1),
    (r"상승" + _S + r"(마감|세|전환|폭\s*확대)|주가" + _S + r"상승", +1),
    (r"하락" + _S + r"(마감|세|전환|폭\s*확대)|주가" + _S + r"하락", -1),
    (r"(관세|규제)" + _S + r"(부담|강화|폭탄|리스크)", -1),
    (r"상장" + _S + r"폐지|거래" + _S + r"정지|관리" + _S + r"종목|불성실" + _S + r"공시", -2),
    (r"워크아웃|법정관리|회생" + _S + r"(절차|신청)|부도|파산|디폴트", -2),
    (r"횡령|배임|분식|주가" + _S + r"조작|시세" + _S + r"조종|압수" + _S + r"수색|검찰|기소|구속", -2),
    (r"점유율" + _S + r"(확대|상승|1위|늘)", +1),
    (r"점유율" + _S + r"(하락|축소|감소|뺏)", -1),
    # ③ 한 단어
    (r"신고가|상한가", +1),
    (r"신저가|하한가", -1),
    (r"급등|폭등|강세|반등|랠리|날개|상승", +1),      # 상승/하락 대칭(예전엔 '5% 하락'만 악재로 셌다)
    (r"급락|폭락|약세|하락|붕괴|추락|곤두박질", -1),
    (r"호조|호실적|흥행|대박|턴어라운드|수혜|호재", +1),
    (r"부진|악화|둔화|위축|침체|악재|압박|타격", -1),
    (r"수주", +1),
    (r"흑자", +1),
    (r"적자", -1),
    (r"증설|투자" + _S + r"확대|생산" + _S + r"확대", +1),
    (r"(승인|허가)" + _S + r"(획득|취득)?|인가" + _S + r"(획득|취득)", +1),   # '~고점인가' 같은 의문문 제외
    (r"손실|손상" + _S + r"차손|평가손", -1),
    (r"리콜|결함|소송|피소|고소|고발", -1),
    (r"제재|과징금|벌금|징계|영업정지", -1),
    (r"화재|폭발|사고|파업|정전", -1),
    (r"무산|철회|좌초|불발", -1),
    (r"우려|경고|리스크|위기|쇼크", -1),
    (r"상회|웃돌", +1),
    (r"하회|밑돌", -1),
    (r"기대감?", +0.5),
    (r"상향", +1),
    (r"하향", -1),
]
_COMPILED = None


def _rules():
    """규칙 + 팀원 추가 단어(POS_WORDS/NEG_WORDS, 공백 유무 무시)를 한 번만 컴파일."""
    global _COMPILED
    if _COMPILED is None:
        def pat(w):                                # 띄어쓰기 유무 모두 맞게
            return re.escape(w).replace("\\ ", " ").replace(" ", r"\s*")
        words = [(pat(w), +1) for w in POS_WORDS] + [(pat(w), -1) for w in NEG_WORDS]
        _COMPILED = [(re.compile(p), w) for p, w in RULES + words]
    return _COMPILED


@dataclass
class Sentiment:
    label: str    # POS(호재) / NEG(악재) / NEU(중립)
    score: float  # -1.0 ~ +1.0


def _lexicon_v2(text: str) -> Sentiment:
    s = text or ""
    if not s:
        return Sentiment("NEU", 0.0)
    taken = bytearray(len(s))                      # 이미 어떤 규칙이 먹은 글자
    total = mag = 0.0
    for rx, w in _rules():
        for m in rx.finditer(s):
            a, b = m.span()
            if a == b or any(taken[a:b]):
                continue
            taken[a:b] = b"\x01" * (b - a)
            total += w
            mag += abs(w)
    if mag == 0:
        return Sentiment("NEU", 0.0)
    score = total / mag
    label = "POS" if score > POS_TH else "NEG" if score < NEG_TH else "NEU"
    return Sentiment(label, round(score, 3))


_V1_POS = ["최대 실적", "실적 개선", "수주", "계약 체결", "신약", "임상 성공", "승인", "허가",
           "목표가 상향", "상향", "자사주", "흑자전환", "수출", "수혜", "특허", "역대 최대",
           "돌파", "급등", "강세", "호실적", "증설"]
_V1_NEG = ["쇼크", "실적 부진", "부진", "적자", "적자전환", "임상 실패", "리콜", "소송",
           "유상증자", "목표가 하향", "하향", "급락", "약세", "손실", "제재", "철회", "무산",
           "횡령", "감자"]


def _lexicon_v1(text: str) -> Sentiment:
    """예전 사전(단어 포함 여부만 셈) — 비교·평가용으로만 남겨 둔다."""
    s = text or ""
    pos = sum(w in s for w in _V1_POS)
    neg = sum(w in s for w in _V1_NEG)
    total = pos + neg
    if total == 0:
        return Sentiment("NEU", 0.0)
    score = (pos - neg) / total
    label = "POS" if score > POS_TH else "NEG" if score < NEG_TH else "NEU"
    return Sentiment(label, round(score, 3))


_lexicon_one = _lexicon_v2        # 게임 뉴스 이벤트(news_events) 등 기존 호출 호환


class LexiconClassifier:
    name = "lexicon-v2.1"          # 규칙을 고치면 올린다 → 다음 배치가 예전 사전으로 매긴 기사를 다시 매김

    def classify(self, texts):
        return [_lexicon_v2(t) for t in texts]


class FinBertClassifier:
    """KR-FinBert-SC 추론. 최초 1회 모델 다운로드 후 로컬 추론(요금 0).
    라벨명(positive/negative/neutral vs LABEL_0/1/2)은 실제 모델 기준으로 한 번 확인."""
    name = "kr-finbert-sc"

    def __init__(self, model_id="snunlp/KR-FinBert-SC", batch_size=16):
        from transformers import pipeline  # 지연 import: 없으면 예외 → 폴백
        self.pipe = pipeline("sentiment-analysis", model=model_id,
                             tokenizer=model_id, truncation=True)
        self.batch_size = batch_size

    @staticmethod
    def _map(label, prob):
        lab = str(label).lower()
        if any(k in lab for k in ("pos", "positive", "호재", "label_2")):
            return Sentiment("POS", round(+prob, 3))
        if any(k in lab for k in ("neg", "negative", "악재", "label_0")):
            return Sentiment("NEG", round(-prob, 3))
        return Sentiment("NEU", 0.0)

    def classify(self, texts):
        raw = self.pipe(list(texts), batch_size=self.batch_size)
        return [self._map(r["label"], float(r["score"])) for r in raw]


def get_classifier(prefer_model=False):
    """prefer_model=True 이고 transformers/torch/모델이 있으면 KR-FinBert, 아니면 사전."""
    if prefer_model:
        try:
            return FinBertClassifier()
        except Exception:
            pass
    return LexiconClassifier()
