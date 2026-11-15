| 호출 | 총 ms | 평균 ms | 최대 ms | 행 | 캐시 적중 % | 쿼리 |
|---:|---:|---:|---:|---:|---:|---|
| 64 | 2598.6 | 40.60 | 177.2 | 4352 | 98.5 | `SELECT symbol, trade_date, open, high, low, close, volume, rn FROM ( SELECT symbol, trade_date, open, high, low, close, volume, ROW_NUMBER() OVER (PARTIT` |
| 159 | 277.7 | 1.75 | 28.4 | 159 | 99.4 | `SELECT MAX(trade_date) AS d FROM ohlc WHERE ticker = ANY($1)` |
| 203 | 210.5 | 1.04 | 11.7 | 39536 | 99.9 | `SELECT trade_date,open,high,low,close,volume FROM ohlc WHERE ticker=$1 AND trade_date >= (SELECT to_char(MAX(trade_date)::date - CAST($2 AS integer), $4) FROM o` |
| 550 | 84.1 | 0.15 | 4.6 | 3326 | 99.9 | `SELECT * FROM (SELECT k.ticker, b.* FROM unnest(CAST($1 AS varchar[])) AS k(ticker) CROSS JOIN LATERAL (SELECT trade_date, change_rate, ROW_NUMBER() OVER (ORDER` |
| 626 | 83.1 | 0.13 | 5.7 | 626 | 100.0 | `SELECT s.session_id, s.username, s.client, s.aal, s.auth_time, s.step_up_at, s.last_seen_at, s.expires_at, s.device_hash, s.last_ip_net, s.risk, s.csrf_hash, s.` |
| 205 | 74.1 | 0.36 | 26.4 | 46081 | 97.6 | `SELECT ticker, trade_date, change_rate FROM daily_price WHERE ticker = ANY($1) AND change_rate IS NOT NULL AND trade_date >= $2 AND trade_date <= $3` |
| 124 | 72.7 | 0.59 | 12.4 | 2108 | 100.0 | `SELECT * FROM (WITH RECURSIVE k(ticker) AS ((SELECT ticker FROM daily_price ORDER BY ticker LIMIT $2) UNION ALL SELECT (SELECT t.ticker FROM daily_price t WHERE` |
| 550 | 69.3 | 0.13 | 4.4 | 6652 | 99.8 | `SELECT * FROM (SELECT k.ticker, b.* FROM unnest(CAST($1 AS varchar[])) AS k(ticker) CROSS JOIN LATERAL (SELECT trade_date, open, high, low, close, volume, ROW_N` |
| 180 | 34.5 | 0.19 | 32.5 | 0 | 99.5 | `SELECT ticker FROM watchlist WHERE user_id=$1 ORDER BY id DESC` |
| 1440 | 26.9 | 0.02 | 3.1 | 1440 | None | `SELECT set_config($3, $1, $4), set_config($5, $2, $6)` |
| 410 | 23.0 | 0.06 | 5.1 | 12300 | 99.9 | `SELECT trade_date, close FROM index_daily WHERE code=$1 ORDER BY trade_date DESC LIMIT $2` |
| 1508 | 18.4 | 0.01 | 0.8 | 1440 | 100.0 | `SELECT value FROM app_meta WHERE key=$1` |
