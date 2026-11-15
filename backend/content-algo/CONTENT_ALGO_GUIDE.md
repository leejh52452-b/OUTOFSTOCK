# OUTOFSTOCK — 추천 알고리즘 · 학습 콘텐츠 · 실습 사례 담당 가이드 (VM 배포판)

> 백엔드(주원) → 콘텐츠·알고리즘 담당 핸드오프.
> 너가 맡은 세 가지가 **코드 어디에 있고**, **어떤 구조로 채우며**, **어떻게 돌려 확인**하는지 정리했다.
> 프론트/DB 배관은 이미 다 돼 있으니, 너는 아래 파일들의 **내용과 로직만** 채우면 화면에 바로 반영된다.

> ### ⚑ 지금 백엔드는 학교 VM에 떠 있다
> 공유 서버(라이브)는 VM에 상시 가동 중 — 결과 화면은 `http://<VM_IP>:8000/`(홈) · `/learn.html`(용어사전), 확인은 `http://<VM_IP>:8000/docs`.
> **너의 작업 방식은 그대로**다: 아래 파일을 **로컬에서 고치고 로컬에서 확인**한 뒤 넘기면, **주원이 VM에 재배포**해서 라이브에 반영한다.
> (라이브 반영 방법은 맨 아래 "라이브(VM) 반영" 참고. 네가 VM에 직접 접근할 필요는 보통 없다.)

---

## 0. 큰 그림 — 네 담당과 파일

| 담당 | 파일 | 화면/API |
|---|---|---|
| **① 학습 콘텐츠** (용어·지표·기법) | `app/services/learn.py` | 학습 화면 · `/learn/*` |
| **② 실습 사례** (과거 구간 기획) | `app/services/practice.py` (시나리오) + `app/collectors/marketgen.py` (데이터) | 실습 화면 · `/practice/*` |
| **③ 추천 알고리즘** (뉴스 감성→테마/오늘의픽) | `app/services/sentiment.py` + `app/services/themes.py` | 홈(추천테마·오늘의픽) · `/themes` `/picks/today` |
| ③-보조 재무 유사도 추천 | `app/services/recommend.py` | 종목상세 '유사종목' · `/stocks/{t}` |
| ③-보조 성향별 추천상품 | `app/services/products.py` (`_P`·`PICKS`) | 설문 결과·홈 · `/reco/products` · `/reco/catalog` |

**공통 작업 흐름**: 파일 수정 → (필요 시) 배치 재실행 → 화면 확인.
- 콘텐츠(①)는 **정적**이라 uvicorn만 재시작하면 끝.
- 실습(②)·추천(③)은 **데이터/배치**가 필요 → 아래 각 섹션의 "돌리는 법" 참고.

**로컬에서 개발·확인**(권장 개발 루프): `cd stock-backend` → `docker compose up -d db` → `python run.py demo` → `uvicorn app.api:app --reload` → `http://localhost:8000/` · `/docs`.
아래 "돌리는 법"의 명령들은 이 **로컬 기준**이다. 완성한 변경을 라이브(VM)에 올리는 건 맨 아래 참고(보통 주원이 재배포).

---

## ① 학습 콘텐츠 — `app/services/learn.py`

전부 코드 상수(dict/list)다. `/learn/terms`·`/learn/indicators`·`/learn/techniques`·`/learn/today` 에 나온다(uvicorn 재시작만).
> ⚠️ **웹 화면(`/learn.html` 용어사전)은 팀 파일 `web/assets/terms.js` 를 그대로 읽는다**(화면 카드 = 그 파일).
> 서버의 `app/data/learn_terms.json`(팀 terms.js + 팀 docx + 지표·기법 상세 병합)은 '이해했어요' 저장 검증·오늘의 용어·`/learn/dictionary` 용 —
> terms.js 를 바꾸면 `python tools/build_learn_terms.py <팀 저장소>` 로 다시 만든다(tests/test_web_front.py 가 '화면 용어 ⊂ 서버 사전'을 확인).
> 퀴즈 문제은행(`web/assets/daily-quiz.js`)을 바꾸면 `python tools/build_daily_quiz.py`(서버 채점용 사본).

