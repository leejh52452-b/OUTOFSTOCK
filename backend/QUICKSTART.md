# OUTOFSTOCK 백엔드 — 설치 후 처음부터 실행

Windows(Docker Desktop + Python 3.11+) 기준. macOS/Linux는 `.venv\Scripts\activate` 대신 `source .venv/bin/activate`.

## 0. 준비물
- Docker Desktop (실행 중)
- Python 3.11+

## 1. 압축 풀고 폴더로 이동
```powershell
# 압축 푼 폴더 안(= requirements.txt 가 있는 곳)으로 이동
cd stock-backend
```

## 2. PostgreSQL 기동 (Docker)
```powershell
docker compose up -d db
docker compose ps          # db 가 healthy 될 때까지 몇 초 대기
```
→ DB `outofstock` + 테스트 DB `outofstock_test` 가 자동 생성된다(oos / oos).

## 3. 가상환경 + 의존성
```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements-dev.txt     # 실행 + 시험 도구(운영 이미지는 requirements.lock 해시 고정)
```

## 4. 환경변수(.env)에 내 API 키 넣기
`.env.example` 을 복사해 `.env` 로 만들고, 아래 값을 채운다(실데이터 쓸 때만 필요):
```
KRX_API_KEY=발급받은_KRX_인증키
DART_API_KEY=발급받은_DART_키
KIS_APP_KEY=한투_APP_KEY
KIS_APP_SECRET=한투_APP_SECRET
# 모의투자 계좌면 true (값 뒤에 주석을 붙이지 말 것 — systemd 에서 그대로 값이 된다)
KIS_PAPER=true
```
DB 접속값(DATABASE_URL)은 기본값이 compose 와 일치하므로 안 건드려도 된다.

## 5. DB 초기화 + 데이터 채우기 (둘 중 하나)
```powershell
python run.py demo         # 빈 DB 를 오프라인 생성 데이터로 — 즉시, 인터넷/키 불필요 (실데이터가 있으면 거부)
# 또는
python run.py real         # 실데이터 — KRX 시총 상위 150 + DART 재무 + 뉴스 (인터넷·키 필요)
# 또는 — 키 없이 VM 의 실데이터를 그대로(주원이 준 스냅샷 파일)
python run.py initdb
python run.py snapshot-import outofstock-snapshot-YYYYMMDD-HHMM.jsonl.gz --yes
```
스냅샷은 시세·지수·재무·뉴스·해외 표만 바꾼다(내 PC 의 계정·지갑·채팅·로그는 그대로). 파일에는 계정·토큰이 들어 있지 않다.

**기존 DB 에 업데이트만 받았다면**(2026-10-01 이후 버전) 한 번:
```powershell
python run.py initdb        # 새 컬럼·표 무손실 추가
python run.py fetch-index   # 코스피·코스닥 지수 + 출처·기준일 확인(실패 소스·이유 출력)
```
지수가 `KRX … 401` 로 실패하면 openapi.krx.co.kr 에서 'KOSPI/KOSDAQ 시리즈 일별시세정보' 이용신청(키와 별개).

## 6. 서버 실행
```powershell
uvicorn app.api:app --host 0.0.0.0 --port 8000
```
- 화면: http://localhost:8000/ (용어사전 learn.html · 뉴스 news.html · 실습 practice.html · 마이페이지 mypage.html)
- API 문서: http://localhost:8000/docs

## 7. (실데이터 실시간) 장중 초 단위 갱신 — 별도 창
uvicorn 은 켜 둔 채 **새 터미널**에서:
```powershell
.venv\Scripts\activate
python -m app.collectors.realtime          # 실제 KIS 체결가 — 평일 09:00~15:30(KST) + KIS 키 필요
```
- **주의**: 실제 실시간은 (1) 장 시간이고 (2) `.env` 에 KIS 키가 있고 (3) 이 프로세스가 계속 떠 있을 때만 흐른다. 셋 중 하나라도 아니면 화면은 **마지막 거래일 종가**(예: 월요일 아침이면 지난 금요일 종가)를 보여주며, 이건 정상이다.
- **키·장시간 없이 실시간 갱신을 눈으로 확인**하려면 시뮬레이션 모드:
```powershell
python -m app.collectors.realtime --sim    # 종목 현재가를 매초 흔들어 넣음(배관 확인용)
```
  → 이 상태로 종목/홈/상세를 열면 "● LIVE" 배지와 함께 값이 초 단위로 바뀐다.

