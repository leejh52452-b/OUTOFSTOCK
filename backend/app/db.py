"""DB 계층 — SQLAlchemy 엔진 + 스키마(창고) + 쿼리/트랜잭션 헬퍼.
PostgreSQL (스키마는 이식성 있게 정의).

타입 표준(2026-11-03 · ADR-0027 · docs/db/standards.md) — 저장은 맞는 타입으로, 서비스 계층의 값 형식은 예전 그대로:
  날짜            date                     → 읽으면 'YYYY-MM-DD' 문자열(아래 연결 로더)
  업무 시각(KST)  timestamp(0)             → 'YYYY-MM-DDTHH:MM:SS' 문자열(clock.iso_now 형식, 시간대 없음 = 한국시간 벽시계)
  외부·시세 시각  timestamptz(0)           → 한국시간 datetime(연결의 TimeZone = Asia/Seoul — isoformat 이 예전 '+09:00' 문자열과 같다)
  보안·운영 시각  timestamptz              → 그대로(이미 timestamptz)
  원화 가격       integer                  → 정수 원(4바이트 — 예전 double 8바이트, 소수 원 가격은 없다)
예외(문자열 유지): audit_log.at(해시 체인 정규형) · dart_corp.modify_date(DART 'YYYYMMDD') · iap_purchase.price(표시가) ·
data_quality_issue.trade_date(격리소 — 깨진 원본 값을 그대로 적는다). 기존 DB 는 init_db 의 _retype() 가 한 번 바꾼다.
"""
from contextlib import contextmanager
from sqlalchemy import event
from sqlalchemy.pool import Pool
from sqlalchemy import (create_engine, MetaData, Table, Column, text, Index, UniqueConstraint, CheckConstraint,
                        Integer, BigInteger, Boolean, Float, String, Text, SmallInteger, DateTime, Date)
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from app.config import settings

metadata = MetaData()

KST_TS = TIMESTAMP(timezone=False, precision=0)   # 업무 시각 — 한국시간 벽시계(대한민국은 서머타임이 없어 모호하지 않다)
TSTZ = TIMESTAMP(timezone=True, precision=0)      # 시각(시간대 포함) — 초 단위(예전 문자열과 같은 정밀도)
WON = Integer                                     # 원화 가격(정수 원) — 최대 21억 원

stock = Table("stock", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("name", String(100)),
    Column("market", String(20)),
    Column("sector", String(100)),
)
daily_price = Table("daily_price", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("close", WON),
    Column("volume", BigInteger),
    Column("change_rate", Float),
    Column("market_cap", BigInteger),
)
financial_statement = Table("financial_statement", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("fiscal_year", Integer, primary_key=True),      # 어느 기간(valid time)
    Column("report_code", String(10), primary_key=True),
    Column("revenue", BigInteger),
    Column("operating_profit", BigInteger),
    Column("net_income", BigInteger),
    Column("total_equity", BigInteger),                    # 자본총계 — PBR(시총/자본) 계산용
    Column("debt_ratio", Float),
    Column("disclosed_at", Date),   # 공시 접수일(YYYY-MM-DD) — 우리가 '알게 된' 시점(knowledge time)
    Column("ingested_at", KST_TS),    # 우리 시스템 적재 시각(system time)
    # ↓ 부실예측(Altman Z'') 용 대차대조표 항목 — 기존 DB 는 init_db 의 ADD COLUMN 으로 추가
    Column("total_assets", BigInteger),        # 자산총계
    Column("current_assets", BigInteger),      # 유동자산
    Column("current_liabilities", BigInteger), # 유동부채
    Column("total_liabilities", BigInteger),   # 부채총계
    Column("retained_earnings", BigInteger),   # 이익잉여금(결손금)
    Column("fs_basis", String(3)),             # 작성 기준 CFS(연결)·OFS(별도) — 데모(표본) 행은 NULL
)
ranking = Table("ranking", metadata,
    Column("ranking_type", String(20), primary_key=True),
    Column("rank", Integer, primary_key=True),
    Column("ticker", String(20)),
    Column("metric_value", Float),
    Column("as_of", Date),
)
recommendation = Table("recommendation", metadata,
    Column("base_ticker", String(20), primary_key=True),
    Column("rank", Integer, primary_key=True),
    Column("similar_ticker", String(20)),
    Column("distance", Float),
)
news = Table("news", metadata,
    Column("news_id", Integer, primary_key=True, autoincrement=True),
    Column("title", String(500)),
    Column("url", String(1000)),   # 중복 방지 = md5(url) 유일 인덱스(ux_news_url_md5 — 아래 _ENSURE_INDEXES)
    Column("source", String(200)),
    Column("published_at", Date),         # YYYY-MM-DD(한국시간 날짜)
    Column("published_ts", TSTZ),         # 발행 시각 ISO(+09:00) — 'N분 전'·오늘 기준 집계
)
news_stock = Table("news_stock", metadata,
    Column("news_id", Integer, primary_key=True),
    Column("ticker", String(20), primary_key=True),
)
search_log = Table("search_log", metadata,
    Column("log_id", Integer, primary_key=True, autoincrement=True),
    Column("keyword", String(200)),
    Column("searched_at", TSTZ),
    Column("ticker", String(20)),               # 사용자가 실제로 고른 종목(인기 종목 집계) — 자동완성 조각은 기록 안 함
)

