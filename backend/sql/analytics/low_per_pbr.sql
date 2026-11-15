-- name: low_per_pbr
-- title: 저PER·저PBR 흑자 기업
-- description: 순이익 흑자이면서 PER·PBR 이 기준 이하인 종목(공시일 기준 최신 재무 — 미래 정보 미사용)
-- params: max_per:float=10:0.5:100, max_pbr:float=1:0.1:20, min_mktcap_eok:int=500:0:1000000, limit:int=50:1:300
-- tags: 머티리얼라이즈드 뷰, PIT
SELECT ticker, name, sector, close, round(mktcap / 1e8) AS mktcap_eok, per, pbr, roe, debt_ratio, fiscal_year
FROM analytics.mv_stock_snapshot
WHERE per > 0 AND per <= :max_per AND pbr > 0 AND pbr <= :max_pbr AND mktcap >= :min_mktcap_eok * 1e8
  AND NOT risky
ORDER BY per ASC, pbr ASC
LIMIT :limit
