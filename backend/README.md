# 주식 데이터 백엔드 (stock-backend)

주식 시세·재무·뉴스를 수집·가공해 DB에 쌓고 API로 서빙하는 백엔드. 종목 검색·장기 모의투자·과거 데이터 실습·투자성향 테스트·가상머니 지갑 포함.
**담당 경계**: 데이터·DB·보안·성능(주원) / 화면 디자인(팀 — 확정본 고정). 팀 확정 화면 다섯 장(`web/`)을 이 서버가 같은 출처로 내보내고
실제 계정 데이터로 움직이게 한다(겉모습은 그대로 — ADR-0021).

## 2026-10 확장 — 7개 축(설계 근거는 `docs/adr`, 검증 순서는 `docs/verification.md`, 면접 준비는 `docs/interview_qa.md`)
| 축 | 한 일 | 근거·결과 |
|---|---|---|
| 1 장애 대응 | 적재 품질 게이트(출처·완전성·이력·적시성·상식) + 웹훅·텔레그램·메일 알림(중복 억제·비밀값 가림) + 장애 보고서 | ADR-0001 · `docs/incidents/` |
| 2 데이터 플랫폼 | `analytics` 스키마: 전 종목 × 10년 연도 파티션, 재개 가능한 COPY 적재, SQL 마이그레이션, 분석 SQL 9개(골라보기 화면은 10-20 기각으로 제거, 분석 계층·이름 있는 SQL 은 배치·CLI 가 사용) | ADR-0002·0003·0004 · `docs/perf/explain_report.md`(최신일 504ms → 0.03ms) |
| 3 정보보호 | DB 계정 3분리 + 소유자 권한 함수, 감사 로그 해시 체인, AES-256-GCM 열 암호화, 개인정보 최소화, 기본 CSP, 보안 점검 | ADR-0005·0006 · `docs/security/` |
| 4 신용위험 | DART 다년 재무 + 시장 피처 → 1년 부실 징후 PD, 공시일 기준(PIT)·시간순 분할·보정, Z''·Merton 비교 | ADR-0007 · `docs/models/credit_risk_card.md` |
| 5 예측 검증 | 5거래일 상대수익 — 무작위 K-겹 vs 시간순+퍼지 누수 크기, 기준선·거래비용 | ADR-0008 · `docs/models/return_validation.md` |
| 6 CI | GitHub Actions: lint(ruff·화면 스크립트) · test(PostgreSQL 16, KST/UTC) · security(pip-audit·bandit·숨은 문자) | `.github/workflows/ci.yml` |
| ~~화면·테마~~ | 시장 분석·종목 토론·테마·게임·단기 시뮬·아바타(예전 상점 — 10-31 '양봉이 꾸미기'로 새로 설계, ADR-0022) — **2026-10 팀 회의에서 기각**, 화면은 스크린샷 판으로 고정. 코드·표·데이터까지 제거(V009 — 목적 달성 후 파기), 경로 175 → 120 | ADR-0009(폐기) |
| 수정주가(점검 수정) | 액면분할·무상증자 날을 −80% 로 보던 결함 — 권리 이벤트 계수(원 가격 보존), 결측 봉·불량 등락률 거르기, 모의투자 수량 반영 | ADR-0010 |
| **8 화면 연동**(10-27~31) | 팀 확정 화면(GitHub `OUTOFSTOCK` — index·learn·news·practice·mypage)을 같은 출처로 서빙 · 실시간 종목(국내·해외) · 양봉이 꾸미기(6부위×5등급, ADR-0022) · 공용 런타임(신뢰 타입·data-on·쿠키 세션·추가 인증·SSE) · 예시 데이터·localStorage → 실제 API · 퀴즈 서버 재채점·정답 보상(하루 1번) · 실습 기록(범위 검증·보상 분리) · '로그인 유지'(세션 쿠키) · 접근성 axe 0 | ADR-0021·0022 · E2E 37항목 |
| **9 운영 복원력·탈취 내성**(11-02) | 금감원 IT 안정성 가이드라인의 성능 관리를 서버 안에 — 임계치 4단계(정상·주의·경계·심각) · 우선순위 대량접속제어(혼잡하면 공개 조회부터 503, 핵심 거래 보호) · 성능관리보고서 자동 기록 · 연결 풀 빠른 실패 · 기능별 비상 차단(원장 이상 시 자동, 재개는 2인 승인) · 요청당 SQL 예산(송금 41 → 33, 조회는 쓰기 0) · 세션을 브라우저 비추출 키에 결속(DPoP — 쿠키를 복사한 다른 기기는 송금·갱신 불가) · 자유 입력 DLP | ADR-0023·0024 · 과부하 시 핵심 거래 p95 899 → 354ms · E2E 39항목 · 제로트러스트 자가 점검 이행 14 → 17 |
| **10 자율보안 — 수준진단·위험·증빙**(11-02 2차) | 금융보안원 「금융보안 수준진단 프레임워크」(7개 분야 45개 항목)로 스스로 진단 — 근거(문서·코드·시험·운영 점검·자동 대응·실측)를 기계가 확인해 성숙도 판정, 결과는 추가만·감사 체인 고정, 매월 재진단·퇴보 경보 · 자산 5종 목록·C·I·A 중요도 · 위협 35개 가능성×영향(고유·잔여)·처리·담당 · 파일 무결성 감시(웹셸·승인 안 된 변경) · 퇴직·휴면 관리 권한 회수(감독규정 제13조 ①11호) · 사고 대응 모의훈련 5개 시나리오 | ADR-0025 · 성숙도 2.93 → 3.21 · 목표 미달 9 → 0 · CSF 2.0 67 → 76/106 · `docs/security/self_assessment.md` |
| **11 IT 내부통제 — 변경·장애·관제·점검**(11-02 3차) | 금감원(6.24 배포 전 검증 누락)·금융위(10.14 24시간 관제 공백) 지적을 코드로 — **배포 게이트**(지금 배포된 코드가 새 설치본을 서비스 중지 전에 검사: 구문·적용된 마이그레이션·정책, 사전 영향 분석, 요청자 ≠ 승인자, 승인은 설치본 해시에 묶임, 배포 금지 시간, 긴급 변경 사후 검토) · 1분 합성 모니터링 → **장애 자동 판정·보고 대상(시행세칙 제7조의4: 30분, 인지 + 24시간)**·보고서 초안(사건 전 배포 = 변경시점 후보) · 경보 확인·15분 상향·MTTA·CEF 내보내기 · **취약점 자가 점검**(침투테스트 가이드라인 4단계·ATT&CK 22개 — 첫 실행에서 IDOR 1건 발견·수정) · 모의훈련 7개 | ADR-0026 · 성숙도 3.21 → 3.26 · CSF 2.0 76 → 80/106 · `docs/security/compliance_mapping.md` §2-3 |
| **12 DB 저장·수명주기·진단**(11-03) | 운영 1년 규모(뉴스 100만 등)를 합성해 전/후 실측 — **타입 정리**(날짜·시각 문자열 → date·timestamp, 원화 가격 → 정수 — 서비스가 받는 값 모양은 연결 로더로 그대로, 연결 시간대 Asia/Seoul 고정) · 재작성 마이그레이션(깨진 값은 격리소에 원값·대사 실패 시 전체 롤백) · 기사 주소 인덱스 291 → 30MB(md5 키) · 겹치는 인덱스 7개 제거 · **수명주기**(뉴스·검색 13개월 → 압축 보관·SHA-256 검증 후 정리·복원) · **DB 진단**(디스크 소진 예측·비대화·XID·느린 쿼리) · vm.sh update 가 서버 설정 자동 적용 · 백업 zstd(7.7배 빠름) | ADR-0027 · **1,182 → 807MB(−32%)** · 3년 뒤 뉴스·검색 2.77 → 0.69GB · `docs/perf/db_space_2026-11-03.md` · `docs/db/standards.md` |
| **13 팀 변경 반영**(11-09) | 팀 저장소 `4c63542..d4fd73f` 를 3방향 병합 — **모의투자(가상 시장 시즌)를 서버 권위로**(팀 시안은 잔액·체결·정산이 localStorage, 미래 봉이 든 시즌 파일을 통째로 받음 → 서버 시계로 공개된 봉까지만·체결가 = 그 봉 종가·옛 봉 409·참가금/정산은 지갑 원장·복식부기·RLS) · **양봉이 주간 추천**(팀 알고리즘 원본 해시 고정 + 분석 계층 KRX 수정주가·코스피 보통주·업종 KSIC · 끝난 주 따라잡기 배치 · 247주 백테스트 시드 · `/yangbong/weekly`) · 실습 원칙 매매·국내 실전 구간 · 용어 문장 풀이 · 첫 접속 투자 유의 안내 | ADR-0028 · 위협 T-41·T-42 · E2E 48항목 · 시험 +34 |
| **7 제로트러스트·내부 고도화**(10-20~22) | 세션(10분 토큰·회전·재사용 탐지·HttpOnly 쿠키·기기 결속) · 정책 PDP/PEP(기본 거부) · 관리자 JIT·TOTP·2인 승인·기기 인증서 · FORCE RLS · 봉투 암호화 · 복식부기 원장·대사 · FDS · 아웃박스 · SSE · CSP nonce·Trusted Types · 탐지 규칙·자동 대응 · 암호화 백업·복구 훈련·PITR · 망 분리·읽기 전용 컨테이너·systemd 샌드박스(노출 9.0→1.2)·egress 3중 | ADR-0011~0020 · `docs/security/README.md` · 부하 시험 185 RPS 오류 0 |

