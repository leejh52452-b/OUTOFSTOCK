# 데이터 사전

`python run.py data-docs` 로 생성 — 손으로 고치지 말 것(tests/test_datadocs.py 가 확인).

등급: **L1** 공개 · **L2** 내부 · **L3** 개인정보 · **L4** 민감·비밀 · 보호: hash(일방향) encrypt(봉투 암호화) pseudonym(가명) rls(행 수준 보안) append_only(추가만)

| 등급 | 표 수 |
|---|---|
| L1 공개 | 19 |
| L2 내부 | 25 |
| L3 개인정보 | 27 |
| L4 민감·비밀 | 3 |

## access_log

접속 기록(로그인·로그아웃·기기 계열)  
등급 **L3** · 보관 1년(접속기록 보관) 후 파기 · 보호 rls · RLS 주인 열 `user_id`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| log_id 🔑 | INTEGER | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| event | VARCHAR(20) | L3 |  |  |
| detail | VARCHAR(200) | L3 |  |  |
| at | TIMESTAMP | L3 |  |  |

## alert_log

운영 알림 발송 기록  
등급 **L2** · 보관 180일

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| alert_id 🔑 | INTEGER | L2 |  |  |
| key | VARCHAR(100) | L2 |  |  |
| level | VARCHAR(10) | L2 |  |  |
| title | VARCHAR(200) | L2 |  |  |
| message | TEXT | L2 |  |  |
| channels | VARCHAR(100) | L2 |  |  |
| delivered | INTEGER | L2 |  |  |
| created_at | TIMESTAMP | L2 |  |  |
| ack_by | VARCHAR(50) | L2 |  |  |
| ack_at | TIMESTAMP | L2 |  |  |
| escalated_at | TIMESTAMP | L2 |  |  |

## app_meta

키-값 운영 메타(출처·기준일·배치 상태·암호화된 외부 토큰)  
등급 **L2** · 보관 값마다 다름

계보: ← EXIM 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| key 🔑 | VARCHAR(60) | L2 |  |  |
| value | TEXT | L2 | encrypt | 외부 API 토큰은 enc:v1/v2 암호문(AAD=app_meta:<키>) |
| updated_at | TIMESTAMP | L2 |  |  |

## app_user

회원 계정(아이디·상태·잠금)  
등급 **L3** · 보관 탈퇴 시 즉시 파기

계보: ← USER 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| username 🔑 | VARCHAR(50) | L3 |  |  |
| password_hash | VARCHAR(200) | L4 | hash | PBKDF2-SHA256 600,000회 + 페퍼(HMAC) |
| salt | VARCHAR(64) | L4 | hash | 계정별 무작위 솔트(해시 입력) |
| created_at | TIMESTAMP | L3 |  |  |
| status | VARCHAR(10) | L3 |  |  |
| failed_count | INTEGER | L3 |  |  |
| locked_until | DATETIME | L3 |  |  |
| password_changed_at | DATETIME | L3 |  |  |
| last_login_at | DATETIME | L3 |  |  |

## archive_log

데이터 수명주기 보관 기록(기간·파일·SHA-256·행 수·정리·복원 시각 — 개인정보 없음, 앱은 삭제·해시 수정 불가)  
등급 **L2** · 보관 영구(보관 파일의 목록·무결성 근거) · 보호 append_only

계보: ← news, search_log · 쓰임: 관리 /admin/db(수명주기)·복원(run.py ilm-restore)·월간 재해시(ilm-verify)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| dataset | VARCHAR(20) | L2 |  |  |
| period_start | DATE | L2 |  |  |
| period_end | DATE | L2 |  |  |
| path | VARCHAR(300) | L2 |  |  |
| sha256 | VARCHAR(64) | L2 |  |  |
| bytes | BIGINT | L2 |  |  |
| raw_bytes | BIGINT | L2 |  |  |
| rows | TEXT | L2 |  |  |
| created_at | DATETIME | L2 |  |  |
| deleted_at | DATETIME | L2 |  |  |
| deleted_rows | INTEGER | L2 |  |  |
| verified_at | DATETIME | L2 |  |  |
| restored_at | DATETIME | L2 |  |  |

## attendance

출석 기록·보상  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| attend_date | DATE | L3 |  |  |
| reward | BIGINT | L3 |  |  |
| streak | INTEGER | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## audit_log

감사 로그(해시 체인 — 누가·언제·무엇을)  
등급 **L3** · 보관 3년 후 파기(체인 시작점 이동 — 권한 내역 3년) · 보호 append_only, pseudonym

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| audit_id 🔑 | BIGINT | L3 |  |  |
| at | VARCHAR(30) | L3 |  |  |
| actor | VARCHAR(50) | L3 |  |  |
| action | VARCHAR(40) | L3 |  |  |
| target | VARCHAR(100) | L3 |  |  |
| detail | TEXT | L3 |  |  |
| ip_hash | VARCHAR(16) | L2 | pseudonym | IP HMAC 가명값 |
| result | VARCHAR(10) | L3 |  |  |
| prev_hash | VARCHAR(64) | L3 |  |  |
| row_hash | VARCHAR(64) | L3 |  |  |

## auth_refresh_used

사용한 갱신 토큰 해시(재사용 탐지)  
등급 **L4** · 보관 12시간 · 보호 hash

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| token_hash 🔑 | VARCHAR(64) | L4 |  |  |
| session_id | VARCHAR(32) | L4 |  |  |
| used_at | DATETIME | L4 |  |  |

## auth_session

로그인 세션(기기 계열·IP 대역·만료·위험 신호·기기 결속 키 지문)  
등급 **L3** · 보관 만료·폐기 후 7일

