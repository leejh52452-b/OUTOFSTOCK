# ADR-0001 적재는 '기대치와 비교해 통과해야' 끝난다 — 품질 게이트 + 장애 알림

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-10 |
| 범위 | 1번 장애 대응 체계 |
| 코드 | `app/services/quality.py`(gate_market_load·gates) · `app/services/alerts.py` · `app/services/monitoring.py`(`/health`) · `run.py`(quality·alert-test) · `app/collectors/dart.py`(상태 코드) |
| 문서 | `docs/incidents/2026-10-10-krx-load-rollback.md`(INC-2026-10-10-01) |

## 맥락
VM 설치 직후부터 매일 23:00 배치의 KRX 시세 적재가 마지막 단계(메타값 저장)에서 `VARCHAR(2000)` 초과로 실패해
트랜잭션 전체가 롤백됐다. 배치는 PARTIAL 로 끝났지만 **로그와 실행 이력에만 남았고**, 사용자가 홈의 빈 '추천 Top 5'를
보고 문의할 때까지 아무도 몰랐다. 문제는 두 가지였다.
1. "예외 없이 끝났다"를 "성공"으로 봤다 — 0행 적재·표본 데이터 대체도 성공처럼 지나갈 수 있었다.
2. 실패를 사람에게 알리는 경로가 없었다.

## 결정
1. **적재 검증(품질 게이트)** — 적재가 끝나면 기대치와 비교해 `ok / warn / fail` 을 매긴다.
   - 출처: KRX 키가 있는데 표본(생성) 데이터면 fail(KRX 실패로 기존 실데이터를 유지했으면 warn)
   - 완전성: 유니버스가 `UNIVERSE_TOP_N` 의 90% 이상인가
   - 이력 깊이: 최근 100일 안에 일봉 60개 이상인 종목이 90% 이상인가
   - 적시성: 직전 거래일보다 1거래일 늦으면 warn, 2거래일 이상이면 fail
   - 상식 검사: 최신일 종가 0 이하 · 가격제한폭(±30%)을 넘는 등락이 있으면 fail
   문턱은 모두 설정값(`QUALITY_MIN_*`). 결과는 `app_meta` 에 남기고 `/health.quality_gates` · `python run.py quality` 로 본다.
2. **장애 알림** — 웹훅(Slack·Discord)·텔레그램·메일 중 설정한 채널 **모두**로 보낸다.
   - 같은 key 는 `ALERT_DEDUP_HOURS`(기본 6시간) 안에 다시 보내지 않는다(알림 피로 방지).
   - 보내기 전에 비밀값을 가린다(오류 문구 속 URL 에 API 키가 섞일 수 있음).
   - 채널 하나가 실패해도 나머지는 보내고, 알림 실패가 배치를 멈추지 않는다.
   - 연결 지점: 배치 단계 실패 · 품질 게이트 fail · 예약 작업 예외 · 실시간 수집기 재시작/중단.
3. **원인을 말하는 화면·도구** — 추천이 비면 이유(시세 없음·이력 부족·성향 탈락)를 안내, `tools/reco_check.py` 로 숫자 진단.
4. DART 오류는 **상태 코드 의미**로 바꿔 기록하고, 키 오류 같은 치명 코드는 그 회차를 즉시 멈춘다(헛호출·한도 소진 방지).

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 로그 모니터링 도구(ELK·Loki) 도입 | 단일 VM·학생 프로젝트 규모에 운영 부담이 크다. 지금 필요한 것은 '기대치 검증 + 통보' |
| 실패 시 배치 전체 중단 | 시세가 실패해도 뉴스·지수 등 독립 단계는 계속 도는 편이 낫다 — 단계별 결과를 기록하고 알림 |
| 게이트 없이 알림만 | 이번 장애처럼 '예외 없는 0행'은 알림이 울리지 않는다 |

## 결과
- 같은 장애가 다시 나면 **배치 당일** 알림 + `/health` 의 `quality_gate_failed` 로 드러난다.
- 감수한 점: 기대치(90%·60봉·2거래일)는 경험값이다 — 휴장·신규 상장이 많은 날 warn 이 날 수 있어 문턱을 설정값으로 뒀다.

## 검증
```bash
.venv/bin/python run.py fetch-market   # 시세 적재 + 게이트 → '적재 검증: ok — 기대치 충족'
.venv/bin/python run.py quality        # 최근 게이트 결과·알림 이력
.venv/bin/python run.py alert-test     # 채널 시험(설정했다면 실제로 도착하는지)
curl -s localhost:8000/health | python3 -m json.tool | grep -A4 quality_gates
.venv/bin/python -m pytest -q tests/test_incident_response.py
```
