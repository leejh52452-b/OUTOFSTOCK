# 검증 절차 — VM 에서 직접 돌려 숫자를 확인하는 순서 (2026-10-24 판)

각 단계의 **명령 → 무엇을 보면 되는지 → 자소서·면접에 옮겨 적을 숫자**. 표본·합성 결과는 '실데이터 결과'라고 쓰지 않는다.
모든 명령은 `~/stock-backend` 에서(`.venv/bin/python` = VM 가상환경). 개발 환경에서 이미 실측한 것(공격 시나리오·배포 경로·독립 점검·정밀 점검·결함 32건)은
[`security/verification_report_2026-10-21.md`](security/verification_report_2026-10-21.md) — 여기는 **내 VM 에서 다시 재는** 절차다.

## 0. 업데이트 · 상태 · 기준선
```bash
bash deploy/vm.sh update ~/install_outofstock_full.sh   # 백업 → 파일 소유권 점검 → 코드 교체(.env·.venv 유지) → 잠금 파일로 패키지
                                                        # → 마이그레이션 → 새 스크립트로 서비스 정의 갱신 → 재시작(실패하면 되돌리는 명령 안내)
bash deploy/vm.sh status                                # 서비스 3개 · 바인딩 · 샌드박스 노출 점수 · egress · 백업/훈련 · 설정 점검 요약
.venv/bin/python tools/security_check.py                # 운영 기준선 24항목 — 근거 조항 표시, FAIL 이면 종료 코드 1
```
확인: 서비스 3개 `active` · 앱이 `127.0.0.1:8000`(nginx 뒤) · `systemd-analyze security` 노출 점수 약 **1.2**(기본 9.x) ·
egress 3중(앱 허용 목록 · squid 프록시 · 커널 직접 연결 차단) · 기준선 **FAIL 0**.

.env 에서 먼저 채울 것(빠지면 기준선이 FAIL/WARN 으로 알려 준다):

| 키 | 값 | 없으면 |
|---|---|---|
| `APP_SECRET_KEY` | `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` | 비밀번호 페퍼·IP 가명화·증인 서명 키 없음(KEY-1 FAIL) |
| `DATA_ENCRYPTION_KEY` | 위와 같은 방법(다른 값) | 민감 값(채팅·토큰·결제 키) 봉투 암호화 꺼짐(KEY-2 FAIL) |
| `BACKUP_AGE_RECIPIENTS` | age 공개키 파일 경로([`dr/README.md`](dr/README.md) '처음 설정') | 백업이 **평문**으로 남는다(DR-1 FAIL · 관리자 헬스 `backup_unencrypted`) |
| `ADMIN_USERS` | 관리 역할 자격을 줄 아이디(활성은 TOTP + 30분 JIT) | 관리 기능 없음 |
| `ALERT_WEBHOOK_URL` 또는 텔레그램·메일 | 알림 채널 | 로그·관리자 헬스에만 남음 |

.env 를 바꾼 뒤에는 `bash deploy/vm.sh restart`. 서버 기본 보안(방화벽·SSH·자동 업데이트)은 `bash deploy/vm.sh harden`(한 번).

## 1. 장애 대응(ADR-0001)
```bash
.venv/bin/python run.py fetch-market      # 마지막 줄: '적재 검증: ok — 기대치 충족'
.venv/bin/python run.py quality           # 항목별 ok/warn/fail
.venv/bin/python run.py dq                # 데이터 계약(데이터셋별 통과/전체, 위반 항목) — 매일 배치에도 포함
.venv/bin/python run.py alert-test        # 설정한 채널로 시험 알림이 오는지
curl -s 127.0.0.1:8000/health             # 공개 헬스는 status·issues 만(세부는 관리자 헬스 /admin/health)
.venv/bin/python tools/reco_check.py      # 적격 종목 수 > 0, 성향별 Top 5
```
확인: 유니버스 150종목 이상, `quality` 의 `kr_market` 이 ok, 데이터 품질 점수, DART 단계 실패 시 상태 코드 의미가 출력되는지.
기록: 장애 인지까지 걸린 시간(이전: 사용자 문의 → 이후: 배치 당일 알림), 품질 게이트·데이터 계약 항목 수.

