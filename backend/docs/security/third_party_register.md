# 제3자(외부 연동) IT 리스크 등록부

> 금감원은 2026 디지털·IT부문 감독 업무설명회(2026.3.4)에서 클라우드·소프트웨어 공급망을 통한 IT 리스크 확산을 짚고
> 「제3자 IT리스크 관리 가이드라인」을 마련해 IT 실태평가에 반영하겠다고 했다(계획). 금융위 망분리 개선 로드맵(2024.8)도
> AI·클라우드·데이터센터 등 제3자 위탁 증가에 맞춰 제도 정비를 과제로 뒀다. 이 서비스는 대상 기관이 아니므로 **설계 기준**으로,
> 외부에 기대는 지점을 한 장에 모아 '무엇을 보내고, 끊기면 어떻게 되고, 어떻게 빠져나오나'를 적는다.
> 허용 목록의 원본은 코드(`app/security/egress.py` `ALLOWED` — 코드에 적힌 외부 주소는 시험이 모두 여기 있는지 확인)다.
> 이 등록부의 §1-1 호스트 표는 허용 목록과 한 줄도 어긋나면 안 된다 — 시험(`test_risk_register.py`)과 운영 점검(프로브 `egress_register`)이 확인한다.

## 0. 선정 기준·이용 요건 — 새 외부 연동을 들이기 전에(금융보안 수준진단 7.1)

새 원천·서비스는 아래 8개를 모두 답한 뒤에 들인다. S1~S4 는 §1 표의 열로 남기고(빈 칸이 있으면 시험 실패),
S5~S8 은 코드가 강제한다. 계약서가 따로 없는 공개 API 는 약관·호출 한도가 '계약 요건'에 해당한다.

| # | 기준 | 확인(증거) |
|---|---|---|
| S1 | 데이터 최소화 — 개인정보를 보내지 않는다. 보내야 하면 목적·항목·보관을 적고 가명·암호화 | §1 '보내는 것' 열 |
| S2 | 중요도 — 끊기면 어떤 업무가 멈추나(높음·중간·낮음) | §1 '중요도'·'끊기면' 열 |
| S3 | 대체 경로 또는 정직한 표시(직전 값 + 기준일·출처) | §1 '대체·완화' 열 |
| S4 | 빠져나오기 — 서비스 종료·약관 변경 시 교체하거나 기능을 끄는 방법 | §1 '빠져나오기' 열 |
| S5 | 전송 보안 — HTTPS·인증서 검증, 리다이렉트도 허용 목록 검사, 내부 주소 거부 | `egress.check_url` · `test_egress.py` |
| S6 | 비밀값 — API 키는 `.env`(600), DB 에 두는 토큰은 봉투 암호화, 로그·알림에서 가림 | `app/crypto.py` · `app/redact.py` |
| S7 | 이용 조건 — 약관·호출 한도·재배포 허용 범위를 확인하고 분기마다 다시 본다 | §4 점검 주기 |
| S8 | 등록 — 허용 목록(`egress.ALLOWED`)과 §1-1 호스트 표에 함께 넣는다(어긋나면 시험·운영 점검 실패) | `tests/test_risk_register.py` · 프로브 `egress_register` |

## 1. 외부 데이터·서비스

| 제3자 | 용도 | 보내는 것(개인정보) | 중요도 | 끊기면 | 대체·완화 | 빠져나오기 |
|---|---|---|---|---|---|---|
| KRX OpenAPI | 국내 일별 시세·지수 | API 키만(개인정보 없음) | 높음 — 국내 시세 원천 | 그날 적재 실패 → 전날 종가 유지 + '기준일' 표시 · 품질 게이트·알림 | KIS 당일 종가 봉 · 3회 연속 실패 시 받은 날까지만 반영 | 공개 통계 데이터 — 다른 원천(FDR)로 교체 가능 |
| 한국투자증권 KIS | 실시간 체결가·당일 봉·지수(REST·웹소켓) | 앱 키·토큰(봉투 암호화 보관) | 중간 — 실시간 표시 | 실시간 대신 '종가 기준' 표시 | 장 마감 후 KRX·야후 지수 대체 | 실시간은 선택 기능 — 끄고 운영 가능 |
| 금융감독원 OpenDART | 재무제표·공시 | API 키만 | 중간 — 재무 건전성·PER | 직전 수집 재무 유지(공시일 기준 PIT) | 대체 원천 없음 → 직전 재무 + 공시일 표시 | 공공 데이터 — 갱신만 멈추고 화면은 '공시일 기준'으로 유지 |
| Google News RSS | 뉴스 메타데이터(제목·출처·링크) | 없음(하루 1번 조회) | 낮음 | 그날 뉴스 갱신 멈춤 | 직전 뉴스 유지 | 본문 미저장 — 다른 RSS 로 교체 |
| Finnhub | 해외 실시간·최신 시세 | API 키 | 낮음 — 해외 화면 | 해외 시세 '기준일' 표시 | FDR 일별 시세 | 화면 기능 축소로 대응 |
| SEC EDGAR | 해외 재무 | 없음(요청 헤더에 연락처만) | 낮음 | 직전 재무 유지 | 대체 원천 없음 → 직전 재무 + 공시일 표시 | 공공 데이터 — 갱신만 멈춤 |
| FinanceDataReader 경유 원천(KRX 정보데이터·KIND·야후·네이버·GitHub 캐시) | 종목 목록·해외 일봉·지수 대체 | 없음 | 낮음 — 보조 원천 | 목록·해외 일봉 갱신 멈춤 | KRX OpenAPI·Finnhub 순 | 라이브러리 교체 또는 해당 화면 '기준일' 유지 |
| 한국수출입은행 · 한국은행 ECOS | 환율·금리 | API 키 | 낮음 | 직전 환율·금리 캐시 | 대체 원천 없음 → 직전 값 + 기준일 표시 | 공공 데이터 — 갱신만 멈춤 |
| 토스페이먼츠 | 가상머니 충전 결제 승인 | 주문 ID·금액(카드정보는 토스 화면 — 서버 미보관) | 중간 — 충전 | 충전만 불가 | 인앱 결제 경로 | 결제 기능 끄기(`enabled:false`) · 비상 차단 '충전' |
| 텔레그램·웹훅·메일(선택) | 운영 알림 | 알림 문구(비밀값·주민번호·카드번호 가림 — `redact`) | 낮음 | 알림 누락 → `alert_log`·`/health` 에는 남음 | 채널 여러 개 동시 | 설정만 비우면 끝 |

