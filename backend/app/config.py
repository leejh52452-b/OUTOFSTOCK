"""환경 기반 설정 — 모든 모듈이 여기서 읽는다. 값은 환경변수/.env 로 주입."""
import os

# .env 파일이 있으면 자동 로드(python-dotenv 설치 시). 미설치면 조용히 통과.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _tz_name(v: str) -> str:
    """DB 시간대 이름 — IANA 형식(Asia/Seoul·UTC·Etc/GMT+9)만, 아니면 Asia/Seoul."""
    import re
    return v if re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-/]{0,40}", v or "") else "Asia/Seoul"


class Settings:
    # PostgreSQL / docker-compose 의 db 서비스 기본값과 일치.
    DATABASE_URL = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://oos:oos@localhost:5432/outofstock")

    DART_API_KEY = os.getenv("DART_API_KEY", "")
    DART_BSNS_YEAR = os.getenv("DART_BSNS_YEAR", "2025")
    DART_REPRT_CODE = os.getenv("DART_REPRT_CODE", "11011")

    # 실데이터(KRX) 유니버스 = 시총 상위 UNIVERSE_TOP_N 종목(+실습 종목). (레거시 FDR 배치·STOCK_UNIVERSE 는 삭제)
    UNIVERSE_TOP_N = int(os.getenv("UNIVERSE_TOP_N", "150"))     # KRX 실데이터 유니버스 크기(상위 150)

    # 종목당 뉴스 수집 건수
    NEWS_PER_STOCK = int(os.getenv("NEWS_PER_STOCK", "20"))   # 종목별 RSS 기사 수 — 뉴스 화면 보도량 집계에 쓰여 5→20
    # 뉴스 수집(하루 1번, run.py real) — 요청 간격(초) · 연속 실패 몇 번이면 그 회차 중단
    NEWS_REQUEST_GAP = float(os.getenv("NEWS_REQUEST_GAP", "0.5"))
    NEWS_MAX_FAILS = int(os.getenv("NEWS_MAX_FAILS", "5"))

    # KIS(한국투자증권) 실시간 시세
    KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
    KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
    KIS_PAPER = os.getenv("KIS_PAPER", "false").lower() in ("1", "true", "yes")

    # KRX 정보데이터시스템 OpenAPI (일별 시세/지수) — 헤더 AUTH_KEY
    KRX_API_KEY = os.getenv("KRX_API_KEY", "")
    # 실데이터 소스: auto(real이면 KRX) | krx | gen(오프라인 생성 데이터). 네이버/FDR 미사용.
    MARKET_SOURCE = os.getenv("MARKET_SOURCE", "auto")
    # 코스피·코스닥 지수 — 장중 홈 화면 요청 시 60초마다 백그라운드 갱신(KIS 실시간 → 야후). false 면 배치 때만.
    INDEX_LIVE_REFRESH = os.getenv("INDEX_LIVE_REFRESH", "true").lower() in ("1", "true", "yes")

    # 오늘의 추천 종목(멀티팩터 모델) — 적격성 필터 기준
    RECO_MIN_VALUE = float(os.getenv("RECO_MIN_VALUE", "500000000"))     # 20일 평균 거래대금 하한(원, 기본 5억)
    RECO_COST_BPS = float(os.getenv("RECO_COST_BPS", "25"))              # 백테스트 왕복 거래비용 가정(0.25%)
    RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.03"))   # Merton 부도거리용 무위험이자율(연, 국고채 수준)

    # ── 제로트러스트: 신원·세션(app/security) — 짧은 접근 토큰 + 회전하는 갱신 토큰, 매 요청 세션 재검증 ──
    ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "600"))       # 접근 토큰 10분
    SESSION_IDLE_SECONDS = int(os.getenv("SESSION_IDLE_SECONDS", "1800"))              # 30분 동안 쓰지 않으면 세션 만료
    SESSION_ABSOLUTE_SECONDS = int(os.getenv("SESSION_ABSOLUTE_SECONDS", "43200"))     # 로그인 후 12시간이면 무조건 만료
    REFRESH_GRACE_SECONDS = int(os.getenv("REFRESH_GRACE_SECONDS", "30"))              # 탭 두 개가 동시에 갱신하는 경우 허용 창
    STEP_UP_MAX_AGE_SECONDS = int(os.getenv("STEP_UP_MAX_AGE_SECONDS", "300"))         # 추가 인증 유효 5분
    MAX_SESSIONS_PER_USER = int(os.getenv("MAX_SESSIONS_PER_USER", "5"))               # 넘으면 가장 오래된 세션 종료
    ACCOUNT_LOCK_THRESHOLD = int(os.getenv("ACCOUNT_LOCK_THRESHOLD", "5"))             # 연속 5회 틀리면
    ACCOUNT_LOCK_SECONDS = int(os.getenv("ACCOUNT_LOCK_SECONDS", "900"))               # 15분 잠금(반복되면 두 배씩, 최대 24시간)
    COOKIE_SECURE = os.getenv("COOKIE_SECURE", "auto").lower()                          # auto: HTTPS 요청이면 Secure·__Host- 접두어
    # 쿠키 인증 변경 요청의 Origin 허용 목록(쉼표) — 비우면 요청 Host 와 같은 출처만
    TRUSTED_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("TRUSTED_ORIGINS", "").split(",") if o.strip()]
    JIT_MAX_MINUTES = int(os.getenv("JIT_MAX_MINUTES", "30"))                          # 관리 권한 활성 최대 시간
    # 관리 기기 인증 — true 면 /admin 동작은 nginx 가 확인한 관리 기기 인증서(mTLS)가 있어야 한다(X-Client-Cert-Verified).
    # 앱이 nginx 뒤(127.0.0.1·내부망)에서만 받을 때 켠다 — 직접 노출된 앱에선 헤더를 위조할 수 있다.
    ADMIN_REQUIRE_CLIENT_CERT = os.getenv("ADMIN_REQUIRE_CLIENT_CERT", "false").lower() in ("1", "true", "yes")
    # 나가는 연결 통제(app/security/egress.py) — enforce(목록 밖 차단, 기본) | monitor(기록만) | off
    EGRESS_MODE = os.getenv("EGRESS_MODE", "enforce").lower()
    EGRESS_EXTRA_HOSTS = os.getenv("EGRESS_EXTRA_HOSTS", "")                            # 추가 허용 호스트(쉼표)
    EGRESS_MAX_CONCURRENCY = int(os.getenv("EGRESS_MAX_CONCURRENCY", "8"))              # 외부 호스트별 동시 호출 상한(격벽)
    ROLE_GRANT_DAYS = int(os.getenv("ROLE_GRANT_DAYS", "90"))                          # 역할 자격 재인증 주기
    ROLE_DORMANT_DAYS = int(os.getenv("ROLE_DORMANT_DAYS", "60"))                      # 이 기간 한 번도 켜지 않은 자격은 자동 회수
    # 마스터 키(base64 32바이트) — 비밀번호 페퍼·IP 가명화 등 용도별 하위 키를 여기서 파생(app/security/keys.py) ★비밀
    #   생성: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
    APP_SECRET_KEYS_OLD = os.getenv("APP_SECRET_KEYS_OLD", "")
    # 비밀번호 해시 반복 횟수(PBKDF2-HMAC-SHA256) — OWASP 2023 권장 600,000. 예전 해시(100,000)는 로그인 때 자동 상향.
    PASSWORD_HASH_ITERATIONS = int(os.getenv("PASSWORD_HASH_ITERATIONS", "600000"))

    # ── 보안 설정 — 기본값은 개발·데모에 맞춤, 외부 공개 시 env.prod.example 의 권장값으로 ──
    # 교차 출처 허용 목록(쉼표) — 기본은 비어 있음 = 같은 출처(/app)만. 다른 출처 UI(Vite·GitHub Pages)는 여기에 적고
    # Bearer 토큰으로 호출한다(쿠키는 SameSite=Strict 라 교차 출처로 실리지 않는다). '*' 는 권장하지 않음.
    CORS_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    DOCS_ENABLED = os.getenv("DOCS_ENABLED", "true").lower() in ("1", "true", "yes")    # /docs·/redoc·/openapi.json
    SECURITY_HSTS = os.getenv("SECURITY_HSTS", "false").lower() in ("1", "true", "yes")  # HTTPS 로 서비스할 때만 true
    SECURITY_CSP = os.getenv("SECURITY_CSP", "")      # Content-Security-Policy — 비우면 기본 정책(api.DEFAULT_CSP), 'off' 면 안 붙임
    # 로그인 시도 제한 — 같은 IP·아이디로 LOGIN_WINDOW_SECONDS 동안 LOGIN_MAX_FAILS 번 틀리면 잠시 막음
    LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
    LOGIN_MAX_FAILS_PER_IP = int(os.getenv("LOGIN_MAX_FAILS_PER_IP", "100"))             # 아이디를 바꿔 가며 시도하는 경우(공유 IP 고려해 넉넉히)
    LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "600"))
    REGISTER_MAX_PER_IP = int(os.getenv("REGISTER_MAX_PER_IP", "20"))                    # IP 당 1시간 가입 시도 수
    PRACTICE_RECORD_MAX_PER_HOUR = int(os.getenv("PRACTICE_RECORD_MAX_PER_HOUR", "120"))  # 계정당 1시간 웹 실습 기록 수
    # 시도 제한 저장소 — memory(프로세스 1개·개발) | postgres(워커 여럿·서버 여럿이 같은 한도를 공유, 운영 권장)
    RATE_LIMIT_BACKEND = os.getenv("RATE_LIMIT_BACKEND", "memory").lower()
    # ── 정보보호(3번) ──
    # 데이터 암호화 키(AES-256-GCM, base64 32바이트) — KIS 접근토큰·결제 키 등 민감 값을 DB 에 암호화 저장.
    #   생성: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"  ★비밀키
    #   비우면 암호화 없이 저장(보안 점검 도구가 경고). 키 교체 시 예전 키는 DATA_ENCRYPTION_KEYS_OLD(쉼표)로 — 읽기만
    DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY", "")
    DATA_ENCRYPTION_KEYS_OLD = os.getenv("DATA_ENCRYPTION_KEYS_OLD", "")
    # 관리 역할 자격 부트스트랩(쉼표) — 상시 권한이 아니라 '자격'만. 쓰려면 TOTP 등록 후 JIT 활성(app/security/rbac.py)
    ADMIN_USERS = {u.strip().lower() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "1095"))                # 감사 로그 보관(일) — 최소 3년(아래 audit.purge)
    ACCESS_LOG_RETENTION_DAYS = int(os.getenv("ACCESS_LOG_RETENTION_DAYS", "365"))       # 접속 로그 보관(일) — 최소 보관
    SECURITY_EVENT_RETENTION_DAYS = int(os.getenv("SECURITY_EVENT_RETENTION_DAYS", "365"))   # 보안 이벤트 보관(일)
    PII_LOG_RETENTION_DAYS = int(os.getenv("PII_LOG_RETENTION_DAYS", "730"))             # 개인정보 열람 기록(2년)
    # 마이그레이션 전용 DB 계정(선택) — DB 권한 분리 시 스키마 변경은 소유자 계정으로만(run.py initdb 가 사용)
    MIGRATION_DATABASE_URL = os.getenv("MIGRATION_DATABASE_URL", "")
    # 무거운 계산(추천 백테스트·모형 비교·모니터링) — 캐시에 없어 새로 계산해야 하는 요청만 IP 당 1분 이 횟수까지
    HEAVY_MISS_PER_MIN = int(os.getenv("HEAVY_MISS_PER_MIN", "12"))
    # 요청 본문 상한(바이트) — VM 은 nginx 없이 uvicorn 이 바로 받으므로 앱에서 막는다(가장 큰 정상 요청: 영수증 수십 KB)
    MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "1000000"))

    # ── 가상머니 지갑(추가 기능) — 출석·인앱결제·송금·장기투자 공용 ──
    WALLET_WELCOME_BONUS = int(os.getenv("WALLET_WELCOME_BONUS", "1000000"))       # 최초 지갑 생성 1회 환영금
    ATTENDANCE_DAILY_REWARD = int(os.getenv("ATTENDANCE_DAILY_REWARD", "1000000"))  # 매일 출석 보상(100만원)
    # Daily Stock Quiz 정답 1문제당 보상(팀 시안 10만 원) — 서버가 채점한 점수 × 이 값, 한국 날짜 하루 1번. 0 이면 보상 없음
    QUIZ_REWARD_PER_CORRECT = int(os.getenv("QUIZ_REWARD_PER_CORRECT", "100000"))
    TRANSFER_MAX = int(os.getenv("TRANSFER_MAX", "100000000"))                      # 1회 송금 한도(오남용 방지)
    # 인앱결제 검증 모드: stub(개발/데모: 서명 형식만 확인) | strict(스토어 서버 검증 연동 지점)
    IAP_VERIFY_MODE = os.getenv("IAP_VERIFY_MODE", "stub")

    # 토스페이먼츠(웹 결제) — 테스트 키를 .env 에 넣으면 실제 결제창/서버 승인 검증이 동작.
    TOSS_CLIENT_KEY = os.getenv("TOSS_CLIENT_KEY", "")     # 프론트 결제창용(공개 클라이언트 키)
    TOSS_SECRET_KEY = os.getenv("TOSS_SECRET_KEY", "")     # 서버 승인 검증용(시크릿 키)
    TOSS_MODE = os.getenv("TOSS_MODE", "stub")             # stub(모의 승인) | real(토스 승인 API 호출)

    # ── 해외 주식(미국) — 국내(KRX/DART/KIS)와 분리된 별도 수집 경로 ──
    #   일봉 : FinanceDataReader(키 불필요) · 실시간 : Finnhub · 재무/주식수 : SEC EDGAR · 환율 : 수출입은행/FDR
    #   키는 반드시 .env(환경변수)로만 주입 — 소스/번들에 하드코딩 금지.
    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")     # finnhub.io 무료 키 — 해외 실시간(realtime-us) 전용.
    # SEC 는 인증키가 아니라 '식별용 연락처'를 User-Agent 로 요구 → 본인 이메일로 교체 권장.
    SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "OUTOFSTOCK-capstone contact@example.com")
    ECOS_API_KEY = os.getenv("ECOS_API_KEY", "")           # 한국은행 ECOS(금리·환율 공식 시계열) — 선택, ecos.bok.or.kr 무료 발급
    EXIM_API_KEY = os.getenv("EXIM_API_KEY", "")           # 한국수출입은행 환율 키(선택). 없으면 폴백 환율.
    # 해외 일봉 소스: auto(real 실행 시 FDR 실데이터, 실패 종목만 표본) | gen(항상 오프라인 표본)
    OVERSEAS_SOURCE = os.getenv("OVERSEAS_SOURCE", "auto")

    # ── 배치 스케줄(scheduler.py) — 매일 이 시각(한국시간)에 'python run.py real' 전체 파이프라인 ──
    REAL_PIPELINE_TIME = os.getenv("REAL_PIPELINE_TIME", "23:00")        # HH:MM (KST)
    REAL_PIPELINE_DAYS = os.getenv("REAL_PIPELINE_DAYS", "mon-sun")      # cron 요일(예: mon-fri)
    # 스케줄러가 꺼져 있어 정기 실행을 놓쳤으면(마지막 성공이 26시간 넘게 전) 켜질 때 1회 보충 실행
    REAL_PIPELINE_CATCHUP = os.getenv("REAL_PIPELINE_CATCHUP", "true").lower() in ("1", "true", "yes")

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    # DB 세션 한도(복원력) — API 프로세스에만 켠다(배치는 오래 걸리는 적재가 있어 0=끔).
    #   statement_timeout: 한 쿼리가 이 시간을 넘으면 DB 가 끊는다(API 는 504) — 느린 쿼리가 커넥션 풀을 다 붙잡지 않게
    #   idle_in_transaction: 트랜잭션을 열어 둔 채 멈춘 연결을 끊는다 — 잠금을 쥔 채 죽은 요청이 다른 요청을 막지 않게
    DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "0"))
    # DB 연결의 시간대(2026-11-03 · ADR-0027) — 서버 설정과 무관하게 고정. 시간대 없는 시각 = 한국시간(clock.py)과 같은 기준
    #   (값은 SQL 에 그대로 들어가므로 IANA 이름 형식만 받는다 — 아니면 Asia/Seoul)
    DB_TIMEZONE = _tz_name(os.getenv("DB_TIMEZONE", "Asia/Seoul"))
    DB_IDLE_TX_TIMEOUT_MS = int(os.getenv("DB_IDLE_TX_TIMEOUT_MS", "60000"))
    # ── 데이터 수명주기·DB 진단(2026-11-03 · ADR-0027 · app/services/ilm.py·dbhealth.py) ──
    #   보관 일수가 지난 '달 전체'를 압축 파일(var/archive)로 보관·검증한 뒤 DB 에서 정리(매월 2일 04:10). 0 = 정리 안 함
    NEWS_RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "400"))                # 뉴스·종목 태깅·감성(약 13개월)
    SEARCH_LOG_RETENTION_DAYS = int(os.getenv("SEARCH_LOG_RETENTION_DAYS", "400"))    # 검색 기록(키워드·고른 종목)
    ILM_ARCHIVE_DIR = os.getenv("ILM_ARCHIVE_DIR", "var/archive")                    # 상대 경로 = 코드 폴더 기준
    ILM_ENABLED = os.getenv("ILM_ENABLED", "1") == "1"
    #   DB 크기 예측 경보 — 디스크가 이 일수 안에 찰 것 같으면 warn(절반 이하면 error). 디스크 = DB 와 같은 서버로 본다
    DB_DISK_ALERT_DAYS = int(os.getenv("DB_DISK_ALERT_DAYS", "90"))
    DB_DISK_PATH = os.getenv("DB_DISK_PATH", "/")
    #   백업 덤프 압축 — auto(pg_dump 16+·zstd 지원이면 zstd:3, 아니면 gzip 6) · 고정하려면 6·zstd:3·lz4 …(tools/dr.py)
    BACKUP_COMPRESSION = os.getenv("BACKUP_COMPRESSION", "auto")
    # ── 양봉이 주간 추천(2026-11-09 · app/services/yangbong.py — 팀 알고리즘 yangbong_picks.py) ──
    #   그 주 후보(코스피 보통주 시총 상위 200 중 상장 200거래일 이상·거래 중)가 이보다 적으면 고르지 않는다
    #   (KRX 전 종목이 아직 다 적재되지 않았거나 표본 DB — 몇 종목만으로 고른 '추천'을 내보내지 않게)
    YANGBONG_MIN_UNIVERSE = int(os.getenv("YANGBONG_MIN_UNIVERSE", "150"))
    YANGBONG_CATCHUP_WEEKS = int(os.getenv("YANGBONG_CATCHUP_WEEKS", "26"))      # 놓친 주를 이만큼까지 거슬러 채운다
    # ── 모의투자(가상 시장 시즌 · 2026-11-09 · app/services/vmarket.py) ──
    VMARKET_MIN_ENTRY = int(os.getenv("VMARKET_MIN_ENTRY", "1000000"))           # 참가금 최소(원) — 팀 화면 규칙
    VMARKET_MAX_ENTRY = int(os.getenv("VMARKET_MAX_ENTRY", "1000000000000"))     # 참가금 상한(오입력·오버플로 방지)
    VMARKET_ORDERS_PER_MIN = int(os.getenv("VMARKET_ORDERS_PER_MIN", "30"))       # 계정당 1분 주문 수(자동 연타·스크립트 남용 방지)
    # 연결 풀이 다 찼을 때 기다리는 시간(초) — 예전엔 기본 30초를 기다린 뒤 500. 짧게 기다리고 503(Retry-After)으로 빨리 실패
    DB_POOL_TIMEOUT_S = float(os.getenv("DB_POOL_TIMEOUT_S", "5"))
    # ── 용량·성능 관리(app/services/capacity.py — 금감원 금융IT 안정성 가이드라인 '성능 관리') ──
    #   임계치 4단계(정상·주의·경계·심각) = 동시 요청/워커 한도 · DB 연결 사용률 비율(주의,경계,심각) + p95 지연(ms) · 5xx 비율
    #   대량접속제어: 경계면 low(공개 시세·뉴스), 심각이면 normal 까지 503 — critical(로그인·송금·결제·관리)은 끝까지 받는다
    CAPACITY_ADMISSION = os.getenv("CAPACITY_ADMISSION", "on").lower() not in ("0", "off", "false", "no")
    CAPACITY_MAX_INFLIGHT = int(os.getenv("CAPACITY_MAX_INFLIGHT", "40"))      # 워커 하나의 스레드풀(40)에 맞춤
    CAPACITY_RATIOS = os.getenv("CAPACITY_RATIOS", "0.6,0.8,0.95")
    CAPACITY_P95_MS = os.getenv("CAPACITY_P95_MS", "500,1000,3000")
    CAPACITY_REPORT_COOLDOWN_S = int(os.getenv("CAPACITY_REPORT_COOLDOWN_S", "600"))
    # 로그 형식 — text(사람이 읽기, 개발) | json(한 줄 JSON: 수집기·SIEM 연동, 운영). json 은 요청 ID 를 모든 줄에 붙인다
    LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()
    # 감사 로그 외부 증인(witness) 파일 — 체인 머리 해시를 DB 밖에 매일 남긴다(append-only 파일 권장: chattr +a)
    AUDIT_WITNESS_FILE = os.getenv("AUDIT_WITNESS_FILE", "")

    # ── 백업·복구 훈련(tools/dr.py) ──
    # 백업 전용 DB 계정(권장) — 읽기 전용(pg_read_all_data) + BYPASSRLS(deploy/sql/roles.sql 의 oos_backup).
    #   개인 데이터 표는 FORCE RLS 라 일반 계정의 pg_dump 는 실패한다. 비우면 소유자/앱 계정 + 시스템 문맥(app.rls=off).
    BACKUP_DATABASE_URL = os.getenv("BACKUP_DATABASE_URL", "")
    BACKUP_DIR = os.getenv("BACKUP_DIR", "var/backups")
    # age 공개키(수신자) 파일 — 한 줄에 하나(age1…). 서버에는 공개키만 두고, 복호화 키(identity)는 오프라인에 보관한다
    BACKUP_AGE_RECIPIENTS = os.getenv("BACKUP_AGE_RECIPIENTS", "")
    BACKUP_KEEP_DAILY = int(os.getenv("BACKUP_KEEP_DAILY", "14"))           # 최근 N개
    BACKUP_KEEP_MONTHLY = int(os.getenv("BACKUP_KEEP_MONTHLY", "12"))       # + 최근 N개월 각 달의 첫 백업
    # 소산(원격 사본) 명령(선택) — {file}·{manifest} 자리표시자, 셸 없이 실행. 예: rclone copy {file} remote:oos-backups/
    BACKUP_OFFSITE_CMD = os.getenv("BACKUP_OFFSITE_CMD", "")
    # 덤프에 넣을 확장(쉼표, 패턴) — 기본 plpgsql 만: pg_stat_statements 같은 운영 확장은 빼야 슈퍼유저 없이도 복원된다
    BACKUP_EXTENSIONS = os.getenv("BACKUP_EXTENSIONS", "plpgsql")
    # 신선도 경보 — 마지막 성공 백업이 이보다 오래되면(시간) 관리자 헬스 경고(일 1회 + 여유 2시간)
    BACKUP_MAX_AGE_HOURS = float(os.getenv("BACKUP_MAX_AGE_HOURS", "26"))
    # 복구 훈련 주기(일) — 마지막 성공 훈련이 이보다 오래되면 경고(분기 1회)
    DR_DRILL_MAX_AGE_DAYS = int(os.getenv("DR_DRILL_MAX_AGE_DAYS", "92"))
    # 복구목표시간(분) — 전자금융감독규정 제23조 핵심업무 3시간. 훈련 실측 RTO 가 이를 넘으면 진단 점검 실패
    #   (한국은행 PFMI 핵심원칙 17: 중요 지급결제 2시간 — 이 서비스는 대상이 아니라 참고 목표로만 문서에 적는다)
    DR_RTO_TARGET_MIN = int(os.getenv("DR_RTO_TARGET_MIN", "180"))

    # ── IT 내부통제(2026-11-02) — 변경관리·가용성 감시·장애 판정(app/services/changes.py · incidents.py) ──
    # 변경관리 — enforce(승인된 설치본만·배포 금지 시간 준수, 금융 운영) | record(막지 않고 기록·경고, 기본) | off
    #   어느 모드든 배포를 깨뜨리는 자동 점검 실패(구문 오류·적용된 마이그레이션 변경·정책 형식 오류)는 막는다
    CHANGE_GATE = os.getenv("CHANGE_GATE", "record").lower()
    # 배포 금지 시간(KST) — 'mon-fri HH:MM-HH:MM' | 'daily HH:MM-HH:MM' | none. 기본: 평일 장 시작 전~마감 직후
    CHANGE_FREEZE = os.getenv("CHANGE_FREEZE", "mon-fri 08:30-16:00")
    # 가용성 감시(합성 모니터링) — 스케줄러가 1분마다 부르는 헬스 주소. auto = http://127.0.0.1:$PORT/health, off = 끔
    #   한 번도 정상 응답을 받은 적이 없으면 장애로 판정하지 않고 '주소 확인' 경고만(잘못된 설정이 가짜 장애를 만들지 않게)
    UPTIME_URL = os.getenv("UPTIME_URL", "auto")
    UPTIME_SLOW_MS = int(os.getenv("UPTIME_SLOW_MS", "3000"))      # 이보다 느리면 '지연'(장애 판정에 포함)
    UPTIME_SLO = float(os.getenv("UPTIME_SLO", "99.5"))            # 30일 가용률 목표(%)
    # 오류 경보를 이 시간(분) 안에 아무도 확인(ack)하지 않으면 상향 — 24시간 관제의 '놓친 경보' 방지
    ALERT_ACK_MINUTES = int(os.getenv("ALERT_ACK_MINUTES", "15"))

    # ── 운영 알림(장애 대응) — 설정한 채널로만 보낸다(여러 개 동시 가능). 하나도 없으면 로그·alert_log·/health 에만 남김.
    #   ★ 셋 다 비밀값: 웹훅 URL 은 URL 자체가 열쇠, 텔레그램 토큰·SMTP 비밀번호도 .env 로만(소스·깃 금지)
    ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")          # Slack/Discord/일반 웹훅(JSON POST)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))                  # STARTTLS
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
    ALERT_DEDUP_HOURS = float(os.getenv("ALERT_DEDUP_HOURS", "6"))  # 같은 알림은 이 시간 안에 다시 보내지 않음(폭주 방지)

    # ── 적재 후 품질 게이트 — 실데이터 적재 결과가 기대치를 벗어나면 '실패'로 기록하고 알린다(10-10 사건 재발 방지) ──
    QUALITY_MIN_UNIVERSE_RATIO = float(os.getenv("QUALITY_MIN_UNIVERSE_RATIO", "0.9"))   # 유니버스 ≥ UNIVERSE_TOP_N×0.9
    QUALITY_MIN_HISTORY_BARS = int(os.getenv("QUALITY_MIN_HISTORY_BARS", "60"))         # 최근 100일 안 일봉 수
    QUALITY_MIN_HISTORY_SHARE = float(os.getenv("QUALITY_MIN_HISTORY_SHARE", "0.9"))    # 위 기준을 넘는 종목 비율

    # ── 분석 계층(데이터 플랫폼) — 전 종목 × N년 일별 시세(analytics 스키마) ──
    ANALYTICS_YEARS = int(os.getenv("ANALYTICS_YEARS", "10"))                     # 백필 기간(년) — 10년 ≈ 670만 행·약 1GB
    ANALYTICS_BACKFILL_BUDGET = int(os.getenv("ANALYTICS_BACKFILL_BUDGET", "1200"))  # 배치 1회 KRX 호출 예산(하루 한도 10,000)
    ANALYTICS_QUERY_TIMEOUT_MS = int(os.getenv("ANALYTICS_QUERY_TIMEOUT_MS", "5000"))  # 분석 쿼리(run.py analytics-query) 시간 제한
    # 읽기 전용 DB 계정(선택) — 분석 쿼리(CLI)는 이 계정으로 접속(쓰기 권한 없음). 비우면 기본 계정 + READ ONLY 트랜잭션
    ANALYTICS_DATABASE_URL = os.getenv("ANALYTICS_DATABASE_URL", "")


settings = Settings()

# 테스트·초기 시드용 대표 종목(conftest·db.seed_stocks)
CURATED = {
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER",
    "035720": "카카오",   "005380": "현대차",     "051910": "LG화학",
}