## 스택
- Python · **SQLAlchemy Core** + **PostgreSQL 16 전용**(psycopg3, SQLite 미사용, `DATABASE_URL`) · **FastAPI** · APScheduler 3.x · pytest
- 데이터원: KRX OpenAPI(국내 일별 시세·시총) · KIS(실시간·당일 종가·지수) · OpenDART(재무) · Google News RSS(뉴스) ·
  해외 FinanceDataReader·Finnhub·SEC EDGAR·수출입은행(환율)

## 폴더 구조
```
app/
  api.py               FastAPI 경로(153개 — 모두 정책 동작 선언, 화면 3 포함)
  webui.py             웹 화면 서빙 — 허용 목록 페이지 · 요청마다 nonce(인라인 스크립트에만) · 화면 CSP · 자산 캐시
  config.py            환경 설정(.env 자동 로드) · clock.py 한국시간 · logging_conf.py 구조 로그(JSON)·비밀값 가림 · redact.py
  db.py                엔진·스키마(타입 표준 — ADR-0027)·트랜잭션 헬퍼·무손실 마이그레이션(타입 정리: 격리·대사·롤백) · 연결 설정
                       (시간대 Asia/Seoul·date/timestamp 로더) · 연결을 꺼낼 때 RLS 문맥 설정
  crypto.py            봉투 암호화(AES-256-GCM, 값마다 DEK, AAD 행 결속, 키 링 교체)
  migrations.py        SQL 마이그레이션 실행기(체크섬) — sql/migrations/V001~V019
  security/            제로트러스트 계층: context(요청 ID·기기) · pep/pdp + policy.json(기본 거부) · sessions · totp · rbac(JIT·2인 승인)
                       · rls · keys(HKDF) · passwords · events(탐지 규칙·자동 대응) · egress(나가는 연결 허용 목록·서킷 브레이커) · classification(L1~L4)
  services/            auth · wallet · ledger(복식부기·일 마감) · fds · outbox · transfer · stream(SSE) · audit(해시 체인·외부 증인) · privacy(열람 점검·파기)
                       · pseudonymize · contracts·quality(데이터 계약·품질 게이트) · monitoring · alerts · reco_engine·signals(추천) · risk·soundness·credit
                       · invest · practice · learn · survey·products · friends·chat · news_home·themes(뉴스 감성) · search · overseas · adjust(수정주가)
                       · ilm(데이터 수명주기) · dbhealth(DB 진단·용량 예측) · dbtune(서버 설정 권장값)
                       · vmarket(모의투자 가상 시장 시즌 — 서버 권위) · yangbong(주간 추천 배치·API) + yangbong_picks(팀 알고리즘 원본) …
  collectors/          KRX·KIS(REST·웹소켓)·DART·뉴스 RSS·지수·해외(FDR·Finnhub·EDGAR)·환율·ECOS — 모든 외부 호출은 egress 검사를 지난다
  models/              신용위험·수익률 검증·FDS 합성 평가
web/                   웹 화면(팀 확정 디자인 — index·learn·news·practice·mypage·invest + 연동 때 더한 stocks·avatar) · assets/(그림·용어 사전·퀴즈·SOXL·국내 실전 구간 데이터·양봉이 아이템·모의투자 차트)
app/data/              서버 사본 — 용어·퀴즈·꾸미기 아이템 · vmarket/(모의투자 시즌 파일 — 화면에 내보내지 않음) · yangbong/(팀 백테스트 247주)
  assets/oos/runtime.js  공용 런타임: 신뢰 타입 · data-on 해석기(등록 함수만) · api()(CSRF·갱신·추가 인증) · 헤더 · SSE
sql/migrations/ · sql/analytics/(이름 있는 분석 쿼리 9개)
deploy/                vm.sh(VM: install·update·units·harden·backup·restore·drill) · deploy.sh(compose) · nginx*.conf · nginx/(compose edge)
                       · egress-proxy/(squid) · sql/(roles.sql·pg_hba_tls.conf) · pki/(사설 CA·기기 인증서) · backup/(PITR) · outofstock-*.service(견본)
tools/                 dr.py(백업·복구 훈련) · security_check.py(설정 기준선) · db_roles_check.py · security_scan.sh · egress.py(프록시 설정 생성)
                       · make_installer.py(VM 설치본) · check_lock.py · e2e_web.py(브라우저 E2E) · loadtest.py · overload_test.py(대량접속제어 비교) · pg_top_queries.py
                       · db_lab.py(DB 용량 실험실 — 1년 규모 합성·크기·쿼리 전/후)
                       · sync_web.py(팀 저장소 화면 → web/) · build_learn_terms.py·build_daily_quiz.py·build_avatar_items.py(화면 데이터 → 서버 사본) · check_frontend.py · …
docs/                  security/(수준진단 자가진단·자산·위험 등록부·제로트러스트·위협 모델·규정 대응·개발보안 69·내부 관리계획·검증 보고서) · adr/(0001~0028) · db/(DB 표준) · dr/ · perf/ · incidents/drills/
                       · data/(데이터 사전·계보 — 자동 생성) · models/ · incidents/ · verification.md · interview_qa.md
tests/                 pytest 955개(배포 설정 정책 · 실제 복원 훈련 · 웹 화면 계약 · 수준진단 근거 무결성 · 배포 게이트·장애 판정·자가 점검 · DB 타입 정리·수명주기 · 모의투자 서버 권위·양봉이 추천 포함)
.github/workflows/ci.yml  lint · test(KST·UTC) · least-privilege · security · image · e2e
scheduler.py · run.py · Dockerfile · docker-compose(.prod).yml · requirements.txt(범위) · requirements.lock(해시 고정)
```