app_user = Table("app_user", metadata,
    Column("username", String(50), primary_key=True),
    Column("password_hash", String(200)),
    Column("salt", String(64)),
    Column("created_at", KST_TS),
    # ── 제로트러스트(신원) — 연속 실패 잠금·상태·비밀번호 변경 시각 ──
    Column("status", String(10), nullable=False, server_default="active"),   # active | locked | disabled
    Column("failed_count", Integer, nullable=False, server_default="0"),     # 연속 로그인 실패 수(성공 시 0)
    Column("locked_until", DateTime(timezone=True)),                         # 이 시각까지 로그인 불가(자동 잠금)
    Column("password_changed_at", DateTime(timezone=True)),
    Column("last_login_at", DateTime(timezone=True)),
)
# 접근 토큰(10분) — 원문은 저장하지 않고 SHA-256 해시만. 세션(auth_session)에 묶여, 세션을 폐기하면 함께 무효.
auth_token = Table("auth_token", metadata,
    Column("token", String(64), primary_key=True),
    Column("username", String(50)),
    Column("created_at", KST_TS),
    Column("session_id", String(32)),
    Column("expires_at", DateTime(timezone=True)),
)
Index("ix_auth_token_session", auth_token.c.session_id)
# 세션 — 로그인 1회 = 세션 1개. 기기 지문·IP 대역에 결속, 유휴·절대 만료, 갱신 토큰 회전(재사용 탐지), CSRF 토큰.
auth_session = Table("auth_session", metadata,
    Column("session_id", String(32), primary_key=True),
    Column("username", String(50), nullable=False),
    Column("client", String(8), nullable=False),                  # web(쿠키) | api(Bearer)
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("auth_time", DateTime(timezone=True), nullable=False),  # 비밀번호(+OTP)를 확인한 시각
    Column("aal", SmallInteger, nullable=False),                   # 1=비밀번호, 2=비밀번호+OTP
    Column("step_up_at", DateTime(timezone=True)),                 # 마지막 추가 인증
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),  # 절대 만료
    Column("device_hash", String(32), nullable=False),
    Column("device", String(60)),                                  # 'Chrome · Windows'(표시용)
    Column("ip_net", String(64)),                                  # 로그인 때 IP 대역
    Column("last_ip_net", String(64)),
    Column("risk", String(100)),                                   # 위험 신호(쉼표) — ip_change …
    Column("refresh_hash", String(64)),
    Column("refresh_prev_hash", String(64)),
    Column("rotated_at", DateTime(timezone=True)),
    Column("refresh_count", Integer, nullable=False, server_default="0"),
    Column("csrf_hash", String(64)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoke_reason", String(40)),
    # 웹 '로그인 유지' — 거짓이면 쿠키에 수명을 적지 않아(세션 쿠키) 브라우저를 닫으면 지워진다(공용 PC). 서버 만료 규칙은 같다
    Column("persistent", Boolean, nullable=False, server_default=text("true")),
    # 기기 결속 키(DPoP) — 로그인한 브라우저가 만든 비추출 개인키의 공개키 지문(RFC 7638). 있으면 갱신·돈 동작에 그 키의 서명이 필요
    Column("dpop_jkt", String(43)),
)
Index("ix_auth_session_user", auth_session.c.username, auth_session.c.revoked_at)
Index("ux_auth_session_refresh", auth_session.c.refresh_hash, unique=True)
# 이미 쓴 갱신 토큰(해시) — 다시 들어오면 '탈취된 토큰 재사용'으로 보고 세션 전체를 폐기(OAuth 2.0 보안 BCP)
auth_refresh_used = Table("auth_refresh_used", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("session_id", String(32), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=False),
)
# 2단계 인증(TOTP) — 비밀키는 AES-256-GCM 으로 암호화 저장(AAD=user_mfa:<아이디>), 복구 코드는 해시만
user_mfa = Table("user_mfa", metadata,
    Column("username", String(50), primary_key=True),
    Column("secret_enc", Text, nullable=False),
    Column("enabled_at", DateTime(timezone=True)),                 # NULL = 등록 중(아직 확인 전)
    Column("last_step", BigInteger, nullable=False, server_default="0"),   # 마지막으로 받은 시간 단계(재사용 차단)
    Column("recovery_hashes", Text),                               # JSON 배열(sha256)
    Column("created_at", DateTime(timezone=True), nullable=False),
)
# 역할 자격(상시 권한이 아니다 — 쓰려면 JIT 활성 필요). 부트스트랩 외에는 2인 승인으로만 부여, 만료(재인증 주기) 있음.
user_role = Table("user_role", metadata,
    Column("username", String(50), primary_key=True),
    Column("role", String(20), primary_key=True),
    Column("granted_by", String(50), nullable=False),
    Column("approved_by", String(50)),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),                 # NULL = 부트스트랩(설정 파일 관리)
    Column("reason", String(200)),
)
# JIT 활성 — 세션에 묶인 시간 제한 권한(사유 필수). 세션이 끝나거나 시간이 지나면 자동으로 사라진다.
role_activation = Table("role_activation", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("username", String(50), nullable=False),
    Column("role", String(20), nullable=False),
    Column("session_id", String(32), nullable=False),
    Column("reason", String(200), nullable=False),
    Column("activated_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
Index("ix_role_activation_session", role_activation.c.session_id, role_activation.c.expires_at)
# 역할 부여 요청 — 요청자와 승인자가 달라야 한다(이중 통제, 4-eyes)
role_request = Table("role_request", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("username", String(50), nullable=False),               # 받을 사람
    Column("role", String(20), nullable=False),
    Column("reason", String(200), nullable=False),
    Column("requested_by", String(50), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("status", String(10), nullable=False),                  # pending | approved | rejected | expired
    Column("decided_by", String(50)),
    Column("decided_at", DateTime(timezone=True)),
)
# 보안 이벤트 — 탐지 규칙의 입력이자 대응 기록(자동 대응 결과 포함). 사용자는 가명(HMAC)과 원문을 분리 보관.
security_event = Table("security_event", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("kind", String(40), nullable=False),                    # login_fail · refresh_reuse · csrf_fail …
    Column("severity", String(8), nullable=False),                 # low | medium | high | critical
    Column("username", String(50)),
    Column("session_id", String(32)),
    Column("ip_hash", String(16)),
    Column("request_id", String(64)),
    Column("detail", Text),                                        # JSON(민감값 제외)
    Column("response", String(60)),                                # 자동 대응 결과(lock_account 등)
)
Index("ix_security_event_at", security_event.c.at)
Index("ix_security_event_kind", security_event.c.kind, security_event.c.at)
Index("ix_security_event_user", security_event.c.username, security_event.c.at)
# 시도 제한 기록(공유 저장소) — 워커·서버가 여럿이어도 같은 한도를 센다(RATE_LIMIT_BACKEND=postgres).
# UNLOGGED: WAL 을 쓰지 않아 빠르고, DB 가 비정상 종료되면 비워진다(시도 제한 기록은 잃어도 되는 데이터).
rate_event = Table("rate_event", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("key", String(32), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    prefixes=["UNLOGGED"],
)
Index("ix_rate_event_key_at", rate_event.c.key, rate_event.c.at)
# 개인정보 처리(열람) 기록 — 관리자가 사용자 개인정보를 볼 때 '누가·언제·누구의·무엇을·왜'(안전성 확보조치 기준 제8조)
pii_access_log = Table("pii_access_log", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("operator", String(50), nullable=False),
    Column("subject", String(50), nullable=False),
    Column("fields", String(200), nullable=False),
    Column("purpose", String(200), nullable=False),
    Column("ip_hash", String(16)),
    Column("request_id", String(64)),
)
Index("ix_pii_access_at", pii_access_log.c.at)


realtime_price = Table("realtime_price", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("price", WON),
    Column("change_rate", Float),
    Column("volume", BigInteger),                    # 당일 누적 거래량
    Column("ts", TSTZ),                        # 수신 시각(ISO, +09:00) — 신선도(LIVE) 판정
    # ↓ 장외 '직전 데이터' 정확도용(기존 DB는 init_db 의 ADD COLUMN IF NOT EXISTS 로 추가)
    Column("change", WON),                         # 전일 대비(원)
    Column("open", WON),                           # 당일 시가
    Column("high", WON),                           # 당일 고가
    Column("low", WON),                            # 당일 저가
    Column("value", BigInteger),                     # 당일 누적 거래대금(원)
    Column("session_date", Date),              # 이 체결이 속한 영업일(YYYY-MM-DD)
    Column("source", String(10)),                    # kis | sim
)
# 데이터 품질 격리소 — 검증에서 걸린 불량 행을 기록(무엇이 왜 걸렸는지 추적)
data_quality_issue = Table("data_quality_issue", metadata,
    Column("issue_id", Integer, primary_key=True, autoincrement=True),
    Column("table_name", String(40)),
    Column("ticker", String(20)),
    Column("trade_date", String(10)),
    Column("rule", String(40)),
    Column("detail", String(200)),
    Column("detected_at", KST_TS),
)
# 파이프라인 실행 이력(lineage) — 배치가 언제 돌았고, 소스별 몇 건, 품질·상태는 어땠는지
pipeline_run = Table("pipeline_run", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", KST_TS),
    Column("finished_at", KST_TS),
    Column("status", String(20)),               # RUNNING / SUCCESS / FAILED
    Column("price_rows", Integer),
    Column("dart_rows", Integer),
    Column("news_rows", Integer),
    Column("quality_ok", Integer),              # 1/0/NULL
    Column("quality_issues", Integer),
    Column("note", String(200)),
)

# 뉴스 감성 — 제목을 호재/악재로 분류(뉴스 1건당 1행). 종목 매핑은 news_stock 조인.
news_sentiment = Table("news_sentiment", metadata,
    Column("news_id", Integer, primary_key=True),
    Column("label", String(4)),                 # POS(호재)/NEG(악재)/NEU(중립)
    Column("score", Float),
    Column("model", String(40)),                # lexicon-v1 / kr-finbert-sc
    Column("classified_at", KST_TS),
)

# ── OUTOFSTOCK 리디자인: 일봉 OHLC · 실습(과거 사례) · 지수 ───────────
# 일봉 OHLC(캔들차트용) — 종목상세·실습 공용. daily_price(종가 요약)와 별개로 시/고/저/종 보관.
ohlc = Table("ohlc", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("open", WON),
    Column("high", WON),
    Column("low", WON),
    Column("close", WON),
    Column("volume", BigInteger),
)
# 실습 세션(과거 사례) — 유저별 시나리오 진행. 하루씩 커서 전진(미래 은닉) + 계좌·OCC.
# 시나리오 정의 자체는 코드 상수(app/services/practice.py SCENARIOS)로 관리.
practice_session = Table("practice_session", metadata,
    Column("session_id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50)),
    Column("scenario_id", String(40)),
    Column("cursor_date", Date),          # 현재(공개된) 마지막 거래일
    Column("cash", BigInteger),
    Column("holding_qty", BigInteger),
    Column("holding_avg", Float),
    Column("start_cash", BigInteger),
    Column("version", Integer),                 # OCC(낙관적 잠금)
    Column("status", String(10)),               # active / done
    Column("started_at", KST_TS),
    Column("ended_at", KST_TS),
    Column("final_return", Float),
    Column("best_return", Float),               # 재도전 시 최고 수익률 유지(요약용)
    Column("attempts", Integer),
)
practice_trade = Table("practice_trade", metadata,
    Column("trade_id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", Integer),
    Column("trade_date", Date),
    Column("side", String(4)),                  # BUY / SELL
    Column("quantity", BigInteger),
    Column("price", WON),
    Column("idem_key", String(80)),
    Column("created_at", KST_TS),
    UniqueConstraint("session_id", "idem_key", name="uq_practice_idem"),   # 멱등성
)
# 시장 지수 — 홈 상단(코스피·코스닥·환율·국고채). 배치/수집기가 채우고, 데모는 시드.
market_index = Table("market_index", metadata,
    Column("code", String(20), primary_key=True),   # KOSPI/KOSDAQ/USDKRW/BOND3Y
    Column("name", String(40)),
    Column("value", Float),
    Column("change", Float),
    Column("change_rate", Float),
    Column("ts", TSTZ),                       # 값을 받은(갱신한) 시각
    Column("source", String(10)),                   # kis(실시간)·yahoo·krx·fdr·kept(직전 실데이터)·gen(표본)
    Column("as_of", Date),                    # 그 값이 속한 거래일(YYYY-MM-DD) — ts 와 다를 수 있음(D-1 종가 등)
)
# 지수 일별 이력 — 홈 카드 미니차트 · 추천 모델의 시장 국면 판단 · 백테스트 벤치마크.
index_daily = Table("index_daily", metadata,
    Column("code", String(20), primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("change", Float),
    Column("change_rate", Float),
    Column("source", String(10)),
)
# 추천 결과 스냅샷(감사 추적) — '언제, 어떤 모델 버전이, 무엇을, 왜' 추천했는지 그대로 남긴다.
# 사후 검증(한 달 전 추천)·모델 모니터링이 '다시 계산한 값'이 아니라 '실제로 보여준 값'을 쓰게 한다.
reco_snapshot = Table("reco_snapshot", metadata,
    Column("as_of", Date, primary_key=True),          # 추천 기준 거래일
    Column("profile", String(10), primary_key=True),        # all / 안정형 / 중립형 / 공격형
    Column("model_version", String(20), primary_key=True),
    Column("rank", Integer, primary_key=True),
    Column("ticker", String(20)),
    Column("name", String(100)),
    Column("score", Float),                                 # 0~100 (적격 종목 내 백분위)
    Column("composite", Float),                             # 가중합 z-점수
    Column("entry_price", WON),                           # 기준일 종가
    Column("tag", String(40)),
    Column("detail", String(2000)),                         # 팩터·근거 JSON
    Column("created_at", KST_TS),
)
# 양봉이 주간 추천(2026-11-09, 팀 알고리즘 yangbong_picks.py) — 한 주 1행. 고른 날(그 주 마지막 거래일)에 5종목,
# 30일이 지나면 같은 행에 채점(다음 거래일 시가 매수 → 30일 뒤 종가 매도). 공개 데이터(L1). app/services/yangbong.py
yangbong_basket = Table("yangbong_basket", metadata,
    Column("signal_date", Date, primary_key=True),            # 고른 날(보통 금요일)
    Column("buy_date", Date),                                 # 매수일(다음 거래일) — 채점 때 채움
    Column("sell_date", Date),                                # 매도일(매수일 + 30일, 휴장이면 직전 거래일)
    Column("codes", ARRAY(String(12)), nullable=False),       # 종목코드(순위 순)
    Column("names", ARRAY(String(100)), nullable=False),
    Column("stock_returns", ARRAY(Float)),                    # 종목별 수익률(0.05 = +5%, codes 와 같은 순서·값 없으면 NULL)
    Column("basket_return", Float),                           # 5종목 단순 평균(20%씩)
    Column("kospi_return", Float),                            # 같은 기간 코스피(매수일 시가 → 매도일 종가)
    Column("source", String(8), nullable=False),              # seed(팀 백테스트 2022~2026) | batch(우리 배치)
    Column("picked_at", TSTZ, nullable=False),
    Column("scored_at", TSTZ),
)

# ── 계정 정보(로그인 유저에 귀속) ─────────────────────────
# 투자성향 분석 결과(설문) — 유저별 이력(최신이 현재 성향).
survey_result = Table("survey_result", metadata,
    Column("result_id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50)),
    Column("type", String(20)),                 # 안정형/중립형/공격형
    Column("score", Integer),
    Column("description", String(300)),
    Column("created_at", KST_TS),
)
# 학습 로그 — 유저가 어떤 용어/지표/기법을 열람했는지.
learning_log = Table("learning_log", metadata,
    Column("log_id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50)),
    Column("kind", String(20)),                 # term / indicator / technique
    Column("item_key", String(60)),
    Column("item_name", String(100)),
    Column("viewed_at", KST_TS),
)
# 접속 로그 — 회원가입/로그인/로그아웃 등 방문 이력.
access_log = Table("access_log", metadata,
    Column("log_id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50)),
    Column("event", String(20)),                # register / login / logout
    Column("detail", String(200)),
    Column("at", KST_TS),
)

# 관심종목(즐겨찾기) — 계정에 귀속. (user_id, ticker) 유니크로 중복 등록 방지.
watchlist = Table("watchlist", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("created_at", KST_TS),
)
# 실습 기록(웹 '학습 및 실습하기' — 매매 계산은 브라우저, 서버는 한 판씩 받아 범위·종류만 확인해 기록) — 마이페이지 '실습 이력'
practice_record = Table("practice_record", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", String(50), nullable=False),
    Column("tech", String(20), nullable=False),               # golden | rsi | pullback | support
    Column("mode", String(10), nullable=False),               # practice(예시 데이터) | real(SOXL 실제 데이터)
    Column("traded", Boolean, nullable=False),                # 매수·매도까지 했는지(안 했으면 수익률 없음)
    Column("ret", Float),                                     # 수익률(비율 — 0.05 = +5%), traded 일 때만
    Column("real_entry", Date),                         # 실전 모드의 사례(진입일 YYYY-MM-DD)
    Column("created_at", DateTime(timezone=True), nullable=False),
)
# Daily Stock Quiz — 계정·날짜마다 1행(하루 한 번). 화면은 고른 보기 번호만 보내고, 서버가 같은 문제은행·같은 날짜 공식으로
# 그날 문제를 다시 만들어 채점한다(app/services/quiz.py — 점수를 화면이 정하지 않는다)
quiz_attempt = Table("quiz_attempt", metadata,
    Column("user_id", String(50), primary_key=True),
    Column("quiz_date", Date, primary_key=True),        # 문제를 정하는 날짜(YYYY-MM-DD)
    Column("answers", String(20), nullable=False),            # '2,0,3,1,1' — 화면에 보인 순서의 보기 번호
    Column("score", SmallInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # 정답 보상(가상머니) — 지급액과 지급한 한국 날짜. 보상은 하루 1번: (user_id, reward_day) 부분 유일 인덱스가 DB 에서 보장
    Column("reward", Integer, nullable=False, server_default="0"),
    Column("reward_day", Date),
)
Index("ux_quiz_attempt_reward_day", quiz_attempt.c.user_id, quiz_attempt.c.reward_day, unique=True,
      postgresql_where=text("reward_day IS NOT NULL"))

# 운영 복원력(2026-11-02) ─────────────────────────────────────────────
# 성능관리보고서 — 부하 단계가 경계·심각으로 올라간 순간의 지표·느린 경로·권고(app/services/capacity.py). 개인정보 없음.
capacity_report = Table("capacity_report", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("level", SmallInteger, nullable=False),               # 2=경계 3=심각
    Column("detail", Text, nullable=False),                      # JSON
)
# 서비스 비상 차단(기능별 스위치) — 중대 전자금융사고 시 피해 확산 방지. 행이 없으면 '운영 중'(app/services/controls.py)
service_control = Table("service_control", metadata,
    Column("feature", String(20), primary_key=True),             # transfer · charge · shop · invest
    Column("state", String(8), nullable=False),                  # on | paused
    Column("reason", String(200), nullable=False),
    Column("changed_by", String(50), nullable=False),            # 관리자 아이디 또는 'system'(자동 차단)
    Column("changed_at", DateTime(timezone=True), nullable=False),
)
# 재개 요청 — 멈추기는 한 사람이 바로, 다시 열기는 다른 관리자의 승인(2인, 직무 분리)
service_control_request = Table("service_control_request", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("feature", String(20), nullable=False),
    Column("reason", String(200), nullable=False),
    Column("requested_by", String(50), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("status", String(10), nullable=False),                 # pending | approved | rejected | expired
    Column("decided_by", String(50)),
    Column("decided_at", DateTime(timezone=True)),
)
Index("ux_service_control_request_pending", service_control_request.c.feature, unique=True,
      postgresql_where=text("status = 'pending'"))
# 기기 결속 증명(DPoP) 재사용 방지 — (키 지문, 증명 ID) 한 번만. 몇 분 뒤 지운다(app/security/dpop.py)
dpop_jti = Table("dpop_jti", metadata,
    Column("jkt", String(43), primary_key=True),
    Column("jti", String(64), primary_key=True),
    Column("seen_at", DateTime(timezone=True), nullable=False),
)
Index("ix_dpop_jti_seen", dpop_jti.c.seen_at)

# 자율보안(2026-11-02) ─────────────────────────────────────────────
# 수준진단 결과 — 추가만(앱 계정은 수정·삭제 권한 없음, V016). 결과 다이제스트는 감사 체인에도(app/security/assessment.py)
security_assessment = Table("security_assessment", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(50), nullable=False),                 # 관리자 아이디 또는 'system'(정기 재진단)
    Column("mode", String(10), nullable=False),                  # runtime | static
    Column("overall", Float),
    Column("below_target", Integer, nullable=False, server_default="0"),
    Column("probe_failures", Integer, nullable=False, server_default="0"),
    Column("result", Text, nullable=False),                      # JSON(축약 — 항목별 등급·근거 종류·점검 결과)
    Column("digest", String(64), nullable=False),                # result 의 SHA-256 — 증빙 위변조 확인
)
Index("ix_security_assessment_at", security_assessment.c.at)
# 파일 무결성 기준선 — 배포 직후 소유자 계정만 쓴다(앱 계정은 읽기만, V016). 파일별 SHA-256(app/security/integrity.py)
integrity_baseline = Table("integrity_baseline", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(50), nullable=False),
    Column("release", String(120)),                              # 설치본 이름 등
    Column("files", Integer, nullable=False),
    Column("digest", String(64), nullable=False),                # 파일 목록 다이제스트 — 행을 고치면 불일치
    Column("manifest", Text, nullable=False),                    # JSON {경로: SHA-256}
)

# IT 내부통제(2026-11-02) ─────────────────────────────────────────────
# 변경 기록 — 요청·사전 영향 분석·자동 점검·승인(요청자≠승인자)·적용·사후 검토(app/services/changes.py).
# 앱 계정은 지울 수 없고, 만든 뒤에는 상태·승인·적용·검토 칸만 바꿀 수 있다(V017) — 승인된 설치본 해시를 바꿔치기 못 하게.
change_record = Table("change_record", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("kind", String(12), nullable=False),                  # planned | emergency | unapproved
    Column("title", String(120), nullable=False),
    Column("reason", String(500), nullable=False),
    Column("requested_by", String(50), nullable=False),
    Column("release_name", String(160)),
    Column("release_sha256", String(64), nullable=False),
    Column("impact", Text),                                      # JSON — 바뀐 파일의 영역·위험도·성능 민감 경로
    Column("risk", String(8), nullable=False),                   # low | medium | high
    Column("prechecks", Text),                                   # JSON — 자동 사전 점검 결과
    Column("evidence", String(1000)),                            # 검증 근거(시험·부하 결과 요약)
    Column("status", String(12), nullable=False),                # requested|approved|rejected|applied|failed|rolled_back
    Column("approved_by", String(50)),
    Column("approved_at", DateTime(timezone=True)),
    Column("applied_at", DateTime(timezone=True)),
    Column("applied_by", String(50)),
    Column("baseline_id", Integer),                              # 적용 직후 파일 무결성 기준선
    Column("review_due", DateTime(timezone=True)),               # 긴급 변경 사후 검토 기한
    Column("reviewed_by", String(50)),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("review_note", String(500)),
)
Index("ix_change_record_sha", change_record.c.release_sha256, change_record.c.status)
Index("ux_change_record_open", change_record.c.release_sha256, unique=True,      # 같은 설치본의 진행 중 정식 요청은 하나
      postgresql_where=text("kind = 'planned' AND status IN ('requested', 'approved')"))
# 가용성 확인 기록(합성 모니터링 1분) — 35일 보관(app/services/incidents.py). 개인정보 없음.
uptime_check = Table("uptime_check", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("ms", Integer, nullable=False),
    Column("code", SmallInteger, nullable=False),                # HTTP 상태(0 = 연결 불가)
)
Index("ix_uptime_check_at", uptime_check.c.at)
# 장애·사고 사건 — 자동 판정(합성 모니터링) 또는 사람이 연다. 보고 대상·기한·보고 단계(최초·중간·종결)를 남긴다.
# 사고보고서 초안의 원천(전자금융감독규정 시행세칙 제7조의4·별지 제2호). 앱 계정은 지울 수 없다(V017), 5년 보관.
incident = Table("incident", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(10), nullable=False),                  # outage | security | data | other
    Column("title", String(200), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("detected_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("status", String(10), nullable=False),                # open | resolved | closed
    Column("severity", String(8), nullable=False),
    Column("opened_by", String(50)),                             # NULL = 자동 판정
    Column("affected_users", Integer),
    Column("reportable", Boolean, nullable=False, server_default=text("false")),
    Column("report_due", DateTime(timezone=True)),
    Column("details", Text),                                     # JSON(지속 시간·가입자 수·실패 확인 수)
    Column("cause", String(1000)),
    Column("action", String(1000)),
    Column("measures", String(1000)),
    Column("impact_note", String(1000)),
    Column("loss_amount", BigInteger),
    Column("reputation", Boolean, nullable=False, server_default=text("false")),
    Column("data_damage", Boolean, nullable=False, server_default=text("false")),
    Column("first_reported_at", DateTime(timezone=True)),
    Column("interim_reported_at", DateTime(timezone=True)),
    Column("final_reported_at", DateTime(timezone=True)),
)
Index("ix_incident_started", incident.c.started_at)
Index("ux_incident_open_outage", incident.c.kind, unique=True,   # 자동 판정 장애는 한 번에 하나(스케줄러가 둘이어도)
      postgresql_where=text("kind = 'outage' AND ended_at IS NULL AND opened_by IS NULL"))

# DB 운영(2026-11-03 · ADR-0027) ─────────────────────────────────────────────
# 데이터 수명주기 보관 기록 — 어느 달의 무엇을 어느 파일(해시)로 내보내고 언제 DB 에서 정리했는지(app/services/ilm.py).
# 앱 계정은 지울 수 없고 파일·해시·행 수를 고칠 수 없다(V018 — 정리·복원·검증 시각만). 개인정보 없음.
archive_log = Table("archive_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dataset", String(20), nullable=False),               # news | search
    Column("period_start", Date, nullable=False),                # 포함
    Column("period_end", Date, nullable=False),                  # 미포함
    Column("path", String(300), nullable=False),                 # 보관 폴더 기준 상대 경로
    Column("sha256", String(64), nullable=False),                # gzip 파일 해시
    Column("bytes", BigInteger, nullable=False),                 # 압축 후
    Column("raw_bytes", BigInteger),                             # 압축 전(JSON Lines)
    Column("rows", Text, nullable=False),                        # JSON {표: 행 수}
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),               # DB 에서 정리를 끝낸 시각(NULL = 아직·중단)
    Column("deleted_rows", Integer),
    Column("verified_at", DateTime(timezone=True)),              # 마지막 재해시 확인
    Column("restored_at", DateTime(timezone=True)),
)
Index("ix_archive_log_period", archive_log.c.dataset, archive_log.c.period_start)
# DB 크기 일일 기록 — 증가 추세·디스크 소진 예측·표별 비대화(app/services/dbhealth.py). 하루 1행, 2년 보관. 개인정보 없음.
db_size_snapshot = Table("db_size_snapshot", metadata,
    Column("day", Date, primary_key=True),                       # 한국 날짜
    Column("at", DateTime(timezone=True), nullable=False),
    Column("db_bytes", BigInteger, nullable=False),
    Column("detail", Text, nullable=False),                      # JSON — 표별 크기·행 수·죽은 행(상위)
)

# 양봉이 꾸미기(2026-10-31) — 산 아이템과 착용 상태. 가격은 산 순간 값(카탈로그가 바뀌어도 원장과 맞게).
# 같은 아이템은 한 번만(PK), 부위마다 하나만 착용(부분 유일 인덱스).
avatar_closet = Table("avatar_closet", metadata,
    Column("username", String(50), primary_key=True),
    Column("item_id", String(20), primary_key=True),
    Column("slot", String(10), nullable=False),               # hat·top·bottom·shoes·glasses·pet
    Column("price", BigInteger, nullable=False),
    Column("equipped", Boolean, nullable=False, server_default=text("false")),
    Column("bought_at", DateTime(timezone=True), nullable=False),
)
Index("ux_avatar_closet_equipped", avatar_closet.c.username, avatar_closet.c.slot, unique=True,
      postgresql_where=text("equipped"))

# 모의투자 — 가상 시장 시즌(2026-11-09, 팀 invest.html). 월~금이 한 시즌, 가상 종목 5개, 평일 09:00~15:00 30분마다 봉 1개 공개.
# 시즌 데이터(사전 봉 120 + 시즌 봉 65)는 서버에만 둔다 — 화면에는 '지금까지 공개된 봉'만 나간다(미래 봉 비공개).
# 참가금은 지갑에서 빠져 시즌 현금이 되고(원장 vmarket_entry), 주말에 남은 주식을 마지막 종가로 팔아 최종 평가금액을
# 지갑으로 돌려준다(vmarket_settle). 체결가는 정수(원)라 현금은 늘 정수다. app/services/vmarket.py
vmarket_season = Table("vmarket_season", metadata,
    Column("season_id", Integer, primary_key=True),             # 1 = 2026-11-02(월) 주
    Column("week_start", Date, nullable=False, unique=True),     # 그 주 월요일
    Column("payload", Text, nullable=False),                    # JSON {stocks: [{id, name, sector, size, scenario, warmup, candles}]}
    Column("sha256", String(64), nullable=False),               # payload 해시 — 시작한 시즌은 바꿀 수 없다(가져오기가 거부)
    Column("source", String(60), nullable=False),               # 가져온 파일 이름
    Column("imported_at", TSTZ, nullable=False),
)
vmarket_entry = Table("vmarket_entry", metadata,
    Column("season_id", Integer, primary_key=True),
    Column("username", String(50), primary_key=True),
    Column("entry", BigInteger, nullable=False),                # 참가금(지갑에서 빠진 돈)
    Column("cash", BigInteger, nullable=False),                 # 시즌 안 현금(5종목이 함께 씀)
    Column("realized", Float, nullable=False),                  # 실현손익(평균단가 기준 — 표시용, 돈은 cash 가 정본)
    Column("joined_bar", SmallInteger, nullable=False),         # 참가할 때 공개돼 있던 봉 수(0~65)
    Column("status", String(8), nullable=False),                # open | settled
    Column("final", BigInteger),                                # 정산 금액(= 지갑으로 돌아간 돈)
    Column("auto_sold", BigInteger),                            # 정산 때 자동으로 판 주식 수 · 종목 수
    Column("auto_stocks", SmallInteger),
    Column("joined_at", TSTZ, nullable=False),
    Column("settled_at", TSTZ),
    CheckConstraint("entry > 0 AND cash >= 0", name="ck_vmarket_entry_money"),
    CheckConstraint("status IN ('open', 'settled')", name="ck_vmarket_entry_status"),
)
Index("ix_vmarket_entry_open", vmarket_entry.c.season_id, postgresql_where=text("status = 'open'"))
vmarket_position = Table("vmarket_position", metadata,
    Column("season_id", Integer, primary_key=True),
    Column("username", String(50), primary_key=True),
    Column("stock_idx", SmallInteger, primary_key=True),        # 0~4(종목 A~E)
    Column("shares", BigInteger, nullable=False),
    Column("avg_price", Float, nullable=False),
    Column("realized", Float, nullable=False),
    CheckConstraint("shares >= 0 AND stock_idx BETWEEN 0 AND 4", name="ck_vmarket_position"),
)
vmarket_trade = Table("vmarket_trade", metadata,
    Column("trade_id", BigInteger, primary_key=True, autoincrement=True),
    Column("season_id", Integer, nullable=False),
    Column("username", String(50), nullable=False),
    Column("stock_idx", SmallInteger, nullable=False),
    Column("side", String(4), nullable=False),                  # buy | sell
    Column("qty", BigInteger, nullable=False),
    Column("price", WON, nullable=False),                       # 체결가 = 그 봉 종가(서버가 정한다)
    Column("bar", SmallInteger, nullable=False),                # 체결한 봉(시즌 봉 1~65)
    Column("auto", Boolean, nullable=False, server_default=text("false")),   # 주말 정산 자동 매도
    Column("idem_key", String(80)),
    Column("created_at", TSTZ, nullable=False),
    UniqueConstraint("username", "idem_key", name="uq_vmarket_trade_idem"),
    CheckConstraint("qty > 0 AND price > 0 AND side IN ('buy', 'sell') AND bar BETWEEN 1 AND 65",
                    name="ck_vmarket_trade"),
)
Index("ix_vmarket_trade_owner", vmarket_trade.c.season_id, vmarket_trade.c.username, vmarket_trade.c.trade_id)

# ══ 추가 기능: 가상머니 지갑 생태계 ═══════════════════════════════
# 지갑 — 계정별 가상머니 잔액. version 으로 OCC(동시 입·출금 경쟁 시 갱신분실 방지).
wallet = Table("wallet", metadata,
    Column("username", String(50), primary_key=True),
    Column("balance", BigInteger),            # 현재 잔액(가상머니, 원 단위)
    Column("version", Integer),               # OCC(낙관적 잠금)
    Column("updated_at", KST_TS),
    Column("account_no", String(16)),         # 원장 계좌번호(복식부기 분개의 계정 — 아이디 대신, 탈퇴하면 연결이 끊겨 가명이 된다)
)
# 지갑 거래원장(append-only) — 모든 잔액 변동 1건=1행. '잔액 == 원장 합' 이어야 함(정합성 대사).
# reason: welcome/attendance/quiz_reward/avatar_buy/iap/sim_stake/sim_settle/transfer_in/transfer_out/shop_buy/invest_buy/invest_sell
wallet_ledger = Table("wallet_ledger", metadata,
    Column("ledger_id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(50)),
    Column("reason", String(20)),
    Column("amount", BigInteger),             # +입금 / -출금
    Column("balance_after", BigInteger),      # 처리 직후 잔액(감사·재구성용)
    Column("ref", String(60)),                # 연관 자원(round_id/position_id/상대 계정 등)
    Column("memo", String(200)),
    Column("idem_key", String(80)),           # 멱등(재전송 방지)
    Column("created_at", KST_TS),
    UniqueConstraint("username", "idem_key", name="uq_ledger_idem"),
)
# 복식부기 분개(append-only) — 지갑 잔액이 바뀔 때마다 '사용자 계좌'와 '상대 시스템 계정'에 같은 금액을 반대 부호로.
# 한 거래(txn_id)의 합은 항상 0(차변=대변) — DB 제약 트리거가 커밋 시점에 확인(V012). 수정·삭제는 트리거가 거부.
# 계정: W<계좌번호>(사용자 지갑 — 아이디를 담지 않는다) · sys:promotion · sys:iap_clearing · sys:pg_clearing ·
#       sys:transfer_clearing · sys:invest_custody · sys:forfeited · sys:opening_equity · sys:suspense(미분류 — 0 이어야 함)
journal_entry = Table("journal_entry", metadata,
    Column("entry_id", BigInteger, primary_key=True, autoincrement=True),
    Column("txn_id", String(32), nullable=False),
    Column("account", String(32), nullable=False),
    Column("amount", BigInteger, nullable=False),            # +차변 / -대변(사용자 계좌 입장: +입금)
    Column("reason", String(20), nullable=False),
    Column("ledger_id", Integer),                            # 사용자 쪽 원장 행(wallet_ledger) — 탈퇴 후엔 끊긴다
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_journal_txn", journal_entry.c.txn_id)
Index("ix_journal_account_at", journal_entry.c.account, journal_entry.c.created_at)
Index("ix_journal_created", journal_entry.c.created_at)
# 일 마감 — 그날 분개 합계·시산표·대사 결과를 남기고, 전날 마감 해시와 이어 붙인다(지난 기간 변조 탐지)
ledger_close = Table("ledger_close", metadata,
    Column("close_date", Date, primary_key=True),       # KST 날짜
    Column("status", String(8), nullable=False),               # ok | fail
    Column("entries", Integer, nullable=False),
    Column("txns", Integer, nullable=False),
    Column("debit", BigInteger, nullable=False),
    Column("credit", BigInteger, nullable=False),
    Column("checks", Text, nullable=False),                    # JSON
    Column("prev_hash", String(64), nullable=False),
    Column("close_hash", String(64), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=False),
)
# 트랜잭셔널 아웃박스 — 업무 변경과 '같은 트랜잭션'에서 사건을 적어 두고, 중계기가 커밋된 것만 나중에 전달(최소 1회).
# 외부 전송(운영 알림·SIEM 웹훅)이 실패해도 업무는 이미 끝났고, 사건은 지수 백오프로 다시 시도 → 8번 실패하면 dead.
outbox = Table("outbox", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("topic", String(40), nullable=False),
    Column("payload", Text, nullable=False),                  # JSON(개인정보 최소 — 아이디 대신 가명·금액 등)
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("status", String(10), nullable=False, server_default="pending"),   # pending | done | dead
    Column("delivered_at", DateTime(timezone=True)),
    Column("last_error", String(300)),
)
Index("ix_outbox_pending", outbox.c.status, outbox.c.available_at)
# 데이터 계약 측정 결과(BCBS 239 품질 차원별) — 매일 쌓아 추세를 본다
dq_result = Table("dq_result", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_at", DateTime(timezone=True), nullable=False),
    Column("dataset", String(40), nullable=False),
    Column("check_name", String(40), nullable=False),
    Column("dimension", String(16), nullable=False),
    Column("value", Float),
    Column("op", String(2), nullable=False),
    Column("threshold", Float, nullable=False),
    Column("severity", String(8), nullable=False),
    Column("status", String(4), nullable=False),               # pass | fail
)
Index("ix_dq_result_run", dq_result.c.run_at)
# 이상거래탐지(FDS) 판정 기록 — 점수·걸린 규칙·재료·결정(사후 검토·임계값 튜닝·오탐 분석)
fds_decision = Table("fds_decision", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("username", String(50), nullable=False),
    Column("action", String(20), nullable=False),
    Column("counterparty", String(50)),
    Column("amount", BigInteger, nullable=False),
    Column("score", Integer, nullable=False),
    Column("rules", Text),                                    # JSON: hits·features·model 결정
    Column("decision", String(24), nullable=False),           # allow | allow_after_step_up | step_up | block
    Column("request_id", String(64)),
    Column("ref", String(40)),                                # 실행된 송금의 키(막힌 시도는 NULL) — 판정 누락 점검용
)
Index("ix_fds_decision_at", fds_decision.c.at)
Index("ix_fds_decision_user", fds_decision.c.username, fds_decision.c.at)
Index("ix_fds_decision_ref", fds_decision.c.ref)
# 출석 체크 — 하루 1회. (username, attend_date) UNIQUE 로 이중 지급 차단. streak=연속 출석일.
attendance = Table("attendance", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(50)),
    Column("attend_date", Date),        # YYYY-MM-DD (KST)
    Column("reward", BigInteger),
    Column("streak", Integer),
    Column("created_at", KST_TS),
    UniqueConstraint("username", "attend_date", name="uq_attendance_day"),
)
# 인앱결제 — Google/Apple 스토어 결제로 가상머니 충전. receipt UNIQUE 로 영수증 재사용(replay) 차단.
iap_purchase = Table("iap_purchase", metadata,
    Column("purchase_id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(50)),
    Column("store", String(10)),              # google / apple
    Column("product_id", String(40)),
    Column("coins", BigInteger),              # 지급된 가상머니
    Column("price", String(20)),              # 결제 실화폐 표시가(예: ₩3,300)
    Column("receipt", String(512), unique=True),   # 스토어 토큰은 길 수 있음(초장문은 해시 저장 권장)
    Column("status", String(12)),             # verified / rejected
    Column("created_at", KST_TS),
)
# 장기 실전 모의투자 — 현재가로 모의 매수 후, 실제 최신가 대비 평가/실현손익 추적. 지갑 연동.
invest_position = Table("invest_position", metadata,
    Column("position_id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(50)),
    Column("ticker", String(20)),
    Column("quantity", BigInteger),
    Column("buy_price", Float),
    Column("buy_date", Date),
    Column("cost", BigInteger),
    Column("status", String(10)),             # open / closed
    Column("sell_price", Float),
    Column("sell_date", Date),
    Column("proceeds", BigInteger),           # 매도대금 누계(부분 매도 포함)
    Column("realized_pnl", BigInteger),       # 실현손익 누계(부분 매도 포함)
    Column("created_at", KST_TS),
    Column("sold_qty", BigInteger),           # 매도 수량 누계(부분 매도 포함) — quantity 는 '남은 보유 수량'
    Column("adj_through", Date),        # 이 날짜까지의 권리 이벤트(액면분할·무상증자)를 수량·매수가에 반영함
)
# ── 웹 결제(토스페이먼츠) 주문 — prepare 시 pending, confirm 성공 시 paid + 지갑 적립. ──
# order_id 가 멱등 단위(같은 주문은 한 번만 적립). 실 스토어 IAP 대신 웹 PG 승인 검증 경로.
payment_order = Table("payment_order", metadata,
    Column("order_id", String(64), primary_key=True),
    Column("username", String(50)),
    Column("product_id", String(40)),
    Column("coins", BigInteger),
    Column("amount", BigInteger),               # 결제 금액(원)
    Column("order_name", String(100)),
    Column("status", String(12)),               # pending / paid / failed
    Column("payment_key", String(600)),         # 토스 결제 키(승인 후 기록) — 봉투 암호화(v2) 저장이라 넉넉히
    Column("created_at", KST_TS),
    Column("paid_at", KST_TS),
)
# ── 친구 — 요청(friend_request) → 상대가 수락하면 friendship 에 (a→b),(b→a) 두 행을 함께 만든다(양방향). ──
#    (owner,friend) 유니크로 중복 방지. 친구 = 두 방향 행이 모두 있을 때. 삭제도 두 행을 함께.
friendship = Table("friendship", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(50)),
    Column("friend", String(50)),
    Column("created_at", TSTZ),
    UniqueConstraint("owner", "friend", name="uq_friendship"),
)
# ── 친구 요청 — pending(대기) → accepted(수락)/declined(거절)/canceled(취소). 같은 방향 대기 요청은 1건. ──
friend_request = Table("friend_request", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sender", String(50), nullable=False),
    Column("receiver", String(50), nullable=False),
    Column("status", String(10), nullable=False),        # pending / accepted / declined / canceled
    Column("created_at", TSTZ),
    Column("responded_at", TSTZ),
)
# ── 친구 1:1 채팅(메신저) — 서로 친구(요청 수락 완료)일 때만 전송. read_at 으로 읽음 처리. ──
chat_message = Table("chat_message", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sender", String(50), nullable=False),
    Column("receiver", String(50), nullable=False),
    Column("body", Text, nullable=False),            # 봉투 암호화 저장(통신 내용, L4) — 암호문 길이 때문에 TEXT
    Column("created_at", TSTZ, nullable=False),
    Column("read_at", TSTZ),                   # NULL = 안 읽음
)

# ── 앱 메타(키-값) — 시세 출처(krx/gen)·기준일, KIS 토큰 캐시 등 운영 상태 ──
app_meta = Table("app_meta", metadata,
    Column("key", String(60), primary_key=True),
    # 길이 제한 없음(TEXT) — 종목별 KRX 시작일(krx_since, 시총 상위 150종목이면 JSON 약 3.6KB)·배치 결과 JSON 이 들어간다.
    # 예전 VARCHAR(2000) 이면 실데이터 첫 적재가 'value too long' 으로 통째로 롤백됐다(10-10, 학교 VM).
    Column("value", Text),
    Column("updated_at", TSTZ),
)

# ── 운영 알림 기록(장애 대응) — 무엇을 언제 누구에게(어느 채널로) 알렸는지. 같은 key 는 일정 시간 안에 다시 안 보냄 ──
alert_log = Table("alert_log", metadata,
    Column("alert_id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(100), nullable=False),            # 중복 판정 단위(예: quality:kr_market, job:KRX 일봉)
    Column("level", String(10), nullable=False),           # info / warn / error
    Column("title", String(200), nullable=False),
    Column("message", Text),                               # 비밀값을 가린 본문
    Column("channels", String(100)),                       # 보낸 채널(webhook,telegram,email) — 없으면 'log'
    Column("delivered", Integer),                          # 1 = 한 채널 이상 전달, 0 = 전부 실패, NULL = 중복이라 생략
    Column("created_at", KST_TS, nullable=False),
    # 관제(2026-11-02) — 누가 언제 확인(ack)했는지, 확인 없이 지나 상향(escalation)했는지. 시각은 created_at 과 같은 KST 문자열
    Column("ack_by", String(50)),
    Column("ack_at", KST_TS),
    Column("escalated_at", KST_TS),
)
Index("ix_alert_log_key_at", alert_log.c.key, alert_log.c.created_at)

# ── DART 회사 고유번호 목록(corpCode.xml) — 상장사만. 매번 3MB zip 을 받지 않고 7일 동안 재사용 ──
# 감사 로그 — 해시 체인(prev_hash → row_hash)으로 위·변조를 검출. 앱 계정은 INSERT·SELECT 만(deploy/sql/roles.sql)
audit_log = Table("audit_log", metadata,
    Column("audit_id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", String(30), nullable=False),
    Column("actor", String(50)),
    Column("action", String(40), nullable=False),
    Column("target", String(100)),
    Column("detail", Text),
    Column("ip_hash", String(16)),                   # IP 원문 대신 HMAC 가명값(개인정보 최소화)
    Column("result", String(10)),
    Column("prev_hash", String(64), nullable=False),
    Column("row_hash", String(64), nullable=False),
)
Index("ix_audit_actor", audit_log.c.actor, audit_log.c.audit_id)
Index("ix_audit_action", audit_log.c.action, audit_log.c.audit_id)
dart_corp = Table("dart_corp", metadata,
    Column("corp_code", String(8), primary_key=True),
    Column("corp_name", String(200)),
    Column("stock_code", String(12)),
    Column("modify_date", String(8)),
    Column("fetched_at", TSTZ),
)
Index("ix_dart_corp_stock", dart_corp.c.stock_code)

# ── 해외 주식(미국) — 국내 stock/ohlc/realtime_price 와 분리(USD·거래소·시간대가 다름) ──
overseas_stock = Table("overseas_stock", metadata,
    Column("symbol", String(12), primary_key=True),
    Column("name", String(100)),                     # 영문명
    Column("name_ko", String(60)),                   # 한글명(검색·표시)
    Column("exchange", String(20)),                  # NASDAQ/NYSE
    Column("sector", String(60)),
    Column("shares", BigInteger),                    # 발행주식수(EDGAR dei) → 시총 계산
    Column("market_cap", BigInteger),                # USD (Finnhub profile2 우선)
    Column("source", String(10)),                    # fdr | gen  (일봉 출처)
    Column("updated_at", TSTZ),
)
overseas_ohlc = Table("overseas_ohlc", metadata,
    Column("symbol", String(12), primary_key=True),
    Column("trade_date", Date, primary_key=True),   # 미국 현지 거래일
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", BigInteger),
)
overseas_financial = Table("overseas_financial", metadata,
    Column("symbol", String(12), primary_key=True),
    Column("payload", String(4000)),                 # EDGAR 재무 JSON (NULL = 재무 없음)
    Column("updated_at", TSTZ),
)
overseas_quote = Table("overseas_quote", metadata,
    Column("symbol", String(12), primary_key=True),
    Column("price", Float),
    Column("change", Float),
    Column("change_rate", Float),
    Column("prev_close", Float),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("volume", BigInteger),
    Column("session_date", Date),              # 미국 현지 거래일
    Column("source", String(10)),                    # finnhub | sim
    Column("ts", TSTZ),
)

# 보조 인덱스 — 자주 조회되는 비-PK 컬럼(전체 스캔 방지). create_all 시 함께 생성.
# (2026-11-03) 앞 열이 같은 복합 키·유일 제약이 이미 있는 단일 열 인덱스 6개는 뺐다 — 같은 조회를 그 키가 처리하고,
#  인덱스마다 쓰기·저장·백업 비용만 든다(ohlc·overseas_ohlc 의 PK, uq_practice_idem·uq_ledger_idem·uq_attendance_day·
#  uq_friendship). 기존 DB 는 _drop_redundant_indexes() 가 덮는 인덱스가 있는 것을 확인한 뒤 지운다.
Index("ix_news_stock_ticker", news_stock.c.ticker)             # 종목별 뉴스 태깅 조회
Index("ix_news_published_at", news.c.published_at)            # 뉴스 화면 기간(오늘·주간·월간) 집계
Index("ix_daily_price_date", daily_price.c.trade_date)         # 최신 거래일/신선도 조회
Index("ix_practice_user", practice_session.c.user_id)          # 유저별 실습 진행 조회
Index("ix_survey_user", survey_result.c.user_id)               # 유저별 성향 이력
Index("ix_learning_user", learning_log.c.user_id)              # 유저별 학습 로그
Index("ix_access_user", access_log.c.user_id)                  # 유저별 접속 로그
Index("ux_watchlist_user_ticker", watchlist.c.user_id, watchlist.c.ticker, unique=True)  # 관심종목·중복방지
Index("ix_practice_record_user", practice_record.c.user_id, practice_record.c.id)   # 내 실습 기록(최신순)·집계
Index("ix_iap_user", iap_purchase.c.username)                     # 유저별 결제 이력
Index("ix_payment_order_user", payment_order.c.username)          # 유저별 결제 주문
Index("ix_invest_user", invest_position.c.username)               # 유저별 장기 투자 포지션
Index("ix_chat_pair", chat_message.c.sender, chat_message.c.receiver, chat_message.c.id)  # 대화 스레드
Index("ix_chat_receiver_unread", chat_message.c.receiver, chat_message.c.read_at)         # 안읽음 집계
Index("ix_friend_req_receiver", friend_request.c.receiver, friend_request.c.status)   # 받은 요청
Index("ix_friend_req_sender", friend_request.c.sender, friend_request.c.status)       # 보낸 요청
Index("ux_friend_req_pending", friend_request.c.sender, friend_request.c.receiver, unique=True,
      postgresql_where=text("status = 'pending'"))                # 같은 방향 대기 요청 1건(중복 요청 차단)

# 기존(이미 운영 중인) DB 에 새 컬럼을 무손실로 추가하는 마이그레이션.
# create_all 은 '없는 테이블'만 만들고 기존 테이블 컬럼은 건드리지 않으므로, 여기서 보강한다.
# 현재 스키마를 먼저 조회해 '필요한 변경만' 실행 → 반복 실행해도 테이블 잠금이 생기지 않는다(운영 중 안전).
_ADD_COLUMNS = {
    "realtime_price": [("change", "INTEGER"), ("open", "INTEGER"),
                       ("high", "INTEGER"), ("low", "INTEGER"), ("value", "BIGINT"),
                       ("session_date", "DATE"), ("source", "VARCHAR(10)")],
    "news": [("published_ts", "TIMESTAMPTZ(0)")],
    "search_log": [("ticker", "VARCHAR(20)")],
    "financial_statement": [("total_assets", "BIGINT"), ("current_assets", "BIGINT"),
                            ("current_liabilities", "BIGINT"), ("total_liabilities", "BIGINT"),
                            ("retained_earnings", "BIGINT"), ("fs_basis", "VARCHAR(3)")],
    # 지수 출처·기준일(데이터 계보) — 'KRX 실데이터인 줄 알았는데 상수'였던 문제를 화면에서 구분
    "market_index": [("source", "VARCHAR(10)"), ("as_of", "DATE")],
    # 부분 매도 누계(실현손익이 사라지던 문제) — 매도 수량 누계
    "invest_position": [("sold_qty", "BIGINT"), ("adj_through", "DATE")],
    # 제로트러스트(신원) — 계정 상태·연속 실패 잠금·비밀번호 변경 시각
    "app_user": [("status", "VARCHAR(10) NOT NULL DEFAULT 'active'"), ("failed_count", "INTEGER NOT NULL DEFAULT 0"),
                 ("locked_until", "TIMESTAMPTZ"), ("password_changed_at", "TIMESTAMPTZ"),
                 ("last_login_at", "TIMESTAMPTZ")],
    # 접근 토큰을 세션에 묶는다(세션 폐기 = 토큰 무효) + 개별 만료
    "auth_token": [("session_id", "VARCHAR(32)"), ("expires_at", "TIMESTAMPTZ")],
    # 복식부기 계좌번호(분개 계정 — 아이디 대신)
    "wallet": [("account_no", "VARCHAR(16)")],
    # 판정 ↔ 실행된 송금 연결(송금 키) — '판정 없이 실행된 송금 0건' 통제 완전성 점검(데이터 계약, 결함 20)
    "fds_decision": [("ref", "VARCHAR(40)")],
    # 웹 '로그인 유지'(세션 쿠키 vs 수명 있는 쿠키) — 기존 세션은 예전처럼 수명 있는 쿠키(true)
    "auth_session": [("persistent", "BOOLEAN NOT NULL DEFAULT true"),
                     ("dpop_jkt", "VARCHAR(43)")],             # 기기 결속 키 지문(2026-11-02)
    # Daily Stock Quiz 정답 보상(2026-10-31) — 예전 기록은 보상 0
    "quiz_attempt": [("reward", "INTEGER NOT NULL DEFAULT 0"), ("reward_day", "DATE")],
    # 관제 — 경보 확인(ack)·상향(2026-11-02)
    "alert_log": [("ack_by", "VARCHAR(50)"), ("ack_at", "TIMESTAMP(0)"), ("escalated_at", "TIMESTAMP(0)")],
}
_WIDEN = [("realtime_price", "ts", 40), ("market_index", "ts", 40), ("payment_order", "payment_key", 600)]
# VARCHAR(n) → TEXT 로 넓힐 열(길이 제한 해제). PostgreSQL 에서 varchar→text 는 표를 다시 쓰지 않는다(즉시).
_TO_TEXT = [("app_meta", "value"), ("chat_message", "body")]
# 한 번만 돌리면 되는 데이터 정리 — 적용 여부를 app_meta('migration:<이름>')에 남겨, 서버를 켤 때마다
# 뉴스 표 전체를 다시 훑던 문제(여러 프로세스가 동시에 켜지면 교착까지)를 없앤다.
_ONCE = [
    # 구버전 시드가 넣던 '가짜 틱'(ts=날짜만) — 실시간 판정/장외 가격을 오염시키던 행.
    # ts 가 문자열이든 timestamptz 든 같은 뜻이 되게 글자로 바꿔 '시:분'이 있는지 본다(새 DB 는 빈 표라 무해)
    ("20260915-realtime-fake-ticks",
     "DELETE FROM realtime_price WHERE source IS NULL AND ts::text !~ '[0-9]{2}:[0-9]{2}'"),
    # 구글 뉴스 제목 끝 ' - 매체명' 꼬리 제거(수집기는 이제 저장 전에 뗀다 — 예전 행만 정리)
    ("20260929-news-title-tail",
     "UPDATE news SET title = left(title, length(title) - length(source) - 3) "
     "WHERE source IS NOT NULL AND source <> '' AND length(title) > length(source) + 3 "
     "AND right(title, length(source) + 3) = ' - ' || source"),
    # 인앱결제 영수증은 해시('h:'+sha256)로만 보관 — 실제 스토어 토큰(수 KB)도 들어가고, 재사용 차단은 그대로.
    # 8차까지 긴 영수증에 쓰던 'sha256:<같은 해시>' 형식을 먼저 'h:' 로 바꾼 뒤(이중 해시 방지) 나머지 원문을 해시로.
    ("20261003-iap-receipt-sha256-prefix",
     "UPDATE iap_purchase SET receipt = 'h:' || substr(receipt, 8) WHERE receipt LIKE 'sha256:%'"),
    ("20261001-iap-receipt-hash",
     "UPDATE iap_purchase SET receipt = 'h:' || encode(sha256(convert_to(receipt, 'UTF8')), 'hex') "
     "WHERE receipt IS NOT NULL AND receipt NOT LIKE 'h:%'"),
    # 예전 배치 기록에 남았을 수 있는 API 키(요청 URL 의 crtfc_key=·authkey=·token=) 가리기
    ("20261001-scrub-secrets-run",
     "UPDATE pipeline_run SET note = regexp_replace(note, "
     "'(crtfc_key|authkey|token|appkey|appsecret)=[^&\\s]+', '\\1=***', 'gi') "
     "WHERE note ~* '(crtfc_key|authkey|token|appkey|appsecret)='"),
    ("20261001-scrub-secrets-meta",
     "UPDATE app_meta SET value = regexp_replace(value, "
     "'(crtfc_key|authkey|token|appkey|appsecret)=[^&\\s\"]+', '\\1=***', 'gi') "
     "WHERE key LIKE 'pipeline%' AND value ~* '(crtfc_key|authkey|token|appkey|appsecret)='"),
    # 거래정지일 등 KRX 가 시가·고가·저가를 0 으로 준 봉 → 종가로 채움(52주 최저 0원·차트 붕괴 방지)
    # 기존 지갑에 계좌번호 부여(무작위 12자 — 아이디와 무관)
    ("20261020-wallet-account-no",
     "UPDATE wallet SET account_no = 'W' || upper(substr(md5(random()::text || username || clock_timestamp()::text), 1, 12)) "
     "WHERE account_no IS NULL"),
    # 세션 도입 전 24시간 토큰(세션에 묶이지 않음)은 무효 — 한 번 다시 로그인(2026-10 제로트러스트 전환)
    ("20261020-legacy-tokens-revoke", "DELETE FROM auth_token WHERE session_id IS NULL"),
    ("20261001-ohlc-zero-fill",
     "UPDATE ohlc SET open = CASE WHEN open IS NULL OR open <= 0 THEN close ELSE open END, "
     "high = GREATEST(CASE WHEN high IS NULL OR high <= 0 THEN close ELSE high END, close), "
     "low = LEAST(CASE WHEN low IS NULL OR low <= 0 THEN close ELSE low END, close) "
     "WHERE close > 0 AND (open IS NULL OR open <= 0 OR high IS NULL OR high <= 0 OR low IS NULL OR low <= 0)"),
]
# 기존 테이블에 나중에 추가한 인덱스 — 없을 때만 만든다(있으면 잠금 없이 건너뜀)
_ENSURE_INDEXES = {
    "ix_fds_decision_ref": "CREATE INDEX IF NOT EXISTS ix_fds_decision_ref ON fds_decision (ref)",
    "ix_news_published_at": "CREATE INDEX IF NOT EXISTS ix_news_published_at ON news (published_at)",
    # 뉴스 화면 기간 집계·최신순(NEWS_TS) — 쿼리의 식과 똑같은 식이어야 인덱스가 쓰인다(식 자체는 아래 NEWS_TS 한 곳)
    "ix_news_ts": None,
    # 기사 중복 방지 — 주소 원문(평균 180바이트) 대신 md5 16바이트(uuid)로 유일 인덱스(1년 290MB → 약 30MB).
    # md5 는 보안용이 아니라 중복 판정 키 — 입력이 외부 RSS 주소라 의도적 충돌을 만들 동기가 없고, 우연 충돌 확률은 10^-26 수준.
    "ux_news_url_md5": "CREATE UNIQUE INDEX IF NOT EXISTS ux_news_url_md5 ON news ((md5(url)::uuid))",
    "ix_news_stock_ticker": "CREATE INDEX IF NOT EXISTS ix_news_stock_ticker ON news_stock (ticker)",
    "ix_search_log_at": "CREATE INDEX IF NOT EXISTS ix_search_log_at ON search_log (searched_at)",
    "ix_auth_token_session": "CREATE INDEX IF NOT EXISTS ix_auth_token_session ON auth_token (session_id)",
    "ux_wallet_account_no": "CREATE UNIQUE INDEX IF NOT EXISTS ux_wallet_account_no ON wallet (account_no)",
    "ux_quiz_attempt_reward_day": "CREATE UNIQUE INDEX IF NOT EXISTS ux_quiz_attempt_reward_day ON quiz_attempt "
                                  "(user_id, reward_day) WHERE reward_day IS NOT NULL",
}
_MIGRATE_LOCK = 0x6F6F7331            # init_db 직렬화용 advisory lock 키('oos1')

# 뉴스 발행 시각 식 — 시각이 없는 예전 기사는 그 날짜 00:00(한국시간)으로 본다. 화면·추천·API 가 모두 이 식을 쓰고,
# ix_news_ts 가 같은 식으로 인덱스를 만든다(식이 한 글자라도 다르면 인덱스를 못 쓴다 → 한 곳에서만 정의).
# date → timestamp 변환과 timezone(text, timestamp) 은 IMMUTABLE 이라 식 인덱스가 된다(세션 시간대와 무관).
NEWS_TS = "COALESCE({p}published_ts, ({p}published_at::timestamp AT TIME ZONE 'Asia/Seoul'))"
_ENSURE_INDEXES["ix_news_ts"] = f"CREATE INDEX IF NOT EXISTS ix_news_ts ON news (({NEWS_TS.format(p='')}))"
# 기사 주소로 중복 판정·조회 — ON CONFLICT 대상과 '주소로 news_id 찾기'가 md5 유일 인덱스를 쓰게(주소 원문 인덱스는 없다)
NEWS_URL_CONFLICT = "ON CONFLICT ((md5(url)::uuid))"
NEWS_ID_BY_URL = "SELECT news_id FROM news WHERE md5(url)::uuid = md5(CAST(:u AS text))::uuid AND url = :u"

# 앞 열이 같은 복합 키·유일 인덱스가 이미 있어 필요 없는 인덱스 — 덮는 인덱스가 실제로 있을 때만 지운다
_REDUNDANT_INDEXES = {
    "ix_ohlc_ticker": "ohlc_pkey",
    "ix_overseas_ohlc_symbol": "overseas_ohlc_pkey",
    "ix_practice_trade_session": "uq_practice_idem",
    "ix_wallet_ledger_user": "uq_ledger_idem",
    "ix_attendance_user": "uq_attendance_day",
    "ix_friendship_owner": "uq_friendship",
    "ix_app_user_lower": "ux_app_user_lower",
}
# 표 저장 매개변수 — 자주 고치는 작은 표는 페이지에 빈자리(fillfactor)를 남겨 HOT 갱신(인덱스를 안 고치는 갱신)이 되게,
# 수명주기로 행을 지우는 큰 표는 죽은 행 2% 에서 자동 VACUUM·통계 갱신(기본 20%·10% 면 한 달 치를 지워도 몇 달 동안 안 돈다).
_ILM_AV = "autovacuum_vacuum_scale_factor=0.02,autovacuum_analyze_scale_factor=0.02"
_STORAGE = {
    "realtime_price": "fillfactor=70", "market_index": "fillfactor=70", "overseas_quote": "fillfactor=70",
    "wallet": "fillfactor=80", "app_meta": "fillfactor=80", "auth_session": "fillfactor=80",
    "practice_session": "fillfactor=80", "service_control": "fillfactor=80", "invest_position": "fillfactor=90",
    "news": _ILM_AV, "news_stock": _ILM_AV, "news_sentiment": _ILM_AV, "search_log": _ILM_AV, "access_log": _ILM_AV,
}

# ── 타입 정리(문자열 날짜·시각 → date/timestamp, 원화 double → integer) — ADR-0027 ─────────────────────
# 메타데이터(위 Table 정의)가 기준이다: DB 의 열이 아직 문자열(또는 double)인데 정의가 date/timestamp/integer 면 바꾼다.
# 표마다 ① 깨진 값 개수 세기(한 번 훑기) ② 깨진 값은 격리소(data_quality_issue)에 원값과 기본키를 남기고 ③ ALTER 한 번으로
# 모든 열을 바꿔 표를 한 번만 다시 쓰고 ④ 바뀐 뒤 값이 있는 행 수를 다시 세어 '깨진 값만큼만 비었는지' 대사, 다르면 전체 롤백
# ⑤ ANALYZE(타입이 바뀐 열의 통계는 지워진다 — 안 하면 바로 다음 쿼리부터 실행 계획이 틀린다).
# 깨진 값: 비울 수 있는 열은 NULL, NOT NULL 열은 1970-01-01(격리소에 원값), 기본키 열은 그 행을 격리 후 삭제(덧붙이기 전용 표는 중단).
_RX_DATE = r"[1-9][0-9]{3}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
_RX_TIME = r"[T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]{1,6})?)?"
_RX_OFS = r"(Z|[+-](0[0-9]|1[0-4])(:?[0-5][0-9])?)"
_TARGET = {"date": "date", "ts": "timestamp(0) without time zone", "tstz": "timestamp(0) with time zone",
           "int": "integer"}
_EPOCH = {"date": "DATE '1970-01-01'", "ts": "TIMESTAMP '1970-01-01 00:00:00'",
          "tstz": "TIMESTAMPTZ '1970-01-01 00:00:00+00'", "int": "0"}
_STRING_TYPES = ("character varying", "text", "character")
_APPEND_ONLY = {"ledger_close", "journal_entry"}              # 행 삭제를 트리거가 거부하는 표


def _retype_ok(kind, c):
    """값 c 가 kind 로 '오류 없이' 바뀌는지(참/거짓). 정규식을 통과한 값만 달의 마지막 날과 견준다 — CASE 는 평가 순서를
    지키므로 깨진 값으로 ::date 를 부르는 일이 없다(2월 30일·13월은 거짓)."""
    if kind == "int":
        return f"({c} BETWEEN -2147483648 AND 2147483647)"     # NaN·무한대·범위 밖은 거짓
    rx = f"^{_RX_DATE}" if kind == "date" else f"^{_RX_DATE}({_RX_TIME})?({_RX_OFS})?$"
    return (f"(CASE WHEN {c} ~ '{rx}' THEN extract(day FROM (substr({c}, 1, 7) || '-01')::date "
            f"+ interval '1 month' - interval '1 day') >= substr({c}, 9, 2)::int ELSE false END)")


def _retype_conv(kind, c):
    """검증을 통과한 값 → 새 타입. 시간대 표기가 있으면 그 시각 그대로(한국시간으로 환산), 없으면 한국시간으로 본다
    (clock.py 의 규칙 — 시간대 없는 저장 시각 = 한국시간). 세션 시간대와 무관하게 같은 결과."""
    if kind == "int":
        return f"round({c})::integer"
    if kind == "date":
        return f"substr({c}, 1, 10)::date"
    has_ofs = f"{c} ~ '{_RX_TIME}{_RX_OFS}$'"
    if kind == "ts":
        return f"(CASE WHEN {has_ofs} THEN ({c}::timestamptz AT TIME ZONE 'Asia/Seoul') ELSE {c}::timestamp END)"
    return f"(CASE WHEN {has_ofs} THEN {c}::timestamptz ELSE ({c}::timestamp AT TIME ZONE 'Asia/Seoul') END)"


def _kind_of(col):
    t = col.type
    if isinstance(t, Date):
        return "date"
    if isinstance(t, DateTime):
        return "tstz" if getattr(t, "timezone", False) else "ts"
    if isinstance(t, Integer) and not isinstance(t, (BigInteger, SmallInteger)):
        return "int"
    return None


def _retype_plan(conn):
    """{표: [(열, 종류, NOT NULL 여부)]} — DB 의 실제 타입과 메타데이터가 '알려진 안전한 변환'으로 다를 때만."""
    have = {}
    for r in conn.execute(text("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                               "WHERE table_schema = current_schema()")).mappings():
        have[(r["table_name"], r["column_name"])] = (r["data_type"], r["is_nullable"] == "NO")
    plan = {}
    for t in metadata.sorted_tables:
        for col in t.columns:
            kind = _kind_of(col)
            cur = have.get((t.name, col.name))
            if not kind or not cur:
                continue
            dtype, notnull = cur
            if (kind == "int" and dtype in ("double precision", "real")) or \
                    (kind != "int" and dtype in _STRING_TYPES):
                plan.setdefault(t.name, []).append((col.name, kind, notnull))
    return plan


def _pk_columns(conn, table):
    """기본키 열 목록(값이 깨지면 그 행을 둘 곳이 없다 — NULL 도 기준 시각도 안 된다)."""
    return [r[0] for r in conn.execute(text(
        "SELECT a.attname FROM pg_index x JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = ANY(x.indkey) "
        "WHERE x.indrelid = to_regclass(:t) AND x.indisprimary ORDER BY array_position(x.indkey::int2[], a.attnum)"),
        {"t": table}).all()]


def _dq_type(conn):
    """격리소 detected_at 의 지금 타입(격리소 자신도 이번에 바뀔 수 있어 쓸 때마다 본다)."""
    return conn.execute(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = "
                             "'data_quality_issue'::regclass AND attname = 'detected_at'")).scalar() or "text"


def _retype(conn):
    """플랜대로 표마다 한 번에 바꾼다. 반환: 적용 목록(로그용). 요약은 app_meta 'migration:retype' 에(JSON)."""
    import json
    import logging
    import time
    log = logging.getLogger(__name__)
    plan = _retype_plan(conn)
    if not plan:
        return []
    now_kst = "to_char(now() AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD\"T\"HH24:MI:SS')"
    order = sorted(plan, key=lambda t: (t != "data_quality_issue", t))    # 격리소 표를 먼저(그다음 격리 기록은 새 타입으로)
    summary, applied = {}, []
    for table in order:
        cols = plan[table]
        t0 = time.perf_counter()
        pk = _pk_columns(conn, table)
        keyed = set(pk)                                           # 유일 제약만 있는 열(NULL 허용)은 NULL 로 — 행을 지우지 않는다
        tcols = {c.name for c in metadata.tables[table].columns}
        stat_sql = ", ".join(f'count("{c}"), count(*) FILTER (WHERE "{c}" IS NOT NULL AND NOT {_retype_ok(k, chr(34) + c + chr(34))})'
                             for c, k, _ in cols)
        st = conn.execute(text(f'SELECT count(*), {stat_sql} FROM "{table}"')).first()
        rows_before = st[0]
        info, deleted = {}, 0
        for i, (c, k, notnull) in enumerate(cols):
            info[c] = {"to": _TARGET[k], "nonnull": st[1 + 2 * i], "invalid": st[2 + 2 * i]}
        for c, k, notnull in cols:
            bad = info[c]["invalid"]
            if not bad:
                continue
            q = f'"{c}"'
            pk_desc = ",".join(pk) or "ctid"
            pk_vals = " || ',' || ".join(f'"{x}"::text' for x in pk) if pk else "ctid::text"
            conn.execute(text(
                "INSERT INTO data_quality_issue(table_name, ticker, trade_date, rule, detail, detected_at) "
                f"SELECT :t, {'ticker' if 'ticker' in tcols else 'NULL'}, left({q}::text, 10), :r, "
                f"left(CAST(:pk AS text) || '=' || {pk_vals} || ' · 원값=' || {q}::text, 200), "
                f"CAST({now_kst} AS {_dq_type(conn)}) "
                f"FROM \"{table}\" WHERE {q} IS NOT NULL AND NOT {_retype_ok(k, q)}"),
                {"t": table, "r": f"retype:{c}"[:40], "pk": pk_desc})
            if c in keyed:                                        # 기본키 열 — 행을 둘 곳이 없다(격리소에 남기고 삭제)
                if table in _APPEND_ONLY:
                    raise RuntimeError(f"{table}.{c} 에 바꿀 수 없는 값 {bad}건 — 덧붙이기 전용 표라 자동으로 지우지 않는다. "
                                       "data_quality_issue 의 원값을 보고 직접 고친 뒤 다시 실행")
                deleted += conn.execute(text(f'DELETE FROM "{table}" WHERE {q} IS NOT NULL AND NOT {_retype_ok(k, q)}')).rowcount
                info[c]["action"] = "row_deleted"
            else:
                info[c]["action"] = "epoch" if notnull else "null"
        if table == "news":                                       # 옛 식 인덱스(문자열 연결)는 새 타입으로 다시 만들 수 없다
            conn.execute(text("DROP INDEX IF EXISTS ix_news_ts"))
        if deleted:                                               # 지운 행만큼 기준 값 개수도 다시 센다
            st = conn.execute(text(f'SELECT count(*), {stat_sql} FROM "{table}"')).first()
            for i, (c, k, _) in enumerate(cols):
                info[c]["nonnull"], info[c]["invalid"] = st[1 + 2 * i], st[2 + 2 * i]
        alters = []
        for c, k, notnull in cols:
            q = f'"{c}"'
            using = f"CASE WHEN {_retype_ok(k, q)} THEN ({_retype_conv(k, q)})::{_TARGET[k]} END"
            if notnull and c not in keyed:
                using = f"COALESCE({using}, {_EPOCH[k]})"
            alters.append(f"ALTER COLUMN {q} TYPE {_TARGET[k]} USING {using}")
        conn.execute(text(f'ALTER TABLE "{table}" ' + ", ".join(alters)))
        after = conn.execute(text('SELECT count(*), ' + ", ".join(f'count("{c}")' for c, _, _ in cols)
                                  + f' FROM "{table}"')).first()
        for i, (c, k, notnull) in enumerate(cols):
            want = info[c]["nonnull"] - (0 if (notnull and c not in keyed) else info[c]["invalid"])
            info[c]["nonnull_after"] = after[1 + i]
            if after[1 + i] != want:                              # 대사 실패 — 조용히 값을 잃지 않는다(전체 롤백)
                raise RuntimeError(f"타입 정리 대사 실패 {table}.{c}: 값 있는 행 {info[c]['nonnull']} → {after[1 + i]} "
                                   f"(깨진 값 {info[c]['invalid']})")
        conn.execute(text(f'ANALYZE "{table}"'))
        sec = round(time.perf_counter() - t0, 2)
        summary[table] = {"rows": rows_before, "rows_deleted": deleted, "sec": sec, "columns": info}
        applied.append(f"retype:{table}({len(cols)})")
        log.info("타입 정리 %s — 열 %d개, %d행, %.1f초", table, len(cols), rows_before, sec)
    stamp = conn.execute(text("SELECT to_char(now() AT TIME ZONE 'Asia/Seoul', 'YYYYMMDDHH24MISS')")).scalar()
    set_meta(f"migration:retype:{stamp}", json.dumps({"tables": summary}, ensure_ascii=False), conn)
    return applied


def _news_url_key(conn, have):
    """기사 주소 유일성: 주소 원문 B-tree(news_url_key) → md5 16바이트 유일 인덱스. 새 인덱스를 먼저 만든 뒤 옛 제약을 지운다
    (같은 트랜잭션 — 중간에 중복이 들어올 틈이 없다)."""
    out = []
    if "ux_news_url_md5" not in have:
        conn.execute(text(_ENSURE_INDEXES["ux_news_url_md5"]))
        out.append("ux_news_url_md5")
    if conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'news_url_key' "
                         "AND conrelid = to_regclass('news')")).first():
        conn.execute(text("ALTER TABLE news DROP CONSTRAINT news_url_key"))
        out.append("-news_url_key")
    return out


def _drop_redundant_indexes(conn):
    have = {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))}
    out = []
    for idx, cover in _REDUNDANT_INDEXES.items():
        if idx in have and cover in have:
            conn.execute(text(f'DROP INDEX IF EXISTS "{idx}"'))
            out.append(f"-{idx}")
    return out


def _storage_params(conn):
    """_STORAGE 의 표 저장 매개변수를 맞춘다(이미 같으면 아무것도 안 함 — 메타데이터만 바뀌고 표를 다시 쓰지 않는다)."""
    cur = {r[0]: set(r[1] or []) for r in conn.execute(text(
        "SELECT c.relname, c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r'"))}
    out = []
    for table, opts in _STORAGE.items():
        want = {o.strip() for o in opts.split(",")}
        if table in cur and not want <= cur[table]:
            conn.execute(text(f'ALTER TABLE "{table}" SET ({", ".join(sorted(want))})'))
            out.append(f"storage:{table}")
    return out


def _migrate(conn):
    cols = {}
    for r in conn.execute(text(
            "SELECT table_name, column_name, character_maximum_length AS len "
            "FROM information_schema.columns WHERE table_schema = current_schema()")).mappings():
        cols.setdefault(r["table_name"], {})[r["column_name"]] = r["len"]
    applied = []
    for table, adds in _ADD_COLUMNS.items():
        for name, typ in adds:
            if table in cols and name not in cols[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {typ}"))
                applied.append(f"{table}.{name}")
    for table, col, n in _WIDEN:
        cur = cols.get(table, {}).get(col)
        if cur is not None and cur < n:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE VARCHAR({n})"))
            applied.append(f"{table}.{col}→{n}")
    for table, col in _TO_TEXT:                      # 길이 제한이 남아 있으면(=VARCHAR(n)) TEXT 로
        if cols.get(table, {}).get(col) is not None:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE TEXT"))
            applied.append(f"{table}.{col}→text")
    done = {r[0] for r in conn.execute(text("SELECT key FROM app_meta WHERE key LIKE 'migration:%'"))}
    for name, sql in _ONCE:
        if f"migration:{name}" in done:
            continue
        conn.execute(text(sql))
        set_meta(f"migration:{name}", "applied", conn)
        applied.append(name)
    have = {r[0] for r in conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))}
    applied += _storage_params(conn)                 # fillfactor 를 먼저 — 아래 표 다시 쓰기(타입 정리)에 바로 반영
    applied += _news_url_key(conn, have)             # 옛 주소 인덱스(290MB/년)를 다시 쓰기 전에 지운다(헛 재구축 방지)
    applied += _retype(conn)
    have = {r[0]: r[1] for r in conn.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"))}
    if "ix_news_ts" in have and "Asia/Seoul" not in have["ix_news_ts"]:    # 옛 식(문자열 연결) 인덱스가 남아 있으면 교체
        conn.execute(text("DROP INDEX ix_news_ts"))
        del have["ix_news_ts"]
    for name, sql in _ENSURE_INDEXES.items():
        if name not in have:
            conn.execute(text(sql))
            applied.append(name)
    if "ix_news_ts" in applied:                     # 식 인덱스는 ANALYZE 해야 행 수 추정(실행 계획)이 맞는다
        conn.execute(text("ANALYZE news"))
    # 아이디는 대소문자만 다른 중복(Alice/alice)을 막는다 — 이미 그런 계정 쌍이 있으면 유일 인덱스 대신 일반 인덱스
    # (서비스 코드가 가입 때 따로 검사) 기존 계정은 그대로 둔다.
    if "ux_app_user_lower" not in have:
        if conn.execute(text("SELECT 1 FROM app_user GROUP BY lower(username) HAVING COUNT(*) > 1 LIMIT 1")).first():
            if "ix_app_user_lower" not in have:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_app_user_lower ON app_user (lower(username))"))
                applied.append("ix_app_user_lower")
        else:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_app_user_lower ON app_user (lower(username))"))
            applied.append("ux_app_user_lower")
    applied += _drop_redundant_indexes(conn)
    applied += _migrate_friends(conn)
    return applied


def _migrate_friends(conn):
    """구버전 '단방향 친구추가' → 새 규칙(상대 수락해야 친구)으로 1회 전환.
    서로 추가했던 사이는 그대로 친구, 한쪽만 추가한 관계는 '대기 중 요청'으로 옮긴다(상대가 수락/거절).
    전환할 행이 없으면 아무것도 쓰지 않는다(반복 실행 무해)."""
    one_way = ("FROM friendship f WHERE NOT EXISTS (SELECT 1 FROM friendship r "
               "WHERE r.owner = f.friend AND r.friend = f.owner)")
    if not conn.execute(text("SELECT 1 " + one_way + " LIMIT 1")).first():
        return []
    moved = conn.execute(text(
        "INSERT INTO friend_request(sender, receiver, status, created_at) "
        "SELECT f.owner, f.friend, 'pending', f.created_at " + one_way +
        " AND NOT EXISTS (SELECT 1 FROM friend_request q WHERE q.sender = f.owner "
        "AND q.receiver = f.friend AND q.status = 'pending') "
        "ON CONFLICT DO NOTHING")).rowcount                  # 두 프로세스가 동시에 init_db 해도 안전
    conn.execute(text("DELETE " + one_way))
    return [f"friendship→friend_request({moved})"]


def _make_engine(url):
    """PostgreSQL 전용 엔진 — 커넥션 풀 + pre_ping(끊긴 커넥션 자동 복구).
    실시간 수집기(쓰기)·API(읽기)·배치가 동시에 붙으므로 풀로 관리."""
    return create_engine(url, future=True, pool_pre_ping=True, pool_use_lifo=True,   # LIFO: 방금 쓴 연결부터 재사용
                         pool_size=10, max_overflow=20, pool_recycle=1800,
                         pool_timeout=settings.DB_POOL_TIMEOUT_S)        # 풀이 다 차면 짧게 기다리고 빨리 실패(503)


_engine = _make_engine(settings.DATABASE_URL)


# ── 연결 설정(물리 연결이 처음 생길 때 한 번) ─────────────────────────────────────────────
#  · 시간대 Asia/Seoul 고정 — 서버 설정(VM 은 UTC, 개발 PC 는 KST)과 무관하게 같은 결과. 시간대 없는 시각 문자열을 timestamptz 와
#    비교·저장할 때 한국시간으로 해석되고(clock.py 규칙과 같다), timestamptz 는 한국시간 datetime 으로 읽힌다(isoformat = '+09:00').
#  · 날짜 표기 ISO · 느린 쿼리·멈춘 트랜잭션 한도 · TOAST 압축 lz4(지원할 때 — pglz 보다 압축·해제가 몇 배 빠르다).
#  · 읽기 로더: date → 'YYYY-MM-DD', timestamp(시간대 없음) → 'YYYY-MM-DDTHH:MM:SS' 문자열 — 저장 타입을 정리해도 서비스 계층이
#    받는 값의 형식은 예전 문자열 그대로(ADR-0027). analytics 스키마의 date 열도 같은 문자열로 읽힌다.
try:
    from psycopg.adapt import Loader as _PgLoader

    class _DateText(_PgLoader):
        def load(self, data):
            return bytes(data).decode("ascii")

    class _KstStampText(_PgLoader):
        def load(self, data):
            s = bytes(data).decode("ascii")
            return s[:10] + "T" + s[11:] if len(s) > 10 and s[10] == " " else s
except ImportError:                                   # pragma: no cover — psycopg 3 가 아닌 드라이버
    _DateText = _KstStampText = None

_LZ4 = {"ok": None}                                   # 서버가 lz4 를 지원하는지(첫 연결에서 한 번 확인)


def setup_connection(dbapi_conn):
    """psycopg 연결 하나에 위 설정을 건다(풀 밖에서 직접 만든 연결 — 도구·시험 — 도 같은 규칙을 쓰게 공개)."""
    st, it = int(settings.DB_STATEMENT_TIMEOUT_MS or 0), int(settings.DB_IDLE_TX_TIMEOUT_MS or 0)
    cur = dbapi_conn.cursor()
    try:
        cur.execute(f"SET TIME ZONE '{settings.DB_TIMEZONE}'")
        cur.execute("SET datestyle TO 'ISO, YMD'")
        if st > 0:
            cur.execute(f"SET statement_timeout = {st}")
        if it > 0:
            cur.execute(f"SET idle_in_transaction_session_timeout = {it}")
        dbapi_conn.commit()
        if _LZ4["ok"] is not False:                   # 따로 커밋 — 실패해도 위 설정은 남는다
            try:
                cur.execute("SET default_toast_compression = 'lz4'")
                dbapi_conn.commit()
                _LZ4["ok"] = True
            except Exception:                         # noqa: BLE001 — lz4 없이 빌드된 서버: 기본(pglz) 그대로
                dbapi_conn.rollback()
                _LZ4["ok"] = False
    finally:
        cur.close()
    adapters = getattr(dbapi_conn, "adapters", None)
    if adapters is not None and _DateText is not None:
        adapters.register_loader("date", _DateText)
        adapters.register_loader("timestamp", _KstStampText)


@event.listens_for(Pool, "connect")
def _session_setup(dbapi_conn, _record):
    setup_connection(dbapi_conn)


# ── 행 수준 보안(RLS) 문맥 — 커넥션을 풀에서 꺼낼 때마다 세션 변수를 지금 문맥으로 맞춘다(app/security/rls.py) ──
#   요청 안 = app.rls 'on' + app.user=주체, 요청 밖(배치·스케줄러) = 'off'. 꺼낼 때마다 덮어쓰므로 이전 요청의 값이 남지 않는다.
#   바로 커밋해 트랜잭션 밖(idle)으로 돌려놓는다 — 그다음 격리 수준 변경(스냅샷 내보내기 등)이 막히지 않게.
#   성능: 물리 연결마다 마지막으로 맞춘 값을 기억해 같으면 건너뛴다(왕복 2번 절약). 풀은 LIFO 라 한 요청의 연속 쿼리가
#   같은 연결을 다시 쓰므로 대부분 건너뛴다. 값이 다를 때만 설정 — 이전 요청의 값이 남는 일은 여전히 없다.
#   (record.info 는 물리 연결이 다시 맺어지면 비워진다 → 새 연결은 반드시 설정. PgBouncer 는 session 모드로만 — 세션 변수 사용)
@event.listens_for(Pool, "checkout")
def _apply_rls_context(dbapi_conn, record, _proxy):
    from app.security import rls
    want = rls.current()
    if record.info.get("rls") == want:
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("SELECT set_config('app.rls', %s, false), set_config('app.user', %s, false)", want)
    finally:
        cur.close()
    dbapi_conn.commit()
    record.info["rls"] = want



def get_engine():
    return _engine


def reset_engine(url):
    """테스트에서 임시 DB로 갈아끼울 때 사용."""
    global _engine
    _engine = _make_engine(url)
    return _engine


def init_db():
    """스키마 생성(존재하면 통과) + 기존 DB 무손실 마이그레이션. 여러 번 실행해도 안전(멱등).
    여러 프로세스(API·스케줄러·실시간)가 동시에 켜져도 advisory lock 으로 한 번에 하나씩만 실행.
    PostgreSQL 서버가 떠 있어야 함(docker compose up -d db)."""
    from app import migrations
    # DB 권한 분리(deploy/sql/roles.sql) 시 스키마 변경은 소유자 계정으로만 — MIGRATION_DATABASE_URL 이 있으면 그 계정
    eng = _make_engine(settings.MIGRATION_DATABASE_URL) if settings.MIGRATION_DATABASE_URL else _engine
    try:
        with eng.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MIGRATE_LOCK})
            # 이 트랜잭션만: 표 다시 쓰기(타입 정리)가 API 의 쿼리 시간 한도에 끊기지 않게, 다른 연결의 잠금을 끝없이 기다리지
            # 않게(1분 — 서비스를 멈추지 않고 켠 경우), 인덱스 재구축은 메모리를 넉넉히
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL lock_timeout = '60s'"))
            conn.execute(text("SET LOCAL maintenance_work_mem = '256MB'"))
            metadata.create_all(conn)
            applied = _migrate(conn)
            applied += migrations.apply_all(conn)        # 분석 계층(analytics 스키마) — sql/migrations/V*.sql
    finally:
        if eng is not _engine:
            eng.dispose()
    try:                                                 # 복식부기 도입 전 지갑 잔액을 한 번 분개(멱등)
        from app.services import ledger
        n = ledger.opening_balances()
        if n:
            applied.append(f"ledger_opening({n})")
    except Exception as e:                               # noqa: BLE001 — 원장 보강 실패가 서버 시작을 막지 않게
        import logging
        logging.getLogger(__name__).warning("원장 시작 잔액 분개 실패: %s", e)
    try:                                                 # 초기 데이터(멱등) — 양봉이 주간 추천 팀 백테스트 247주 · 모의투자 시즌 파일
        from app.services import vmarket, yangbong
        n = yangbong.seed()
        if n:
            applied.append(f"yangbong_seed({n})")
        v = vmarket.seed()
        if v["new"] or v["replaced"]:
            applied.append(f"vmarket_seed(new {v['new']}, replaced {v['replaced']})")
    except Exception as e:                               # noqa: BLE001 — 초기 데이터 실패가 서버 시작을 막지 않게
        import logging
        logging.getLogger(__name__).warning("초기 데이터(양봉이·모의투자) 적재 실패: %s", e)
    return applied



def idem_key(prefix, key):
    """클라이언트가 보낸 멱등키(Idempotency-Key) → 저장용 키(80자 이하, 안전한 문자만).
    짧고 평범한 키는 그대로(prefix:키), 길거나 특수문자가 섞이면 SHA-256 앞 40자로 줄인다."""
    import hashlib
    key = str(key)
    if len(key) <= 48 and all(c.isascii() and (c.isalnum() or c in "-_:.") for c in key):
        return f"{prefix}:{key}"
    return f"{prefix}:h{hashlib.sha256(key.encode('utf-8', 'surrogatepass')).hexdigest()[:40]}"


# ── 앱 메타(키-값) 헬퍼 ──────────────────────────────────
def get_meta(key, default=None):
    try:
        rows = query("SELECT value FROM app_meta WHERE key=:k", {"k": key})
    except Exception:                                 # 테이블 미생성(구버전 DB) → 기본값
        return default
    return rows[0]["value"] if rows else default


def set_meta(key, value, conn=None):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    sql = text("INSERT INTO app_meta(key, value, updated_at) VALUES(:k, :v, :t) "
               "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t")
    params = {"k": key, "v": None if value is None else str(value), "t": now}
    if conn is not None:
        conn.execute(sql, params)
    else:
        with _engine.begin() as c:
            c.execute(sql, params)


def seed_stocks(stocks: dict):
    with _engine.begin() as conn:
        for ticker, name in stocks.items():
            conn.execute(
                text("INSERT INTO stock(ticker, name) VALUES (:t, :n) "
                     "ON CONFLICT(ticker) DO NOTHING"),
                {"t": ticker, "n": name})


def latest_per_ticker(table, cols, tickers=None, n=1, where="", params=None, asc=False):
    """종목별 최신 n행(trade_date 내림차순) — PK(ticker, trade_date) 인덱스로 종목마다 n행만 읽는다.
    예전: DISTINCT ON / ROW_NUMBER() OVER 로 표 전체(수만 행)를 정렬 → 거의 모든 화면이 매 요청 20~40ms(행이 쌓일수록 증가).
    tickers=None 이면 그 표에 있는 전 종목(느슨한 인덱스 스캔으로 종목 목록을 구함). where 는 'trade_date <= :a' 같은
    추가 조건(내부 상수 문자열만 — 사용자 입력 금지). 반환: [{ticker, cols..., rn}] (rn=1 이 최신, asc=True 면 종목·날짜 오름차순)."""
    params = dict(params or {})
    cond = f" AND {where}" if where else ""
    inner = (f"SELECT {cols}, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn FROM ("
             f"SELECT {cols} FROM {table} x WHERE x.ticker = k.ticker{cond} ORDER BY trade_date DESC LIMIT :_n) y")
    if tickers is None:
        head = (f"WITH RECURSIVE k(ticker) AS ((SELECT ticker FROM {table} ORDER BY ticker LIMIT 1) UNION ALL "
                f"SELECT (SELECT t.ticker FROM {table} t WHERE t.ticker > k.ticker ORDER BY t.ticker LIMIT 1) "
                f"FROM k WHERE k.ticker IS NOT NULL) SELECT k.ticker, b.* FROM k CROSS JOIN LATERAL ({inner}) b "
                f"WHERE k.ticker IS NOT NULL")
    else:
        head = (f"SELECT k.ticker, b.* FROM unnest(CAST(:_ts AS varchar[])) AS k(ticker) "
                f"CROSS JOIN LATERAL ({inner}) b")
        params["_ts"] = sorted(set(tickers))
    params["_n"] = int(n)
    order = " ORDER BY ticker, trade_date" if asc else ""
    return query(f"SELECT * FROM ({head}) z{order}", params)


def query(sql, params=None):
    """SELECT → dict 리스트. (행마다 RowMapping 을 거치지 않고 열 이름과 튜플을 바로 묶는다 —
    추천 엔진처럼 수만~수십만 행을 읽을 때 변환 비용이 절반 이하로)"""
    with _engine.connect() as conn:
        res = conn.execute(text(sql), params or {})
        keys = list(res.keys())
        return [dict(zip(keys, row)) for row in res.fetchall()]


def execute(sql, params=None):
    """쓰기 1건(트랜잭션 자동 커밋) → 영향받은 행 수."""
    with _engine.begin() as conn:
        return conn.execute(text(sql), params or {}).rowcount


def write_query(sql, params=None):
    """쓰기 + 결과 행(INSERT … RETURNING 등) — 커밋까지 한 번에(query 는 읽기 전용 연결이라 커밋하지 않는다)."""
    with _engine.begin() as conn:
        res = conn.execute(text(sql), params or {})
        keys = list(res.keys())
        return [dict(zip(keys, row)) for row in res.fetchall()]


@contextmanager
def transaction():
    """여러 쓰기를 한 트랜잭션으로 — 실패 시 전체 롤백(원자성)."""
    with _engine.begin() as conn:
        yield conn


def tx_cache(conn) -> dict:
    """트랜잭션 하나 동안만 쓰는 메모 — 같은 트랜잭션 안에서 같은 확인·조회를 되풀이하지 않게(잡은 잠금·만든 지갑·계좌번호).
    Connection 객체(트랜잭션마다 새로 생김)에 붙이므로 트랜잭션이 끝나면 사라진다 — 다른 트랜잭션에 새지 않는다."""
    c = getattr(conn, "_oos_tx_cache", None)
    if c is None:
        c = {}
        conn._oos_tx_cache = c
    return c


@contextmanager
def advisory_lock(name):
    """PostgreSQL 세션 advisory lock(비차단) — 같은 배치가 동시에 두 번 돌지 않게.
    (예: 스케줄러의 23:00 run.py real 과 수동 실행이 겹치면 늦게 온 쪽은 건너뜀)
    with advisory_lock("real") as got:  got=False 면 이미 다른 프로세스가 실행 중."""
    import zlib
    key = zlib.crc32(("oos:" + name).encode()) & 0x7FFFFFFF
    conn = _engine.connect()
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
    finally:
        conn.close()
