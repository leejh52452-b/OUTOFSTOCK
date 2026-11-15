-- name: op_profit_growth
-- title: 영업이익 증가율 상위
-- description: 최근 사업연도 영업이익이 전년보다 크게 늘어난 종목(LAG 윈도 함수, 전년 흑자 기업만)
-- params: min_growth:float=30:0:1000, limit:int=50:1:300
-- tags: 윈도 함수(LAG), 연간 재무
WITH f AS (
    SELECT ticker, fiscal_year, operating_profit, revenue,
           lag(operating_profit) OVER (PARTITION BY ticker ORDER BY fiscal_year) AS op_prev,
           lag(fiscal_year) OVER (PARTITION BY ticker ORDER BY fiscal_year) AS y_prev,
           row_number() OVER (PARTITION BY ticker ORDER BY fiscal_year DESC) AS k
    FROM analytics.fin_annual
)
SELECT f.ticker, m.name, m.sector, f.fiscal_year, f.op_prev, f.operating_profit,
       round((f.operating_profit - f.op_prev)::numeric / f.op_prev * 100, 1) AS growth_pct,
       round(f.operating_profit::numeric / NULLIF(f.revenue, 0) * 100, 1) AS op_margin
FROM f JOIN analytics.security_master m ON m.ticker = f.ticker
WHERE f.k = 1 AND f.y_prev = f.fiscal_year - 1 AND f.op_prev > 0
  AND (f.operating_profit - f.op_prev)::numeric / f.op_prev * 100 >= :min_growth
ORDER BY growth_pct DESC
LIMIT :limit