## 설치 & 실행
```bash
docker compose up -d db       # PostgreSQL 16 (localhost:5432, oos/oos/outofstock + outofstock_test) — 또는 OS 의 PostgreSQL
pip install -r requirements-dev.txt   # 실행 + 시험 도구(운영 이미지는 requirements.lock 해시 고정)
install -m 600 .env.example .env   # 키 채우기(.env 는 자동 로드) — 권한 600
python run.py initdb          # 스키마·무손실 마이그레이션(업데이트 후에도 1번 권장 — API 서버도 시작할 때 같은 작업을 멱등하게 함)
python run.py demo            # 빈 DB 를 오프라인 표본 데이터로 (실데이터: python run.py real) — 실데이터가 있으면 거부
uvicorn app.api:app --reload  # 서버 기동 → 화면: / (learn·news·practice·invest·mypage·stocks·avatar .html) · API문서: /docs
pytest -q                     # 테스트(outofstock_test DB) — 952 passed · 3 skipped(최소 권한 모드 전용) (CI 와 같음)
```
`python run.py` 만 치면 사용법만 나온다(예전엔 곧바로 demo 가 돌았다). 감성·테마만 다시 계산: `python run.py recompute`.
VM 배포(Docker·systemd·학교 VM tmux): **DEPLOY_UBUNTU.md**.
**팀원 PC 에 VM 실데이터 옮기기**(API 키 없이 같은 화면): VM 에서 `python run.py snapshot-export` →
만들어진 `snapshots/…jsonl.gz` 를 전달 → 팀원 PC 에서 `python run.py initdb` → `python run.py snapshot-import <파일> --yes`.
시세·지수·재무·뉴스·해외 표만 바뀌고 계정·지갑·채팅·로그는 그대로(파일에도 계정·토큰·KIS 토큰은 들어가지 않음).

