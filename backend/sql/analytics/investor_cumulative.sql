-- name: investor_cumulative
-- title: 투자자별 누적 순매수
-- description: 시장·투자자별 일별 순매수와 기간 누적(SUM OVER) — 외국인·기관이 어느 쪽으로 움직였나
-- params: days:int=60:5:500, market:enum=KOSPI:KOSPI|KOSDAQ
-- tags: 윈도 함수(누적 합), 긴 형식 표
SELECT trade_date, investor, net_value,
       sum(net_value) OVER (PARTITION BY investor ORDER BY trade_date) AS cum_value
FROM analytics.investor_flow_market
WHERE market = :market AND investor IN ('prsn', 'frgn', 'orgn')
  AND trade_date > (SELECT max(trade_date) FROM analytics.investor_flow_market) - :days * 2
ORDER BY trade_date, investor