계보: ← DEVICE 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| session_id 🔑 | VARCHAR(32) | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| client | VARCHAR(8) | L3 |  |  |
| created_at | DATETIME | L3 |  |  |
| auth_time | DATETIME | L3 |  |  |
| aal | SMALLINT | L3 |  |  |
| step_up_at | DATETIME | L3 |  |  |
| last_seen_at | DATETIME | L3 |  |  |
| expires_at | DATETIME | L3 |  |  |
| device_hash | VARCHAR(32) | L3 | hash | 기기 계열 지문(원문 UA 미저장) |
| device | VARCHAR(60) | L3 |  |  |
| ip_net | VARCHAR(64) | L3 |  |  |
| last_ip_net | VARCHAR(64) | L3 |  |  |
| risk | VARCHAR(100) | L3 |  |  |
| refresh_hash | VARCHAR(64) | L4 | hash | 갱신 토큰 SHA-256(원문 미저장) |
| refresh_prev_hash | VARCHAR(64) | L4 | hash | 직전 갱신 토큰 해시(재사용 탐지) |
| rotated_at | DATETIME | L3 |  |  |
| refresh_count | INTEGER | L3 |  |  |
| csrf_hash | VARCHAR(64) | L4 | hash | CSRF 토큰 해시 |
| revoked_at | DATETIME | L3 |  |  |
| revoke_reason | VARCHAR(40) | L3 |  |  |
| persistent | BOOLEAN | L3 |  |  |
| dpop_jkt | VARCHAR(43) | L3 |  |  |

## auth_token

접근 토큰(10분) — 해시만  
등급 **L4** · 보관 만료 즉시 정리 · 보호 hash

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| token 🔑 | VARCHAR(64) | L4 |  |  |
| username | VARCHAR(50) | L4 |  |  |
| created_at | TIMESTAMP | L4 |  |  |
| session_id | VARCHAR(32) | L4 |  |  |
| expires_at | DATETIME | L4 |  |  |

## avatar_closet

양봉이 꾸미기 — 산 아이템·산 가격·착용 상태  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← USER → wallet_ledger · 쓰임: 양봉이 꾸미기·마이페이지 프로필·홈 로그인 카드

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| username 🔑 | VARCHAR(50) | L3 |  |  |
| item_id 🔑 | VARCHAR(20) | L3 |  |  |
| slot | VARCHAR(10) | L3 |  |  |
| price | BIGINT | L3 |  |  |
| equipped | BOOLEAN | L3 |  |  |
| bought_at | DATETIME | L3 |  |  |

## capacity_report

성능관리보고서(부하 단계·지표·느린 경로·권고 — 개인정보 없음)  
등급 **L2** · 보관 1년

계보: ← OPS · 쓰임: 관리 /admin/capacity·운영 알림(성능관리보고서)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| at | DATETIME | L2 |  |  |
| level | SMALLINT | L2 |  |  |
| detail | TEXT | L2 |  |  |

## change_record

변경 기록(요청·사전 영향 분석·자동 점검·승인·적용·사후 검토 — 개인정보 없음, 앱은 삭제·해시 수정 불가)  
등급 **L2** · 보관 영구(배포 변경 이력)

계보: ← ADMIN, RELEASE · 쓰임: 배포 게이트(vm.sh)·관리 /admin/changes·사고보고서 '변경시점'

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| created_at | DATETIME | L2 |  |  |
| kind | VARCHAR(12) | L2 |  |  |
| title | VARCHAR(120) | L2 |  |  |
| reason | VARCHAR(500) | L2 |  |  |
| requested_by | VARCHAR(50) | L2 |  |  |
| release_name | VARCHAR(160) | L2 |  |  |
| release_sha256 | VARCHAR(64) | L2 |  |  |
| impact | TEXT | L2 |  |  |
| risk | VARCHAR(8) | L2 |  |  |
| prechecks | TEXT | L2 |  |  |
| evidence | VARCHAR(1000) | L2 |  |  |
| status | VARCHAR(12) | L2 |  |  |
| approved_by | VARCHAR(50) | L2 |  |  |
| approved_at | DATETIME | L2 |  |  |
| applied_at | DATETIME | L2 |  |  |
| applied_by | VARCHAR(50) | L2 |  |  |
| baseline_id | INTEGER | L2 |  |  |
| review_due | DATETIME | L2 |  |  |
| reviewed_by | VARCHAR(50) | L2 |  |  |
| reviewed_at | DATETIME | L2 |  |  |
| review_note | VARCHAR(500) | L2 |  |  |

## chat_message

1:1 채팅  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `sender,receiver`

계보: ← USER 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L3 |  |  |
| sender | VARCHAR(50) | L3 |  |  |
| receiver | VARCHAR(50) | L3 |  |  |
| body | TEXT | L4 | encrypt | 통신 내용 — 봉투 암호화(AAD=chat:<보낸이>:<받는이>) |
| created_at | TIMESTAMP | L3 |  |  |
| read_at | TIMESTAMP | L3 |  |  |

## daily_price

국내 일별 시세(종가·거래량·시총)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← KRX → ranking, reco_snapshot · 쓰임: 국내 종목 화면·추천·위험(VaR)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ticker 🔑 | VARCHAR(20) | L1 |  |  |
| trade_date 🔑 | DATE | L1 |  |  |
| close | INTEGER | L1 |  |  |
| volume | BIGINT | L1 |  |  |
| change_rate | FLOAT | L1 |  |  |
| market_cap | BIGINT | L1 |  |  |

## dart_corp

DART 고유번호 ↔ 종목코드  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← DART 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| corp_code 🔑 | VARCHAR(8) | L1 |  |  |
| corp_name | VARCHAR(200) | L1 |  |  |
| stock_code | VARCHAR(12) | L1 |  |  |
| modify_date | VARCHAR(8) | L1 |  |  |
| fetched_at | TIMESTAMP | L1 |  |  |

## data_quality_issue

