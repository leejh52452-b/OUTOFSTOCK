-- name: drawdown_1y
-- title: 1년 최대낙폭(MDD) 큰 종목
-- description: 최근 1년 고점 대비 최대 하락률 — 수정주가(액면분할·무상증자 반영) 위에서 누적 최댓값 윈도(MAX OVER ROWS UNBOUNDED PRECEDING)
-- params: min_mktcap_eok:int=1000:0:1000000, limit:int=50:1:300
-- tags: 윈도 함수(누적 최댓값), 수정주가, 위험 지표
WITH last AS (SELECT analytics.latest_trade_date() AS d),
a AS (                                       -- adj = 이후 권리 이벤트 계수의 곱 → 종가 × adj = 수정주가(분할 날 가짜 −80% 제거)
    SELECT ticker, trade_date, close * adj AS px
    FROM analytics.adj_prices((SELECT d FROM last) - 365, (SELECT d FROM last))
), p AS (
    SELECT ticker, px, max(px) OVER (PARTITION BY ticker ORDER BY trade_date ROWS UNBOUNDED PRECEDING) AS peak
    FROM a
), dd AS (
    SELECT ticker, min(px / peak - 1) AS mdd FROM p GROUP BY ticker
)
SELECT dd.ticker, s.name, s.sector, round((dd.mdd * 100)::numeric, 1) AS mdd_pct, s.ret_1y, round(s.mktcap / 1e8) AS mktcap_eok
FROM dd JOIN analytics.mv_stock_snapshot s ON s.ticker = dd.ticker
WHERE s.mktcap >= :min_mktcap_eok * 1e8
ORDER BY dd.mdd ASC
LIMIT :limit
