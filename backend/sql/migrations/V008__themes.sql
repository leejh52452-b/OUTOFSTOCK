-- V008 테마(네이버 증권 '테마'처럼 업종을 가로지르는 종목 묶음) — 근거 결합 분류 · 시점(PIT) 소속 · 성과 함수 · 검증 라벨.
--
-- 왜 직접 만드나: 네이버 테마 목록은 네이버가 손으로 관리하는 분류라 공식 API 가 없다(자동 수집은 약관 문제).
-- 대신 공식 자료에서 근거를 모아 소속을 정한다 — 테마형 ETF 구성종목(한국투자증권 API), 사업보고서 '사업의 내용'
-- (OpenDART 공시서류원본파일), 업종코드(KSIC, DART 기업개황), 뉴스 제목(보조). 근거마다 점수를 주고 합이 기준을 넘으면
-- 소속. 소속에는 근거와 '알 수 있게 된 날'(valid_from)을 남겨, 화면에서 근거를 보여 주고 과거 계산에 미래 정보를 쓰지 않는다.

-- 업종코드를 처음 받은 날 — 테마 근거의 '알 수 있게 된 날'(이미 받은 코드는 이 마이그레이션 날부터로 본다: 보수적)
ALTER TABLE analytics.security_master ADD COLUMN IF NOT EXISTS ksic_seen_at date;
UPDATE analytics.security_master SET ksic_seen_at = CURRENT_DATE WHERE ksic_code IS NOT NULL AND ksic_seen_at IS NULL;

-- 테마 정의(사전 app/data/themes.json 을 그대로 옮김 — 무엇으로 분류했는지 재현)
CREATE TABLE IF NOT EXISTS analytics.theme (
    theme_id     varchar(30) PRIMARY KEY,
    name         varchar(40) NOT NULL,
    description  text,
    sort         smallint    NOT NULL DEFAULT 0,
    definition   jsonb       NOT NULL,          -- keywords · etf · ksic
    dict_version varchar(20) NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ETF 목록(KRX ETF 일별매매정보) — 이름으로 테마형 ETF 를 고른다(레버리지·인버스·해외·채권 제외)
CREATE TABLE IF NOT EXISTS analytics.etf_master (
    etf_code   varchar(12)  PRIMARY KEY,
    name       varchar(120) NOT NULL,
    index_name varchar(200),                     -- 기초지수명
    aum        bigint,                           -- 순자산총액(원)
    theme_ids  text[]       NOT NULL DEFAULT '{}',
    as_of      date,
    source     varchar(10)  NOT NULL,            -- krx | manual | gen
    updated_at timestamptz  NOT NULL DEFAULT now()
);

-- ETF 구성종목 스냅샷(한국투자증권 'ETF 구성종목시세') — 날짜별로 남겨 '그때의 구성'을 안다
CREATE TABLE IF NOT EXISTS analytics.etf_holding (
    etf_code varchar(12) NOT NULL,
    as_of    date        NOT NULL,
    ticker   varchar(12) NOT NULL,
    name     varchar(100),
    weight   real,                               -- 구성 비중(%)
    source   varchar(10) NOT NULL,
    PRIMARY KEY (etf_code, as_of, ticker)
);
CREATE INDEX IF NOT EXISTS ix_etf_holding_ticker ON analytics.etf_holding (ticker, as_of DESC);

-- 사업보고서 '사업의 내용'(공시서류원본파일에서 그 구간만) — 사전을 고쳐도 다시 내려받지 않고 다시 분류한다
CREATE TABLE IF NOT EXISTS analytics.business_text (
    ticker       varchar(12) NOT NULL,
    fiscal_year  smallint    NOT NULL,
    rcept_no     varchar(14) NOT NULL,
    disclosed_at date,                           -- 공시일(이 날부터 알 수 있었다)
    chars        integer     NOT NULL,
    body         text        NOT NULL,
    extracted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, fiscal_year)
);

-- 근거(테마 × 종목 × 원천) — 화면의 '왜 이 테마인가'
CREATE TABLE IF NOT EXISTS analytics.theme_evidence (
    theme_id   varchar(30) NOT NULL,
    ticker     varchar(12) NOT NULL,
    source     varchar(10) NOT NULL CHECK (source IN ('etf', 'dart', 'ksic', 'news', 'gen')),
    score      real        NOT NULL,
    detail     text,                             -- ETF 이름·비중 / 공시 문장 / 업종코드 / 기사 제목
    known_at   date,                             -- 이 근거를 알 수 있게 된 날(PIT)
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (theme_id, ticker, source)
);
CREATE INDEX IF NOT EXISTS ix_theme_evidence_ticker ON analytics.theme_evidence (ticker);