### 용어사전 — `TERMS`
`{ "카테고리명": [ (용어, 설명, 중요여부), ... ] }`
```python
TERMS = {
    "가격": [
        ("시가", "장이 열린 뒤 처음 체결된 가격이에요.", False),
        ("등락률", "전날 종가 대비 가격이 얼마나 움직였는지 비율로 나타내요.", True),  # True=중요(강조표시)
    ],
    "거래": [ ("거래량", "일정 기간 손바뀜이 일어난 주식 수예요.", False) ],
}
```
- 세 번째 값 `True/False` = 중요 표시(프론트가 강조). 응답에선 `info` 필드로 나감.
- 카테고리는 자유롭게 추가 가능. 현재: 가격 · 거래 · 내 계좌.

### 보조지표 — `INDICATORS` (리스트)
항목마다 아래 **모든 키**를 채운다:
```python
{"key": "RSI", "name": "RSI 14", "unit": "0~100",
 "desc": "쉬운 한 줄 설명(초보 눈높이)",
 "calc": "계산식 요약", "period": "기본 기간",
 "watch": "무엇을 보나", "caution": "주의점", "related": 2}   # related = 관련 실습 개수(숫자, 표시용)
```

### 매매기법 — `TECHNIQUES` (리스트)
```python
{"key": "golden-cross", "name": "골든크로스 매매",
 "desc": "쉬운 설명", "signal": "매수/매도 신호 조건",
 "risk": "리스크·함정", "related": 2}
```

> ⚠️ 지표/기법의 `key` 는 **영문·하이픈**으로 유일하게. 프론트가 식별자로 쓸 수 있다.
> 정확한 빈칸 양식은 함께 준 **`CONTENT_TEMPLATES.py`** 참고(복붙용).

**돌리는 법**: `learn.py` 저장 → (로컬) uvicorn 재시작 → `/learn/terms` `/learn/indicators` `/learn/techniques` 에 즉시 반영. (라이브 반영은 코드 변경이라 VM 재배포 필요 — 맨 아래 참고.)

---

## ② 실습 사례 — `app/services/practice.py` (`SCENARIOS`) + 데이터

과거 특정 구간을 하루씩 넘기며 매매를 연습하는 "과거 사례". **시나리오 정의**(무엇을) 와 **캔들 데이터**(어디서) 두 부분.

### 시나리오 정의 — `SCENARIOS` (리스트)
```python
{"scenario_id": "samsung-golden",     # 유일 식별자(영문-하이픈)
 "ticker": "005930", "stock": "삼성전자",
 "name": "삼성전자 골든크로스 구간", "difficulty": "초급",   # 초급 | 중급 | 고급
 "start_date": "2020-04-01", "end_date": "2020-08-04",
 "pattern": "golden_cross",            # (생성 데이터일 때) 캔들 생성 패턴 키 — 아래 목록에서
 "indicators": ["SMA5", "SMA20"],      # 차트에 겹칠 지표: SMA5 · SMA20 · SMA60 · RSI14 · VOL
 "objective": "SMA 5가 SMA 20을 위로 통과하는 시점을 찾아 매수·매도 판단을 연습해요."}
```
- `pattern` 유효 키(생성 데이터용): **golden_cross · rsi_oversold · ma_support_fail · sideways_cross · rsi_divergence · volume_breakout**. 새 패턴을 만들려면 `marketgen.py` 의 `_drift_vol()`(+필요시 `gen_closes`)에 분기 추가.
- 실습 종목이 대표 유니버스(현재 VM엔 KRX 상위 150종목 적재됨) 밖이면, 상세/시세도 함께 보이도록 종목을 유니버스에 넣는 게 좋다(주원과 상의).

