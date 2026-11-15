# 위협 모델(STRIDE) — OUTOFSTOCK

방법: 자산과 신뢰 경계를 먼저 정하고, 데이터 흐름마다 STRIDE(위장·변조·부인·정보 노출·서비스 거부·권한 상승)를 대입했다.
위협마다 **막는 장치(코드) → 확인 방법(시험·실측) → 남은 위험**을 한 줄로 잇는다. 확인 ID(E-·N-·A-…)는
[`verification_report_2026-10-21.md`](verification_report_2026-10-21.md) 의 실측 항목이다. 갱신 시점: 2026-10-23(독립 점검 반영 — T-06·T-09·T-16 보강, T-29·T-30 추가) · 2026-11-02(T-32~T-38) · 2026-11-09(T-41 모의투자 서버 권위·T-42 양봉이 추천 데이터) · 2026-11-03(T-39~T-40 — 위협마다 가능성×영향 점수·처리·담당은 [`risk_register.md`](risk_register.md), '확인' 열의 시험은 `tests/test_risk_register.py` 가 실제로 있는지 검사).

## 1. 자산(데이터 분류 L1~L4 — `app/security/classification.py`)

| 자산 | 등급 | 유출·변조 시 피해 |
|---|---|---|
| 비밀번호 해시·세션/갱신 토큰 해시·TOTP 비밀키·복구 코드 | L4 | 계정 탈취 |
| 마스터 키·데이터 키(KEK)·DB 비밀번호·API 키(.env) | L4 | 전부 — 암호문 복호화·외부 API 도용 |
| 채팅 본문·결제 키·KIS 토큰(봉투 암호화 열) | L4 | 사생활·외부 계정 도용 |
| 가상머니 지갑·원장·송금·투자 기록 | L3 | 부정 이체·잔액 조작(신뢰 붕괴) |
| 계정·접속 기록·관심종목·학습·실습·성향 | L3 | 개인 활동 노출 |
| 감사 로그·보안 이벤트·개인정보 열람 기록 | L2(무결성 중요) | 부인·사고 은폐 |
| 백업(전체 DB 사본) | L4 상당 | 위 전부 |
| 시세·뉴스·추천(공개) | L1(무결성 중요) | 잘못된 투자 학습 |

## 2. 신뢰 경계와 흐름

```
[TB1 인터넷] ── 브라우저 ──▶ nginx(edge) ──[TB2]──▶ 앱(API·SSE) ──[TB3]──▶ PostgreSQL
                                                     │   ▲                      ▲
                     스케줄러·실시간 수집기 ─────────┘   │ pg_notify             │ [TB6] 백업(age) → 원격 보관
                          │                              │                       │
                          └──[TB4 egress 프록시]──▶ KRX·DART·KIS·Finnhub·뉴스·토스·텔레그램
[TB5 운영자] ── SSH(키·방화벽) / 관리 화면(기기 인증서 + TOTP + JIT) ──▶ 서버·앱
```

흐름: DF1 가입·로그인·갱신 · DF2 송금·충전(돈) · DF3 채팅·SSE 알림 · DF4 배치 수집(외부 → DB) · DF5 관리자 조회(개인정보) ·
DF6 백업·복구 · DF7 배포·업데이트(공급망)

## 3. STRIDE 표

