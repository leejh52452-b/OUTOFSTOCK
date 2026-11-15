-- name: new_high_52w
-- title: 52주 신고가 종목
-- description: 최근 거래일 고가가 직전 1년 고가를 넘은 종목 — 거래대금 순
-- params: limit:int=50:1:300
-- tags: 머티리얼라이즈드 뷰
SELECT ticker, name, sector, close, ret_1d, ret_1m, ret_1y, round(value_1d / 1e8) AS value_eok, vol_ratio20
FROM analytics.mv_stock_snapshot
WHERE new_high_52w
ORDER BY value_1d DESC NULLS LAST
LIMIT :limit
