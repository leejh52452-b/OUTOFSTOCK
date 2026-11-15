# 보안·배포 검증 보고서 — 2026-10-21~24

무엇을 '됐다'고 말할 수 있는지를 실제로 띄워서 확인한 기록이다. 각 항목은 **시도 → 기대 → 결과**이고, ID 는
위협 모델([`threat_model.md`](threat_model.md))과 제로트러스트 자가 진단([`zero_trust.md`](zero_trust.md))에서 참조한다.
환경: 개발 샌드박스(리눅스 6.18, Docker 29 · compose v5, PostgreSQL 16). Docker Hub 가 막혀 공식 이미지 대신
우분투 24.04 rootfs 로 만든 대체 이미지를 썼다(설정·권한 구조는 같음). VM 경로는 **우분투 24.04 + systemd 255(cgroup v2)
컨테이너**에서 `deploy/vm.sh` 를 그대로 실행했다.

## A. 애플리케이션 공격 시나리오

| ID | 시도 | 기대 | 결과 |
|---|---|---|---|
| A-1 | 같은 IP·아이디로 연속 오입력 → 다른 아이디로 대입 | 5회째 잠금(없는 아이디와 같은 응답) · IP 다수 실패 → 일시 차단 | 통과 — `test_zero_trust.py` 3건(잠금·열거 방지·IP 차단) |
| A-2 | 쿠키 세션으로 CSRF 헤더 없이 변경 요청 · 다른 Origin 에서 로그인 | 403 `csrf_failed` · 거부 | 통과 — compose 실기동(403/200) · VM 컨테이너(403/200) · 시험 2건 |
| A-3 | 화면에 이벤트 핸들러가 든 HTML 주입(Trusted Types 우회 시도) | 신뢰 타입이 막고 `/csp-report` 에 위반 보고 | 통과 — E2E ⑦ · `test_web_security.py`(보고 수집·폭주 상한) |
| A-4 | 쓴 갱신 토큰 재사용 · 다른 브라우저 계열에서 같은 토큰 | 사용자 전체 세션 폐기 · 세션 폐기 + 경보 | 통과 — `test_access_expiry_refresh_rotation_grace_and_reuse_detection` · `test_token_replayed_from_another_device…` |
| A-5 | 100만원 송금 · 탈퇴(방금 로그인한 세션 포함 — 2026-10-23 부터 로그인은 추가 인증으로 치지 않음) | 추가 인증 창 → 비밀번호 → 송금(입력 유지)·탈퇴 | 통과 — E2E ⑥⑨ · `test_stolen_session_right_after_login_cannot_skip_step_up` |
| A-6 | 클라이언트가 `X-Client-Cert-Verified: SUCCESS`·가짜 `X-Forwarded-For` 를 보냄 | nginx 가 덮어씀 → /admin 403 · 시도 제한은 실제 IP 기준 | 통과 — VM 컨테이너(위조 헤더 403) · nginx 설정 4종 정책 시험 |
| A-7 | 비로그인으로 화면 전부 순회 | JS 오류 0 · CSP 위반 0 · 인라인 핸들러 0 | 통과 — E2E ① |
| A-8 | 관리 역할 없이·사유 없이·2단계 없이 관리 동작 / 혼자 권한 부여 | 거부 · 거부 · 거부 · 두 번째 관리자 필요 | 통과 — `test_jit_elevation…` · `test_role_grant_needs_two_different_admins` |

## E. 경계(edge)