### 캔들 데이터는 어디서 오나
`ohlc` 테이블에서 읽는다. 두 가지 소스:
- **생성 데이터(demo)** — `pattern` 으로 그럴듯한 가짜 캔들 생성. 인터넷/키 불필요. 기획·발표용.
  → `python run.py demo` 하면 SCENARIOS 전체가 자동 생성 적재된다.
- **실데이터(real)** — KRX 정보데이터시스템에서 그 구간 실제 시세를 가져온다. (VM은 이미 실데이터로 적재돼 있음)
  → `python run.py fetch-practice` (KRX 키 필요, `.env`).
- 로직: `marketgen.seed_scenario_ohlc(scenario, real=True/False)`.
  · 실데이터는 **한 번 받으면 다시 받지 않는다**(바뀌지 않는 과거 — 매일 밤 1천 번 넘던 KRX 호출 절약).
    시나리오 기간을 바꿨거나 다시 받고 싶으면 `python run.py fetch-practice --force`.
  · 받다가 실패하면 **아무것도 쓰지 않는다**(기존 실데이터 유지). 구간에 봉이 하나도 없을 때만 생성 데이터로 채운다.
  · 시작일 이전 약 60거래일(120일) 봉도 함께 받아 **SMA60 등을 첫 화면부터** 그린다(화면에는 시작일부터만 보임).

### 실습 메커니즘(이미 구현됨 — 참고만)
- 시작 시 초기 캔들 `START_OFFSET=20`개만 공개. 지표는 시작 전 워밍업 봉(`WARMUP_BARS=60`)까지 넣어 계산. 시작자금 `START_CASH=1천만원`.
- `series` 는 **커서(현재 시점)까지만** 반환 → 미래 은닉. `step` 으로 1일/5일 전진.
- 한 번에 한 시나리오만 active.

**돌리는 법**: `SCENARIOS` 수정 → (로컬) `python run.py demo`(생성) 또는 `python run.py fetch-practice`(실데이터) → `/practice/scenarios` 확인.

---

## ③ 추천 알고리즘 — 뉴스 감성 → 테마/오늘의 픽

핵심 파이프라인: **뉴스 제목 → 감성 분류(호재/악재) → 종목별 집계 → 테마 랭킹 / 오늘의 픽**.
배치에서 계산해 DB(`news_sentiment`, `theme_reco`)에 저장하고, API는 저장된 결과만 읽는다.

### 3-1. 감성 분류기 — `app/services/sentiment.py`
- 기본 = **사전 v2(lexicon-v2)**: `RULES`(구절 규칙, 가중치) 를 위에서부터 적용하고, 먼저 맞은 구절이 차지한 글자는 뒤 규칙이 다시 못 쓴다
  → '적자 폭 축소'(호재)·'유상증자 철회'(호재)·'FDA 승인 불발'(악재)·'반등 실패'(악재) 같은 **반전 표현**을 제대로 읽는다.
  점수 = Σ가중치/Σ|가중치|(-1~+1), `POS_TH/NEG_TH`(±0.15) 로 호재/악재/중립.
  - **가장 쉬운 개선**: 단어 하나는 `POS_WORDS`·`NEG_WORDS` 에 추가(가중치 1, 규칙 뒤에 적용). 반전 구절은 `RULES` 의 ① 구역에.
  - **측정**: `python tools/eval_sentiment.py` — 라벨 달린 제목 120개 + 규칙 확정 뒤 새로 적은 40개로 v1/v2 정확도·틀린 제목 출력.
    현재 처음 보는 제목 40개 기준 v1 62.5% → v2 82.5%. 제목 하나 확인: `python tools/eval_sentiment.py "제목"`.
  - 사전을 바꾸면 `LexiconClassifier.name` 을 올려라(예: lexicon-v3) — 다음 배치에서 예전 사전으로 매긴 라벨을 자동으로 다시 매긴다.