데이터 품질 규칙 위반 기록  
등급 **L2** · 보관 1년

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| issue_id 🔑 | INTEGER | L2 |  |  |
| table_name | VARCHAR(40) | L2 |  |  |
| ticker | VARCHAR(20) | L2 |  |  |
| trade_date | VARCHAR(10) | L2 |  |  |
| rule | VARCHAR(40) | L2 |  |  |
| detail | VARCHAR(200) | L2 |  |  |
| detected_at | TIMESTAMP | L2 |  |  |

## db_size_snapshot

DB 크기 일일 기록(표별 크기·행 수 — 증가 추세·디스크 소진 예측, 개인정보 없음)  
등급 **L2** · 보관 2년

계보: ← OPS · 쓰임: 관리 /admin/db(증가 추세·디스크 소진 예측)·DB 진단 경보

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| day 🔑 | DATE | L2 |  |  |
| at | DATETIME | L2 |  |  |
| db_bytes | BIGINT | L2 |  |  |
| detail | TEXT | L2 |  |  |

## dpop_jti

기기 결속 증명 재사용 방지(키 지문·증명 ID — 아이디 없음)  
등급 **L2** · 보관 10분

계보: ← DEVICE · 쓰임: PEP·토큰 갱신(재사용 거부)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| jkt 🔑 | VARCHAR(43) | L2 |  |  |
| jti 🔑 | VARCHAR(64) | L2 |  |  |
| seen_at | DATETIME | L2 |  |  |

## dq_result

데이터 계약 측정 결과(품질 차원별 값·통과 여부)  
등급 **L2** · 보관 1년

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| run_at | DATETIME | L2 |  |  |
| dataset | VARCHAR(40) | L2 |  |  |
| check_name | VARCHAR(40) | L2 |  |  |
| dimension | VARCHAR(16) | L2 |  |  |
| value | FLOAT | L2 |  |  |
| op | VARCHAR(2) | L2 |  |  |
| threshold | FLOAT | L2 |  |  |
| severity | VARCHAR(8) | L2 |  |  |
| status | VARCHAR(4) | L2 |  |  |

## fds_decision

이상거래탐지 판정(점수·규칙·재료) — 관리자 검토용  
등급 **L3** · 보관 1년 후 파기

계보: ← wallet_ledger · 쓰임: FDS 검토(관리자)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L3 |  |  |
| at | DATETIME | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| action | VARCHAR(20) | L3 |  |  |
| counterparty | VARCHAR(50) | L3 |  |  |
| amount | BIGINT | L3 |  |  |
| score | INTEGER | L3 |  |  |
| rules | TEXT | L3 |  |  |
| decision | VARCHAR(24) | L3 |  |  |
| request_id | VARCHAR(64) | L3 |  |  |
| ref | VARCHAR(40) | L3 |  |  |

## financial_statement

DART 재무(공시일·적재일 — 시점 조회)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← DART → reco_snapshot, recommendation · 쓰임: 재무 건전성·PER/PBR

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ticker 🔑 | VARCHAR(20) | L1 |  |  |
| fiscal_year 🔑 | INTEGER | L1 |  |  |
| report_code 🔑 | VARCHAR(10) | L1 |  |  |
| revenue | BIGINT | L1 |  |  |
| operating_profit | BIGINT | L1 |  |  |
| net_income | BIGINT | L1 |  |  |
| total_equity | BIGINT | L1 |  |  |
| debt_ratio | FLOAT | L1 |  |  |
| disclosed_at | DATE | L1 |  |  |
| ingested_at | TIMESTAMP | L1 |  |  |
| total_assets | BIGINT | L1 |  |  |
| current_assets | BIGINT | L1 |  |  |
| current_liabilities | BIGINT | L1 |  |  |
| total_liabilities | BIGINT | L1 |  |  |
| retained_earnings | BIGINT | L1 |  |  |
| fs_basis | VARCHAR(3) | L1 |  |  |

## friend_request

친구 요청  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `sender,receiver`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L3 |  |  |
| sender | VARCHAR(50) | L3 |  |  |
| receiver | VARCHAR(50) | L3 |  |  |
| status | VARCHAR(10) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |
| responded_at | TIMESTAMP | L3 |  |  |

## friendship

친구 관계(양방향 두 행)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `owner,friend`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L3 |  |  |
| owner | VARCHAR(50) | L3 |  |  |
| friend | VARCHAR(50) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## iap_purchase

인앱 충전 기록(영수증은 해시)  
등급 **L3** · 보관 탈퇴 시 아이디 분리(영수증 재사용 차단용으로 해시만 보존) · 보호 rls · RLS 주인 열 `username`

계보: ← STORE → wallet_ledger 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| purchase_id 🔑 | INTEGER | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| store | VARCHAR(10) | L3 |  |  |
| product_id | VARCHAR(40) | L3 |  |  |
| coins | BIGINT | L3 |  |  |
| price | VARCHAR(20) | L3 |  |  |
| receipt | VARCHAR(512) | L4 | hash | 스토어 영수증 SHA-256('h:' 접두) |
| status | VARCHAR(12) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## incident

장애·사고 사건(지속 시간·영향 이용자 수·보고 대상·기한·단계·원인·대책 — 개인정보 없음, 앱은 삭제 불가)  
등급 **L2** · 보관 5년(사고 보고 근거)

