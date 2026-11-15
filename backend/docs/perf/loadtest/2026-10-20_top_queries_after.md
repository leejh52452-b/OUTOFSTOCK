| 호출 | 총 ms | 평균 ms | 최대 ms | 행 | 캐시 적중 % | 쿼리 |
|---:|---:|---:|---:|---:|---:|---|
| 297 | 495.9 | 1.67 | 24.6 | 297 | 100.0 | `SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY($1)` |
| 390 | 258.0 | 0.66 | 12.2 | 76250 | 99.9 | `SELECT trade_date,open,high,low,close,volume FROM ohlc WHERE ticker=$1 AND trade_date >= (SELECT to_char(MAX(trade_date)::date - CAST($2 AS integer), $4) FROM o` |
| 1053 | 155.6 | 0.15 | 9.7 | 12918 | 100.0 | `SELECT * FROM (SELECT k.ticker, b.* FROM unnest(CAST($1 AS varchar[])) AS k(ticker) CROSS JOIN LATERAL (SELECT trade_date, open, high, low, close, volume, ROW_N` |
| 128 | 142.9 | 1.12 | 7.9 | 8704 | 100.0 | `WITH RECURSIVE k(symbol) AS ( (SELECT symbol FROM overseas_ohlc ORDER BY symbol LIMIT $1) UNION ALL SELECT (SELECT o.symbol FROM overseas_ohlc o WHERE o.symbol ` |
| 249 | 130.7 | 0.53 | 9.7 | 4233 | 100.0 | `SELECT * FROM (WITH RECURSIVE k(ticker) AS ((SELECT ticker FROM daily_price ORDER BY ticker LIMIT $2) UNION ALL SELECT (SELECT t.ticker FROM daily_price t WHERE` |
| 1053 | 128.9 | 0.12 | 4.7 | 6459 | 100.0 | `SELECT * FROM (SELECT k.ticker, b.* FROM unnest(CAST($1 AS varchar[])) AS k(ticker) CROSS JOIN LATERAL (SELECT trade_date, change_rate, ROW_NUMBER() OVER (ORDER` |
| 390 | 124.7 | 0.32 | 51.0 | 76250 | 100.0 | `SELECT ticker, trade_date, change_rate FROM daily_price WHERE ticker = ANY($1) AND change_rate IS NOT NULL AND trade_date >= $2 AND trade_date <= $3` |
| 221 | 117.5 | 0.53 | 49.3 | 221 | 99.6 | `SELECT (SELECT COUNT(*) FROM wallet_ledger WHERE username=$1) AS wallet, (SELECT COUNT(*) FROM practice_session WHERE user_id=$1) AS practice, (SELECT COUNT(*) ` |
