<div align="center">

# OUTOFSTOCK

**양봉이와 함께 배우는 주식 첫걸음**

주식 입문자가 **실제 돈을 잃을 걱정 없이 배우고, 연습하고, 스스로 판단하는 과정**을<br>
한곳에서 경험할 수 있는 학습형 웹 서비스입니다.

</div>

> [!WARNING]
> 본 서비스는 학습용 서비스이며, 서비스 내 추천 종목과 정보는 투자 권유가 아닙니다.

---

## 프로젝트 자료

| 구분 | 파일 |
| --- | --- |
| 시연 영상 | [OUTOFSTOCK 시연 영상](https://www.youtube.com/watch?v=KgwQZfjFGgk) |
| 포스터 | [OUTOFSTOCK 포스터](docs/OUTOFSTOCK_poster.pdf) |
| 프로젝트 결과 보고서 | [OUTOFSTOCK 결과보고서]() |

---

## 주요 기능

| 기능 | 설명 |
| --- | --- |
| 용어사전 | 주식 용어 169개를 한 줄 비유와 그림으로 설명 |
| Daily Quiz | 하루 5문제로 용어 복습 |
| 투자 성향 분석 | 10문항으로 내 투자 유형 확인 |
| 매매기법 실습 | 과거 실제 차트로 4가지 기법 연습 후 복기 |
| 가상시장 모의투자 | 매주 열리는 가상 시장에서 투자 |
| 주간 추천 | 추천 5종목과 한 달 전 추천 결과 공개 |
| 실시간 종목 · 뉴스 | 국내·미국 종목 순위와 금융 뉴스 |
| 양봉이 꾸미기 | 가상머니로 캐릭터 꾸미기 |
| 마이페이지 | 학습 기록, 친구 채팅·송금 |
---

## 서비스 실행 방법

> 추후 작성 예정입니다.

---

## 기술 스택

| 구분 | 기술 |
| --- | --- |
| Frontend | ![HTML5](https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white) ![CSS3](https://img.shields.io/badge/CSS3-1572B6?logo=css3&logoColor=white) ![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?logo=javascript&logoColor=black) |
| Backend | ![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white) |
| Database | ![PostgreSQL](https://img.shields.io/badge/PostgreSQL_16-4169E1?logo=postgresql&logoColor=white) |
| Infra | ![Ubuntu](https://img.shields.io/badge/Ubuntu-E95420?logo=ubuntu&logoColor=white) ![Nginx](https://img.shields.io/badge/Nginx-009639?logo=nginx&logoColor=white) ![systemd](https://img.shields.io/badge/systemd-333333?logo=linux&logoColor=white) |
| Data | KRX, 한국투자증권(KIS) API, OpenDART, 뉴스 · 해외 시세 |

---

## 프로젝트 구조

```
OUTOFSTOCK/
├── frontend/                    # 웹 화면
│   ├── index.html               # 홈 · 주간 추천 · 로그인 · 투자 성향 분석
│   ├── learn.html               # 용어사전 · Daily Quiz
│   ├── practice.html            # 매매기법 실습 게임
│   ├── invest.html              # 가상시장 모의투자
│   ├── stocks.html              # 실시간 종목
│   ├── news.html                # 기사
│   ├── avatar.html              # 아바타
│   ├── mypage.html              # 마이페이지 · 학습 기록 · 친구
│   ├── assets/                  # 화면 스크립트 · 캐릭터 데이터 · 용어 데이터
│   └── data/                    # 모의투자 시즌별 가상 주가 데이터
│
├── backend/                     # FastAPI 서버
│   ├── app/
│   │   ├── api.py               # REST API 라우터
│   │   ├── webui.py             # 웹 화면 서빙
│   │   ├── collectors/          # 데이터 수집 (KRX · KIS · OpenDART · 뉴스 · 해외 시세)
│   │   ├── services/            # 기능별 로직
│   │   │   ├── auth · account                    # 회원가입 · 로그인
│   │   │   ├── survey · products                 # 투자 성향 분석 · 상품 추천
│   │   │   ├── yangbong_picks · recommend        # 주간 5종목 추천
│   │   │   ├── vmarket · invest · ledger         # 가상시장 모의투자
│   │   │   ├── practice · practice_records       # 매매기법 실습 기록
│   │   │   ├── learn · quiz                      # 용어사전 · Daily Quiz
│   │   │   ├── avatar · wallet · friends         # 아바타
│   │   │   └── market · quotes · news_home · themes  # 뉴스
│   │   ├── security/            # 인증 · 세션 · 권한 · 보안 정책
│   │   ├── models/              # 수익률 · 리스크 모델
│   │   └── data/                # 용어 · 퀴즈 · 아바타 아이템 데이터
│   ├── sql/                     # DB 스키마 · 마이그레이션
│   ├── tests/                   # 테스트
│   ├── deploy/                  # 배포 설정
│   ├── docs/                    # 설계 결정(ADR) · 보안 · 성능 문서
│   ├── Dockerfile · docker-compose.yml
│   └── .env.example             # 환경변수 (실제 키 제외)
│
├── recommend/                   # 주간 추천 알고리즘 · 2022~2026 과거 시뮬레이션
├── tools/
│   ├── virtual_stock_generator.py   # 통계 기반 모의투자 가상 종목 생성기
│   └── fetch_real_prices.py         # 실습 실전 모드용 실제 시세 받기 (아래 '데이터 출처 및 저작권' 참고)
└── docs/
    ├── 발표자료/                # 발표 자료 · 논문
    └── 자료조사/                # 용어 · 투자성향 설문 · 추천 상품 · 실전 사례 분석
```

---

## 데이터 출처 및 저작권

### 저장소에 포함하지 않은 데이터

실습하기의 **과거 데이터로 실전연습하기** 모드는 실제 시세를 사용합니다.
다만 실제 시세 원본은 아래 이유로 공개 저장소에 올리지 않았습니다.

| 파일 | 내용 | 출처 | 제외 사유 |
| --- | --- | --- | --- |
| `frontend/assets/real-stocks-kr.js` | 국내 8종목의 실전 구간 일봉 | KRX (FinanceDataReader) | KRX 시장정보는 거래소 승인 없이 재배포할 수 없음 |
| `frontend/assets/soxl-real.js` | SOXL 일봉 (2016~2026, 수정주가) | Yahoo Finance (FinanceDataReader) | Yahoo Finance 이용약관상 데이터 재배포 금지 |

- 운영 서버에서는 실전 모드가 정상 동작합니다.
- 저장소를 받아 직접 실행할 때는 아래 명령으로 같은 데이터를 받을 수 있습니다. 파일이 없으면 실전 모드 구간만 숨겨지고 나머지 기능은 그대로 동작합니다.

```bash
pip install finance-datareader
python tools/fetch_real_prices.py
```

### 서비스가 이용하는 외부 데이터

시세 · 공시 · 뉴스는 서버가 각 제공처의 공식 API로 그때그때 조회하며, 원본을 저장소에 포함하지 않습니다.

| 데이터 | 제공처 |
| --- | --- |
| 국내 시세 · 종목 정보 | KRX Open API, 한국투자증권(KIS) Open API |
| 공시 · 재무 정보 | OpenDART (금융감독원) |
| 경제 지표 · 환율 | 한국은행 ECOS, 한국수출입은행 |
| 해외 시세 · 공시 | Finnhub, SEC EDGAR |
| 뉴스 | 언론사 RSS (기사 제목 · 링크만 표시, 본문은 원문 링크로 연결) |

### 직접 만든 데이터

- **모의투자 가상 주가** (`frontend/data/`, `backend/app/data/vmarket/`): 통계를 바탕으로 생성한 가상 데이터이며 실제 시세가 아닙니다.
- **주간 추천 과거 결과** (`recommend/history_baskets_2022_2026.csv`): 추천 알고리즘을 과거 구간에 적용해 계산한 수익률입니다.
- **용어 · 퀴즈 · 투자 성향 문항**: 팀이 직접 작성했습니다.


---

## 팀원

| 이름 | 역할 | 담당 업무 |
| :---: | :---: | --- |
| 이진형 | 프론트엔드 총괄 | 서비스 UI/UX 설계 및 전체 페이지 구현<br>공통 레이아웃 · 디자인<br>매매기법 실습 및 복기 콘텐츠 개발<br>주간 추천 알고리즘 설계 및 검증 |
| 유주원 | 백엔드 총괄 | FastAPI 서버 및 PostgreSQL DB 설계 · 구축<br>데이터 수집 및 품질 검사<br>회원 · 가상머니 시스템, 보안<br>서버 배포 및 백업 운영 |
| 최준혁 | 프론트엔드 지원 | 용어사전 페이지 및 용어 데이터 구성<br>Daily Quiz 문제은행 · 투자 성향 분석 문항 설계<br>가상시장 모의투자 설계 및 구현 |
