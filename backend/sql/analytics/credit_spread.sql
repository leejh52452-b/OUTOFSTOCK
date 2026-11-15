-- name: credit_spread
-- title: 신용스프레드·장단기 금리차
-- description: 회사채(AA-·BBB-) − 국고채 3년, 국고채 10년 − 3년 — 기업 신용 여건과 경기 기대(한국은행 ECOS)
-- params: days:int=365:30:3650
-- tags: 피벗(FILTER), 시계열
SELECT trade_date,
       max(value) FILTER (WHERE series = 'KTB3Y')      AS ktb3y,
       max(value) FILTER (WHERE series = 'KTB10Y')     AS ktb10y,
       max(value) FILTER (WHERE series = 'CORP_AA3Y')  AS corp_aa,
       max(value) FILTER (WHERE series = 'CORP_BBB3Y') AS corp_bbb,
       max(value) FILTER (WHERE series = 'KDB1Y')      AS kdb1y,
       round(max(value) FILTER (WHERE series = 'CORP_AA3Y') - max(value) FILTER (WHERE series = 'KTB3Y'), 3)  AS spread_aa,
       round(max(value) FILTER (WHERE series = 'CORP_BBB3Y') - max(value) FILTER (WHERE series = 'KTB3Y'), 3) AS spread_bbb,
       round(max(value) FILTER (WHERE series = 'KTB10Y') - max(value) FILTER (WHERE series = 'KTB3Y'), 3)     AS term_spread
FROM analytics.indicator_daily
WHERE series IN ('KTB3Y', 'KTB10Y', 'CORP_AA3Y', 'CORP_BBB3Y', 'KDB1Y')
  AND trade_date > (SELECT max(trade_date) FROM analytics.indicator_daily) - :days
GROUP BY trade_date
ORDER BY trade_date
