# ADR-0005 DB 계정을 소유자·앱·조회 셋으로 나누고, DDL 이 필요한 배치는 소유자 권한 함수로 감싼다

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-10 · 'RLS 기각' 판단은 ADR-0013 이 대체(2026-10-20), 백업 전용 계정 oos_backup 추가(ADR-0019) |
| 범위 | 3번 정보보호 — 최소 권한(접근 통제) |
| 코드 | `deploy/sql/roles.sql` · `sql/migrations/V005__definer_functions.sql` · `app/db.py`(MIGRATION_DATABASE_URL) · `tools/db_roles_check.py` |
| 문서 | `docs/security/db_roles.md`(실제 시도 결과) · `docs/security/owasp_checklist.md` A01 |

## 맥락
지금까지 API·스케줄러·마이그레이션이 **한 계정(oos, 사실상 소유자)** 으로 붙었다. 앱 코드에 SQL 인젝션 하나만 생겨도
표 삭제·감사 로그 조작·계정 표 열람이 가능하다. 반면 앱이 정당하게 DDL 을 해야 하는 순간도 있다 —
새 연도 파티션 생성, 종목 스냅샷(MV) 새로 고침.

## 결정
| 계정 | 쓰는 곳 | 권한 |
|---|---|---|
| `oos_owner` | 마이그레이션(`run.py initdb`, `MIGRATION_DATABASE_URL`) | 스키마 소유(DDL). 서비스 프로세스는 쓰지 않음 |
| `oos_app` | API·스케줄러·수집기(`DATABASE_URL`) | 표 읽기/쓰기(DML)만. **DDL 없음 · 감사 로그 UPDATE/DELETE 없음 · 마이그레이션 이력 쓰기 없음** |
| `oos_ro` | 분석 쿼리(`ANALYTICS_DATABASE_URL` — 2026-10-20 부터 CLI 전용) | analytics + 공개 시세 표 SELECT 만. 계정·지갑·채팅·결제·감사 표 접근 불가, 읽기 전용·문장 시간 제한 |

- DDL 이 필요한 두 작업은 **`SECURITY DEFINER` 함수**(소유자 권한으로 실행)로 감싸고 앱 계정에는 `EXECUTE` 만 준다.
  함수마다 `SET search_path = analytics, pg_temp` 로 고정 — 호출자가 다른 스키마에 같은 이름의 가짜 함수·표를 만들어
  바꿔치기하는 공격 차단. `PUBLIC` 의 기본 실행 권한은 회수하고(`REVOKE ALL … FROM PUBLIC`) 앱 계정에만 `EXECUTE`.
- `public` 스키마의 CREATE 권한도 `PUBLIC` 에서 회수, 앱 계정의 `TEMPORARY` 는 남긴다(대량 적재의 COPY 임시 표).
- `roles.sql` 은 멱등(다시 실행해도 같은 상태), 비밀번호는 실행할 때 `-v` 로만 받는다(파일에 없음).
- 서버 시작 시 스키마 확인은 '대기 마이그레이션 없음'이면 DDL 을 보내지 않는다 → 앱 계정으로도 정상 기동.

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 한 계정 유지 + 코드 리뷰로 방어 | 사람의 실수 하나가 전부를 연다 — 권한은 '실수했을 때의 피해 범위'를 줄이는 장치 |
| 앱 계정에 CREATE 권한만 추가 | 파티션 하나 만들자고 모든 DDL 을 여는 셈 |
| 행 수준 보안(RLS) | 지금 위협(DDL·감사 조작·교차 표 열람)에는 계정 분리가 더 단순하고 확실. 다중 테넌트가 아님 |

## 결과(새 DB 에 적용 후 실제 시도, PostgreSQL 16)
| 시도 | 결과 |
|---|---|
| 앱: 2030년 파티션에 적재 · 스냅샷 새로 고침 | 성공(소유자 권한 함수) |
| 앱: 감사 로그 INSERT | 성공 |
| 앱: 감사 로그 UPDATE / DELETE · CREATE TABLE · analytics 표 DROP · 마이그레이션 이력 삭제 | **거부** |
| 조회: analytics·종목 SELECT | 성공 |
| 조회: app_user·audit_log SELECT · analytics INSERT | **거부** |

감수한 점: 배포 단계가 하나 늘었다(roles.sql 1회 + .env 연결 문자열 3개). 업데이트 때만 소유자 계정으로 initdb.

## 검증
```bash
sudo -u postgres psql -d outofstock -v owner_pw="'…'" -v app_pw="'…'" -v ro_pw="'…'" -f deploy/sql/roles.sql
.venv/bin/python tools/db_roles_check.py      # 권장 상태와 다른 항목 수(0 이 정상)
```
