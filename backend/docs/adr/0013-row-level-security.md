# ADR-0013 개인 데이터 표는 FORCE ROW LEVEL SECURITY — 앱 인가 뒤에 DB 가 한 번 더 막는다

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-20 (ADR-0005 의 'RLS 기각' 판단을 대체) |
| 범위 | 제로트러스트 — 데이터 |
| 코드 | `sql/migrations/V010__row_level_security.sql` · `app/security/rls.py` · `app/db.py`(연결 꺼낼 때 문맥 설정) · `deploy/sql/roles.sql`(oos_backup) |
| 시험 | `test_rls.py` · 최소 권한 모드 전체 시험(CI least-privilege) · `test_data_protection.py`(등록부 rls 표시 = DB FORCE RLS) |

## 맥락
ADR-0005 는 "다중 테넌트가 아니라 RLS 는 과하다"고 봤다. 그러나 지갑·원장·채팅·접속 기록은 **사용자 단위로 남의 것을 보면 안 되는**
데이터이고, 앱 코드의 `WHERE user_id = …` 하나를 빠뜨리면 전부 노출된다. 인가가 앱 한 곳에만 있으면 그 한 곳이 단일 실패점이다.

## 결정
- 개인 데이터 15개 표에 `ENABLE` + `FORCE ROW LEVEL SECURITY`(소유자에게도 적용). *(2026-10-27: 웹 활동 표 `practice_record`·`quiz_attempt` 추가 → 17개, ADR-0021 · 2026-10-31: 양봉이 꾸미기 옷장 `avatar_closet` → 18개, ADR-0022)* 행이 보이는 조건:
  `app.rls = 'off'`(시스템 문맥) **또는** 행의 주인 열 = `app.user`. 아무것도 설정 안 된 연결은 **0행**(기본 거부).
- 앱이 풀에서 연결을 꺼낼 때마다 지금 요청의 주체로 `set_config` — 요청 밖(배치·스케줄러)은 시스템 문맥.
  성능: 물리 연결마다 마지막 값을 기억해 같으면 건너뜀(왕복 2번 절약, `/me` 397 → 326ms) — 이전 요청 값이 남는 일은 없다.
- 남의 행을 정당하게 읽는 곳(FDS 의 받는 쪽 유입, 로그아웃 기록, 탈퇴)은 `rls.elevated("사유")` 구간으로만.
- 앱 계정은 `NOBYPASSRLS`. 백업은 전용 계정 `oos_backup`(읽기 전용 + BYPASSRLS, 접속 2개) — 일반 계정 `pg_dump` 는 FORCE RLS 에
  막히거나 빈 표를 만든다(복구 훈련이 행 수 대조로 탐지).

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 앱 인가만 | 한 줄 실수 = 전체 노출(단일 실패점) |
| 사용자마다 DB 역할 | 계정 수 = 사용자 수, 연결 풀 공유 불가 — 운영 비용 과다 |
| 보안 장벽 뷰(security_barrier view) | 쓰기 경로까지 막기 어렵고 뷰·표 이중 관리 |

## 개정(2026-10-23, 독립 점검) — V013
두 사람 표(채팅·친구 요청)의 정책을 명령별로 나눴다: 보기·삭제는 양쪽, **새 행은 보내는 사람 = 주체만**, 수정은 양쪽이되 앱 계정의 열 단위
권한으로 바꿀 수 있는 열을 제한(채팅 `read_at`·`body`(키 교체) · 요청 `status`·`responded_at`, 친구 관계는 수정 불가). 예전 WITH CHECK 는
'어느 한쪽이 나'면 통과라, 인가 버그가 생기면 '남이 나에게 보낸 것처럼' 메시지·요청을 쓸 수 있었다. 친구 관계는 수락할 때 두 행을 함께
쓰므로 양쪽 정책 유지. 시험 `test_rls.py::test_two_party_writes_cannot_forge_the_sender`(최소 권한 모드는 열 권한까지).

## 결과와 한계
- 교차 사용자 UPDATE 0행, 문맥 없는 직접 접속 0행(compose 실기동).
- **한계**: 세션 변수는 앱 계정이 바꿀 수 있다. RLS 는 '앱 로직 실수'를 막는 2차 방어이지 SQL 주입으로 `SET` 을 실행하는 공격자를
  막지 않는다 → 1차 방어(바인딩 100%·DDL 불가·감사 체인)와 함께 쓴다. `threat_model.md` T-09·T-10.
- PgBouncer 를 넣는다면 session 모드만(세션 변수 사용).

## 검증
`TEST_DATABASE_URL=…oos_owner… TEST_APP_DATABASE_URL=…oos_app… python -m pytest -q` (CI least-privilege 작업과 같음)