## 주요 API (경로 153개 — 전체는 `/docs`, 운영에선 숨김)
| 묶음 | 경로 | 비고 |
|---|---|---|
| 인증·세션 | `POST /auth/register·login·refresh·logout·logout-all·step-up·change-password` · `GET /auth/sessions`·`DELETE /auth/sessions/{id}` · `/auth/mfa/*` | 웹은 `X-Client: web` → HttpOnly 쿠키 + CSRF(`remember`=로그인 유지, 기본은 세션 쿠키), 앱은 Bearer. 10분 접근 토큰 + 회전 갱신 토큰 |
| 실시간 | `GET /events` | SSE — 지갑·채팅·친구·세션 알림(커밋된 것만) |
| 시세·종목 | `/indices` · `/market/stocks` · `/stocks/{t}/detail·ohlc·news·peers·risk·var-backtest·soundness` · `/search/suggest·popular`·`POST /search/log` | 국내 종목·종목 상세 |
| 해외 | `/overseas/indices·list` · `/overseas/{symbol}/detail·ohlc` | |
| 뉴스 | `/news` · `/news/home` · `/api/news/home` | 팀 news.html 계약 |
| 추천·성향 | `/yangbong/weekly`(홈 양봉이 주간 추천 — 팀 알고리즘, 수익률은 소수) · `/reco/top5·top5/review·top5/backtest·model·model/monitor·model/challengers·products` · `/survey/questions`·`POST /survey/submit` | 교육용 예시 · 투자 권유 아님 |
| 화면 | `/` · `/{learn,news,practice,invest,mypage,stocks,avatar}.html`(허용 목록·요청마다 nonce) · `/assets/*` · `/images/*` · `/app`→`/` | 팀 확정 디자인(web/) + 연동 때 더한 실시간 종목·양봉이 꾸미기 |
| 학습·실습 | `/learn/*` · `GET·POST /learn/quiz`(서버 재채점·하루 1번) · `GET·POST /practice/records`(웹 실습 기록 — 범위 검증) · `/practice/*` | |
| 지갑·돈 | `/wallet` · `POST /transfer`(멱등키·FDS·고액 추가 인증) · `/attendance` · `/iap/*` · `/pay/toss/*` · `/invest`·`POST /invest/buy`·`/invest/{id}/sell`·`/invest/risk` | 복식부기 원장에 분개 |
| 모의투자(가상 시장 시즌) | `/vmarket/season[?id=]`(공개 — 서버 시계로 공개된 봉까지만, 끝난 시즌은 전체·장세) · `/vmarket/me` · `POST /vmarket/join`(참가금 = 지갑 출금) · `POST /vmarket/order`(현재가 = 그 봉 종가, `bar` 다르면 409 · `Idempotency-Key`) · `POST /vmarket/settle` | 월~금 한 시즌·평일 30분마다 봉 1개 · 주말 정산(매일 00:05 스케줄러) · RLS · ADR-0028 |
| 양봉이 꾸미기 | `/avatar/catalog` · `/avatar/me` · `POST /avatar/buy`(아이템 id + 화면이 보여 준 가격 → 서버 가격과 다르면 409) · `POST /avatar/equip` | 6부위 × 5등급(1만~1억, 10배씩) — 산 기록·차감 한 트랜잭션, 부위당 착용 1개(DB 부분 유일 인덱스) |
| 소셜 | `/friends/*` · `/users` · `/chat/*`(서로 친구만, 본문 암호화) | |
| 내 정보 | `/me` · `/me/logs/{kind}` · `/me/badges` · `/me/survey` · `DELETE /me`(추가 인증) · `/watchlist` | |
| 관리(JIT 활성 필요) | `/admin/me·elevate·health·metrics·audit·audit/verify·security/events·security/digest·pii-access·users/{u}/pii·ledger·fds·data-quality·policy·roles…` | 사유 + TOTP 로 30분 활성, 열람은 목적 기록 |
| 운영 관제·비상 차단 | `/admin/capacity`(부하 단계·성능관리보고서) · `/admin/controls` · `POST /admin/controls/{기능}/pause·resume` · `POST /admin/controls/requests/{id}/approve·reject` | 멈춤은 한 사람이 즉시, 재개는 다른 관리자 승인 · 원장 이상 시 자동 차단 |
| 자율보안·감사 수검 | `/admin/assessment`·`POST /admin/assessment/run`·`/admin/assessment/{id}`(수준진단 — 감사·보안 역할) · `/admin/risk`(자산·위험 등록부) · `/admin/integrity`(파일 무결성) · `POST /admin/users/{아이디}/offboard`(인사조치 일괄 회수) · `DELETE /admin/security/ip-blocks/{ip_hash}` | 진단 결과는 추가만·다이제스트 감사 체인(`intact` 재대조) · 기준선은 소유자 계정만 기록 |
| IT 내부통제 | `/admin/changes` · `POST /admin/changes/{id}/approve·reject·review`(요청자 ≠ 승인자 · 긴급 변경 사후 검토) · `/admin/incidents`·`/admin/incidents/{id}`(`?fmt=md` 사고보고서 초안) · `POST /admin/incidents`·`/admin/incidents/{id}`(원인·보고 단계) · `/admin/alerts` · `POST /admin/alerts/{id}/ack` · `/admin/security/export`(CEF·JSON Lines) | 조회는 운영·감사·보안 역할, 승인·사건 기록은 운영·보안 관리자(ADR-0026) |
| DB 운영 | `/admin/db`(크기·증가 추세·디스크 소진 예측·판정·설정·느린 쿼리·수명주기) · CLI `run.py db-report`·`ilm-status`·`ilm-archive`·`ilm-restore`·`ilm-verify` | 조회만(운영 역할) — 보관·정리는 스케줄러(매월 2일)·CLI(ADR-0027) |
| 운영 | `/health`(공개 — 내부 상태 숨김) · `POST /csp-report` | |