| ID | 시도 | 기대 | 결과 |
|---|---|---|---|
| E-1 | compose 운영 오버레이의 공개 포트 | edge(80/443 → 8080/8443) 만 | 통과 — 실기동 + `test_compose_prod_least_privilege_and_segmentation`(합쳐진 설정 검사) |
| E-2 | `/docs`·`/openapi.json`·`/.env` | 404 · 404 · 403 | 통과 — compose 실기동 |
| E-3 | 관리 기기 인증서 없이 `/admin/*`(TLS 판) · 헤더만 위조 | 403 · 403 | 통과 — VM 컨테이너(nginx TLS + 사설 CA) |
| E-4 | nginx 경유 SSE(`/events`) | 버퍼링 없이 즉시 `hello` 이벤트 | 통과 — VM 컨테이너(1초 안에 수신) |
| E-5 | 80·443 이 아닌 포트로 서비스(compose `HTTP_PORT=8000` 등)할 때 웹 로그인 · 같은 서버 다른 포트 출처의 변경 요청 | 200 · 403 `csrf_failed` | 처음엔 **정상 로그인도 403**(결함 13) → 고친 뒤 통과 — nginx 1.24 실측(HTTP/1.1·HTTP/2) · `test_same_origin_check_is_port_exact` · 설정 시험 4종 |
| E-6 | 프론트 개발 서버(Vite 프록시 → nginx → 앱)에서 쿠키 로그인 · CSRF · 갱신 · SSE · 로그아웃 | 전부 동작, 접근 토큰은 JS 로 못 읽음 | 통과 — 실제 크로미움(2026-10-22, `frontend/FRONTEND_API_GUIDE.md` 1.1 설정 그대로 · `types.ts` 래퍼의 만료→갱신→추가 인증 재시도 포함) |

## N. 네트워크·데이터

| ID | 시도 | 기대 | 결과 |
|---|---|---|---|
| N-1 | compose: 앱 컨테이너에서 인터넷(1.1.1.1:443) 직접 연결 | 불가(내부망만) | 통과 — OSError |
| N-2 | VM: nginx 가 있으면 앱 바인딩 · 바깥 IP 로 :8000 | 127.0.0.1 · 연결 거부 | 통과 — `BIND_HOST=auto` → 127.0.0.1, 바깥 연결 rc=7 |
| N-3 | 앱↔DB TLS(임시 클러스터): 평문 TCP · verify-full · 다른 CA · 이름 불일치 | 거부 · 성공(TLSv1.3) · 거부 · 거부 | 통과 |
| N-4 | egress 3중 — ① 목록 밖 호스트 ② 앱 검사를 끈 채 ③ 프록시를 우회해 직접 ④ 허용 이름이 내부 주소로 풀림(DNS rebinding) ⑤ 메타데이터 IP | ① EgressDenied ② 프록시 403 ③ 커널 차단(VM)·내부망(compose) ④ 프록시 403 ⑤ 차단 | 통과 — VM 컨테이너 API cgroup 안에서 실측, compose 실기동 · squid 로그 `TCP_DENIED/403` |
| N-5 | RLS: 앱 계정 직접 접속(문맥 없음) · 주체=사용자1 · 남의 지갑 UPDATE · 감사 로그 DELETE | 0행 · 자기 1행 · 0행 · 권한 없음 | 통과 — compose 실기동 · `test_rls.py` · 최소 권한 모드 전체 시험 669 passed |

## S. 시스템

| ID | 시도 | 기대 | 결과 |
|---|---|---|---|
| S-1 | 실행 이미지: 사용자·pip·코드 쓰기·루트 FS·capability·권한 상승 | uid 10001 · 없음 · 불가 · 읽기 전용 · CapEff 0 · NoNewPrivs 1 | 통과 |
| S-2 | systemd 샌드박스(API 의 마운트 네임스페이스에서): 홈 폴더 · SSH 키 · 셸 기록 · 코드 쓰기 · var/ 쓰기 · /etc 쓰기 · /tmp · 백업 폴더 | 코드 폴더만 보임 · 없음 · 없음 · EROFS · 가능 · EROFS · 비공개 · 안 보임 | 통과 — 노출 점수 9.0 UNSAFE → **1.2 OK**(postgresql·ssh 기본 9.6) · CapBnd 0 · Seccomp 2 |
| S-3 | `vm.sh harden` | ufw 기본 거부·SSH limit·80/443 · sshd 드롭인(`sshd -t` 통과) · 자동 보안 업데이트 · sysctl · PG 접속 기록 | 통과 — 바꾼 파일 목록 출력, 실패 단계는 경고 후 계속 |