계보: ← ADMIN, uptime_check · 쓰임: 관리 /admin/incidents(보고 기한·초안)·수준진단 점검

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| kind | VARCHAR(10) | L2 |  |  |
| title | VARCHAR(200) | L2 |  |  |
| started_at | DATETIME | L2 |  |  |
| detected_at | DATETIME | L2 |  |  |
| ended_at | DATETIME | L2 |  |  |
| status | VARCHAR(10) | L2 |  |  |
| severity | VARCHAR(8) | L2 |  |  |
| opened_by | VARCHAR(50) | L2 |  |  |
| affected_users | INTEGER | L2 |  |  |
| reportable | BOOLEAN | L2 |  |  |
| report_due | DATETIME | L2 |  |  |
| details | TEXT | L2 |  |  |
| cause | VARCHAR(1000) | L2 |  |  |
| action | VARCHAR(1000) | L2 |  |  |
| measures | VARCHAR(1000) | L2 |  |  |
| impact_note | VARCHAR(1000) | L2 |  |  |
| loss_amount | BIGINT | L2 |  |  |
| reputation | BOOLEAN | L2 |  |  |
| data_damage | BOOLEAN | L2 |  |  |
| first_reported_at | DATETIME | L2 |  |  |
| interim_reported_at | DATETIME | L2 |  |  |
| final_reported_at | DATETIME | L2 |  |  |

## index_daily

지수 일별 이력  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← market_index → yangbong_basket 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| code 🔑 | VARCHAR(20) | L1 |  |  |
| trade_date 🔑 | DATE | L1 |  |  |
| open | FLOAT | L1 |  |  |
| high | FLOAT | L1 |  |  |
| low | FLOAT | L1 |  |  |
| close | FLOAT | L1 |  |  |
| change | FLOAT | L1 |  |  |
| change_rate | FLOAT | L1 |  |  |
| source | VARCHAR(10) | L1 |  |  |

## integrity_baseline

파일 무결성 기준선(배포 파일별 SHA-256 — 소유자 계정만 기록, 앱은 읽기만)  
등급 **L2** · 보관 영구(배포 변경 이력) · 보호 append_only

계보: ← REPO · 쓰임: 무결성 검사(10분)·/admin/integrity

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| created_at | DATETIME | L2 |  |  |
| created_by | VARCHAR(50) | L2 |  |  |
| release | VARCHAR(120) | L2 |  |  |
| files | INTEGER | L2 |  |  |
| digest | VARCHAR(64) | L2 |  |  |
| manifest | TEXT | L2 |  |  |

## invest_position

장기 모의투자 보유·실현손익  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| position_id 🔑 | INTEGER | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| ticker | VARCHAR(20) | L3 |  |  |
| quantity | BIGINT | L3 |  |  |
| buy_price | FLOAT | L3 |  |  |
| buy_date | DATE | L3 |  |  |
| cost | BIGINT | L3 |  |  |
| status | VARCHAR(10) | L3 |  |  |
| sell_price | FLOAT | L3 |  |  |
| sell_date | DATE | L3 |  |  |
| proceeds | BIGINT | L3 |  |  |
| realized_pnl | BIGINT | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |
| sold_qty | BIGINT | L3 |  |  |
| adj_through | DATE | L3 |  |  |

## journal_entry

복식부기 분개(계좌번호·시스템 계정 — 아이디 없음, 추가만)  
등급 **L2** · 보관 5년(거래 기록 보존 관행) · 보호 append_only, pseudonym

계보: ← wallet_ledger → ledger_close · 쓰임: 일 마감·시산표(관리자)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| entry_id 🔑 | BIGINT | L2 |  |  |
| txn_id | VARCHAR(32) | L2 |  |  |
| account | VARCHAR(32) | L2 |  |  |
| amount | BIGINT | L2 |  |  |
| reason | VARCHAR(20) | L2 |  |  |
| ledger_id | INTEGER | L2 |  |  |
| created_at | DATETIME | L2 |  |  |

## learning_log

학습 열람·진도 기록  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| log_id 🔑 | INTEGER | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| kind | VARCHAR(20) | L3 |  |  |
| item_key | VARCHAR(60) | L3 |  |  |
| item_name | VARCHAR(100) | L3 |  |  |
| viewed_at | TIMESTAMP | L3 |  |  |

## ledger_close

일 마감 결과(합계·대사·해시 사슬, 추가만)  
등급 **L2** · 보관 5년 · 보호 append_only

계보: ← journal_entry 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| close_date 🔑 | DATE | L2 |  |  |
| status | VARCHAR(8) | L2 |  |  |
| entries | INTEGER | L2 |  |  |
| txns | INTEGER | L2 |  |  |
| debit | BIGINT | L2 |  |  |
| credit | BIGINT | L2 |  |  |
| checks | TEXT | L2 |  |  |
| prev_hash | VARCHAR(64) | L2 |  |  |
| close_hash | VARCHAR(64) | L2 |  |  |
| closed_at | DATETIME | L2 |  |  |

## market_index

시장 지수 최신값(출처·기준일 포함)  
등급 **L1** · 보관 최신값만 유지

계보: ← KIS, KRX → index_daily · 쓰임: 홈 지수

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| code 🔑 | VARCHAR(20) | L1 |  |  |
| name | VARCHAR(40) | L1 |  |  |
| value | FLOAT | L1 |  |  |
| change | FLOAT | L1 |  |  |
| change_rate | FLOAT | L1 |  |  |
| ts | TIMESTAMP | L1 |  |  |
| source | VARCHAR(10) | L1 |  |  |
| as_of | DATE | L1 |  |  |

## news

뉴스 제목·링크(Google News RSS)  
등급 **L1** · 보관 DB 약 13개월(NEWS_RETENTION_DAYS) → 압축 보관 파일(복원 가능, archive_log)

계보: ← GNEWS → archive_log, news_sentiment, news_stock · 쓰임: 뉴스 화면

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| news_id 🔑 | INTEGER | L1 |  |  |
| title | VARCHAR(500) | L1 |  |  |
| url | VARCHAR(1000) | L1 |  |  |
| source | VARCHAR(200) | L1 |  |  |
| published_at | DATE | L1 |  |  |
| published_ts | TIMESTAMP | L1 |  |  |

## news_sentiment