## 업데이트 후(2026-10-01) — 한 번만
```bash
python run.py initdb        # 새 컬럼·표(지수 출처/기준일, 지수 이력, 추천 스냅샷, 재무 대차대조표 항목) 무손실 추가
python run.py fetch-index   # 코스피·코스닥을 바로 받아 출처·기준일 확인(실패한 소스와 이유도 출력)
python run.py fetch-dart    # (선택) 부실예측 Altman Z'' 용 대차대조표 항목까지 다시 수집
```

## 문제 해결
- **코스피·코스닥이 비어 있거나 3,124.68 같은 상수로 보임** → `python run.py fetch-index` 결과의 출처를 본다.
  `KRX … 401/403` 이면 KRX OpenAPI 에서 **'KOSPI 시리즈 일별시세정보'·'KOSDAQ 시리즈 일별시세정보' 이용신청**이 필요(키와 별개).
  승인 전에도 KIS(키 있으면 실시간)·야후로 대체된다. `/health` 의 `indices`·`issues`(`index_sample`·`index_stale`·`krx_index_denied`)로 확인.
- **검색 엔터가 안 먹던 문제** → 자동완성 결과가 그려지기 전·한글 조합 중 엔터를 무시하던 버그. 이제 즉시 조회해 1순위로 이동.
- **입력값이 이상하면 500 이 나던 문제** → 길이 초과·NUL 문자·정수 범위 초과는 이제 422(`{"detail": ...}`), 내부 SQL 은 노출하지 않는다.
- **장기 모의투자 매도를 연타하면 대금이 두 번 들어오던 문제** → 포지션 행 잠금(`SELECT … FOR UPDATE`)으로 한 번만 체결.
- **동시 송금 대부분이 '동시 요청 충돌(OCC)'로 실패하던 문제** → 지갑 행 잠금 + 송금은 두 지갑을 이름순으로 잠가 교착 없이 모두 처리.
- **Windows 에서 `python run.py real > log.txt`(또는 Git Bash)로 돌리면 중간에 죽던 문제** → cp949 콘솔에 없는 문자(—·✓)를 `?` 로 바꿔 출력.
- **API 키가 로그·공개 `/health` 에 찍힐 수 있던 문제** → DART·Finnhub·수출입은행은 키를 URL 로 보내 실패 메시지에 키가 섞였다. 로그·DB 메모·응답에서 `***` 로 가림.
- **52주 최고/최저가 몇 년 전 가격이거나 1년 차트가 중간에 몇 년을 건너뜀** → 실습용 과거 구간이 섞이던 문제. 이제 최근 연속 구간·365일만 사용.
- **실데이터인데 PER 14.2·배당 2.1% 같은 값이 보임 / 샘플일보·가상경제TV 뉴스가 보임** → 예전에 `run.py demo` 를 돌린 DB 의 데모 재무·뉴스가 남아 있던 것.
  서버를 다시 켜거나 `python run.py initdb`(실데이터 모드일 때) 하면 자동 정리. 실제 재무는 `python run.py fetch-dart`. PER 옆 '표본'은 데모 값이라는 뜻.
- **장 마감 후 종가가 실제와 다름(실시간 수집기를 장중에 껐을 때)** → 멈춘 시각의 마지막 체결가가 '종가'로 남던 문제. 이제 마감 뒤엔 공식 종가 봉 우선(15:45 `fetch-latest`·23:00 `real` 이 받아 옴).
- **송금 버튼을 빠르게 두 번 누르면 두 번 송금됨** → 진행 중 재클릭 무시 + `Idempotency-Key`(같은 키 재전송은 1회). API 를 직접 부를 땐 헤더에 키를 넣을 것.
- **토스 결제는 됐는데 가상머니가 안 들어옴** → 승인 API 가 동시에 두 번 불릴 때의 경합. 주문 단위로 줄 세우고, 같은 주문을 다시 승인하면 결제 조회로 확인해 적립.
- **VM 에서 지갑·실습·채팅 기록 시각이 9시간 느림(새벽엔 하루 전 날짜)** → 서버 시간대(UTC)를 따르던 문제. 이제 한국시간 고정(`app/clock.py`).
  이미 저장된 예전 기록은 그대로이고, VM 이 UTC 였다면 업데이트 직후 로그인이 한 번 풀릴 수 있다(다시 로그인).