## 2. 데이터 플랫폼(ADR-0002·0003·0004·0010) — 화면 API 는 없고 배치·CLI 로만
```bash
df -h ~                                   # 여유 3GB 이상(시세 약 1GB + 인덱스·스냅샷)
.venv/bin/python run.py platform-load --years 10 --budget 5000   # 1~2시간. 끊기면 같은 명령 = 이어 받기
.venv/bin/python run.py platform-history  # 재무·공시·업종·금리 이력(원천별 예산 안에서 이어 받기)
.venv/bin/python run.py platform-refresh  # 스냅샷·수정주가 이벤트
.venv/bin/python run.py platform-status   # 행 수·파티션·최신일 커버리지·적재 이력·마이그레이션
.venv/bin/python run.py analytics-query   # 이름 있는 분석 쿼리 목록(읽기 전용 계정·시간 제한) → 예: analytics-query drawdown_1y
```
확인: 행 수 약 600만~700만, 적재한 연도마다 파티션 1개, 최신일 커버리지 약 2,600~2,800종목, 마이그레이션 전부 `applied`,
적재 이력에 `failed` 가 남아 있으면 같은 명령을 다시 실행. 수정주가(ADR-0010):
`psql -d outofstock -c "SELECT count(*), min(factor), max(factor) FROM analytics.price_adj_event"` 에서 10년치 수백 건.
```bash
.venv/bin/python tools/bench_explain.py   # (선택, 수 분) 별도 스키마에 합성 700만 행 → 인덱스 전후 실측 → docs/perf/explain_report.md
```
기록: 실데이터 행 수·용량·종목 수, 최신 거래일 조회 시간(파티션만 vs B-tree).

## 3. 정보보호 — 제로트러스트(ADR-0005·0006·0011~0018)
```bash
# DB 계정 분리(권장) — 비밀번호 4개는 새로 정해 .env 에도 반영(docs/security/db_roles.md)
sudo -u postgres psql -d outofstock -v owner_pw="'…'" -v app_pw="'…'" -v ro_pw="'…'" -v backup_pw="'…'" -f deploy/sql/roles.sql
bash deploy/vm.sh restart
.venv/bin/python tools/db_roles_check.py --strict    # 권장 상태와 다르면 종료 코드 1
bash tools/security_scan.sh                          # 잠금 파일 취약점·bandit·숨은 문자 — 실패 항목이 있으면 종료 코드 1
.venv/bin/python run.py audit-verify                 # 감사 로그 해시 체인 + 외부 증인 대조 — '정상'
.venv/bin/python run.py ledger-close                 # 마감 안 한 날을 어제까지 날짜순으로(서버가 꺼져 있던 날도) — 오늘·미래는 거부
.venv/bin/python run.py ledger-verify                # 마감 사슬(분개 다이제스트) · 빠진 날 · 감사 체인 앵커 · 시산표 합 0
.venv/bin/python run.py security-status              # 정책 판 · 활성 세션 · 24시간 보안 이벤트 요약 · 역할 자격
curl -sI http://127.0.0.1:8000/ | grep -i -E 'content-security-policy|x-frame-options|x-content-type'   # 화면: nonce·trusted-types
```
화면에서 직접(검증 보고서의 시나리오를 내 VM 에서):
1. 로그인 → 개발자 도구 Application 탭: `oos_at`·`oos_rt` 가 **HttpOnly**, localStorage 에 토큰 없음.
2. 다른 기기(휴대폰)로 같은 계정 로그인 → `GET /auth/sessions`(frontend/outofstock.http)에 2개 → `DELETE /auth/sessions/{id}` 로 휴대폰 세션 종료
   → 휴대폰 화면은 다음 요청에 로그인 화면(세션 관리·2단계 인증은 API 로 제공 — 화면은 스크린샷 기능 그대로 유지).