뉴스 감성(호재·악재) 분류 결과·모형  
등급 **L1** · 보관 뉴스와 같음

계보: ← news → reco_snapshot · 쓰임: AI 뉴스 픽

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| news_id 🔑 | INTEGER | L1 |  |  |
| label | VARCHAR(4) | L1 |  |  |
| score | FLOAT | L1 |  |  |
| model | VARCHAR(40) | L1 |  |  |
| classified_at | TIMESTAMP | L1 |  |  |

## news_stock

뉴스 ↔ 종목 연결  
등급 **L1** · 보관 뉴스와 같음

계보: ← news 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| news_id 🔑 | INTEGER | L1 |  |  |
| ticker 🔑 | VARCHAR(20) | L1 |  |  |

## ohlc

국내 일봉(시고저종)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← KIS, KRX · 쓰임: 종목 차트·실습

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ticker 🔑 | VARCHAR(20) | L1 |  |  |
| trade_date 🔑 | DATE | L1 |  |  |
| open | INTEGER | L1 |  |  |
| high | INTEGER | L1 |  |  |
| low | INTEGER | L1 |  |  |
| close | INTEGER | L1 |  |  |
| volume | BIGINT | L1 |  |  |

## outbox

트랜잭셔널 아웃박스(전달 대기 사건 — 가명·금액만)  
등급 **L2** · 보관 전달 후 30일 · 보호 pseudonym

계보: ← wallet_ledger 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| topic | VARCHAR(40) | L2 |  |  |
| payload | TEXT | L2 |  |  |
| created_at | DATETIME | L2 |  |  |
| available_at | DATETIME | L2 |  |  |
| attempts | INTEGER | L2 |  |  |
| status | VARCHAR(10) | L2 |  |  |
| delivered_at | DATETIME | L2 |  |  |
| last_error | VARCHAR(300) | L2 |  |  |

## overseas_financial

해외 재무(EDGAR)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← EDGAR 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| symbol 🔑 | VARCHAR(12) | L1 |  |  |
| payload | VARCHAR(4000) | L1 |  |  |
| updated_at | TIMESTAMP | L1 |  |  |

## overseas_ohlc

해외 일봉  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← FDR · 쓰임: 해외 종목 화면

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| symbol 🔑 | VARCHAR(12) | L1 |  |  |
| trade_date 🔑 | DATE | L1 |  |  |
| open | FLOAT | L1 |  |  |
| high | FLOAT | L1 |  |  |
| low | FLOAT | L1 |  |  |
| close | FLOAT | L1 |  |  |
| volume | BIGINT | L1 |  |  |

## overseas_quote

해외 실시간·최신 시세  
등급 **L1** · 보관 최신값만 유지

계보: ← FINNHUB 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| symbol 🔑 | VARCHAR(12) | L1 |  |  |
| price | FLOAT | L1 |  |  |
| change | FLOAT | L1 |  |  |
| change_rate | FLOAT | L1 |  |  |
| prev_close | FLOAT | L1 |  |  |
| open | FLOAT | L1 |  |  |
| high | FLOAT | L1 |  |  |
| low | FLOAT | L1 |  |  |
| volume | BIGINT | L1 |  |  |
| session_date | DATE | L1 |  |  |
| source | VARCHAR(10) | L1 |  |  |
| ts | TIMESTAMP | L1 |  |  |

## overseas_stock

해외 종목 마스터  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| symbol 🔑 | VARCHAR(12) | L1 |  |  |
| name | VARCHAR(100) | L1 |  |  |
| name_ko | VARCHAR(60) | L1 |  |  |
| exchange | VARCHAR(20) | L1 |  |  |
| sector | VARCHAR(60) | L1 |  |  |
| shares | BIGINT | L1 |  |  |
| market_cap | BIGINT | L1 |  |  |
| source | VARCHAR(10) | L1 |  |  |
| updated_at | TIMESTAMP | L1 |  |  |

## payment_order

토스 결제 주문  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← TOSS → wallet_ledger 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| order_id 🔑 | VARCHAR(64) | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| product_id | VARCHAR(40) | L3 |  |  |
| coins | BIGINT | L3 |  |  |
| amount | BIGINT | L3 |  |  |
| order_name | VARCHAR(100) | L3 |  |  |
| status | VARCHAR(12) | L3 |  |  |
| payment_key | VARCHAR(600) | L4 | encrypt | 결제 키 AES-256-GCM |
| created_at | TIMESTAMP | L3 |  |  |
| paid_at | TIMESTAMP | L3 |  |  |

## pii_access_log

관리자 개인정보 열람 기록(목적 포함)  
등급 **L3** · 보관 2년 후 파기 · 보호 append_only

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L3 |  |  |
| at | DATETIME | L3 |  |  |
| operator | VARCHAR(50) | L3 |  |  |
| subject | VARCHAR(50) | L3 |  |  |
| fields | VARCHAR(200) | L3 |  |  |
| purpose | VARCHAR(200) | L3 |  |  |
| ip_hash | VARCHAR(16) | L2 | pseudonym | IP HMAC 가명값 |
| request_id | VARCHAR(64) | L3 |  |  |

## pipeline_run

배치 실행 기록(상태·건수·메모 — 비밀값 가림)  
등급 **L2** · 보관 1년

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| run_id 🔑 | INTEGER | L2 |  |  |
| started_at | TIMESTAMP | L2 |  |  |
| finished_at | TIMESTAMP | L2 |  |  |
| status | VARCHAR(20) | L2 |  |  |
| price_rows | INTEGER | L2 |  |  |
| dart_rows | INTEGER | L2 |  |  |
| news_rows | INTEGER | L2 |  |  |
| quality_ok | INTEGER | L2 |  |  |
| quality_issues | INTEGER | L2 |  |  |
| note | VARCHAR(200) | L2 |  |  |

## practice_record