## DR. 백업·복구

| ID | 시도 | 기대 | 결과 |
|---|---|---|---|
| DR-1 | 샌드박스 안 백업 서비스(age 암호화) → `vm.sh backups` | 암호화 · manifest · 목록 | 통과 — 0.22MB · 75표 · 139행 |
| DR-2 | `vm.sh restore` — 복호화 → 새 DB 복원 → 검증 8항목 → 이름 교체 → 서비스 재시작 | 실패 시 운영 DB 그대로, 성공 시 로그인 정상 | 통과 — **6.8초**, 검증 8/8 |
| DR-3 | 격리 DB 복구 훈련(tools/dr.py drill) | RTO·RPO 실측 보고서 | 통과 — RTO 2.3~2.7초(0.8MB · 75표 · 3만 행) — `docs/dr/drills/` |
| DR-4 | 시점 복구(PITR): '사고' 커밋 직전으로 | 사고 직전 상태 | 통과 — 7.9초 |

## D. 배포 경로

| ID | 시도 | 결과 |
|---|---|---|
| D-1 | compose: `./deploy/deploy.sh`(빌드 → DB → roles.sql → 마이그레이션(소유자) → 기동 → healthy → 시드) | 2분 43초 · 서비스 6개 healthy/정상 종료 |
| D-2 | VM: `bash deploy/vm.sh install`(systemd) | 54초 · 샌드박스 켬 · 헬스 정상 |
| D-3 | VM: `bash deploy/vm.sh update 설치본`(322~325파일) · `rollback` → `update` 왕복 | 8.1초 · 설정 유지(BIND_HOST·HARDEN·EGRESS_PROXY) · 왕복 정상 |
| D-4 | 설치본 변조(한 글자) · 전송 중 잘림 | 둘 다 아무것도 풀지 않고 중단(SHA-256·끝 표식) |
| D-5 | 정책 시험·CI | `test_deploy_config.py` 27개 · 전체 시험(파이썬 3.12, 해시 고정 설치, 복원 훈련 포함) · 최소 권한 모드 · actionlint 통과 — 숫자는 아래 '최종 회귀' |
| D-6 | (2026-10-23 최종 설치본) 새 VM 에 `install` → nginx(HTTP) 뒤로 `units` → `update` | 설치 1분 42초(패키지 52개 모두 **잠금 파일 해시 설치** — 범위 설치로 내려가지 않음) · 앱 127.0.0.1 · 바깥 :8000 연결 거부 · nginx **80·8081 포트 모두** 웹 로그인·CSRF·갱신 200, 다른 포트 출처 403 · update 7.4초(사전 백업·마이그레이션 V013·설정 유지) · 방금 로그인한 세션의 100만 원 송금 401 → 추가 인증 → 200 · 노출 점수 1.3 |

## 발견하고 고친 것(검증이 실제로 잡은 결함)

