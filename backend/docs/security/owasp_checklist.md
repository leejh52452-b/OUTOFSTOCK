# 정보보호 점검표 — OWASP Top 10(2021) 기준 (2026-10-22 갱신)

각 항목은 **위험 → 대응(코드 위치) → 확인(시험 이름)** 순서다. 시험은 `pytest -q tests/<파일>::<이름>` 으로 바로 돌린다.
더 넓은 그림은 [`zero_trust.md`](zero_trust.md)(제로트러스트 자가 진단)·[`threat_model.md`](threat_model.md)(STRIDE)·
[`secure_coding_69.md`](secure_coding_69.md)(행안부 개발보안 69개).

| # | 항목 | 이 서비스에서의 위험 | 대응 | 확인 |
|---|---|---|---|---|
| A01 | 접근 통제 실패 | 남의 지갑·채팅·실습을 조회·조작, 관리 기능 무단 사용 | 모든 경로 PEP + 기본 거부 정책(PDP) · 개인 데이터 18개 표 **FORCE RLS** · 관리 역할은 자격 + JIT 30분·TOTP·2인 승인 · 관리 경로 기기 인증서(선택) | `test_zero_trust::test_every_api_route_declares_a_known_policy_action` · `test_rls::test_buggy_authorization_code_still_cannot_leak_other_users_rows` · `test_zero_trust::test_jit_elevation_requires_eligibility_mfa_and_reason` |
| A02 | 암호화 실패 | DB·백업 유출 시 토큰·채팅·외부 키 악용 | 비밀번호 PBKDF2 600,000회 + 페퍼 · 토큰은 SHA-256 만 저장 · 민감 값 **봉투 암호화**(값마다 DEK, AAD 행 결속, 키 링 교체) · 백업 age · TLS 1.2+ | `test_data_protection::test_chat_bodies_are_encrypted_at_rest_and_rotated` · `test_infosec::test_aes_gcm_roundtrip_binding_and_rotation` |
| A03 | 주입(SQL·XSS) | SQL 주입, 화면 스크립트 주입 | 바인드 변수 + 화이트리스트(이름 있는 SQL) · 앱 계정 DDL 없음 · **CSP nonce + Trusted Types + 인라인 핸들러 0**(`data-on`) · `esc()` · 아이디 형식 검사 | `test_web_security::test_page_has_no_inline_event_handlers_or_eval` · `::test_app_gets_a_fresh_nonce_and_strict_csp_each_time` · `test_market_source::test_username_validation_blocks_script_injection` |
| A04 | 안전하지 않은 설계 | 계정 탈취 후 인출·이중 결제·크리덴셜 스터핑 | FDS(규칙 점수 → 허용/추가 인증/차단) · 고액 송금 추가 인증 · 멱등키·행 잠금 · 복식부기(합 0 DB 제약) · IP·아이디 시도 제한(공유 저장소) | `test_fds::test_account_takeover_drain_is_blocked_and_recorded` · `test_ledger::test_database_rejects_unbalanced_and_edited_entries` · `test_security::test_login_spray_across_usernames_limited_per_ip` |
| A05 | 보안 설정 오류 | 문서·관리 경로 노출, 클릭재킹, 약한 기본값 | 보안 헤더 · 문서 숨김(앱·nginx) · 운영 견본이 기준선 통과(CI) · `tools/security_check.py`(24항목) · systemd 샌드박스·읽기 전용 컨테이너 · 배포 설정 정책 시험 | `test_security::test_security_headers_on_every_response` · `test_deploy_config::test_prod_env_example_passes_the_security_baseline_and_holds_no_secrets` |
| A06 | 취약한 구성요소 | 알려진 취약점 라이브러리·변조 패키지 | **해시 고정 잠금 파일**(`--require-hashes`) · pip-audit(배포마다 + 주 1회) · SBOM · 잠금 = requirements 범위 검사 | `test_deploy_config::test_requirements_lock_matches_requirements` · CI `security` |
| A07 | 식별·인증 실패 | 토큰 탈취·재사용, 무차별 대입 | 접근 토큰 10분 · 갱신 토큰 회전 + **재사용 탐지** · 기기 결속 · **기기 키 결속(DPoP — 쿠키 복사 무력화)** · 유휴 30분 · 5회 잠금 · TOTP(재사용 차단) · 열거 방지 | `test_zero_trust::test_access_expiry_refresh_rotation_grace_and_reuse_detection` · `::test_consecutive_failures_lock_account_without_leaking_existence` · `::test_totp_enrollment_login_replay_recovery_and_encryption` |
| A08 | 무결성 실패 | 스키마 불일치, 감사 기록·원장 조작, 설치본 변조 | 마이그레이션 체크섬 · 감사 **해시 체인 + 외부 증인** · 원장 일 마감 사슬 · 설치본 SHA-256 · CI 액션 SHA 고정 | `test_platform::test_changed_applied_migration_blocks_start` · `test_visibility::test_external_witness_detects_a_fully_recomputed_chain` · `test_ledger::test_daily_close_is_chained_and_detects_tampering` |
| A09 | 로깅·모니터링 실패 | 사고를 늦게 앎 | 요청 ID 로 로그·보안 이벤트·감사 연결 · JSON 로그 · 탐지 규칙 9개 + 자동 대응 · 장애 알림 · 비밀값 가림 · 개인정보 열람 점검 | `test_visibility::test_json_log_lines_carry_request_id_and_structured_fields` · `test_zero_trust::test_request_id_links_response_events_and_audit` · `test_incident_response::test_scheduler_job_failure_sends_alert` |
| A10 | SSRF | 설정 URL·리다이렉트로 내부망·메타데이터 접근 | 나가는 연결 허용 목록 + 내부 주소 거부(리다이렉트 포함) · 프록시 계층 `to_internal` · (VM) 커널 직접 연결 차단 | `test_egress::test_configured_webhook_cannot_point_to_internal_addresses` · `::test_unlisted_host_is_blocked_before_any_network_io` · 검증 보고서 N-4 |