웹 실습 기록(기법·방식·수익률 — 브라우저 계산값, 보상과 무관한 표시용)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

계보: ← WEB · 쓰임: 실습 목록 '나의 실습 기록'·마이페이지 실습 이력

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| tech | VARCHAR(20) | L3 |  |  |
| mode | VARCHAR(10) | L3 |  |  |
| traded | BOOLEAN | L3 |  |  |
| ret | FLOAT | L3 |  |  |
| real_entry | DATE | L3 |  |  |
| created_at | DATETIME | L3 |  |  |

## practice_session

과거 사례 실습 진행 상태(계정 또는 게스트 쿠키 ID)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

계보: ← USER → practice_trade 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| session_id 🔑 | INTEGER | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| scenario_id | VARCHAR(40) | L3 |  |  |
| cursor_date | DATE | L3 |  |  |
| cash | BIGINT | L3 |  |  |
| holding_qty | BIGINT | L3 |  |  |
| holding_avg | FLOAT | L3 |  |  |
| start_cash | BIGINT | L3 |  |  |
| version | INTEGER | L3 |  |  |
| status | VARCHAR(10) | L3 |  |  |
| started_at | TIMESTAMP | L3 |  |  |
| ended_at | TIMESTAMP | L3 |  |  |
| final_return | FLOAT | L3 |  |  |
| best_return | FLOAT | L3 |  |  |
| attempts | INTEGER | L3 |  |  |

## practice_trade

실습 체결 기록(소속 실습의 주인 기준 RLS)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls

계보: ← practice_session 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| trade_id 🔑 | INTEGER | L3 |  |  |
| session_id | INTEGER | L3 |  |  |
| trade_date | DATE | L3 |  |  |
| side | VARCHAR(4) | L3 |  |  |
| quantity | BIGINT | L3 |  |  |
| price | INTEGER | L3 |  |  |
| idem_key | VARCHAR(80) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## quiz_attempt

Daily Stock Quiz 결과(날짜·고른 답·서버 채점 점수 — 하루 1행)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

계보: ← WEB → wallet_ledger · 쓰임: 용어사전 퀴즈·마이페이지 퀴즈 완료

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| user_id 🔑 | VARCHAR(50) | L3 |  |  |
| quiz_date 🔑 | DATE | L3 |  |  |
| answers | VARCHAR(20) | L3 |  |  |
| score | SMALLINT | L3 |  |  |
| created_at | DATETIME | L3 |  |  |
| reward | INTEGER | L3 |  |  |
| reward_day | DATE | L3 |  |  |

## ranking

종목 순위(배치 계산)  
등급 **L1** · 보관 배치마다 교체

계보: ← daily_price 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ranking_type 🔑 | VARCHAR(20) | L1 |  |  |
| rank 🔑 | INTEGER | L1 |  |  |
| ticker | VARCHAR(20) | L1 |  |  |
| metric_value | FLOAT | L1 |  |  |
| as_of | DATE | L1 |  |  |

## rate_event

시도 제한 기록(키는 해시 — 아이디·IP 원문 아님)  
등급 **L2** · 보관 1시간(UNLOGGED) · 보호 pseudonym

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| key | VARCHAR(32) | L2 |  |  |
| at | DATETIME | L2 |  |  |

## realtime_price

국내 실시간 체결가(최신 1건)  
등급 **L1** · 보관 최신값만 유지(덮어씀)

계보: ← KIS · 쓰임: 실시간 시세 표시

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ticker 🔑 | VARCHAR(20) | L1 |  |  |
| price | INTEGER | L1 |  |  |
| change_rate | FLOAT | L1 |  |  |
| volume | BIGINT | L1 |  |  |
| ts | TIMESTAMP | L1 |  |  |
| change | INTEGER | L1 |  |  |
| open | INTEGER | L1 |  |  |
| high | INTEGER | L1 |  |  |
| low | INTEGER | L1 |  |  |
| value | BIGINT | L1 |  |  |
| session_date | DATE | L1 |  |  |
| source | VARCHAR(10) | L1 |  |  |

## reco_snapshot

오늘의 추천 Top 5 공개 기록(사후 검증·감사 추적)  
등급 **L1** · 보관 영구(모형 감사 추적)

계보: ← daily_price, financial_statement, news_sentiment · 쓰임: 오늘의 추천·사후 검증

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| as_of 🔑 | DATE | L1 |  |  |
| profile 🔑 | VARCHAR(10) | L1 |  |  |
| model_version 🔑 | VARCHAR(20) | L1 |  |  |
| rank 🔑 | INTEGER | L1 |  |  |
| ticker | VARCHAR(20) | L1 |  |  |
| name | VARCHAR(100) | L1 |  |  |
| score | FLOAT | L1 |  |  |
| composite | FLOAT | L1 |  |  |
| entry_price | INTEGER | L1 |  |  |
| tag | VARCHAR(40) | L1 |  |  |
| detail | VARCHAR(2000) | L1 |  |  |
| created_at | TIMESTAMP | L1 |  |  |

## recommendation

재무 유사 종목  
등급 **L1** · 보관 배치마다 교체

계보: ← financial_statement 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| base_ticker 🔑 | VARCHAR(20) | L1 |  |  |
| rank 🔑 | INTEGER | L1 |  |  |
| similar_ticker | VARCHAR(20) | L1 |  |  |
| distance | FLOAT | L1 |  |  |

## role_activation

관리 권한 JIT 활성 기록(사유·시간)  
등급 **L2** · 보관 3년(권한 내역 — 안전성 확보조치 기준 제5조③) 후 파기

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| username | VARCHAR(50) | L2 |  |  |
| role | VARCHAR(20) | L2 |  |  |
| session_id | VARCHAR(32) | L2 |  |  |
| reason | VARCHAR(200) | L2 |  |  |
| activated_at | DATETIME | L2 |  |  |
| expires_at | DATETIME | L2 |  |  |
| revoked_at | DATETIME | L2 |  |  |