| # | 발견 | 영향 | 수정 |
|---|---|---|---|
| 1 | 공식 postgres 이미지의 `POSTGRES_USER` 는 슈퍼유저 — 앱이 그 계정이면 RLS 가 통째로 우회 | 데이터 격리 무력화 | 앱은 `oos_app` 만, 비밀번호 없으면 compose 가 기동 거부 |
| 2 | FORCE RLS 뒤 일반 계정 `pg_dump` 가 실패 · `--enable-row-security` 만 주면 **빈 표를 조용히** 덤프 | 백업 매일 실패 / 빈 백업 | 백업 전용 계정(BYPASSRLS·읽기 전용) + 행 수 대조로 빈 백업 탐지 |
| 3 | 덮어쓰기 복원(`pg_restore --clean`)이 분할 표 제약에서 실패 → 반쯤 복구된 DB | 복구 실패 | 새 DB 에 복원 → 검증 → 이름 교체(한 트랜잭션) |
| 4 | 우분투 24.04 는 첫 접속 전 `/run/sshd` 가 없어 `sshd -t` 실패 | 하드닝 중단 | 검사 전에 폴더 생성 |
| 5 | root 소유 파일이 있으면 update 의 복사가 중간에 실패 → 코드 반쯤 바뀐 채 서비스 정지 | 서비스 중단 | 서비스를 멈추기 전에 소유자 점검 + 중단 시 rollback 안내 |
| 6 | 키 없는 실시간 수집기를 짧게 여러 번 재시작 → systemd 시작 횟수 제한 → restart 실패 | 헬스체크 전에 종료 | reset-failed + 서비스별 재시작 |
| 7 | update 가 '옛' 스크립트로 서비스 파일을 씀 | 새 보안 설정이 다음 실행까지 빠짐 | 코드 교체 뒤 새 스크립트로 이어서 실행(`exec … _post-update`) |
| 8 | 백업 공개키 파일이 설치본에 들어가면 update 가 운영 키를 덮어씀 | 백업 평문화·실패 | 예시 파일만 배포(`recipients.example.txt`) |
| 9 | IPv6 를 끈 VM 에서 `listen [::]:80` 이 있으면 nginx 기동 실패 | 서비스 불가 | 기본 설정에서 제외(주석 안내) |
| 10 | 숨은 문자 점검이 `services/auth.py` 의 폭 없는 공백(사칭 차단 목록)을 잡음 → CI security 실패 상태 | 점검 무력화 | 소스는 `\u200b` 이스케이프로 |
| 11 | 프록시 허용 목록이 이름만 봄 — 허용 이름이 내부 주소로 풀리면 통과 | DNS rebinding | squid `to_internal` 거부를 허용 규칙보다 먼저 |
| 12 | `requirements.txt` 에서 pytest 를 뺀 뒤 CI 가 여전히 그 파일로 설치 | CI 실패 | CI 를 잠금 파일 + 시험 도구로 |
| 13 | nginx 가 `Host` 를 `$host`(포트 없음)로 넘김 — 80·443 이 아닌 포트(학교 방화벽 때문에 `HTTP_PORT=8000` 등)면 브라우저 Origin `http://주소:8000` ≠ 앱이 보는 `http://주소` | **웹 로그인·모든 변경 요청 403** — Vite 프록시 개발 구성도 같은 이유로 실패 | 4개 설정 모두 `$http_host`(포트 포함). 출처 비교는 포트까지 정확히 유지(같은 서버 다른 포트의 앱은 여전히 거부) |
| 14 | 문서 2곳에 실제 숨은 문자(U+200B·U+202E)·시험 코드에 한글 채움 문자(U+3164) | 검토자를 속일 수 있는 문자 — 점검이 코드 확장자만 봐서 놓침 | 이스케이프 표기로 + 점검 범위를 문서·설정·잠금 파일까지(`tools/security_scan.sh`) |
| 15 | 로그인 시각을 '최근 재확인'으로 인정 — 로그인 직후 5분은 탈취된 세션도 고위험 동작 통과(독립 점검 R-1) | 계정 탈취 직후 인출·탈퇴 | 추가 인증 시각만 인정 · 시험 `test_stolen_session_right_after_login_cannot_skip_step_up` · E2E ⑥⑨ |
| 16 | 두 사람 표 RLS 쓰기 정책이 '어느 한쪽이 나'(R-3) | 보낸 사람 위조(앱 버그 시) | V013 명령별 정책 + 열 단위 UPDATE 권한 · `test_two_party_writes_cannot_forge_the_sender` |
| 17 | compose 앱이 모든 망에서 듣고 전달 헤더를 전부 신뢰(R-5) | 내부 컨테이너의 헤더 위조 | web 망 별칭에서만 listen · 이미지 기본은 같은 컨테이너만 신뢰 · 실측(다른 망 → 연결 거부) |
| 18 | `vm.sh status` 의 '보안 설정 점검' 요약 줄이 한 줄 파이썬(f-string 안의 `\"`)의 문법 오류로 **항상 조용히 빠져 있었다**(오류는 `2>/dev/null`) | 운영자가 FAIL 을 못 봄 | 문법 수정 + 셸 속 `python -c` 코드를 모두 컴파일해 보는 시험(`test_inline_python_snippets_compile`) |
| 19 | 차단 기록의 '5분에 한 번' 중복 억제가 '기록한 적 없음'을 monotonic 0 으로 봄 — monotonic 은 부팅 뒤 초라서 **서버가 켜진 지 5분 안의 egress 차단이 기록되지 않았다**(IP 자동 차단 목록도 부팅 30초 안엔 안 읽힘) | 재부팅 직후(systemd 가 앱을 바로 띄우는 때)의 유출 시도가 보안 이벤트에 안 남음 | '없음'은 None/−∞ · 시험 `test_first_block_right_after_boot_is_recorded` · `test_ip_block_applies_even_right_after_boot`(전체 시험이 부팅 직후 돌 때만 실패하던 원인) |
| 20 | FDS 판정이 지갑 잠금 '밖'(API)에서 먼저 — **TOCTOU** | 동시에 보낸 10건이 모두 '최근 10분 0건'을 보고 통과(속도 규칙 무력화) | 판정을 송금 트랜잭션 안·두 지갑 잠금 뒤로(동시 10건 → 5건 실행·5건 추가 인증) · `test_parallel_transfers_cannot_slip_past_velocity` |
| 21 | 같은 Idempotency-Key 재시도를 판정보다 늦게 확인 | 처리된 송금을 다시 채점해 4xx → 화면이 새 키로 다시 보내 **이중 송금** 가능 | 처리 여부 확인 → 판정 순서 고정 · `test_retry_of_completed_transfer_returns_original_result_without_rescoring` |
| 22 | 받는 사람 앞뒤 공백: 실행은 정규화한 값, 판정은 원문 | 집금·왕복 규칙 회피 | 요청 모델에서 한 번 정규화 · `test_payee_is_normalized_once_for_screening_and_execution` |
| 23 | 탈퇴가 잔액을 잠그지 않고 읽음 · 지갑 생성이 계정 존재를 확인하지 않음 | 진행 중 입금이 **지갑 없는 계좌에 붕 뜸**(마감 대사도 못 봄) · 탈퇴한 아이디로 지갑 부활(재가입자가 물려받음) | 지갑 쓰기 = 계정 행 KEY SHARE(잠금 순서 계정 → 업무 행 → 지갑) · 소멸 FOR UPDATE · 마감에 고아 계좌 0 · `test_deletion_waits_for_in_flight_credit…` 외 2 |
| 24 | 일 마감이 '어제'만 · CLI 는 오늘·미래도 마감 | 서버가 꺼진 날은 영영 미마감·사슬이 건너뜀 / 나중에 '변조' 오경보 | 빠진 날을 날짜순으로 메우는 `close_pending` · 끝나지 않은 날·사슬 앞쪽 거부 · 검증이 빠진 날 보고 · `test_missed_days_are_closed_in_order…` |
| 25 | 마감 해시가 합계만 덮음 | 합계를 유지한 채 거래 사이 금액을 옮기는 변조 미탐지 | 분개 한 줄 한 줄 다이제스트(canon 2) + 마감 해시를 감사 체인(외부 증인)에 고정 · `test_aggregate_preserving_swap…` · `test_rewritten_close_record_is_caught_by_the_audit_anchor` |
| 26 | 아웃박스 처리기가 꺼낸 트랜잭션 안에서 외부 전송 · 전송 실패를 삼킴 | 유휴 트랜잭션 제한에 끊기면 묶음 롤백·재전송 / 알림 유실이 '완료'로 | 리스 방식(빌리고 커밋 → 트랜잭션 밖 처리 → 사건마다 완료/재시도) · 채널 전부 실패는 재시도 · 시험 3개 |
| 27 | 분개마다 앱 시계를 따로 읽음 | 자정에 걸친 송금이 가계정을 이틀로 쪼개 마감 실패(오경보) | 분개 시각 = DB 트랜잭션 시각 · `test_entries_of_one_transaction_share_one_timestamp` |
| 28 | 법정 보관 '1년·3년'을 365·1095일로 셈 · 권한 요청 내역을 요청 시각으로 셈 | 윤년이 낀 해엔 하루 일찍 파기(최소 보관 위반) · 늦게 결정된 권한 내역이 3년 전에 파기 | 달력 기준 하한(`retention_cut`) · 마지막 결정 시각 기준 · 시험 2개 |
| 29 | 데이터 계약이 `CURRENT_DATE`(DB 서버 시간대) | UTC 서버에서 07:50 측정 때 그날 새벽 기사를 '미래 발행'으로, 신선도는 하루 덜 늦게 | 한국 날짜 식으로 · 추세도 한국 날짜로 묶음 · `test_contract_dates_are_korean_dates…` |
| 30 | 파기 배치의 일부 단계 예외가 그대로 전파 | 앞서 지운 건수의 **파기 기록(감사) 누락** · 같은 일일 작업의 요약·**외부 증인**까지 그날 빠짐 | 단계마다 따로 실행·파기 기록은 항상(부분 실패 표시)·실패는 다 돌린 뒤 알림 · 권한 부족만 '건너뜀'(다른 오류를 권한 탓으로 숨기지 않음) · 시험 2개 |
| 31 | 복원 검증 프로세스의 환경에서 다른 DB 주소를 '지움' | 앱 설정의 `load_dotenv` 가 운영 `.env` 값으로 되살려 **복원 훈련의 init_db 가 운영 DB 에서 실행**, '스키마 최신' 판정도 운영 DB 기준(실측: 옛 코드는 검증 프로세스가 운영 주소를 봄) | 빈 값으로 고정 + 앱 기동 전 '모든 DB 주소 = 복원 대상' 확인(어긋나면 중단) · `test_verify_process_cannot_fall_back_to_production_db_urls` |
| 32 | 시작 잔액 분개가 마이그레이션 잠금 밖 | 프로세스 동시 기동 때 같은 지갑을 두 번 분개(옛 코드로 재현) | 트랜잭션 잠금 · `test_opening_balances_run_once…` |
| 33 | 예전 공용 데모 실습 세션(주인 'guest')을 누구에게나 열어 둠 — **취약점 자가 점검 A4(IDOR)가 2026-11-02 첫 실행에서 발견** | 번호만 알면 로그인 없이도 그 실습을 읽고·진행하고·매매·종료(화면은 방문자마다 게스트 ID 라 정상 경로로는 쓰이지 않던 세션) | 주인 'guest' 세션은 아무에게도 열지 않음(404 + 보안 이벤트) · `test_pentest.py::test_legacy_shared_demo_session_is_not_reachable_by_number` · [점검 보고서(발견)](pentest/pentest-20261102T221508.md) → [수정 후](pentest/pentest-20261102T221702.md) |

