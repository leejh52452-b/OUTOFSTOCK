# ADR-0027 DB 저장 표준 · 공간 효율 · 데이터 수명주기 · DB 진단 · 서버 자동 튜닝

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-11-03 |
| 근거(설계 기준) | PostgreSQL 16 문서(데이터 타입·TOAST·식 인덱스·자동 VACUUM·ALTER SYSTEM·pg_stat_statements·pg_dump 압축) · NIST CSF 2.0 PR.IR-04(가용성을 지키는 자원 용량)·ID.AM-08(데이터를 수명주기 전체에 걸쳐 관리) · 개인정보 보호법 제21조(보관 기간이 지난 개인정보는 '파기' — 따로 보관하지 않는다) |
| 코드 | `app/db.py`(타입 표준·연결 로더·연결 시간대·`_retype`·`NEWS_TS`·md5 주소 키·`_STORAGE`·겹치는 인덱스 정리) · `app/services/ilm.py` · `app/services/dbhealth.py` · `app/services/dbtune.py` · `deploy/vm.sh`(`db_tune`·되돌리기 가드) · `tools/dr.py`(`dump_compression`) · `tools/db_lab.py` · `sql/migrations/V018__db_operations.sql` |
| 시험 | `test_db_types.py` · `test_ilm.py` · `test_dbhealth.py` |
| 기록 | [DB 공간 효율 실측(1년 규모)](../perf/db_space_2026-11-03.md) · [DB 표준](../db/standards.md) |

## 맥락
기능은 많아졌는데 저장 구조는 첫 버전 그대로였다. 날짜·시각을 문자열(varchar 10~40)로, 원화 가격을 double 로 저장했고,
기사 중복은 주소 원문(평균 180바이트) B-tree 로 막았다. 단일 열 인덱스가 같은 앞 열의 복합 키와 겹쳤고, 뉴스·검색 기록은
지우는 규칙 없이 계속 쌓였다. DB 서버는 설치 기본값(shared_buffers 128MB·느린 쿼리 통계 없음)이었고 백업은 gzip 이었다.
운영 1년 규모를 합성해 재 보니 1,182MB 중 **78%(923MB)가 뉴스·검색 4개 표**, 그중 주소 인덱스 하나가 291MB 였다.

## 결정
1. **타입 표준** — 저장은 맞는 타입으로:
   | 무엇 | 타입 | 서비스 계층이 받는 값(예전과 같음) |
   |---|---|---|
   | 날짜(거래일·기준일·공시일·출석일…) | `date`(4바이트) | `'YYYY-MM-DD'` 문자열 |
   | 업무 시각(원장·출석·결제·경보·로그…, 예전 `clock.iso_now`) | `timestamp(0)` 한국시간 벽시계(8바이트) | `'YYYY-MM-DDTHH:MM:SS'` 문자열 |
   | 외부·시세 시각(예전 `'+09:00'` 문자열 — 시세·지수·뉴스·채팅·친구·메타) | `timestamptz(0)` | 한국시간 `datetime`(isoformat 이 예전 문자열과 같다) |
   | 보안·운영 시각 | `timestamptz`(이미) | 한국시간 `datetime`(예전엔 UTC — 화면이 시각 앞부분을 잘라 쓰던 곳의 9시간 어긋남도 사라짐) |
   | 원화 가격(일봉·실시간·실습 체결·추천 기준가) | `integer`(4바이트, 최대 21억 원) | 정수 |
   예외(문자열 유지 — 이유가 있다): `audit_log.at`(해시 체인의 정규형) · `dart_corp.modify_date`(DART `YYYYMMDD`) ·
   `iap_purchase.price`(표시가 `₩3,300`) · `data_quality_issue.trade_date`(격리소 — 깨진 원값을 그대로 적는다).
   권리 반영으로 소수가 되는 `invest_position` 매수·매도가, 지수·해외(USD)·비율·점수는 `double` 그대로.
2. **서비스 계약 유지** — 저장 타입만 바꾸고 값의 모양은 연결 경계에서 예전 그대로 맞춘다: psycopg 연결마다 `date`·`timestamp`
   읽기 로더(문자열), 연결 시간대를 **Asia/Seoul 로 고정**(서버 설정과 무관 — VM 은 UTC, 개발 PC 는 KST 였다), `DateStyle ISO`.
   시간대 없는 시각 문자열 = 한국시간이라는 `clock.py` 규칙을 DB 도 같은 뜻으로 해석한다. 쓰기는 문자열 그대로 보내면 DB 가 바꾼다.