## role_request

역할 부여 요청·승인(2인)  
등급 **L2** · 보관 3년(권한 내역 — 안전성 확보조치 기준 제5조③) 후 파기

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| username | VARCHAR(50) | L2 |  |  |
| role | VARCHAR(20) | L2 |  |  |
| reason | VARCHAR(200) | L2 |  |  |
| requested_by | VARCHAR(50) | L2 |  |  |
| requested_at | DATETIME | L2 |  |  |
| status | VARCHAR(10) | L2 |  |  |
| decided_by | VARCHAR(50) | L2 |  |  |
| decided_at | DATETIME | L2 |  |  |

## search_log

검색 기록(인기 검색어 집계용 — 사용자 식별 없음)  
등급 **L2** · 보관 DB 약 13개월(SEARCH_LOG_RETENTION_DAYS, 집계는 최근 90일) → 압축 보관 파일(archive_log)

계보: → archive_log 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| log_id 🔑 | INTEGER | L2 |  |  |
| keyword | VARCHAR(200) | L2 |  | 검색어(계정·IP 와 연결하지 않음) |
| searched_at | TIMESTAMP | L2 |  |  |
| ticker | VARCHAR(20) | L2 |  |  |

## security_assessment

금융보안 수준진단 결과(항목별 등급·근거 종류·운영 점검 — 개인정보 없음, 추가만)  
등급 **L2** · 보관 5년(진단 증빙) · 보호 append_only

계보: ← CATALOG · 쓰임: 관리 /admin/assessment(이력·퇴보 경보)·경영진 보고

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| at | DATETIME | L2 |  |  |
| actor | VARCHAR(50) | L2 |  |  |
| mode | VARCHAR(10) | L2 |  |  |
| overall | FLOAT | L2 |  |  |
| below_target | INTEGER | L2 |  |  |
| probe_failures | INTEGER | L2 |  |  |
| result | TEXT | L2 |  |  |
| digest | VARCHAR(64) | L2 |  |  |

## security_event

보안 이벤트·자동 대응 결과  
등급 **L3** · 보관 1년 후 파기(탈퇴 시 아이디 가명화) · 보호 pseudonym

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L3 |  |  |
| at | DATETIME | L3 |  |  |
| kind | VARCHAR(40) | L3 |  |  |
| severity | VARCHAR(8) | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| session_id | VARCHAR(32) | L3 |  |  |
| ip_hash | VARCHAR(16) | L2 | pseudonym | IP HMAC 가명값 |
| request_id | VARCHAR(64) | L3 |  |  |
| detail | TEXT | L3 |  |  |
| response | VARCHAR(60) | L3 |  |  |

## service_control

서비스 비상 차단 상태(기능별 운영/중지·사유·변경자)  
등급 **L2** · 보관 영구(변경 이력은 감사 로그)

계보: ← ADMIN · 쓰임: PEP(돈 동작 집행)·관리 /admin/controls

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| feature 🔑 | VARCHAR(20) | L2 |  |  |
| state | VARCHAR(8) | L2 |  |  |
| reason | VARCHAR(200) | L2 |  |  |
| changed_by | VARCHAR(50) | L2 |  |  |
| changed_at | DATETIME | L2 |  |  |

## service_control_request

서비스 재개 요청·승인(2인)  
등급 **L2** · 보관 3년(권한 내역과 같게) 후 파기

계보: ← ADMIN 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L2 |  |  |
| feature | VARCHAR(20) | L2 |  |  |
| reason | VARCHAR(200) | L2 |  |  |
| requested_by | VARCHAR(50) | L2 |  |  |
| requested_at | DATETIME | L2 |  |  |
| status | VARCHAR(10) | L2 |  |  |
| decided_by | VARCHAR(50) | L2 |  |  |
| decided_at | DATETIME | L2 |  |  |

## stock

국내 종목 마스터(유니버스)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← KRX 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ticker 🔑 | VARCHAR(20) | L1 |  |  |
| name | VARCHAR(100) | L1 |  |  |
| market | VARCHAR(20) | L1 |  |  |
| sector | VARCHAR(100) | L1 |  |  |

## survey_result

투자 성향 분석 결과(이력)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

계보: ← USER 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| result_id 🔑 | INTEGER | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| type | VARCHAR(20) | L3 |  |  |
| score | INTEGER | L3 |  |  |
| description | VARCHAR(300) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## uptime_check

가용성 확인 기록(1분 합성 모니터링 — 응답 코드·지연, 개인정보 없음)  
등급 **L2** · 보관 35일

계보: ← OPS → incident · 쓰임: 가용률(/admin/incidents)·장애 판정

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | BIGINT | L2 |  |  |
| at | DATETIME | L2 |  |  |
| ok | BOOLEAN | L2 |  |  |
| ms | INTEGER | L2 |  |  |
| code | SMALLINT | L2 |  |  |

## user_mfa

2단계 인증 비밀키(암호화)·복구 코드(해시)  
등급 **L4** · 보관 해제·탈퇴 시 즉시 파기 · 보호 encrypt, hash

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| username 🔑 | VARCHAR(50) | L4 |  |  |
| secret_enc | TEXT | L4 | encrypt | TOTP 비밀키 AES-256-GCM(AAD=user_mfa:<아이디>) |
| enabled_at | DATETIME | L4 |  |  |
| last_step | BIGINT | L4 |  |  |
| recovery_hashes | TEXT | L4 | hash | 복구 코드 SHA-256 목록 |
| created_at | DATETIME | L4 |  |  |

## user_role