## 정밀 점검(2026-10-24) — 내부 처리·데이터 무결성

범위: 돈이 움직이는 경로(송금·FDS·지갑·탈퇴·원장 마감·아웃박스)와 보관·파기·복원 훈련. 방법: 코드 추적으로 가설 → **옛 코드(10-23 전달본)에서 재현** →
회귀 시험을 먼저 쓰고(옛 코드에서 실패) → 수정. 결과: 결함 19~32(위 표).

| 확인 | 결과 |
|---|---|
| 옛 코드 재현(별도 사본, 같은 시험 DB) | FDS 3건(동시 10건 전부 통과 · 재시도 401 · 공백 회피) · egress/IP 차단 2건 · 원장·탈퇴·아웃박스·보관·계약·시작 잔액 12건 **모두 재현** · 복원 검증 환경: 옛 코드는 검증 프로세스가 운영 `MIGRATION_DATABASE_URL` 을 읽음 → 수정 후 빈 값 |
| 새 회귀 시험 | 위 결함마다 1개 이상(옛 코드에서 실패 → 새 코드에서 통과) |
| 작은 개선 | 개인정보 열람 점검이 2,000건에서 조용히 잘리던 것 → 전체 건수·`truncated` 표시 · 운영 기준선 PAY-1(결제 검증 stub 이면 INFO 로 명시) |