3. 친구에게 100만 원 송금(방금 로그인했어도) → 비밀번호 확인 창 → 완료. 받은 쪽 화면의 지갑 잔액이 새로고침 없이 바뀐다(SSE).
4. 30분 아무것도 안 하고 두기 → 다음 클릭에 '오랫동안 사용하지 않아 로그아웃' 안내.

확인: `db_roles_check` 다른 항목 0, 취약점 0, 체인 '정상', 깨진 날·빠진 날 없음·앵커 일치, 시산표 0, `run.py dq` 의 `wallet.transfer_unscreened`(판정 없는 송금) 0. 기록: 정책 동작 수(29)·경로 수(153)·RLS 표 수(21),
권한 거부 시도 결과표([`security/db_roles.md`](security/db_roles.md)), 보안 이벤트 종류별 건수.

자율보안(ADR-0025 — 배포 뒤 VM 에서):
```bash
.venv/bin/python run.py integrity-check        # 배포 기준선과 같음(vm.sh update 가 기준선을 기록) — 아무 파일이나 web/ 에 만들면 '새 파일' 위반
.venv/bin/python run.py self-assess           # 운영 점검까지 — 개발 PC 와 달리 키·백업·훈련 프로브가 'ok' 여야 한다
sudo -u postgres createdb -O oos outofstock_drill
.venv/bin/python run.py incident-drill --target-db postgresql+psycopg://oos:<비번>@127.0.0.1:5432/outofstock_drill   # 7/7 통과
```
확인: 진단 결과의 `probe_failures` 0 · `/admin/assessment/{id}` 의 `intact` true · 훈련 보고서(`docs/incidents/drills/`)의 차단·복구 시간.

IT 내부통제(ADR-0026 — 배포 뒤 VM 에서):
```bash
.venv/bin/python run.py change-list            # 이번 update 가 남긴 변경 기록(기록 모드면 '미승인' — 정식은 change-request → 다른 관리자 승인)
.venv/bin/python run.py uptime-check           # 헬스 1회 + 장애 판정 — 30일 가용률·감시 범위(스케줄러가 1분마다)
.venv/bin/python run.py pentest --base http://127.0.0.1:8000 --register   # 취약점 자가 점검 22개 — 열린 경로 0(주의: API 문서 공개면 DOCS_ENABLED=false)
.venv/bin/python run.py siem-export --hours 24 --format cef | head        # 외부 관제 형식 — 아이디는 u:가명
```
확인: `change-list` 에 이번 배포 1건(적용·기준선 번호) · 가용률 목표 99.5% · 자가 점검 열린 경로 0 · 오류 경보는 15분 안에 `POST /admin/alerts/{id}/ack`.

DB 저장·수명주기·진단(ADR-0027 — 배포 뒤 VM 에서):
```bash
.venv/bin/python run.py db-report              # 크기·증가 추세·디스크 소진 예측·판정(죽은 행·겹치는 인덱스·캐시·XID)·설정 — 오류 판정이면 종료 코드 1
.venv/bin/python run.py ilm-status             # 보관 기한·기한 지난 행·보관 파일(1년 미만이면 아직 대상 없음)
bash deploy/vm.sh db-tune --plan               # 지금 설정 → 권장값(바꿀 것이 없으면 아무것도 안 나온다)
psql "$DATABASE_URL" -c "SELECT key, left(value, 120) FROM app_meta WHERE key LIKE 'migration:retype:%'"   # 표별 대사 기록
```
확인: `\d news` 의 `published_ts` 가 `timestamp(0) with time zone` · `ux_news_url_md5` 있음·`news_url_key` 없음 · `db-report` 판정에 error 없음 ·
`SHOW shared_preload_libraries` 에 pg_stat_statements(재시작 뒤) · 실측 재현은 `tools/db_lab.py`(`docs/perf/db_space_2026-11-03.md` §9).

