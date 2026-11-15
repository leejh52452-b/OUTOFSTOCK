-- V006 모형 계층 — 피처 표(월말 시장 피처)·모형 실행 이력·신용위험 점수.
--
-- 월말 시장 피처(feature store): 신용위험 모형은 '공시일 시점'의 시가총액·1년 변동성·1년 수익률·고점 대비 하락을
-- 쓴다. 관측마다 670만 행을 다시 훑지 않도록 월말 단위로 한 번 계산해 둔다(윈도 함수 2회 — 배치에서 갱신).
CREATE TABLE IF NOT EXISTS analytics.market_monthly (
    ticker      varchar(12) NOT NULL,
    month_end   date        NOT NULL,          -- 그 달 마지막 거래일
    close       integer     NOT NULL,
    mktcap      bigint,
    ret_1y      real,                          -- 1년(250거래일) 수익률
    vol_1y      real,                          -- 연율 변동성(일간 로그수익률 표준편차 × √252)
    dd_1y       real,                          -- 1년 고점 대비 현재 하락률(≤ 0)
    PRIMARY KEY (ticker, month_end)
);

-- 모형 실행 이력(모형 위험 관리: 무엇으로·언제·어떤 성능이었나)
CREATE TABLE IF NOT EXISTS analytics.model_run (
    run_id      bigserial   PRIMARY KEY,
    model       varchar(30) NOT NULL,          -- credit_risk | return_5d
    version     varchar(20) NOT NULL,
    data_source varchar(10) NOT NULL CHECK (data_source IN ('real', 'synthetic')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    params      jsonb,
    metrics     jsonb,
    notes       text
);
CREATE INDEX IF NOT EXISTS ix_model_run_model ON analytics.model_run (model, run_id DESC);

-- 기업 신용위험 점수(공시일 기준) — 화면은 종목별 최신 1건만, 순위표로는 공개하지 않는다
CREATE TABLE IF NOT EXISTS analytics.credit_score (
    ticker        varchar(12) NOT NULL,
    as_of         date        NOT NULL,        -- 재무 공시일(이 날 알 수 있었던 정보로 계산)
    fiscal_year   smallint    NOT NULL,
    model_version varchar(20) NOT NULL,
    pd            double precision NOT NULL,   -- 12개월 내 부실 징후 확률(보정 후)
    grade         varchar(2)  NOT NULL,
    zscore        real,                        -- Altman Z''(비교용)
    merton_pd     double precision,            -- Merton 부도확률(비교용 — 매우 작은 값이 나와 real 은 언더플로)
    factors       jsonb,                       -- 위험을 올린 상위 요인(로지스틱 기여도)
    PRIMARY KEY (ticker, as_of, model_version)
);
CREATE INDEX IF NOT EXISTS ix_credit_score_ticker ON analytics.credit_score (ticker, as_of DESC);

-- 월말 시장 피처 갱신 — 소유자 권한 함수(DB 권한 분리 시 앱 계정에서 호출). 최근 months 개월만 다시 계산.
CREATE OR REPLACE FUNCTION analytics.refresh_market_monthly(months integer DEFAULT 1200) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = analytics, pg_temp AS $$
DECLARE
    since date := (SELECT max(trade_date) FROM analytics.price_history) - (months * 31 + 400);
    n bigint;
BEGIN
    DELETE FROM analytics.market_monthly WHERE month_end >= since + 400;
    INSERT INTO analytics.market_monthly(ticker, month_end, close, mktcap, ret_1y, vol_1y, dd_1y)
    SELECT ticker, trade_date, close, mktcap,
           CASE WHEN n_obs >= 200 THEN (close::float8 / close_1y - 1) END,
           CASE WHEN n_obs >= 200 THEN sd * sqrt(252) END,
           (close::float8 / peak - 1)
    FROM (
        SELECT ticker, trade_date, close, mktcap,
               stddev_samp(lr) OVER w AS sd, max(close) OVER w AS peak, first_value(close) OVER w AS close_1y,
               count(*) OVER w AS n_obs,
               row_number() OVER (PARTITION BY ticker, date_trunc('month', trade_date) ORDER BY trade_date DESC) AS k
        FROM (
            SELECT ticker, trade_date, close, mktcap,
                   ln(close::float8 / NULLIF(lag(close) OVER (PARTITION BY ticker ORDER BY trade_date), 0)) AS lr
            FROM analytics.price_history WHERE trade_date >= since AND NOT halted
        ) r
        WINDOW w AS (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 249 PRECEDING AND CURRENT ROW)
    ) s
    WHERE k = 1 AND trade_date >= since + 400
    ON CONFLICT (ticker, month_end) DO UPDATE SET close = EXCLUDED.close, mktcap = EXCLUDED.mktcap,
        ret_1y = EXCLUDED.ret_1y, vol_1y = EXCLUDED.vol_1y, dd_1y = EXCLUDED.dd_1y;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION analytics.refresh_market_monthly(integer) FROM PUBLIC;
