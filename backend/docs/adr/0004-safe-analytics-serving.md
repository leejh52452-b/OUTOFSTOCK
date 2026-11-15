# ADR-0004 분석 쿼리는 '이름 있는 SQL 파일 + 화이트리스트 파라미터'로만, 읽기 전용·시간 제한 연결에서 돌린다

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-10 · **화면 API 부분 폐기 · 2026-10-20** — 분석·골라보기·산업 화면이 기각되어 `/analytics/*` 경로를 지웠다. 이름 있는 SQL·화이트리스트 파라미터·읽기 전용·시간 제한 원칙은 CLI(`run.py analytics-query`)에 그대로 남는다 |
| 범위 | 2번 분석 SQL 서빙 · 토스 '주식 골라보기'(11개 + 직접 조건) · '지금 뜨는 산업' |
| 코드 | `sql/analytics/*.sql`(9개) · `app/services/platform.py`(queries·run_query·ro_conn) · `run.py analytics-query`(2026-10-20 까지는 `app/api.py` `/analytics/*`·골라보기 `screen`) |

## 맥락
분석 화면은 사용자가 조건을 바꿔 가며 SQL 을 실행하는 기능이다. 그대로 열면 ① SQL 인젝션 ② 무거운 쿼리 하나가 DB 를 붙잡는
자원 고갈 ③ 분석용 연결로 계정·결제 표를 읽는 권한 남용이 생길 수 있다. 동시에 면접·검증을 위해 **어떤 SQL 이 돌았는지 보여 줄 수 있어야** 한다.

## 결정
1. **이름 있는 쿼리**: `sql/analytics/<이름>.sql` 파일이 곧 API 다(`/analytics/queries/<이름>`). 파일 머리에
   `-- params: days:int=60:5:250` 처럼 이름·형식·기본값·범위(또는 `enum` 선택지)를 적고, 서버는 **선언된 파라미터만**
   받아 형식 변환·범위 검사 후 **바인드 변수**로 넘긴다. 사용자가 SQL 조각을 보낼 길이 없다.
2. **읽기 전용 + 시간 제한 연결**: `SET TRANSACTION READ ONLY` + 트랜잭션 범위 `statement_timeout`(기본 5초,
   `ANALYTICS_QUERY_TIMEOUT_MS`). 넘기면 PostgreSQL 이 취소(SQLSTATE 57014)하고 API 는 504 로 알린다.
   운영에서는 분석 연결을 **읽기 전용 DB 계정**(`oos_ro`, `ANALYTICS_DATABASE_URL`)으로 분리(ADR-0005).
3. **골라보기 = 서버가 가진 조건만**: 토스증권 공개 조건 11개를 서버 상수(SQL 조각)로 옮기고, 직접 조건은
   허용 항목 20개(`FILTER_COLS` — PER·PBR·ROE·배당수익률·등락률 …)의 `항목_min/항목_max` 숫자만 받는다.
   정렬 항목도 같은 화이트리스트, 결과는 최대 300행. 투자 유의 종목은 기본으로 뺀다(`hide_risky=true`).
4. **투명성**: 화면의 'SQL 보기'가 실행한 원문을 그대로 보여 주고, 응답에 파라미터·실행 시간(ms)·기준일을 싣는다.

## 쿼리 목록(무엇을 보여 주려고 골랐나)
| 이름 | SQL 기법 | 질문 |
|---|---|---|
| volume_spike | 윈도 함수(ROWS 프레임) + 파티션 제외 | 오늘 거래량이 20일 평균의 N배 이상인 종목 |
| new_high_52w · low_per_pbr | 머티리얼라이즈드 뷰 | 52주 신고가 · 저PER·저PBR 흑자 기업 |
| op_profit_growth | LAG | 영업이익이 전년보다 크게 는 기업 |
| market_breadth | 집계 + 이동평균 | 상승·하락 종목 수와 등락비율(시장 체력) |
| sector_rotation | RANK() OVER (PARTITION BY 월) | 달마다 어느 업종이 1등이었나 |
| investor_cumulative | SUM() OVER (누적) | 외국인·기관 누적 순매수 |
| credit_spread | 조건부 집계(FILTER) 피벗 | 회사채 − 국고채 스프레드, 장단기 금리차 |
| drawdown_1y | 누적 최댓값 윈도 | 1년 최대낙폭이 큰 종목 |

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 자유 SQL 입력(읽기 전용 계정만 믿기) | 읽기만으로도 무거운 조인으로 서비스를 멈출 수 있고, 권한 실수 한 번이면 개인정보 노출 |
| ORM 쿼리 빌더로 동적 조건 | 조건 조합은 막을 수 있지만 '어떤 SQL 이 돌았나'를 보여 주기 어렵다 — 파일이 문서이자 테스트 대상 |
| 결과 캐시만 두기 | 파라미터 조합이 많아 적중률이 낮다 — 스냅샷 MV(ADR-0003)가 더 단순 |

## 결과
- 인젝션 시도(`per_max=1;DROP …`·없는 항목·범위 밖 값)는 400, 오래 걸리는 쿼리는 5초에 끊긴다.
- 감수한 점: 새 분석을 넣으려면 SQL 파일과 테스트를 추가해야 한다(대신 리뷰 가능한 변경이 된다).

## 검증
```bash
.venv/bin/python run.py analytics-query volume_spike min_ratio=3 limit=10 | head -30
.venv/bin/python run.py analytics-query volume_spike 'min_ratio=1;DROP TABLE x'   # 쿼리 오류(종료 코드 2) — SQL 에 닿기 전에 거부
.venv/bin/python -m pytest -q tests/test_platform.py -k named_queries
```