## 개인정보 최소화
- 저장하는 개인정보: 아이디·비밀번호 해시·활동 기록(지갑·관심·학습·실습·접속). 실명·전화번호·주민번호·계좌·카드번호는 받지 않는다.
- 접속·감사 기록의 IP 는 원문 대신 용도별 키 HMAC 가명값(되돌릴 수 없음), 접속 기록에는 쿼리 값을 남기지 않는다.
- 탈퇴: 계정 데이터 즉시 삭제, 보안 기록의 아이디는 가명으로(보관 기간 동안 추적성 유지), 관리 권한 활성 내역은 가명으로 3년.
- 보관 기간: 접속 기록 365일 · 보안 이벤트 365일 · 개인정보 열람 기록 730일 · 감사 로그 **1095일**(권한 내역 3년 — 코드 하한).
- 기각된 기능(커뮤니티·게임·아바타 등)의 표는 데이터째 지웠다(V009 — 목적 달성 후 파기).

## 남은 한계(정직하게)
- RLS 문맥(세션 변수)은 앱이 정한다 — SQL 주입이 생기면 우회될 수 있어 1차 방어(바인딩·DDL 불가)가 계속 중요하다.
- 키(KEK)가 서버 환경변수에 있다 — KMS/HSM 이전이 다음 단계.
- 일반 사용자 2단계 인증은 선택(관리 역할만 필수).

## 참고한 규정
개인정보의 안전성 확보조치 기준(2025.10.31 시행 개정)·전자금융감독규정(제13·15·23·26·33조)·N²SF·BCBS 239 를 **설계 기준**으로 썼다
— 조항별 대응은 [`compliance_mapping.md`](compliance_mapping.md). 이 서비스는 전자금융업자가 아니므로 '준수'가 아니라 설계 기준이다.