3. **재작성 마이그레이션**(`init_db` → `_retype`) — Table 정의가 기준. DB 의 열이 아직 문자열·double 인데 정의가 date·timestamp·integer
   면 표마다 ① 검증 정규식(달의 마지막 날까지 — 2월 30일·13월 거름, CASE 로 순서를 지켜 깨진 값으로 캐스트하지 않는다)으로 깨진 값 세기
   ② 깨진 값은 격리소에 기본키·원값 ③ ALTER 한 번(표를 한 번만 다시 쓴다) ④ **대사** — 바뀐 뒤 값 있는 행 = 전 − 깨진 값,
   다르면 **전체 롤백** ⑤ ANALYZE(타입이 바뀐 열의 통계는 지워진다). 깨진 값은 NULL(비울 수 있는 열)·1970-01-01(NOT NULL)·
   행 삭제(기본키 — 덧붙이기 전용 표면 중단). 시간대 표기가 있으면 그 순간 그대로, 없으면 한국시간. 다시 돌리면 할 일 없음.
   이 트랜잭션만 `statement_timeout 0`·`lock_timeout 60s`·`maintenance_work_mem 256MB`.
4. **공간** — 기사 중복 키를 주소 원문 대신 `md5(url)::uuid`(16바이트) 유일 인덱스로(보안 해시가 아니라 중복 판정 키 — 외부 RSS 주소라
   의도적 충돌 동기가 없고 우연 충돌은 10^-26 수준). 새 인덱스를 만든 뒤 옛 제약을 지운다(같은 트랜잭션 — 틈 없음).
   앞 열이 같은 키·유일 제약과 겹치는 인덱스 7개 제거(덮는 인덱스가 실제로 있을 때만). 자주 고치는 작은 표는 fillfactor 70~90
   (HOT 갱신 — 인덱스를 안 고치는 갱신), 수명주기로 행을 지우는 큰 표는 자동 VACUUM·통계 기준 2%. 앱 연결은 TOAST 압축 lz4.
   발행 시각 식(`NEWS_TS`)은 한 곳에서 정의하고 식 인덱스와 쿼리가 같은 식을 쓴다(date→timestamp·timezone(text, timestamp) 는 IMMUTABLE).
5. **데이터 수명주기**(`ilm`) — 뉴스(기사·태깅·감성)와 검색 기록만. 보관 기한(기본 400일 ≈ 13개월)이 지난 **달 전체**를
   gzip JSON Lines 로 내보내며 SHA-256 → 다시 읽어 검증 → manifest·`archive_log`(앱은 지우거나 해시를 고칠 수 없다 — V018) →
   5,000건씩 짧은 트랜잭션으로 정리 → VACUUM. 외부 보관 명령(`BACKUP_OFFSITE_CMD`)이 있으면 그 복사가 성공한 달만 지운다.
   복원은 해시부터 확인하고 `ON CONFLICT DO NOTHING`. 보관 폴더 밖 manifest 는 거부. 개인정보 표는 대상이 아니다(파기 대상).
6. **DB 진단**(`dbhealth`) — 크기·표별 비대화(죽은 행)·쓰이지 않는/겹치는 인덱스·캐시 적중률·XID 나이·연결 사용률·오래 열린
   트랜잭션·임시 파일·주요 설정·느린 쿼리 상위. 매일 크기 1행(`db_size_snapshot`, 2년) → 최근 60일 최소제곱 기울기로 **디스크 소진 예측**.
   기준을 넘으면 경보, 화면 `GET /admin/db`(운영 역할), 명령 `run.py db-report`, 수준진단 운영 점검 2개(`db_capacity`·`archive_integrity`).
7. **서버 설정 자동 적용** — 권장값 계산은 시험 가능한 파이썬(`dbtune` — 메모리·CPU·버전): shared_buffers(8GB 미만 서버는 20% — 앱과
   같은 서버)·effective_cache_size·work_mem·maintenance_work_mem·SSD 비용·WAL 압축(PG15+ lz4)·TOAST lz4·체크포인트·느린 쿼리
   500ms·잠금 대기·임시 파일·자동 VACUUM 기록·자동 VACUUM 10%·유휴 트랜잭션 10분·JIT off·pg_stat_statements. `vm.sh update` 가
   서비스가 멈춘 동안 바뀐 값만 `ALTER SYSTEM` 으로, 재시작이 필요한 값이 바뀌면 PostgreSQL 을 한 번 재시작 — 뜨지 않으면
   바꾸기 전 `postgresql.auto.conf` 로 되돌린다. 같은 서버의 DB 일 때만, `DB_AUTOTUNE=off` 로 끈다, `vm.sh db-tune --reset` 으로 되돌린다.
