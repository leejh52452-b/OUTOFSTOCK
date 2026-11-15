# ADR-0014 민감 값은 봉투 암호화(값마다 DEK, AAD 로 행 결속), 키는 용도별로 파생하고 ID 로 교체한다

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-20 (ADR-0006 의 열 암호화 v1 을 확장) |
| 범위 | 제로트러스트 — 데이터 · 안전성 확보조치 기준 제7조 |
| 코드 | `app/crypto.py`(`ENCRYPTED_COLUMNS`·`rotate_all`·`verify_all`) · `app/security/keys.py`(HKDF) · `run.py crypto-rotate` |
| 시험 | `test_infosec.py`(AES-GCM 결속·교체) · `test_dr.py`(키 링 점검이 '키 분실'을 찾음) · `test_zero_trust.py`(페퍼·마스터 키 교체) |

## 맥락
v1 은 열 값을 KEK 하나로 바로 암호화했다. 키를 바꾸려면 모든 값을 다시 암호화해야 하고, 암호문을 다른 행으로 옮겨 붙여도
복호화가 됐다(행 결속 없음). 채팅 본문은 평문이었고, 비밀번호 페퍼·IP 가명화 키가 따로 관리되지 않았다.

## 결정
- 형식 `enc:v2:a256gcm:<KEK ID>:<포장된 DEK>:<nonce|암호문+태그>` — 값마다 무작위 DEK(256비트)로 AES-256-GCM, DEK 는 KEK 로 포장.
- **AAD = 표·기본키·열**(예: `chat:alice:bob`) — 암호문을 다른 행·열로 옮기면 복호화 실패.
- **키 링** — `DATA_ENCRYPTION_KEY`(현재) + `DATA_ENCRYPTION_KEYS_OLD`(읽기만). `crypto-rotate` 는 DEK 만 다시 포장(본문 재암호화 없음).
  v1·평문(도입 전 행)도 읽는다 — 점진 전환.
- **용도별 하위 키** — 마스터 키 `APP_SECRET_KEY` 에서 HKDF-SHA256 로 페퍼·IP 가명화·감사 증인 서명 키를 분리 파생(한 키 = 한 용도).
- 대상은 등록부(`ENCRYPTED_COLUMNS`)가 정하고, 분류 등록부의 L4 열은 hash/encrypt 중 하나여야 한다(시험이 강제).
- 복구 훈련은 복원한 DB 의 **모든 암호문이 지금 키 링으로 풀리는지** 확인 — 키를 잃은 백업은 '복구 가능'이 아니다.

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| pgcrypto(DB 안 암호화) | 키가 SQL 문·DB 로그·`pg_stat_statements` 에 남는다 |
| 디스크·TDE 암호화만 | DB 를 통째로 읽는 공격(SQL 주입·덤프 유출)에는 무력 |
| KMS/HSM | 학교 VM·로컬 compose 에 없음 — KEK 래핑 지점만 바꾸면 되게 구조를 맞춤(다음 단계) |

## 결과와 한계
- 교체 = DEK 재포장만(본문 재암호화 없음). 저장 크기는 값당 약 145바이트 + 본문의 base64 증가분(1바이트 → 145자, 100바이트 → 277자 실측).
- KEK 가 환경변수(.env 600)에 있다 — 서버 침해 시 함께 노출. KMS 이전이 남은 과제(`threat_model.md` §4).

## 검증
`python run.py crypto-rotate` → `python tools/dr.py drill`(키 링 점검 항목) · `python -m pytest -q tests/test_infosec.py`
