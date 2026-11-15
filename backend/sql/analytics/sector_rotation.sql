-- name: sector_rotation
-- title: 업종 순환(월별 업종 수익률 순위)
-- description: 최근 N개월 월별 업종 수익률(시가총액 가중, 수정주가 로그수익률 합)과 그달의 순위 — RANK() OVER (PARTITION BY 월)
-- params: months:int=6:1:24
-- tags: 윈도 함수(RANK), 업종 집계, 수정주가
WITH last AS (                               -- 기준일·기간 경계는 한 번만 계산(행마다 함수를 부르지 않게)
    SELECT d, (date_trunc('month', d) - make_interval(months => :months - 1))::date AS m0
    FROM (SELECT analytics.latest_trade_date() AS d) x
), a AS (                                    -- 한 달 앞부터 읽어 그달 첫 거래일의 수익률(전월 말 대비)까지 포함
    SELECT ticker, trade_date, mktcap, lr
    FROM analytics.adj_prices((SELECT m0 FROM last) - 31, (SELECT d FROM last))
), m AS (
    SELECT ticker, date_trunc('month', trade_date)::date AS mon,
           exp(sum(lr)) - 1 AS r,                            -- 월 수익률 = 그달 수정 로그수익률 합
           (array_agg(mktcap ORDER BY trade_date))[1] AS cap0
    FROM a
    WHERE trade_date >= (SELECT m0 FROM last) AND lr IS NOT NULL
    GROUP BY ticker, date_trunc('month', trade_date)
), s AS (
    SELECT m.mon, sm.sector,
           round((sum(m.cap0 * m.r) / NULLIF(sum(m.cap0), 0) * 100)::numeric, 2) AS ret,
           count(*) AS members
    FROM m JOIN analytics.security_master sm ON sm.ticker = m.ticker
    WHERE sm.sector IS NOT NULL AND m.cap0 > 0
    GROUP BY m.mon, sm.sector
)
SELECT mon, sector, ret, members, rank() OVER (PARTITION BY mon ORDER BY ret DESC) AS rnk
FROM s
ORDER BY mon DESC, rnk