### 1-1. 허용 목록 ↔ 등록부(호스트 표 — `egress.ALLOWED` 와 한 줄도 어긋나면 안 된다)

| 호스트 | 제3자(§1) |
|---|---|
| data-dbg.krx.co.kr | KRX OpenAPI |
| openapi.koreainvestment.com | 한국투자증권 KIS |
| openapivts.koreainvestment.com | 한국투자증권 KIS |
| ops.koreainvestment.com | 한국투자증권 KIS |
| opendart.fss.or.kr | 금융감독원 OpenDART |
| dart.fss.or.kr | 금융감독원 OpenDART |
| news.google.com | Google News RSS |
| finnhub.io | Finnhub |
| ws.finnhub.io | Finnhub |
| data.sec.gov | SEC EDGAR |
| www.sec.gov | SEC EDGAR |
| data.krx.co.kr | FinanceDataReader 경유 원천 |
| kind.krx.co.kr | FinanceDataReader 경유 원천 |
| query1.finance.yahoo.com | FinanceDataReader 경유 원천 |
| query2.finance.yahoo.com | FinanceDataReader 경유 원천 |
| fchart.stock.naver.com | FinanceDataReader 경유 원천 |
| api.stock.naver.com | FinanceDataReader 경유 원천 |
| finance.naver.com | FinanceDataReader 경유 원천 |
| raw.githubusercontent.com | FinanceDataReader 경유 원천 |
| financedata.github.io | FinanceDataReader 경유 원천 |
| ecos.bok.or.kr | 한국수출입은행 · 한국은행 ECOS |
| www.koreaexim.go.kr | 한국수출입은행 · 한국은행 ECOS |
| oapi.koreaexim.go.kr | 한국수출입은행 · 한국은행 ECOS |
| api.tosspayments.com | 토스페이먼츠 |
| api.telegram.org | 텔레그램·웹훅·메일(선택) |

## 2. 소프트웨어 공급망

| 대상 | 통제 | 증거 |
|---|---|---|
| 파이썬 패키지 | 해시 고정 잠금 파일(`requirements.lock`)로만 설치 · 주 1회 취약점 점검(pip-audit) · SBOM | CI `security` 작업 · `docs/security/scan_report.md` |
| GitHub Actions | 액션을 커밋 SHA 로 고정 | `.github/workflows/ci.yml` · `test_deploy_config.py` |
| 화면 스크립트 | 외부 CDN 금지(같은 출처 `assets/` 만) — CSP `script-src 'self' 'nonce-…'` · 팀 화면 반영 도구가 외부 스크립트를 미리 경고 | `test_web_security.py` · `tools/sync_web.py` |
| 컨테이너 기반 이미지 | 공식 이미지 · 읽기 전용 · capability 0 | `Dockerfile` · `docker-compose.prod.yml` |

## 3. 통제 — 끊김·오염이 번지지 않게
- **나가는 연결 3중** — 앱 허용 목록(위 표만) → egress 프록시(같은 목록 + 내부 주소 거부) → 커널 차단(VM). 목록 밖은 연결 전에 거부·기록.
- **서킷 브레이커** — 원천이 연속으로 실패하면 잠시 호출을 멈추고 빠르게 실패(`egress.status()` → `/admin/metrics` 의 `egress_circuits`).
- **적재 품질 게이트** — 출처·완전성·이력·적시성·상식 검사, 위반이면 그날 적재를 반영하지 않고 알림(ADR-0001).
- **비밀값** — 외부 키는 `.env`(600)·봉투 암호화(DB 저장 토큰) · 로그·알림·DB 메모에서 가림(`redact`).
- **비상 차단** — 결제 원천 이상이면 '충전' 기능만 멈춘다(ADR-0023, 재개는 2인 승인).

## 4. 점검 주기(제안)
분기 1회 — 위 표의 중요도·대체 경로가 여전히 맞는지, 허용 목록에 쓰지 않는 호스트가 남았는지(`tools/egress.py`), 원천 약관·호출 한도 변경 여부(S7).
허용 목록 ↔ §1-1 일치와 선정 기준 칸(S1~S4)은 매월 수준진단(7.1·7.2)이 자동으로 다시 본다.