- **`python run.py demo` 가 거부됨** → 실데이터 DB 보호(정상). demo 는 시세를 표본으로 덮고 공개 비밀번호 데모 계정을 만든다. 정말 덮으려면 `--force`.
  (인자 없이 `python run.py` 는 이제 사용법만 출력 — 예전엔 곧바로 demo 가 돌았다)
- **적자 기업의 영업이익·순이익이 비어 있음** → DART 계정명 '영업이익(손실)'·'당기순이익(손실)'을 못 읽던 문제. `python run.py fetch-dart` 로 다시 수집(연결재무 우선, 없으면 별도 — 섞지 않음).
- **창 크기를 바꾸거나 휴대폰을 돌리면 차트가 화면 밖으로 넘침** → 차트가 처음 폭에 고정되던 문제. 이제 카드 크기를 따라 다시 그림.
- **화면을 켜 두기만 해도 요청이 계속 나감** → 장이 닫혀 실시간 값이 없을 땐 30초마다만(예전 5초), 다른 탭에 있으면 멈췄다가 돌아오면 바로 갱신.
- **가입이 "비밀번호는 8~128자"로 거부됨** → 화면·비밀번호 변경과 같은 규칙으로 맞춘 것(예전엔 가입 API 만 1자도 받았다). 기존 계정은 그대로 로그인된다.
  비밀번호 해시는 PBKDF2-SHA256 600,000회(OWASP 2023) — 예전 계정은 다음 로그인 때 자동으로 새 기준 저장(재설정 불필요).
- **모델 검증 화면에서 "계산 요청이 많아요(429)"** → 백테스트·모형 비교를 값만 바꿔 연달아 새로 계산시키면 IP 당 1분 12번까지(`HEAVY_MISS_PER_MIN`).
  이미 계산된 결과(10분 캐시)와, 누가 계산 중인 같은 결과를 기다리는 요청은 제한 없음(한 IP 뒤 여러 명이어도 됨). 값은 허용 값 중 가장 가까운 값으로 맞춰진다(응답 `params`).
- **"요청 본문이 너무 커요(413)"** → 1MB 넘는 요청은 거절(`MAX_BODY_BYTES`) — VM 은 nginx 없이 바로 받으므로 큰 요청 몇 개로 메모리가 바닥나던 문제를 막음.
- **`vm.sh logs api` 에 앱 로그가 안 보이거나 시각·모듈이 없음** → API 서버가 앱 로그 설정을 안 하던 문제. 이제 `10-02 15:45:01 | INFO | app.api | …` 형식,
  시각은 서버 시간대와 무관하게 한국시간. 실시간 수집기는 장외 대기를 1분마다 찍지 않고 1번만, KIS 가 거절하면(`ALREADY IN USE appkey` 등) 이유를 경고로 남김.
- **`/health` 에 `last_pipeline_partial`(실행 기록 `PARTIAL`)** → 23:00 `run.py real` 의 일부 단계만 실패(나머지는 반영). 어느 단계인지는 관리자 헬스의 `last_run.note`
  (예: `실패: DART 재무`). 예전엔 한 단계(KIS 토큰·DART corpCode 등)만 실패해도 뒤 단계가 전부 건너뛰어졌다. 재시작으로 끊긴 실행은 `RUNNING` 으로
  남지 않고 다음 실행·스케줄러 시작 때 `FAILED` 로 정리되며, 가장 최근 정기 실행을 놓쳤으면 스케줄러가 켜질 때 1회 보충 실행.
- **KRX 장애로 23:00 배치가 몇 시간 멈춤** → 연속 3회 실패하면 받은 날짜까지만 반영하고 끝낸다(기존 봉은 지우지 않음). `KRX HTTPS 인증서 확인 실패` 는
  VM 의 시계·CA 인증서(`sudo apt install --reinstall ca-certificates`) 문제 — 키가 평문으로 나가지 않도록 http 로 바꿔 보내지 않는다.
- **실습 시나리오 구간을 다시 받고 싶음** → 실데이터로 한 번 받은 구간은 매일 다시 받지 않는다(과거는 안 바뀜). 강제로: `python run.py fetch-practice --force`.
- **해외 시세·지수가 하루 '표본'으로 바뀜** → 야후/FDR 일시 장애 때 실데이터를 표본으로 덮던 문제. 이제 가진 실데이터를 유지(`run.py real` 출력의 `kept`).
- **KIS 키를 바꾸거나 모의↔실전(`KIS_PAPER`)을 바꿨더니 하루 동안 KIS 호출이 전부 실패** → 예전 토큰을 24시간 재사용하던 문제. 이제 앱키·모의/실전별로 캐시하고
  무효 토큰 응답(EGW00121·EGW00123)이면 1번 새로 받는다.