- 고급 = **KR-FinBert 모델**(`FinBertClassifier`): `transformers`·`torch` 설치 시 `get_classifier(prefer_model=True)` 로 자동 사용, 없으면 사전으로 폴백. 배치에서만 돌아 API 비용 0.
  - 켜는 법: `pip install transformers torch` 후 배치에서 `classify_news(prefer_model=True)`.

### 3-2. 테마 매핑·랭킹 — `app/services/themes.py`
- `THEME_MAP` = 테마 → {종목코드: 이름}. **테마/멤버 추가는 여기서.**
- `classify_news()` → 미분류 뉴스 + 예전 사전 버전으로 매긴 뉴스를 분류해 `news_sentiment` 에 저장(멱등, 모델 결과는 유지).
- `_stock_agg()` → 종목별 {평균감성, 호재비율, 뉴스수}.
- `build_theme_reco()` → 테마별로 `평균감성 × log(1+뉴스수)` 로 정렬·랭킹해 `theme_reco` 갱신.
  - **개선 포인트**: 랭킹식(가중치). 지금은 감성×로그(뉴스수). 여기에 모멘텀(등락률)·거래량·최신성(오래된 뉴스 감쇠) 등을 섞으면 훨씬 그럴듯해진다.
- `today_picks()` → 전 종목 감성 정렬 → 최상위=추천, 최하위=비추천 + 근거 뉴스.

### 3-3. 재무 유사도 추천 — `app/services/recommend.py`
- 종목을 `[log(매출), 영업이익률, 순이익률, 부채비율]` 벡터로 → min-max 정규화 → 유클리드 거리 → 가까운 종목을 '유사종목'으로 `recommendation` 테이블에 저장.
- **개선 포인트**: 피처 추가(PER·PBR·섹터), 피처 가중, 거리척도(코사인).
- 실행: `build_recommendations()` — `python run.py real`(DART 재무 수집 직후)·`python run.py demo` 에 포함(예전 레거시 `run.py pipeline` 은 삭제). 재무는 `python run.py fetch-dart` 로 적재.

### 3-4. 투자성향 설문 + 성향별 추천상품 — `survey.py` · `products.py` (팀 문서 기준)
- 설문: `OUTOFSTOCK_투자성향설문.docx` — 5영역×4문항=20문항 은행(`_BANK`), 영역별 2문항 무작위 → 10문항, 보기 0~3점, 총점 0~30점.
  등급 = 총점÷최대점수: 1/3 이하 안정형 · 2/3 이하 중립형 · 초과 공격형 (10문항: 0~10 / 11~20 / 21~30).
- 상품: `OUTOFSTOCK_추천상품_선정.docx` — 위험 단계별 12개(`_P`), 성향별 3개(`PICKS`).
  조합 규칙(안정형 저위험2+중위험1 · 중립형 중위험2+저위험1 · 공격형 중위험1+고위험2)과 파생형(레버리지·인버스·커버드콜) 제외는
  `tests/test_products.py` 가 자동 검증 → 문구·상품을 바꿔도 규칙이 깨지면 테스트가 실패한다.

### 추천 돌리는 법(배치)
- **한 방(오프라인)**: `python run.py demo` — 데모 뉴스 시드 + `classify_news()` + `build_theme_reco()` 까지 자동. → `/themes` `/picks/today` `/news/tagged` 확인.
- **실데이터**: `python run.py fetch-news` — 구글 RSS 수집 + 분류 + 테마 재생성.
- 결과 테이블: `news_sentiment`(뉴스별 라벨/점수), `theme_reco`(테마별 랭킹), `recommendation`(유사종목).

### 데모 뉴스는 어디에
`app/services/themes.py` 의 `DEMO_NEWS` (오프라인용 종목별 가짜 뉴스). 발표 시나리오를 예쁘게 만들려면 여기 문장을 조정하면 감성/테마 결과가 바뀐다.

---

## 4. 알고리즘 개선 로드맵 (캡스톤용 — 발표에서 "설계했다" 할 거리)