탐지 통제 추가: 실행된 송금과 FDS 판정을 같은 트랜잭션에서 커밋하고 송금 키로 잇는다 → 데이터 계약 `wallet.transfer_unscreened`(critical)가
'최근 30일 판정 없이 실행된 송금 0건'을 매일 확인한다(판정을 거치지 않는 경로가 새로 생겨도 다음 날 드러난다).

## 최종 회귀(2026-10-24 — 이 판 그대로)

| 점검 | 결과 |
|---|---|
| 전체 시험(파이썬 3.12 · 잠금 파일 해시 설치 · 실제 복원 훈련) | **913 passed**(11-03: DB 타입 표준·재작성 마이그레이션(대사·롤백)·수명주기 보관·복원·DB 진단·튜닝 권장값 시험 추가 — 최소 권한 915 passed · 11-02 3차: 배포 게이트·장애 판정·사고보고·경보 확인·SIEM·자가 점검 시험 추가 — 최소 권한 890 passed · 11-02 2차: 수준진단·위험 등록부·파일 무결성·권한 생명주기·모의훈련 시험 추가 · 11-02: 운영 복원력·기기 결속·DLP·SQL 예산 시험 추가 · 10-31: 양봉이 꾸미기 시험 추가 · 퀴즈 보상 시험 추가 · 10-27: 팀 화면 연동 시험 추가 · 10-23: 683 → 정밀 점검 회귀 시험 24개 추가) |
| 최소 권한 모드(앱 계정 oos_app 으로 전체) · `db_roles_check --strict` | **856 passed · 1 skipped**(11-02 2차, 복원 훈련은 DB 생성 권한 필요) · 차이 0(무결성 기준선 쓰기·진단 기록 수정 권한 없음 포함) |
| 시간대 교차(프로세스 TZ × DB 세션 시간대: UTC·서울·UTC−12) — 원장·계약·보관·아웃박스·FDS·egress | 3조합 모두 51/51 |
| 브라우저 E2E(쿠키·CSRF·갱신·추가 인증·SSE·CSP/Trusted Types·탈퇴, axe) | **18/18** · 접근성 위반 0(새 열 `fds_decision.ref` 는 기동 시 자동 추가 확인) |
| 개발 DB 에서 마감 따라잡기 | 첫 분개일(10-20)부터 어제까지 4일 날짜순 마감 → 검증: 깨진 날·빠진 날 0 · 감사 체인 앵커 4/4 · 시산표 0 · 오늘 마감은 거부 |
| ruff · shellcheck(-S warning) · 셸 속 파이썬 컴파일 · 화면 스크립트 문법 · 잠금 파일 대조 | 모두 통과 |
| 의존성 취약점(pip-audit, 잠금 52개) · bandit · 숨은 문자(코드·설정·문서) · 운영 견본 기준선(24항목) | 0 · B608 외 0 · 0 · FAIL 0 · WARN 0 |
| VM(실제 systemd) 새 설치 → nginx → update | D-6(10-23 측정 — 이번 변경은 배포 스크립트·서비스 정의를 바꾸지 않음) |
| 프론트 연동 구성(Vite 프록시 → nginx → 앱, `types.ts` 래퍼) | E-6 |