관리 역할 자격(2인 승인·만료)  
등급 **L2** · 보관 회수 후 감사 로그로 추적

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| username 🔑 | VARCHAR(50) | L2 |  |  |
| role 🔑 | VARCHAR(20) | L2 |  |  |
| granted_by | VARCHAR(50) | L2 |  |  |
| approved_by | VARCHAR(50) | L2 |  |  |
| granted_at | DATETIME | L2 |  |  |
| expires_at | DATETIME | L2 |  |  |
| reason | VARCHAR(200) | L2 |  |  |

## vmarket_entry

모의투자 시즌 참가(참가금·시즌 현금·정산 금액)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← USER → wallet_ledger · 쓰임: 모의투자 화면 참가·수익률·시즌 기록(/vmarket/me)·마이페이지 지갑 내역

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| season_id 🔑 | INTEGER | L3 |  |  |
| username 🔑 | VARCHAR(50) | L3 |  |  |
| entry | BIGINT | L3 |  |  |
| cash | BIGINT | L3 |  |  |
| realized | FLOAT | L3 |  |  |
| joined_bar | SMALLINT | L3 |  |  |
| status | VARCHAR(8) | L3 |  |  |
| final | BIGINT | L3 |  |  |
| auto_sold | BIGINT | L3 |  |  |
| auto_stocks | SMALLINT | L3 |  |  |
| joined_at | TIMESTAMP | L3 |  |  |
| settled_at | TIMESTAMP | L3 |  |  |

## vmarket_position

모의투자 시즌 종목별 보유(수량·평균단가·실현손익)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← vmarket_trade 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| season_id 🔑 | INTEGER | L3 |  |  |
| username 🔑 | VARCHAR(50) | L3 |  |  |
| stock_idx 🔑 | SMALLINT | L3 |  |  |
| shares | BIGINT | L3 |  |  |
| avg_price | FLOAT | L3 |  |  |
| realized | FLOAT | L3 |  |  |

## vmarket_season

모의투자 가상 시장 시즌 데이터(가상 종목 5개 × 봉 185개·SHA-256 — 실제 시세 아님, 미래 봉 비공개)  
등급 **L2** · 보관 영구(지난 시즌 결과 다시 보기 근거)

계보: ← TEAM → vmarket_trade · 쓰임: 모의투자 화면 차트(/vmarket/season — 공개된 봉까지만)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| season_id 🔑 | INTEGER | L2 |  |  |
| week_start | DATE | L2 |  |  |
| payload | TEXT | L2 |  |  |
| sha256 | VARCHAR(64) | L2 |  |  |
| source | VARCHAR(60) | L2 |  |  |
| imported_at | TIMESTAMP | L2 |  |  |

## vmarket_trade

모의투자 시즌 체결(매수·매도·정산 자동 매도)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← vmarket_season → vmarket_position 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| trade_id 🔑 | BIGINT | L3 |  |  |
| season_id | INTEGER | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| stock_idx | SMALLINT | L3 |  |  |
| side | VARCHAR(4) | L3 |  |  |
| qty | BIGINT | L3 |  |  |
| price | INTEGER | L3 |  |  |
| bar | SMALLINT | L3 |  |  |
| auto | BOOLEAN | L3 |  |  |
| idem_key | VARCHAR(80) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## wallet

가상머니 지갑 잔액(낙관적 잠금 version)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← wallet_ledger · 쓰임: 마이페이지 지갑

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| username 🔑 | VARCHAR(50) | L3 |  |  |
| balance | BIGINT | L3 |  |  |
| version | INTEGER | L3 |  |  |
| updated_at | TIMESTAMP | L3 |  |  |
| account_no | VARCHAR(16) | L3 |  |  |

## wallet_ledger

지갑 원장(모든 입출금 — 잔액 대사 근거)  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `username`

계보: ← USER, avatar_closet, iap_purchase, payment_order, quiz_attempt, vmarket_entry → fds_decision, journal_entry, outbox, wallet 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| ledger_id 🔑 | INTEGER | L3 |  |  |
| username | VARCHAR(50) | L3 |  |  |
| reason | VARCHAR(20) | L3 |  |  |
| amount | BIGINT | L3 |  |  |
| balance_after | BIGINT | L3 |  |  |
| ref | VARCHAR(60) | L3 |  |  |
| memo | VARCHAR(200) | L3 |  |  |
| idem_key | VARCHAR(80) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## watchlist

관심종목  
등급 **L3** · 보관 탈퇴 시 즉시 파기 · 보호 rls · RLS 주인 열 `user_id`

계보: ← USER 

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| id 🔑 | INTEGER | L3 |  |  |
| user_id | VARCHAR(50) | L3 |  |  |
| ticker | VARCHAR(20) | L3 |  |  |
| created_at | TIMESTAMP | L3 |  |  |

## yangbong_basket

양봉이 주간 추천 바구니(고른 5종목·30일 채점·코스피 비교 — 팀 백테스트 247주 포함)  
등급 **L1** · 보관 영구(공개 데이터 — 원천 정정 시 갱신)

계보: ← TEAM, analytics.price_history, index_daily · 쓰임: 홈 '이번 주 양봉이가 고른 5종목'·'한 달 전 양봉이 바구니 결과'(/yangbong/weekly)

| 열 | 형식 | 등급 | 보호 | 설명 |
|---|---|---|---|---|
| signal_date 🔑 | DATE | L1 |  |  |
| buy_date | DATE | L1 |  |  |
| sell_date | DATE | L1 |  |  |
| codes | ARRAY | L1 |  |  |
| names | ARRAY | L1 |  |  |
| stock_returns | ARRAY | L1 |  |  |
| basket_return | FLOAT | L1 |  |  |
| kospi_return | FLOAT | L1 |  |  |
| source | VARCHAR(8) | L1 |  |  |
| picked_at | TIMESTAMP | L1 |  |  |
| scored_at | TIMESTAMP | L1 |  |  |