우선순위 순. 하나만 제대로 해도 좋다.
1. **감성 사전 보강 + 임계값 튜닝** (가장 쉬움, 즉효). v2 + 평가 도구는 준비됨 → **실제 수집 기사 제목 100개에 정답 라벨**을 붙여
   `tools/eval_sentiment.py` 로 재 보는 게 다음 단계(지금 평가 제목은 예시 문장이라 실제 기사와 다를 수 있음).
2. **테마 랭킹식 고도화**: `감성 × log(뉴스수)` 에 최신성 감쇠(오래된 뉴스 가중↓), 거래량/등락률 모멘텀 결합. 가중치 근거를 문서화.
3. **KR-FinBert 모델 도입**: 사전 대비 정확도 비교표(사전 vs 모델)를 만들면 "왜 이 방식을 골랐나"가 발표 포인트가 된다.
4. **재무 유사도에 밸류에이션(PER/PBR) 피처 추가** + 유사도 척도 비교(유클리드 vs 코사인).
5. **추천 평가**: "7일 전 추천 종목의 실제 수익률"(`/whatif` 로직)로 추천이 맞았는지 사후 검증 지표를 붙이면 알고리즘의 설득력이 확 올라간다.

---

## 5. 요약 — 뭘 고치면 어디에 나오나

| 하고 싶은 것 | 고치는 곳 | 로컬 반영 명령 |
|---|---|---|
| 용어/지표/기법 추가·수정 | `learn.py` (TERMS/INDICATORS/TECHNIQUES) | uvicorn 재시작 |
| 실습 시나리오 추가 | `practice.py` `SCENARIOS` | 빈 DB `run.py demo` / 실데이터 `fetch-practice --force` |
| 감성 정확도 개선 | `sentiment.py` (규칙·단어) — 고치면 `LexiconClassifier.name` 버전을 올릴 것(예전 라벨 자동 재분류) | `run.py recompute` |
| 테마·멤버·랭킹식 | `themes.py` (THEME_MAP / build_theme_reco) | `run.py recompute` |
| 유사종목 추천 | `recommend.py` | `build_recommendations()` |
| 추천상품·설문 | `products.py` `_P`/`PICKS` · `survey.py` `_BANK` | uvicorn 재시작 |

※ 위 "반영 명령"은 **로컬 기준**. 라이브 VM에 올리는 건 아래 참고.

---

## 6. 라이브(VM) 반영 — 완성한 변경을 공유 서버에 올리기

너는 로컬에서 고치고 확인만 하면 되고, **VM 재배포는 보통 주원이** 한다. 참고로 원리는 이렇다(주원이 VM에서 실행):

- **코드만 바뀜**(용어/지표/기법, 추천 로직 `.py`): 변경을 저장소에 올린 뒤 VM에서
  `git pull && ./deploy/deploy.sh` — 이미지 재빌드·재기동으로 반영. (라이브는 컨테이너라 `uvicorn --reload` 가 아니라 재빌드가 필요.)
- **데이터 재계산 필요**(감성/테마/실습 데이터): 재배포 후 배치를 컨테이너 안에서 한 번 실행 —
  `DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full"`
  `$DC exec app python run.py recompute`  (감성·테마·추천만 재계산 — 수집·시세 그대로)  또는  `fetch-news` / `fetch-practice --force` (실데이터).
  ⚠️ **라이브 VM 에서 `run.py demo` 는 쓰지 말 것** — 실데이터(시세·실습 구간·해외)를 생성 데이터로 덮고 공개 비밀번호의
  데모 계정을 만든다. 그래서 실데이터가 있으면 `demo` 는 거부된다(`--force` 로만 강행).
- 즉, **너 → (로컬 개발·확인) → 저장소 push/전달 → 주원이 VM 재배포**. 급하면 주원한테 "이 파일 반영해줘" 하면 된다.

응답 형태(프론트가 받는 JSON)는 함께 준 프론트 가이드(`frontend/FRONTEND_API_GUIDE.md`)의 해당 화면 섹션과 동일하다. 막히면 주원한테.
