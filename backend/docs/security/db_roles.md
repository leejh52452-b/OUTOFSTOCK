# DB 계정 분리(최소 권한) — 설계와 검증 결과

| 계정 | 쓰임 | 권한 |
|---|---|---|
| `oos_owner` | 스키마 소유·마이그레이션(`run.py initdb`) | 소유자(DDL) — 서비스 프로세스는 쓰지 않음 |
| `oos_app` | API·스케줄러·실시간 수집기 | 데이터 읽기/쓰기(DML)만. **DDL 없음**, **감사 로그 수정·삭제 없음**, 마이그레이션 이력 쓰기 없음 |
| `oos_ro` | 분석 CLI(`run.py analytics-query`, `ANALYTICS_DATABASE_URL`) — 화면 API 없음 | analytics 스키마 + 공개 시세 표 SELECT 만. 계정·지갑·채팅·결제·감사 표 접근 불가, 읽기 전용 트랜잭션·10초 제한 |
| `oos_backup` | 백업(`BACKUP_DATABASE_URL` — 백업 타이머·compose backup 작업만) | `pg_read_all_data` + **BYPASSRLS**(FORCE RLS 표까지 빠짐없이), 쓰기·DDL 없음, 기본 읽기 전용 트랜잭션, 동시 접속 2. 강한 권한이라 백업 경로에서만 쓴다 |

2026-10-20 부터 개인 데이터 표(현재 17개)는 **FORCE ROW LEVEL SECURITY**(ADR-0013) — 앱 계정(`NOBYPASSRLS`)은 세션 변수로 정한 주체의
행만 본다. 그래서 일반 계정의 `pg_dump` 는 실패하거나(정책) 빈 표를 만든다(`--enable-row-security`) → 백업 전용 계정을 둔다(ADR-0019).

DDL 이 필요한 배치 작업 두 가지(새 연도 파티션, 종목 스냅샷 새로 고침)는 소유자 권한 함수(`SECURITY DEFINER`,
`search_path` 고정 — V005)로 감싸 앱 계정에는 **실행 권한만** 준다.

## 적용
```bash
sudo -u postgres psql -d outofstock -v owner_pw="'…'" -v app_pw="'…'" -v ro_pw="'…'" -v backup_pw="'…'" -f deploy/sql/roles.sql
# .env: DATABASE_URL=…oos_app…, ANALYTICS_DATABASE_URL=…oos_ro…, BACKUP_DATABASE_URL=…oos_backup…
# 업데이트 때만: MIGRATION_DATABASE_URL=…oos_owner… .venv/bin/python run.py initdb
.venv/bin/python tools/db_roles_check.py --strict   # 권장 상태와 다르면 종료 코드 1(CI least-privilege 작업과 같다)
```

## 검증 결과(2026-10-10, PostgreSQL 16, 새 DB 에 roles.sql 적용 후 실제 시도)
| 시도 | 결과 |
|---|---|
| 앱: 서버 시작 시 스키마 확인(init_db, 대기 마이그레이션 없음) | 성공(DDL 을 보내지 않음) |
| 앱: 새 연도(2030) 파티션에 적재 | 성공(소유자 권한 함수) |
| 앱: 종목 스냅샷 새로 고침 | 성공(소유자 권한 함수) |
| 앱: 감사 로그 추가 | 성공 |
| 앱: 감사 로그 UPDATE / DELETE | **거부**(InsufficientPrivilege) |
| 앱: CREATE TABLE / analytics 표 DROP / 마이그레이션 이력 삭제 | **거부** |
| 조회: analytics·종목 표 SELECT | 성공 |
| 조회: app_user·audit_log SELECT | **거부** |
| 조회: analytics INSERT | **거부**(ReadOnlySqlTransaction) |

## 계속 확인하는 방법(2026-10-22~)
- CI `least-privilege` 작업: 빈 DB 에 `roles.sql` 을 적용하고 **전체 시험을 앱 계정(oos_app)으로** 돌린 뒤 `db_roles_check --strict`
  (로컬 실측 824 passed · 1 skipped, 2026-11-02 — 소유자 권한이 필요한 시뮬레이션만 `as_owner` 픽스처로).
- compose 운영 오버레이 실기동(검증 보고서 N-5): 문맥 없는 직접 접속 지갑 0행 · 자기 1행 · 남의 지갑 UPDATE 0행 · 감사 로그 DELETE 권한 없음 ·
  public CREATE 권한 없음. 앱 컨테이너 환경에는 다른 계정 비밀번호가 없다(`test_deploy_config.py`).