## 독립 점검(2026-10-23) — 구현에 참여하지 않은 검토자의 공격자 관점 검토

범위: `app/security/*`(세션·PEP/PDP·RBAC·RLS·암호화·egress·TOTP) · 인증·송금·관리·SSE·소유 확인 경로 · 원장·FDS·아웃박스·채팅·친구 · nginx·compose·vm.sh·roles.sql.
방법: 코드 추적 + 개발 서버에 대한 비파괴 탐침(일회용 계정). 지적마다 **재현 → 판단 → 조치**.

| # | 지적 | 판단 | 조치 |
|---|---|---|---|
| R-1 | '최근 5분 재확인'에 로그인 시각도 쳐서, 로그인 직후 탈취된 세션이 다른 IP 대역에서도 탈퇴·고액 송금·FDS 추가 인증 등급을 재확인 없이 통과 | **유효** — 실측: 로그인 직후 150만 원 송금 200 | 추가 인증 시각만 인정(결함 15) |
| R-2 | 아이디 단위 잠금을 남이 일부러 걸 수 있다(분산 IP 로 반복 → 최대 24시간) | 유효하나 **감수** | 규정(제5조⑥)·금융권 관행상 잠금 유지, 경보·관리자 해제(T-29) |
| R-3 | 두 사람 표의 RLS WITH CHECK 가 '어느 한쪽이 나' — 인가 버그가 생기면 보낸 사람을 위조한 행을 쓸 수 있다(지금 앱 경로로는 불가) | **유효**(2차 방어의 빈틈) | 명령별 정책 + 열 단위 권한(V013, 결함 16) |
| R-4 | VM 의 uvicorn 이 `--proxy-headers` 없이 떠 HTTPS 뒤에서도 Secure 쿠키가 안 나갈 것 | **오탐** — uvicorn 0.54 는 기본으로 전달 헤더를 켜고 127.0.0.1 을 신뢰(실측: nginx TLS·HTTP/2 뒤 `__Host-` 쿠키에 Secure) | 없음 |
| R-5 | compose 앱이 0.0.0.0 + 전달 헤더 전부 신뢰 — data·egress 망 컨테이너(인터넷에 닿는 프록시 포함)가 edge 를 건너뛰어 IP·인증서 확인 헤더 위조 | **유효**(내부 구성요소 침해 전제) | 앱은 web 망 별칭에서만 듣는다(결함 17, T-30) |
| R-6 | 결제 스텁 모드는 아무 영수증이나 적립 · 개발 기본값 `DOCS_ENABLED=true` | 의도된 데모 기본값(가상머니 — 현금화 불가). 운영 견본은 문서 경로를 막고, 결제는 데모 충전을 위해 stub 유지 | 문서화 · 운영 기준선 PAY-1 이 stub 을 INFO 로 표시(10-24 — 예전 판의 '둘 다 막음'은 결제에 대해선 틀린 서술이라 고침) |