- **`대소문자만 다른 아이디는 쓸 수 없어요`** → `Alice`·`alice` 처럼 대소문자만 다른 계정을 새로 만들 수 없게 함(친구 찾기·송금에서 헷갈림 방지). 기존 계정은 그대로.
- **`vm.sh update` 끝에 `syntax error` · 뒤에도 예전 파일이 남음** → 실행 중인 vm.sh 가 새 버전으로 덮이며 생기던 표시(업데이트 자체는 완료)와, 덮어쓰기만 하던 문제.
  7차 vm.sh 부터는 둘 다 없음 — 6차에서 올릴 땐 `update` 를 한 번 더 실행하면 정리된다. 새 버전에서 지운 파일(예: 7차의 `legacy.html`·`app/pipeline.py`)을 update 가 정리한다.
  기준은 설치한 릴리스의 파일 목록(`.release-files`)이라 `.env`·`.venv`·직접 만든 파일은 건드리지 않고, 지운 파일은 이전 코드 tar 에 보관(`rollback` 으로 복구).
- **`as_of` 를 줬더니 422** → 날짜는 `YYYY-MM-DD`(예: `2025-01-05`)만 받는다. 예전엔 `2025-1-5` 를 문자열로 비교해 그 뒤(미래) 재무까지 섞였다. NaN·무한대 같은 숫자도 422.
- **오늘 추천 기록(`/reco/top5/history`)이 장 마감 직후 바로 안 생김** → 확정 종가(15:45 KIS 마감 종가 또는 23:00 KRX 적재)로 확인된 날만 기록한다(예전엔 15:35~15:40 가격으로 영구 기록).
  장 마감(15:30) 뒤 기사는 다음 거래일 추천에, 공시는 기준일 전날까지만 쓴다(미래 참조 방지).
- **미국 시세가 주말에도 'LIVE' 였음** → 저장 시각을 체결 시각으로 쓰던 문제. 이제 실제 체결 시각 기준이고, 장 마감 뒤엔 공식 종가가 애프터마켓 체결가보다 우선.
- **틀린 비밀번호를 동시에 여러 번 보내면 시도 제한이 늦게 걸림** → 시도를 먼저 차감하도록 바꿈(동시에 보내도 한도만큼만 검사, 나머지 429). 비밀번호를 바꾸는 순간 옛 비밀번호로
  진행 중이던 로그인은 실패로 끝난다(예전엔 그 틈에 옛 비밀번호로 24시간짜리 토큰이 나왔다). 두 곳에서 동시에 바꾸면 늦은 쪽은 '비밀번호가 방금 바뀌었어요'.
- **탈퇴했는데 인앱결제 기록이 남아 있음** → 같은 영수증으로 새 계정이 다시 충전받지 못하게 영수증 기록만 남기고 계정 이름은 지운다(지갑·채팅 등 나머지는 삭제).
- **데모로 쓰던 DB 를 실데이터로 바꾼 뒤 1년 차트 앞쪽에 이상한 봉 / 실습 진행률 0%** → 실데이터보다 오래된 생성(데모) 봉이 남던 문제. `python run.py initdb`·실데이터 적재 때 자동 정리(실습 구간은 유지),
  진행 중인 실습은 마지막 종가로 이어진다.
- **휴대폰·해외(UTC) 기기에서 뉴스·채팅 시각이 9시간 다름** → 기기 시간대를 따르던 문제. 이제 항상 한국시간.
- **"다른 요청과 겹쳤어요"** → 같은 실습·투자를 두 탭에서 동시에 진행했을 때(한쪽만 반영). 다시 누르면 된다.

## 보안 설정(.env) — 기본값은 개발·데모용, 외부에 공개할 땐 env.prod.example(보안 기준선 통과 견본)
| 변수 | 기본 | 설명 |
|---|---|---|
| `APP_SECRET_KEY` · `DATA_ENCRYPTION_KEY` (+`_OLD`) | (없음) | 마스터 키(HKDF 로 페퍼·IP 가명화·증인 서명 키 파생) · 데이터 키(봉투 암호화 KEK). base64 32바이트, 서로 다르게. 교체: `run.py crypto-rotate` |
| `DOCS_ENABLED` | true | false 면 `/docs`·`/redoc`·`/openapi.json` 숨김(운영) |
| `CORS_ORIGINS` · `TRUSTED_ORIGINS` | (비움) | 다른 출처 화면(쿠키 없이) · 쿠키 변경 요청의 Origin 허용 목록 — 비우면 같은 출처만 |
| `COOKIE_SECURE` · `SECURITY_HSTS` · `SECURITY_CSP` | auto · false · (기본) | HTTPS 면 Secure·`__Host-` · HTTPS 일 때만 HSTS · CSP 는 비우면 엄격한 기본(nonce·Trusted Types) |
| `ACCESS_TOKEN_TTL_SECONDS` · `SESSION_IDLE_SECONDS` · `SESSION_ABSOLUTE_SECONDS` | 600 · 1800 · 43200 | 접근 토큰 · 유휴 · 절대 만료 |
| `ACCOUNT_LOCK_THRESHOLD` · `LOGIN_MAX_FAILS(_PER_IP)` · `REGISTER_MAX_PER_IP` | 5 · 5/100 · 20 | 계정 잠금 · 시도 제한 · 가입 제한 |
| `RATE_LIMIT_BACKEND` | memory | 워커·서버가 여럿이면 `postgres`(한도 공유) |
| `ADMIN_USERS` · `ADMIN_REQUIRE_CLIENT_CERT` | (없음) · false | 관리 역할 **자격** 부트스트랩(활성은 TOTP + JIT) · nginx 관리 기기 인증서 요구(앱이 127.0.0.1 일 때만) |
| `EGRESS_MODE` · `EGRESS_EXTRA_HOSTS` | enforce · (없음) | 나가는 연결 허용 목록 차단/기록 · 추가 호스트 |
| `LOG_FORMAT` · `AUDIT_WITNESS_FILE` | text · (없음) | json(요청 ID 포함, SIEM) · 감사 체인 외부 증인 파일 |
| `AUDIT_RETENTION_DAYS` · `ACCESS_LOG_RETENTION_DAYS` · `PII_LOG_RETENTION_DAYS` | 1095 · 365 · 730 | 감사(권한 내역 3년 하한) · 접속 기록 · 개인정보 열람 기록 |
| `API_STATEMENT_TIMEOUT_MS` | 5000 | API 서비스에만 넣는 쿼리 시간 제한(vm.sh·compose) — 배치는 끊지 않는다 |
| `BACKUP_AGE_RECIPIENTS` · `BACKUP_DATABASE_URL` · `BACKUP_OFFSITE_CMD` | (없음) | 백업 공개키 파일 · 백업 전용 계정(oos_backup) · 원격 사본 명령 |
| `ANALYTICS_DATABASE_URL` · `MIGRATION_DATABASE_URL` | (없음) | 읽기 전용 조회 계정 · 마이그레이션 소유자 계정(업데이트 때만) |