| ID | 흐름 | 위협(유형) | 막는 장치 | 확인 | 남은 위험 |
|---|---|---|---|---|---|
| T-01 | DF1 | 비밀번호 무차별 대입·크리덴셜 스터핑(S) | IP·아이디 시도 제한(공유 저장소 postgres) · 연속 5회 계정 잠금(지수 연장) · 한 IP 다수 실패 → IP 차단 규칙 | `test_zero_trust.py::test_credential_stuffing…` · A-1 | 대규모 분산 IP(봇넷) — WAF·CAPTCHA 미도입 |
| T-02 | DF1 | 아이디 존재 여부 탐색(I) | 없는 아이디·틀린 비밀번호·잠긴 계정에 같은 문구·같은 시간 | `test_consecutive_failures_lock_account_without_leaking_existence` | 가입 화면의 '이미 있는 아이디'(사용성 선택) |
| T-03 | DF1 | 탈취한 토큰 재사용(S) | 접근 토큰 10분 · 갱신 토큰 회전 + 재사용 탐지(→ 사용자 전체 세션 폐기) · 기기 지문 결속 · **기기 키 결속(DPoP, 2026-11 — 갱신·돈·관리 동작은 로그인한 브라우저의 비추출 키 서명 필요, 쿠키만 복사하면 실패·세션 종료)** | `test_access_expiry_refresh_rotation_grace_and_reuse_detection` · `test_token_replayed_from_another_device…` · `test_dpop.py::test_stolen_cookies_on_another_device_cannot_refresh_or_move_money` · E2E ⑧-3 | 그 PC 를 원격 조종하는 공격(같은 브라우저) — 단말 보안 영역 |
| T-04 | DF1 | XSS 로 토큰 탈취(I) | 토큰은 HttpOnly·SameSite=Strict 쿠키(HTTPS 면 `__Host-`) · localStorage 에 토큰 없음 · CSP nonce + Trusted Types | E2E ③⑦ · `test_web_security.py` | 같은 출처에서 요청 대리(세션 라이딩)는 CSP·TT 로만 막음 |
| T-05 | DF2 | CSRF 로 송금(T) | 쿠키 인증 변경 요청은 CSRF 이중 제출 + Origin 확인, SameSite=Strict · 실패 반복 → 세션 폐기 | `test_cookie_requests_need_csrf_token_and_same_origin` · A-2 | — |
| T-06 | DF2 | 계정 탈취 후 잔액 인출·대포 계좌 집금(S·E) | 고액 송금 추가 인증(5분 이내 재인증 — **로그인은 재인증으로 치지 않음**, 2026-10-23) · FDS 규칙 10개(70점 차단·40점 추가 인증)를 **송금 트랜잭션 안·지갑 잠금 뒤**에서 채점(동시 송금으로 속도 규칙을 비켜 가는 TOCTOU 차단, 2026-10-24) · 받는 사람 정규화 · 세션 IP 대역 변경 = 위험 신호 · 판정 없는 송금 0건을 데이터 계약이 매일 확인 | `docs/models/fds_eval.md`(탐지 100%·정상 차단 0.05%) · `test_parallel_transfers_cannot_slip_past_velocity` · E2E ⑥ | 합성 평가 — 실제 분포는 운영 기록으로 재조정 |
| T-07 | DF2 | 이중 결제·경쟁 조건(T) | 멱등키(처리 여부를 판정보다 먼저 확인 — 재시도가 4xx 를 받아 새 키로 이중 송금되지 않게) · 행 잠금(FOR UPDATE) · 잠금 순서 고정(계정 → 업무 행 → 지갑, A→B·B→A 이름순) · 탈퇴와 진행 중 거래는 계정 행에서 줄 세움 · 송금 시 원장 분개 2줄 + 커밋 시 합 0 제약 트리거 | `test_fix_money.py` · `test_concurrency_more.py` · `test_ledger.py` · `test_fds.py` | — |
| T-08 | DF2 | 잔액·원장 사후 변조(T·R) | 복식부기 일 마감(해시 사슬 — 합계 + **분개 한 줄 한 줄 다이제스트**, 빠진 날 없이 날짜순) · 마감 해시를 감사 체인(외부 증인)에 고정 · 시산표 합 0 · 고아 계좌 0 대사 | `test_ledger.py`(합계 보존 변조·마감 기록 재계산 위조 탐지) · 복구 훈련 검증 항목 | DB 소유자 권한을 가진 내부자 — 외부 증인으로 탐지만 |
| T-09 | DF3 | 남의 채팅·지갑 조회·대화 위조(I·T·E) | PDP 인가 + **DB FORCE RLS**(주인 열 = app.user; 두 사람 표는 보기 양쪽·새 행은 보내는 사람만 — V013) · 보낸·받는 사람 열은 앱 계정이 고칠 수 없음(열 단위 권한) · 앱 계정은 RLS 우회 권한 없음 | `test_rls.py` · N-5(교차 사용자 UPDATE 0행) | RLS 문맥을 앱이 설정 — SQL 주입이 있으면 우회 가능(→ T-10) |
| T-10 | 전체 | SQL 주입(T·I·E) | 바인딩 파라미터만 · 동적 SQL 은 화이트리스트(분석 쿼리 이름 있는 SQL) · bandit B608 검토 · 앱 계정 DDL·감사 수정 불가 | `secure_coding_69.md` 구현-1 · `db_roles_check --strict`(CI) | 새 코드의 실수 — 리뷰·정적 분석 의존 |
| T-11 | DF3 | 알림 스트림으로 남의 이벤트 수신(I) | SSE 는 세션 주인에게만 · 30분마다 끊고 재검증 · 세션 폐기 시 즉시 종료 · pg_notify 는 커밋된 것만 | `test_stream.py` · E2E ④ | — |
| T-12 | DF4 | SSRF·내부망 탐색(설정값 URL·리다이렉트)(I·E) | 허용 목록(앱) · 사설·루프백·메타데이터 주소 거부 · 리다이렉트도 같은 검사 · 프록시 `to_internal` 거부(DNS rebinding) | `test_egress.py` · N-4 | 허용 목록 호스트 자체의 침해 |
| T-13 | DF4 | 침해된 서버가 데이터를 바깥으로(I) | egress 3중: 앱 검사 → 프록시 허용 목록 → (VM) 커널 직접 연결 차단 · (compose) 앱은 인터넷 없는 내부망 | N-4(앱 검사 끔 → 프록시 403, 프록시 우회 → 연결 불가) | 허용 호스트를 통한 은닉 채널(예: 텔레그램 봇) |
| T-14 | DF4 | 외부 원천 응답 변조·악성 XML(T) | TLS 인증서 검증 · defusedxml · 적재 품질 게이트(기대치 비교, 실패 시 롤백 + 알림) · 데이터 계약 | `test_quality.py` · `test_contracts.py` | 원천 자체의 오류 — 출처·기준일 표시로 투명화 |
| T-15 | DF5 | 관리자 과다 열람·사적 조회(I·R) | 상시 관리자 없음(JIT 30분, 사유·TOTP) · 열람 목적 필수 · 열람 기록 + 하루 50명↑ 경보 · 월 점검 리포트 · 2인 승인 | `test_pii_view_needs_purpose…` · `test_jit_elevation…` · `test_role_grant_needs_two_different_admins` | 공모(두 관리자) — 감사 기록·외부 증인으로 사후 추적 |
| T-16 | DF5 | 관리 화면 위장 접속(S) | nginx mTLS(사설 CA 기기 인증서) · 앱이 인증서 확인 헤더 재확인 · 헤더는 nginx 가 덮어씀 · 앱은 127.0.0.1(VM)·web 망 주소(compose, edge 만 연결 가능 — T-30)에서만 | E-3(인증서 없음 403·헤더 위조 403) | ADMIN_REQUIRE_CLIENT_CERT=false(기본)일 때는 TOTP·JIT 만 |
| T-17 | 전체 | 감사 기록 변조·삭제(R) | 해시 체인(중간 행 수정 → 이후 전부 불일치) · 앱 계정 UPDATE/DELETE 권한 없음 · 매일 체인 머리를 DB 밖 증인 파일에 서명 기록 | `test_infosec.py::test_audit_chain_detects_tampering` · `test_data_protection.py` | DB 통째 교체 + 증인 파일 동시 변조(증인을 원격 보관하면 차단) |
| T-18 | DF6 | 백업 유출(I) | age 공개키 암호화(서버엔 공개키만, 수신자 2개) · 파일 600 · 평문 백업은 관리자 헬스 경보 · 복호화 키는 오프라인 | `test_dr.py` · DR 훈련 보고서 | 백업 전 메모리·임시 파일(덤프는 파이프로 바로 암호화 — 평문 파일 없음) |
| T-19 | DF6 | 백업 위·변조·랜섬웨어(T·D) | manifest(암호문·덤프 SHA-256·행 수·체인 머리) · 복원 전 검증 · 새 DB 에 복원 후 검증 통과 시에만 교체 · GFS 보관 + 원격 사본 | `vm.sh restore` 실측(6.8초) · DR-1 | 원격 사본까지 지우는 공격 — 불변 저장소(WORM) 미도입 |
| T-20 | DF7 | 악성·변조 패키지(공급망)(T) | requirements.lock 해시 고정(`--require-hashes`) · pip-audit · SBOM · 설치본 SHA-256 확인 · CI 액션 커밋 SHA 고정 | `test_deploy_config.py`(잠금·설치본) · CI security | 베이스 이미지 태그(다이제스트 미고정)·업스트림 침해 |
| T-21 | 운영 | 서비스 침해 후 서버 장악(E) | 컨테이너: 루트 아님·읽기 전용·capability 0·권한 상승 금지 · systemd 샌드박스(홈 숨김·코드 읽기 전용·시스템 호출 제한, 노출 1.2) | S-1·S-2(격리 실측) | 커널 취약점(컨테이너 탈출) |
| T-22 | TB5 | SSH 무차별 대입·root 원격 로그인(S·E) | `vm.sh harden`: 방화벽 기본 거부·SSH 시도 제한(ufw limit)·root 원격 금지·인증 3회·키 전용 옵션 | S-3 | 비밀번호 로그인 허용(키 등록 전) |
| T-23 | TB1 | 서비스 거부(느린 요청·큰 본문·무거운 계산)(D) | nginx 요청률·연결 수·본문 1MB·타임아웃 · 앱 본문 상한 · 무거운 계산 IP 당 분당 12회 · API 쿼리 5초 제한(504) · 외부 원천 서킷 브레이커 · **우선순위 대량접속제어(경계 low·심각 normal 503 — 핵심 거래 보호) · 연결 풀 대기 5초 후 503**(2026-11) | `test_resilience.py` · `test_capacity.py` · 부하 시험(오류 0) · 과부하 시험(핵심 거래 p95 899 → 354ms) | 대역폭 공격 — 상위망 DDoS 방어 필요 |
| T-24 | TB1 | 클릭재킹·MIME 혼동·정보 누설 헤더(T·I) | frame-ancestors 'self'·X-Frame-Options · nosniff · Referrer-Policy · 서버 버전 숨김 · 문서(/docs) 404 | `test_security.py` · `test_hardening.py` · nginx 정책 시험 | — |
| T-25 | 전체 | 로그·오류에 비밀값 노출(I) | API 키 가리기(redaction — URL 쿼리 키 포함) · 오류 응답은 일반 문구 + 요청 ID · 프록시 로그는 URL 경로 미기록 | `test_logging.py` · `test_security.py` · `test_incident_response.py` | — |
| T-26 | DF3 | 남의 행동 부인(“나는 송금 안 했다”)(R) | 송금·충전·탈퇴·권한 변경을 감사 체인에 요청 ID·세션·기기와 함께 기록 · 추가 인증 시각 기록 | `test_request_id_links_response_events_and_audit` | 계정 공유(사용자 책임) |
| T-27 | 운영 | 키 분실·교체 불가(D) | 키 ID 로 다중 키 링 · `crypto-rotate`(값마다 DEK 재포장) · 복구 훈련이 '키 링으로 전부 복호화'를 검사 | `test_dr.py`(키 분실 탐지) · `test_pepper_hash_and_master_key_rotation` | KEK 원본 분실 = 암호문 영구 손실 → 오프라인 금고 보관 |
| T-28 | 운영 | 설정 실수(문서 노출·CORS *·평문 쿠키)(I) | `tools/security_check.py`(기준선 24항목, 견본은 CI 가 검사) · `vm.sh status` 요약 | CI security · `test_prod_env_example_passes…` | 운영자가 경고를 무시 |
| T-29 | DF1 | 남의 아이디로 일부러 틀려 계정 잠그기(D) | 잠금은 아이디 단위(규정상 실패 횟수 제한 필수 — 안전성고시 제5조⑥) · 같은 IP·아이디 10분 5회 · IP 당 100회 시도 제한 · 잠금마다 보안 이벤트·경보 · 관리자·CLI 해제 · 아이디 목록은 로그인 사용자만 | `test_consecutive_failures_lock_account_without_leaking_existence` · 독립 점검(2026-10-23) | 분산 IP 로 반복하면 최대 24시간 잠금 — **감수**(§4) |
| T-30 | 운영 | 내부 구성요소가 경계를 건너뛰어 앱에 직접 요청(전달 헤더·인증서 확인 헤더 위조)(S) | compose: 앱은 web 망 주소(`app-web`)에서만 듣는다 → data·egress 망의 컨테이너(프록시·스케줄러)는 연결 불가, 연결할 수 있는 건 edge 뿐 · 이미지 기본값은 같은 컨테이너의 전달 헤더만 신뢰 | `test_compose_prod_least_privilege_and_segmentation` · 실측(다른 망 → 연결 거부) | VM: 같은 서버의 프로세스는 127.0.0.1 로 닿는다 — **감수**(§4) |
| T-31 | 운영 | 복구 훈련·검증이 운영 DB 를 건드림(T) | 검증 프로세스의 모든 DB 주소를 복원 대상으로 고정(다른 주소는 빈 값 — `.env` 가 되살리지 못하게) · 앱 기동 전 '모든 DB 주소 = 복원 대상' 확인, 어긋나면 중단 | `test_verify_process_cannot_fall_back_to_production_db_urls` · 실제 복원 훈련 | — |
| T-32 | DF2 | 원장 정합성이 깨진 채 돈이 계속 오감 — 사고 확산(T·D) | 원장 변조 탐지·마감 실패·FDS 차단 급증 → 돈 기능 **자동 비상 차단** · 관리자 즉시 차단 · 재개는 다른 관리자 승인(2인) | `test_controls.py` | 차단 상태는 워커별 2초 캐시(그동안 받은 요청도 원장·FDS·멱등이 지킨다) |
| T-33 | DF3 | 사용자가 주민번호·카드번호를 채팅·메모에 붙여 넣어 퍼짐(I) | 저장 전 가림(형식 + 날짜/룬 검사, 되돌릴 수 없게) · 가린 사실만 보안 이벤트 · 로그·알림 출구에도 같은 규칙 | `test_dlp.py` | 형식으로 구분되지 않는 정보(계좌·전화)는 가리지 않음(오탐 회피) |
| T-34 | DF7·운영 | 배포 뒤 코드·화면 파일 변조·웹셸 심기(승인 안 된 변경)(T·E) | 배포 기준선(파일별 SHA-256 — 소유자 계정만 기록, 앱 계정은 읽기만) 대비 10분마다 검사: 바뀜·사라짐·**새 파일** → 보안 이벤트(심각)·경보 · 코드 읽기 전용(systemd)·읽기 전용 컨테이너 | `test_integrity.py::test_changed_missing_and_new_files_are_detected_against_the_baseline` · `test_integrity.py::test_app_account_cannot_rewrite_the_baseline` | 서버 root·DB 소유자 권한을 함께 가진 공격자 — 기준선 갱신은 감사 체인(외부 증인)에 남아 사후 추적 |
| T-35 | TB5 | 쓰지 않거나 퇴직한 사람의 관리 자격 남용(S·E) | 자격 90일 만료 + **60일 동안 한 번도 켜지 않은 자격 자동 회수**(부트스트랩은 비상용으로 표시) · **인사조치 일괄 회수**(자격·활성·대기 요청·세션 + 계정 중지 한 번에) · JIT 30분 | `test_privilege_lifecycle.py::test_dormant_admin_eligibility_is_revoked_but_bootstrap_is_flagged` · `test_privilege_lifecycle.py::test_offboarding_revokes_everything_in_one_step` | 설정(ADMIN_USERS)에 남은 부트스트랩 계정 — 중지된 계정은 다시 부여하지 않고, 설정에서 빼라고 경고 |
| T-36 | DF7·운영 | 검증 안 된 변경을 배포해 서비스가 멈춤(구문 오류·적용된 마이그레이션 변경·성능 저하)(D·T) | **배포 게이트**(vm.sh update — 지금 배포된 코드가 새 설치본을 확인): 자동 사전 점검(구문·적용된 마이그레이션 보존·정책 형식)은 어느 모드든 **서비스를 멈추기 전에** 중단 · 사전 영향 분석(돈·인증·스키마·성능 민감 경로) · 요청자 ≠ 승인자 + 검증 근거 · 배포 금지 시간(평일 08:30~16:00) · 긴급 변경 3일 사후 검토 · 변경 기록은 앱 계정이 지우거나 설치본 해시를 바꿀 수 없음(V017) | `test_changes.py::test_gate_blocks_broken_release_even_in_record_mode` · `test_changes.py::test_approval_needs_a_second_person_and_evidence` | 구문은 맞지만 업무 논리가 틀린 코드 — 시험·승인 검토가 막고, 장애가 나면 사고보고서 초안이 사건 전 변경을 '변경시점 후보'로 붙여 원인 추적이 빠르다 |
| T-37 | 운영 | 장애·침해를 늦게 알거나 감독규정 보고 기한을 놓침(D·R) | 1분 합성 모니터링 → 10분 이어지면 장애 사건·경보 → 30분이면 보고 대상·최초 보고 기한(인지 + 24시간) · 보안 침해 사건은 등록 즉시 보고 대상 · 보고서 초안(시행세칙 별지 제2호 12개 항목) · 기한 6시간 전·초과 알림 · 한 번도 정상 응답이 없으면 장애로 판정하지 않음(설정 오류가 가짜 장애를 만들지 않게) | `test_incidents.py::test_outage_is_opened_marked_reportable_and_closed` · `test_incidents.py::test_reporting_rule_follows_enforcement_detail_7_4` | 서버 전체가 꺼진 동안은 같은 서버의 감시가 볼 수 없다 — 감시 공백 경고 + 바깥 감시(edge 헬스체크·외부 모니터링) 권장 |
| T-38 | 운영 | 경보를 아무도 확인하지 않아 대응이 늦음(관제 공백)(D) | 경보 확인(ack — 누가·언제, 감사 기록) · 15분 미확인 오류 경보 1회 상향 · MTTA·미확인 지표 · 수준진단 점검(기한 지난 미확인 0) · 외부 관제 연동(CEF·JSON Lines, 아이디 가명) | `test_alerts_ops.py::test_unacked_error_alert_escalates_once_and_ack_clears_it` · `test_siem.py::test_cef_export_pseudonymizes_users_and_escapes_fields` | 당번 1명인 소규모 운영 — 채널 이중화(웹훅·텔레그램·메일)로 보완 |
| T-39 | 운영·DS | 쌓이기만 하는 데이터·비대화·기본 설정으로 디스크가 차거나 느려져 서비스가 멈춤(D) | 타입 정리(1년 규모 −32%)·겹치는 인덱스 제거 · 뉴스·검색 13개월 넘으면 압축 보관 후 정리 · 매일 크기 기록 → **디스크 소진 예측**·죽은 행·XID·연결·오래 열린 트랜잭션 경보 · 수준진단 점검(크기 기록 2일 이내·오류 판정 0) · 서버 설정 자동 적용(공유 버퍼·자동 VACUUM·느린 쿼리 통계) | `test_dbhealth.py::test_forecast_fits_the_growth_trend_and_days_to_full` · `test_dbhealth.py::test_findings_cover_bloat_cache_wraparound_connections_and_long_transactions` | 예측은 선형 추세 — 급증(대량 적재)은 일일 경보로 잡는다. 디스크는 DB 와 같은 서버로 가정 |
| T-40 | DS·운영 | 정리한 데이터의 보관 파일이 바뀌거나 사라져 복원 불가·위조 데이터 반입(T·D) | 보관 파일 SHA-256(쓰면서 계산 → 다시 읽어 검증한 뒤에만 DB 정리) · 보관 기록은 앱 계정이 지우거나 해시를 고칠 수 없음(V018) · 복원은 해시부터 확인·보관 폴더 밖 파일 거부·있는 행은 건너뜀 · 매월 전체 재해시(없어짐·바뀜 경보) · 외부 보관 명령이 있으면 복사 성공한 달만 정리 | `test_ilm.py::test_tampered_or_foreign_archives_are_refused` · `test_ilm.py::test_archive_verifies_then_deletes_and_restore_brings_rows_back` | 외부 보관 명령이 없으면 파일이 서버 한 곳에만 있다 — 진단·문서에 표시 |
| T-41 | TB1·DF2 | 모의투자(가상 시장 시즌) — 브라우저에서 잔액·체결가·정산을 고치거나, 미리 받은 시즌 파일로 미래 봉을 보고 거래해 가상머니를 부풀림(T·I·E) | 팀 시안은 localStorage 에 잔액·체결·정산을 두고 시즌 파일(미래 봉 포함)을 통째로 받았다 → **서버가 시계·체결가·정산을 정한다**: 시즌 데이터는 DB 에만 두고 API 가 서버 시계로 '공개된 봉'까지만 잘라 준다(끝난 시즌만 전체·장세) · 체결가 = 그 봉 종가(서버) · 화면이 본 봉과 다르면 409 · Idempotency-Key 재전송 1번 · 참가 행 FOR UPDATE(동시 주문 줄 세움)·현금·보유 초과 거부 · 참가금·정산은 지갑 원장·복식부기(sys:vmarket_custody)·멱등키 · 개인 표 FORCE RLS(V019) · 시즌 파일 형식·가격 검사·SHA-256, 시작한 시즌은 변경 거부·저장 뒤 해시 불일치면 사용 중지 | `test_vmarket.py::test_future_bars_never_leave_the_server` · `test_vmarket.py::test_join_order_settle_flow_with_wallet_ledger` · `test_vmarket.py::test_concurrent_orders_cannot_overspend` · `test_vmarket.py::test_import_validates_files_and_refuses_to_change_a_started_season` | 시즌 데이터 자체(가상 주가)는 팀 생성기 산출물을 그대로 쓴다(형식·가격만 검사) — 시즌 11 이후 파일은 운영자가 넣어야 하고, 없으면 금요일 경보 |
| T-42 | DS | 양봉이 주간 추천 — 부분 적재·수정주가 오류·우선주/리츠가 섞인 데이터로 엉뚱한 종목을 '이번 주 추천'으로 보여 줌(T·I) | 팀 알고리즘 원본을 해시로 고정(바꾸면 시험 실패) · 팀 방식 수정주가(종가+KRX 등락률) · 코스피 행만·보통주 규칙(우선주·리츠·스팩·투자회사·외국주권 제외)·업종 KSIC 앞 3자리 · 그 주 마지막 거래일 시세가 없거나 후보가 150 미만이면 고르지 않음 · 끝난 주만·멱등·동시 실행 차단 · 화면 첫 접속 투자 유의 안내·교육용 고지 | `test_yangbong.py::test_algorithm_file_is_the_team_original` · `test_yangbong.py::test_guards_small_universe_and_missing_signal_day` · `test_yangbong.py::test_batch_picks_finished_weeks_with_team_rules_on_our_data` | 보통주 구분은 KRX 증권구분 API 대신 코드·이름 규칙 — 새 종류 증권이 생기면 규칙 보강. 실데이터 재현(2026-10-23 5종목)은 운영 서버에서 확인 |