견고하다고 확인된 것(탐침 포함): 남의 투자·실습 번호로 접근(IDOR) · 연결 풀에서의 RLS 문맥 · 게스트 실습 격리(위조 쿠키 포함) · CSRF(토큰·Origin) ·
세션 고정·갱신 토큰 재사용 탐지 · 로그아웃 뒤 SSE 종료 · 멱등키(보낸 사람 범위·다른 내용 409) · 잔액·출석 경쟁 조건 · SQL 주입 · SSRF(리다이렉트·IPv4 매핑 포함) ·
nginx 헤더 덮어쓰기 · 공개 응답의 정보 노출 · 관리자 JIT·2인 승인 · 봉투 암호화·TOTP 재사용 차단.

## 재현

```bash
python -m pytest -q                                   # 전체(복원 훈련: DR_TEST_ADMIN_URL)
python -m pytest -q tests/test_deploy_config.py      # 배포 정책 27개(compose 검사는 docker 필요)
python tools/security_check.py                        # 운영 설정 기준선(FAIL 이 있으면 종료 코드 1)
bash deploy/vm.sh status                              # 샌드박스 노출 점수·egress·백업·훈련·설정 점검 요약
python tools/e2e_web.py --base http://127.0.0.1:8000 --axe axe.min.js   # 브라우저 E2E
```