-- 소속(이력형 SCD2) — valid_to 가 NULL 이면 지금 소속. 빠지면 그날로 닫고, 다시 들어오면 새 행.
CREATE TABLE IF NOT EXISTS analytics.theme_member (
    theme_id   varchar(30) NOT NULL,
    ticker     varchar(12) NOT NULL,
    valid_from date        NOT NULL,
    valid_to   date,
    score      real        NOT NULL,
    sources    text[]      NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (theme_id, ticker, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_theme_member_current ON analytics.theme_member (theme_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_theme_member_ticker ON analytics.theme_member (ticker) WHERE valid_to IS NULL;

-- 사람이 단 정답(정밀도 측정용) — CSV 로 내보내 표시한 뒤 가져온다(run.py theme-label-export/import)
CREATE TABLE IF NOT EXISTS analytics.theme_label (
    theme_id   varchar(30) NOT NULL,
    ticker     varchar(12) NOT NULL,
    is_member  boolean     NOT NULL,
    note       text,
    labeled_by varchar(50),
    labeled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (theme_id, ticker)
);

-- ── 테마별 성과 — 단순평균·시가총액 가중·중앙값을 함께 ─────────────────────────
-- 단순평균(네이버 테마 등락률 방식)과 시총 가중의 차이가 크면 소형주가 테마를 끌고 간다는 뜻(테마주 과열 신호).
-- 중앙값은 '보통의 구성 종목'(급등 한두 종목에 끌리지 않음). 시총 가중은 기간 초 시총으로(업종 성과와 같은 방식).
-- members = 지금 소속(시세 있는 종목), priced = 그 기간 수익률이 계산되는 종목(신규 상장은 1년 수익률이 없다).
-- 주도주 = 그 기간 수익률 1위, 대표 종목 = 시가총액 1위. 투자 유의 종목 수를 함께 준다.
CREATE OR REPLACE FUNCTION analytics.theme_performance(p_period text)
RETURNS TABLE (theme_id text, name text, sort smallint, members bigint, priced bigint, ret_eq numeric,
               ret_cap numeric, ret_median numeric, value_sum numeric, mktcap_sum numeric, up_count bigint,
               down_count bigint, risky_count bigint, leader_ticker text, leader_name text, leader_ret numeric,
               top_ticker text, top_name text)
LANGUAGE sql STABLE AS $$
    WITH s AS (
        SELECT tm.theme_id, v.ticker, v.name, v.mktcap::numeric AS mktcap, v.risky,
               (CASE p_period WHEN '1d' THEN v.ret_1d WHEN '1w' THEN v.ret_1w WHEN '1m' THEN v.ret_1m
                              WHEN '3m' THEN v.ret_3m WHEN '1y' THEN v.ret_1y END)::numeric AS r,
               (CASE p_period WHEN '1d' THEN v.value_1d WHEN '1w' THEN v.value_1w WHEN '1m' THEN v.value_1m
                              WHEN '3m' THEN v.value_3m WHEN '1y' THEN v.value_1y END)::numeric AS val
        FROM analytics.theme_member tm
        JOIN analytics.mv_stock_snapshot v ON v.ticker = tm.ticker
        WHERE tm.valid_to IS NULL
    ), w AS (
        SELECT s.*, (s.r IS NOT NULL AND s.r > -100) AS ok,
               CASE WHEN s.r IS NOT NULL AND s.r > -100 THEN s.mktcap / (1 + s.r / 100) END AS cap0
        FROM s
    )
    SELECT t.theme_id::text, t.name::text, t.sort, count(*), count(*) FILTER (WHERE w.ok),
           round(avg(w.r) FILTER (WHERE w.ok), 2),
           round(sum(w.cap0 * w.r) FILTER (WHERE w.ok) / NULLIF(sum(w.cap0) FILTER (WHERE w.ok), 0), 2),
           round((percentile_cont(0.5) WITHIN GROUP (ORDER BY w.r::float8) FILTER (WHERE w.ok))::numeric, 2),
           sum(COALESCE(w.val, 0)), sum(w.mktcap),
           count(*) FILTER (WHERE w.ok AND w.r > 0), count(*) FILTER (WHERE w.ok AND w.r < 0),
           count(*) FILTER (WHERE w.risky),
           (array_agg(w.ticker ORDER BY w.r DESC, w.mktcap DESC NULLS LAST) FILTER (WHERE w.ok))[1]::text,
           (array_agg(w.name ORDER BY w.r DESC, w.mktcap DESC NULLS LAST) FILTER (WHERE w.ok))[1]::text,
           round(max(w.r) FILTER (WHERE w.ok), 2),
           (array_agg(w.ticker ORDER BY w.mktcap DESC NULLS LAST))[1]::text,
           (array_agg(w.name ORDER BY w.mktcap DESC NULLS LAST))[1]::text
    FROM analytics.theme t JOIN w ON w.theme_id = t.theme_id
    GROUP BY t.theme_id, t.name, t.sort
$$;

-- ── DB 계정이 분리돼 있으면 새 객체 권한(roles.sql 을 다시 돌리지 않아도) ──────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_ro') THEN
        EXECUTE 'GRANT SELECT ON analytics.theme, analytics.etf_master, analytics.etf_holding, analytics.business_text, '
                'analytics.theme_evidence, analytics.theme_member, analytics.theme_label TO oos_ro';
        EXECUTE 'GRANT EXECUTE ON FUNCTION analytics.theme_performance(text) TO oos_ro';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON analytics.theme, analytics.etf_master, analytics.etf_holding, '
                'analytics.business_text, analytics.theme_evidence, analytics.theme_member, analytics.theme_label TO oos_app';
    END IF;
END $$;
