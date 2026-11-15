-- name: volume_spike
-- title: 거래량 급증 종목
-- description: 최근 거래일 거래량이 직전 20거래일 평균의 N배 이상인 종목(윈도 함수 + 최근 2개 파티션만 읽음, 분할 전 거래량은 주식 수 기준으로 환산)
-- params: min_ratio:float=3:1:50, limit:int=30:1:200
-- tags: 윈도 함수, 파티션 제외, 수정주가
WITH last AS (SELECT analytics.latest_trade_date() AS d),
w AS (
    SELECT a.ticker, a.trade_date, a.close, a.volume, a.change_rate,
           -- 액면분할(5:1) 직후엔 거래량이 5배로 보이므로 이전 거래량을 같은 주식 수 기준(÷ adj)으로 맞춘다
           avg(a.volume / a.adj) OVER (PARTITION BY a.ticker ORDER BY a.trade_date
                                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20
    FROM analytics.adj_prices((SELECT d FROM last) - 40, (SELECT d FROM last)) a
)
SELECT w.ticker, m.name, m.sector, w.close, w.change_rate, w.volume,
       round(w.avg20) AS avg20, round((w.volume / w.avg20)::numeric, 2) AS ratio
FROM w JOIN analytics.security_master m ON m.ticker = w.ticker
WHERE w.trade_date = (SELECT d FROM last) AND w.avg20 > 0 AND w.volume >= :min_ratio * w.avg20
ORDER BY ratio DESC
LIMIT :limit