점검: `python tools/security_check.py` — 위 값들을 기준선 24항목과 비교해 FAIL/WARN 과 근거 조항(안전성 확보조치 기준·전자금융감독규정)을 보여 준다.
항상 적용: 보안 헤더 · 모든 경로 정책 집행(기본 거부) · 토큰 해시 저장 · 로그아웃·비밀번호 변경·탈퇴 즉시 폐기 · 요청 필드 길이 제한 ·
로그의 API 키 가림 · 개인 데이터 FORCE RLS · 감사 해시 체인.

## 배포
- **학교 VM(우분투)**: `bash deploy/vm.sh install` 한 번 → 이후 `update 설치본`·`status`·`backup`·`restore`·`drill`·`rollback`.
  서비스는 systemd 샌드박스로 돈다(노출 점수 1.2). `bash deploy/vm.sh harden`(방화벽·SSH·자동 보안 업데이트·커널·DB 접속 기록),
  nginx 를 깔면 앱이 자동으로 127.0.0.1 로, `EGRESS_PROXY=1 bash deploy/vm.sh units` 면 나가는 연결을 허용 목록 프록시로만.
  새 버전 설치본: `python tools/make_installer.py` → `dist/install_outofstock_full.sh`(SHA-256·끝 표식 확인). 자세히: DEPLOY_UBUNTU.md.
- **Docker(운영 오버레이)**: `.env`(앱 설정) + `.env.db`(DB 비밀번호) → `./deploy/deploy.sh` — 최소 권한 계정(oos_app·oos_owner·oos_ro·oos_backup),
  망 4개(공개는 edge 하나), 읽기 전용·capability 없는 컨테이너, egress 프록시. `./deploy/deploy.sh backup`·`status`.
- 수집 자동화: 스케줄러 — **매일 23:00(KST) `run.py real`** + 보안 일일 작업(06:10)·원장 마감(00:20)·데이터 계약(07:50)·아웃박스(30초).
- `/health` 는 DB 에 못 붙으면 **HTTP 503**. 백업·훈련·보안 이벤트 상태는 관리자 헬스(`/admin/health`)에만.

## CI
`.github/workflows/ci.yml` — 작업을 나눠 돌린다(하나가 실패해도 나머지 결과는 보이게). 액션은 커밋 SHA 고정, 토큰은 읽기 권한만.
- **lint**: ruff · shellcheck(배포 스크립트) · 화면 스크립트 문법(`node --check`) · 잠금 파일 = requirements 범위(`tools/check_lock.py`)
- **test**: PostgreSQL 16 + 해시 고정 설치 + `pytest` 전체를 **Asia/Seoul·UTC** 두 번 — 실제 복원 훈련·배포 정책 시험 포함
- **least-privilege**: `roles.sql` 로 계정을 나눈 DB 에서 **앱 계정(oos_app)으로** 전체 시험 + `db_roles_check --strict`
- **security**: pip-audit(잠금 파일) · bandit(app·tools) · 숨은 문자 · 운영 견본 보안 기준선 · SBOM(CycloneDX)
- **image**: 운영 이미지 빌드 → 읽기 전용·capability 0·권한 상승 금지로 띄워 `/health` · compose 운영 오버레이 설정 검증
- **e2e**: 실제 크로미움 — 쿠키 세션·CSRF·갱신·추가 인증·SSE·CSP/Trusted Types·접근성(axe)

## 확장 포인트 (TODO)
- 키 보관을 KMS/HSM 으로(지금은 .env 600) · 일반 사용자 2단계 인증 기본화(FIDO2/패스키)
- 로그를 SIEM 으로(JSON 로그·요청 ID 는 준비됨) · 이미지 다이제스트 고정·서명 검증 · 원격 백업 불변 저장소(객체 잠금)
- 뉴스 감성: 사전 v2(정확도 62.5% → 82.5%) — 더 올리려면 KR-FinBert(`classify_news(prefer_model=True)`)
- FDS: 운영 판정 기록(`fds_decision`)이 쌓이면 규칙 가중치 보정
