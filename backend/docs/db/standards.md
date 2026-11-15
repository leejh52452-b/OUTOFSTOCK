# DB 표준 — 새 표·열·인덱스·마이그레이션을 만들 때

> 근거와 실측: [ADR-0027](../adr/0027-db-storage-and-lifecycle.md) · [공간 효율 실측](../perf/db_space_2026-11-03.md).
> 이 규칙 일부는 시험이 강제한다(`tests/test_db_types.py::test_metadata_has_no_string_dates_outside_documented_exceptions`).

## 1. 타입
| 담을 것 | 타입 | 파이썬에서 받는 값 | 쓰지 말 것 |
|---|---|---|---|
| 날짜(거래일·기준일·공시일) | `Date` | `'YYYY-MM-DD'` | `String(10)` |
| 업무 시각(사용자 행동·원장·결제·로그 — 화면에 한국시간으로) | `KST_TS` = `timestamp(0)` | `'YYYY-MM-DDTHH:MM:SS'` | `String(30)` |
| 외부에서 받은 순간(시세·지수·뉴스 발행·메시지) | `TSTZ` = `timestamptz(0)` | 한국시간 `datetime` | 오프셋을 붙인 문자열 |
| 보안·운영 사건(세션·이벤트·감사 기준 시각) | `DateTime(timezone=True)` | 한국시간 `datetime`(비교는 `clock.utc()`) | naive datetime |
| 원화 금액·가격 | `BigInteger`(금액) · `WON` = `integer`(주가) | `int` | `Float` — 소수 원이 없다(권리 반영으로 소수가 되는 평균단가만 `Float`) |
| 비율·점수·지수·외화 | `Float`(double) | `float` | |
| 상태·코드 | `String(n)` 짧게 | | 긴 `Text` |
| 큰 JSON | `Text`(TOAST — 2KB 넘으면 압축, 앱 연결은 lz4) | | `String(4000)` 같은 큰 varchar |
- 문자열로 남겨도 되는 시각은 넷뿐: 해시 체인 정규형(`audit_log.at`) · 외부 형식 그대로(`dart_corp.modify_date`) ·
  표시용 문자열(`iap_purchase.price`) · 격리소의 원값(`data_quality_issue.trade_date`).
- 시간대: DB 연결은 항상 `Asia/Seoul`(`DB_TIMEZONE`) — 시간대 없는 시각 문자열은 한국시간으로 해석된다. SQL 의 '오늘'은
  `(now() AT TIME ZONE 'Asia/Seoul')::date`(`CURRENT_DATE` 는 쓰지 않는다 — 결함 29).

## 2. SQL 작성
- 날짜·시각 열에 문자열 함수(`substr`·`left`·`LIKE`·`||`)를 쓰지 않는다 — `EXTRACT`·`date_trunc`·`to_char(… AT TIME ZONE 'Asia/Seoul', …)`.
- 빈 문자열(`''`)을 '없음'으로 쓰지 않는다 — `NULL`(`COALESCE(d, '')` 는 date 열에서 오류).
- 파이썬 목록을 날짜 열과 비교할 땐 배열 타입을 밝힌다: `trade_date = ANY(CAST(:ds AS date[]))`(문자열 목록은 text[] 로 간다).
- 식 인덱스를 쓰는 식은 한 곳에서 정의해 쿼리와 인덱스가 같은 글자를 쓴다(예: `db.NEWS_TS`). 식에는 IMMUTABLE 함수만.
- 긴 값의 중복 판정은 원문 대신 해시 키 인덱스(`md5(x)::uuid`) — 조회도 `md5(x)::uuid = md5(:x)::uuid AND x = :x`.

## 3. 인덱스
- 새 인덱스 전에 같은 앞 열의 기본키·유일 제약이 있는지 본다 — 있으면 만들지 않는다(진단이 '겹치는 인덱스'로 잡는다).
- 일부 행만 찾는 조회는 부분 인덱스(`WHERE status = 'pending'`) — 작고 빠르다.
- BRIN 은 삭제 없이 시간순으로만 쌓이는 큰 표(분석 계층 `price_history`)에만 — 수명주기로 지우는 표는 B-tree.
- 쓰이지 않는 인덱스는 진단(`run.py db-report`)이 통계 7일 뒤부터 알려 준다 — 지우기 전 월간 작업이 쓰는지 확인.

## 4. 표 저장·수명
- 자주 고치는 작은 표는 `_STORAGE` 에 fillfactor(70~90) — HOT 갱신. 지우는 큰 표는 자동 VACUUM 2%.
- 계속 쌓이는 표는 만들 때 보관 기간을 정한다: 개인정보 → `privacy.retention_purge`(파기), 공개·운영 데이터 → `ilm.DATASETS`(압축 보관 후 정리),
  돈 → 5년 보관(전자금융거래법). 분류 등록부(`app/security/classification.py`)의 보관 칸과 같아야 한다.
- 열 순서: 새 표는 8바이트 열(bigint·timestamp·double) → 4바이트(integer·date) → 가변 길이 순으로 두면 정렬 패딩이 준다.

## 5. 마이그레이션
- 기존 열 타입 변경은 `_retype` 규칙(검증 → 격리 → 한 번 다시 쓰기 → 대사 → ANALYZE)을 따른다. 새 표는 Table 정의만.
- 권한·역할·분석 계층 변경은 `sql/migrations/V0NN__*.sql`(적용된 파일은 고치지 않는다 — 새 번호로).
- 큰 표를 다시 쓰는 변경은 실험 DB 에서 시간을 잰다(`tools/db_lab.py`) — 그 시간이 배포 중단 시간이다.
- 되돌릴 수 없는 변경(타입 변경 등)은 `vm.sh rollback` 가드에 등록하고 업데이트 직전 백업 복원 절차를 적는다.

## 6. 리뷰 체크리스트
- [ ] 날짜·시각·금액 타입이 위 표대로인가 · 문자열 날짜 예외에 넣을 이유가 있나
- [ ] 새 인덱스가 기존 키와 겹치지 않나 · 식 인덱스의 식이 쿼리와 같은가
- [ ] 보관 기간·파기/보관 방식이 분류 등록부·수명주기에 들어갔나
- [ ] 최소 권한(V0NN) — 앱 계정이 지우거나 고치면 안 되는 칸은 막았나 · `db_roles_check --strict` 차이 0
- [ ] 데이터 사전 다시 생성(`run.py data-docs`)