## 4. 이상거래탐지(FDS) · 기업 신용위험(ADR-0016·0007)
```bash
.venv/bin/python run.py fds-eval          # 합성 2만 건 — 사기 탐지율·정상 차단율·정밀도 → docs/models/fds_eval.md
.venv/bin/python run.py credit-train      # 재무·공시 이력(2단계) 이후. 데이터 부족이면 무엇이 부족한지 출력
.venv/bin/python run.py model-report      # docs/models/model_results.md 생성
```
기록: FDS 정상 거래 차단율(가장 중요한 오탐)·추가 인증률, 신용위험 시험 연도·표본/부실 건수·로지스틱·부스팅·Z''·Merton 의 AUC·KS.
**합성 FDS 수치는 실제 성능이 아니고, 신용 모형이 Z''·Merton 을 못 이기면 그대로 적는다.**

## 5. 수익률 예측 검증(ADR-0008)
```bash
.venv/bin/python run.py return-validate   # 거래대금 상위 300종목 × 5년 — 수 분
```
기록: 무작위 K-겹 AUC vs 시간순 AUC(누수 크기), IC t값, 비용 후 스프레드, 판정 문구.

## 6. 복원력 — 백업·복구 훈련(ADR-0019)
```bash
bash deploy/vm.sh backup && bash deploy/vm.sh drill     # 일관 스냅샷 → age 암호화 → 격리 DB 에 복원 → 검증 8항목 → RTO/RPO 보고서
ls docs/dr/drills/                                       # 훈련 보고서(자동 생성)
```
기록: 훈련 RTO(초)·백업 크기·표 수·행 수, 검증 8항목 통과. 운영 DB 교체 복원은 [`dr/README.md`](dr/README.md) 절차대로(새 DB 에 복원 → 검증 → 이름 교체).

## 7. CI
```bash
ruff check .                              # All checks passed!
python3 tools/check_frontend.py           # 화면 스크립트 문법(node 필요)
python3 tools/check_lock.py               # 잠금 파일 = requirements 범위
.venv/bin/python -m pytest -q             # 테스트 DB(outofstock_test) 필요
```
GitHub 에 올렸다면 Actions `ci` 의 lint · test(KST·UTC — 실제 복원 훈련 포함) · least-privilege · security · image · e2e(실제 크로미움·axe)가 모두 초록인지
(액션은 커밋 SHA 고정).

## 8. 화면 점검(스크린샷 기준 기능)
| 화면 | 볼 것 |
|---|---|
| 홈 | 지수 배지(실시간/종가/표본) · 추천 Top 5(근거·주의) · 성향별 상품 · 오늘의 용어 · 진행 중 실습 배너 |
| 투자 성향 분석 | 10문항 → 결과(영역별 점수) → 상품 3개 · 로그인 시 마이페이지 성향 이력 |
| 학습하기 · 실습하기 | 용어 사전 · '이해했어요' 저장 · 시나리오 진행(미래 구간 숨김) · 매매 · 종료 정산 |
| 국내 종목 · 해외 종목 | 순위 탭 · 검색(초성·조합 중 글자·별칭) · 상세(차트·지표·뉴스·리스크) · 관심종목 별 |
| 뉴스 | 이슈 TOP · 테마 · 주간/월간 랭킹 · AI 뉴스 픽 |
| 장기 모의투자 · 친구·채팅 | 매수/매도 · 포트폴리오 리스크 · 친구 요청/수락 · 채팅(새 메시지 즉시) |
| 마이페이지 | 프로필·지갑·출석·송금·충전 · 기록 '더보기' · 탈퇴(비밀번호 재확인 창) |

표본(생성) 데이터는 주황 '표본' 배지가 붙는다 — 실데이터가 들어오면 사라져야 정상. 화면 오류·CSP 위반은 0 이어야 한다
(위반은 보안 이벤트 `csp_violation` 으로 남는다 — `run.py security-status`).