> **날짜가 안 바뀌면**: 일별 종가는 `run.py real`(또는 `fetch-market`)을 돌린 그 시점의 스냅샷이다. 새 거래일 데이터를 받으려면 그 명령을 다시 돌린다. 장중 실시간은 위 realtime 프로세스가 담당한다.

---

## 처음부터 깨끗하게 다시 (DB 통째로 리셋)
```powershell
docker compose down -v      # DB 볼륨까지 삭제
docker compose up -d db
python run.py demo          # 또는 real
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

## DB 내용 눈으로 확인
```powershell
docker compose exec db psql -U oos -d outofstock -c "\dt"
docker compose exec db psql -U oos -d outofstock -c "SELECT username,created_at FROM app_user;"
docker compose exec db psql -U oos -d outofstock -c "SELECT user_id,event,at FROM access_log ORDER BY log_id DESC LIMIT 20;"
docker compose exec db psql -U oos -d outofstock -c "SELECT user_id,ticker FROM watchlist;"
```
GUI가 편하면 DBeaver로 host `localhost` port `5432` db `outofstock` user/pw `oos/oos`.

## 테스트
```powershell
pytest -q          # db 컨테이너가 떠 있어야 함 → 전부 passed(2026-11-02 기준 889개 + 최소 권한 전용 2개는 건너뜀, 복원 훈련 시험은 DR_TEST_ADMIN_URL 이 있을 때만)
```

## 로그인·계정 API 요약(2026-10-22 — 자세한 규칙은 `frontend/FRONTEND_API_GUIDE.md` 2장)
웹 화면은 **HttpOnly 쿠키 + CSRF 헤더**(토큰을 JS 가 모른다), 스크립트·앱은 **Bearer**. 접근 토큰 10분 → 401 `token_expired` 면 `/auth/refresh`.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | /auth/register · /auth/login · /auth/logout | 가입(비밀번호 8~128자·영문+숫자) · 로그인(웹은 `X-Client: web` → 쿠키) · 로그아웃(서버 세션 즉시 폐기) |
| POST | /auth/refresh · /auth/step-up | 토큰 갱신(갱신 토큰 1회용 — 재사용되면 세션 폐기) · 비밀번호 재확인(탈퇴·100만 원 이상 송금 등, 5분 유효) |
| GET·DELETE·POST | /auth/sessions · /auth/sessions/{id} · /auth/logout-all | 로그인한 기기 목록·종료·전부 로그아웃 |
| GET·POST | /auth/mfa · /auth/mfa/setup·confirm·disable | 2단계 인증(TOTP — 관리 역할은 필수) |
| POST | /auth/change-password | 비밀번호 변경(이 세션 유지, 다른 기기 종료) |
| GET | /me · /me/logs/{kind} · /me/badges | 마이페이지(각 기록 최근 5건 + 전체 건수) · 더보기(wallet·practice·learning·access·survey·watchlist) · 배지 |
| DELETE | /me | 회원 탈퇴(추가 인증 필수, 계정 데이터 삭제) |
| GET/POST/DELETE | /watchlist · /watchlist/{ticker} | 관심종목 |
| GET | /events | 실시간 알림(SSE — 잔액·채팅·친구 요청·세션 종료) |

연결 정보 하나 더: 화면·API 는 같은 출처가 기본(CORS 꺼짐). 다른 출처의 개발 서버는 Vite 프록시로(`frontend/FRONTEND_API_GUIDE.md` 1.1).