8. **백업 압축** — pg_dump 16+·zstd 지원이면 zstd:3(실측 gzip:6 대비 7.7배 빠르고 10% 작다), 아니면 gzip 6. manifest 에 방식 기록.
9. **되돌리기** — 타입 정리는 앞으로만 간다. 코드만 V018 전 버전으로 되돌리면 이전 코드가 새 형식을 못 읽으므로 `vm.sh rollback` 이
   막고, 업데이트 직전 백업 복원(`vm.sh restore`) 뒤 `FORCE=1` 로 진행하라고 안내한다.

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 서비스 계층 전체를 date·datetime 객체로 | 수백 곳(문자열 자르기·비교·JSON 캐시) 수정 + 화면 계약 변경 위험 — 저장만 바꾸고 경계(드라이버 로더)에서 예전 모양 유지 |
| 모든 시각을 timestamptz 로 | 업무 시각(KST 문자열 계약)까지 datetime 이 되어 문자열 연산이 깨진다 — 한국은 서머타임이 없어 KST 벽시계 timestamp 가 모호하지 않다 |
| 뉴스 표 월별 파티션 | PostgreSQL 유일 인덱스는 파티션 키를 포함해야 해 주소 전역 유일성을 지킬 수 없다 — 1년 100만 행은 일괄 삭제 + 빈자리 재사용으로 충분(분석 계층 price_history 는 이미 연도 파티션) |
| 검색 시각 BRIN | 앞쪽 페이지를 지우면 새 행이 빈자리로 들어가 시간 순서(상관)가 깨져 BRIN 범위가 넓어진다 — 삭제 없는 price_history 에만 BRIN |
| 열 순서 재배치(정렬 패딩 줄이기) | 표를 새로 만들어 옮겨야 한다(RLS 정책·권한·시퀀스·트리거 재생성) — 이득은 행당 몇 바이트. 새 표 설계 규칙으로만 |
| sha256 주소 키 | 32바이트 — 중복 판정에는 md5 16바이트로 충분(무결성·인증에는 쓰지 않는다) |
| VACUUM FULL·pg_repack 정기 실행 | 표 잠금·디스크 2배 — 자동 VACUUM 기준을 낮추고 재사용, 비대화는 진단이 감시 |
| 외부 DBA 서비스(pganalyze 등) | 계정·외부 전송이 늘고 경보·진단 점검과 이어지지 않는다 — pg_stat_statements + 자체 진단 |

## 결과
- 같은 1년 규모 데이터 **1,182MB → 807MB(−32%, 재구축 대비 −26%)** · 뉴스 −46% · 주소 인덱스 291 → 30MB · 겹치는 인덱스 7개 제거.
- 같은 실행 계획, 더 적은 버퍼 — 전 종목 정렬 144 → 70ms, 뉴스 7일 집계 3.1 → 1.7ms.
- 마이그레이션 40개 표·72개 열 61~70초(1년 규모), 깨진 값 5개 → 격리소, 대사 일치.
- 수명주기: 두 달 59만 행 → 18.5MB 파일(뉴스 5.2배·검색 15배 압축) 17.6초, 재사용 빈자리 73MB. 3년 뒤 뉴스·검색 DB 2.77GB → 약 0.69GB.
- 백업: zstd:3 111MB·4.6초(gzip:6 124MB·35.6초).

## 한계
- 타입 정리는 되돌릴 수 없다(백업 복원으로만). 디스크 소진 예측은 DB 와 같은 서버라고 가정한다(`DB_DISK_PATH`).
- 보관 파일은 서버 디스크(`var/archive`) — 외부 보관 명령이 없으면 한 곳뿐이다(진단·문서에 표시).
- 자동 튜닝은 같은 서버의 PostgreSQL 만 — Docker 운영(compose)은 `docker-compose.prod.yml` 의 고정값(환경변수로 조정).
- 시험 환경 DB 는 작아서 인덱스 선택을 실측하지 못한다 — 실행 계획은 실험 DB(`tools/db_lab.py`)로 확인했다.
