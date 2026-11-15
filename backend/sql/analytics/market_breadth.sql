-- name: market_breadth
-- title: 시장 폭(상승·하락 종목 수)
-- description: 최근 N거래일 날짜별 상승·하락 종목 수와 등락비율(ADR)의 20일 이동평균 — 지수가 가리는 '시장 체력'
-- params: days:int=60:5:250
-- tags: 집계, 이동평균(윈도 프레임)
WITH d AS (
    SELECT trade_date,
           count(*) FILTER (WHERE change_rate > 0) AS up,
           count(*) FILTER (WHERE change_rate < 0) AS down,
           count(*) AS total
    FROM analytics.price_history
    WHERE trade_date > analytics.latest_trade_date() - (:days * 2 + 40)
    GROUP BY trade_date
)
SELECT trade_date, up, down, total,
       round(100.0 * sum(up) OVER w20 / NULLIF(sum(down) OVER w20, 0), 1) AS adr20
FROM d
WINDOW w20 AS (ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
ORDER BY trade_date DESC
LIMIT :days
