-- V001 분석 계층(데이터 플랫폼) 핵심 — 전 종목 × 10년 일별 시세를 담는 '분석용' 스키마.
--
-- 왜 별도 스키마인가(ADR-0002): 화면용 표(public.ohlc 등)는 유니버스 150종목만 빠르게 읽는 '서빙 계층'이고,
-- 여기는 전 종목(약 2,700) × 10년(약 670만 행)을 쌓아 SQL 로 분석하는 '분석 계층'이다.
-- 둘을 섞으면 화면 쿼리가 대용량 표를 건드리고, 분석 쿼리가 서비스 잠금과 다툰다.
--
-- 규칙: 이미 적용된 마이그레이션 파일은 고치지 않는다(체크섬 검증). 바꿀 것은 새 파일(V00N)로.

CREATE SCHEMA IF NOT EXISTS analytics;

-- ── 종목 마스터 ─────────────────────────────────────────────
-- 최신 상태 1행(SCD 1형) + 처음/마지막으로 본 거래일. 상장폐지 종목도 지우지 않는다(생존 편향 방지).
CREATE TABLE IF NOT EXISTS analytics.security_master (
    ticker        varchar(12)  PRIMARY KEY,
    name          varchar(100) NOT NULL,
    market        varchar(10)  NOT NULL,                 -- KOSPI | KOSDAQ
    sect_tp       varchar(40),                           -- KRX 소속부(관리종목(소속부없음) 등)
    corp_code     varchar(8),                            -- DART 고유번호
    ksic_code     varchar(10),                           -- DART 업종코드(통계청 표준산업분류)
    sector        varchar(40),                           -- 투자자용 업종(analytics.ksic_sector 매핑)
    listed_shares bigint,
    first_seen    date         NOT NULL,
    last_seen     date         NOT NULL,
    updated_at    timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE analytics.security_master IS '종목 마스터 — 상장폐지 종목도 보존(생존 편향 방지)';

-- ── 일별 시세(연도별 범위 파티션) ─────────────────────────────
-- 파티션: 연도 단위. 최근 연도만 읽는 쿼리는 나머지 파티션을 건너뛴다(partition pruning).
-- 오래된 연도는 파티션째 분리·보관할 수 있다(DETACH PARTITION — 대량 DELETE 불필요).
-- 가격은 원 단위 정수(integer 4바이트) — 670만 행에서 double(8바이트)보다 표가 작다.
CREATE TABLE IF NOT EXISTS analytics.price_history (
    ticker      varchar(12) NOT NULL REFERENCES analytics.security_master(ticker),
    trade_date  date        NOT NULL,
    mkt         char(1)     NOT NULL,                    -- K=코스피 Q=코스닥(그날 기준 — 이전상장 추적)
    open        integer,
    high        integer,
    low         integer,
    close       integer     NOT NULL CHECK (close > 0),
    volume      bigint      NOT NULL DEFAULT 0 CHECK (volume >= 0),
    value       bigint      NOT NULL DEFAULT 0 CHECK (value >= 0),   -- 거래대금(원)
    mktcap      bigint,
    change_rate real,                                     -- KRX 등락률(%) — 권리락·액면분할 반영값
    halted      boolean     NOT NULL DEFAULT false,       -- 시가·고가·저가 0 + 거래량 0 = 매매거래정지 추정
    PRIMARY KEY (ticker, trade_date)
) PARTITION BY RANGE (trade_date);
COMMENT ON TABLE analytics.price_history IS '전 종목 일별 시세 — 연도별 범위 파티션';

-- 연도 파티션을 필요할 때 만든다(적재기가 넣기 전에 호출). DEFAULT 파티션은 두지 않는다 —
-- DEFAULT 에 행이 들어간 뒤에는 그 연도 파티션을 새로 만들 수 없어서(제약 위반) 운영이 꼬인다.
CREATE OR REPLACE FUNCTION analytics.ensure_price_partition(y integer) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    part text := format('price_history_%s', y);
BEGIN
    IF to_regclass('analytics.' || part) IS NULL THEN
        EXECUTE format('CREATE TABLE analytics.%I PARTITION OF analytics.price_history '
                       'FOR VALUES FROM (%L) TO (%L)', part, make_date(y, 1, 1), make_date(y + 1, 1, 1));
    END IF;
    RETURN part;
END $$;

SELECT analytics.ensure_price_partition(y) FROM generate_series(2014, 2027) AS y;

-- ── 적재 이력(데이터 계보) ──────────────────────────────────
-- 적재 단위(데이터셋 × 키) 마다 마지막 시도 결과. 재개(resume)·누락 확인·재적재 판단의 근거.
CREATE TABLE IF NOT EXISTS analytics.load_log (
    dataset      varchar(40)  NOT NULL,                  -- krx_daily | investor_market | ...
    part_key     varchar(40)  NOT NULL,                  -- 예: 2026-10-08:K
    status       varchar(10)  NOT NULL CHECK (status IN ('ok', 'empty', 'failed', 'running')),
    rows_in      integer,
    rows_upserted integer,
    checksum     varchar(64),                            -- 받은 행 내용 해시 — 같은 키 재적재 때 변경 감지
    source       varchar(20),
    message      text,
    attempts     integer      NOT NULL DEFAULT 1,
    started_at   timestamptz  NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    PRIMARY KEY (dataset, part_key)
);
COMMENT ON TABLE analytics.load_log IS '적재 단위별 마지막 결과(재개·누락 확인·데이터 계보)';