## 4. 남은 위험의 처리

| 위험 | 처리 | 근거 |
|---|---|---|
| RLS 문맥 우회(SQL 주입 시) | **감수** + 1차 방어 강화 | 바인딩 100%·DDL 불가·감사 체인. DB 역할을 사용자마다 두는 근본책은 운영 비용이 과도 |
| 키가 서버(.env)에 있음 | **이전 계획** | KMS/HSM 또는 비밀 관리자(Vault) — 클라우드 배포 시 |
| 기기 지문 위조 | **감수** | 사용자 단말은 관리 대상 아님. 관리 단말만 인증서 |
| 계정 잠금 악용(T-29) | **감수** | 금융권 관행도 '비밀번호 5회 오류 → 잠금 + 본인 확인 후 해제'. 잠금을 없애면 대입 공격이 쉬워진다. 경보로 알아채고 관리자가 해제 — 본인 확인 수단(메일·휴대폰)이 생기면 셀프 해제로 |
| VM 의 같은 서버 프로세스(T-30) | **감수** | 같은 OS 위의 프로세스는 이미 같은 신뢰 경계(침해 시 더 큰 일이 가능). squid 는 `to_internal` 거부로 앱을 향한 요청을 중계하지 않는다. 근본책은 nginx↔앱 유닉스 소켓(권한 660) |
| 봇넷 분산 대입·대역폭 DDoS | **이전** | 상위 WAF·CDN·CAPTCHA |
| 원격 백업 삭제 | **이전 계획** | 불변 저장소(객체 잠금) |
| 공급망(베이스 이미지) | **감소 계획** | 이미지 다이제스트 고정·서명 검증(cosign) |
