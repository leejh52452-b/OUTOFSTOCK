-- V003 분석 뷰 — 종목 스냅샷(머티리얼라이즈드 뷰)·업종 성과 함수·적재 현황.
--
-- 왜 머티리얼라이즈드 뷰인가(ADR-0003): '주식 골라보기'와 '지금 뜨는 산업'은 매 요청마다
-- 670만 행에서 수익률·52주 고가·연속 상승·재무 비율을 다시 계산하면 느리다. 적재가 끝날 때 한 번
-- 계산해 두고(REFRESH ... CONCURRENTLY — 읽기를 막지 않음) 화면은 그 결과만 읽는다.
-- 대가: 데이터는 '마지막 새로 고침 시점' 기준 — 화면에 기준일(as_of)을 함께 표시한다.

-- ── 최신 거래일(전 종목 적재가 끝난 가장 최근 날짜) ────────────────────
-- 적재 이력 기준: 코스피(K)·코스닥(Q) 둘 다 ok 인 날 중 가장 최근. 이력이 없으면(표본·테스트) 시세의 최대 날짜.
CREATE OR REPLACE FUNCTION analytics.latest_trade_date() RETURNS date
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        (SELECT max(split_part(part_key, ':', 1)::date) FROM analytics.load_log l
          WHERE dataset = 'krx_daily' AND status = 'ok' AND part_key LIKE '%:K'
            AND EXISTS (SELECT 1 FROM analytics.load_log q WHERE q.dataset = 'krx_daily' AND q.status = 'ok'
                        AND q.part_key = split_part(l.part_key, ':', 1) || ':Q')),
        (SELECT max(trade_date) FROM analytics.price_history))
$$;

-- ── 종목 스냅샷 — 골라보기·산업·경보 화면의 공통 입력 ──────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_stock_snapshot AS
WITH last AS (
    SELECT analytics.latest_trade_date() AS d
), win AS (                                   -- 최근 약 1년(달력 400일) — 파티션 2개만 읽는다
    SELECT p.ticker, p.trade_date, p.close, p.high, p.volume, p.value, p.mktcap, p.change_rate, p.halted, p.mkt,
           row_number() OVER (PARTITION BY p.ticker ORDER BY p.trade_date DESC) AS rn
    FROM analytics.price_history p
    -- 스칼라 서브쿼리(InitPlan)로 넘겨야 실행 시점 파티션 제외(runtime pruning)가 된다 — CTE 를 조인하면 전 파티션을 훑는다
    WHERE p.trade_date > (SELECT d FROM last) - 400 AND p.trade_date <= (SELECT d FROM last)
), px AS (
    SELECT ticker,
        max(trade_date) FILTER (WHERE rn = 1)                        AS as_of,
        max(mkt) FILTER (WHERE rn = 1)                               AS mkt,
        max(close) FILTER (WHERE rn = 1)                             AS close,
        max(close) FILTER (WHERE rn = 2)                             AS close_1d,
        max(close) FILTER (WHERE rn = 6)                             AS close_1w,
        max(close) FILTER (WHERE rn = 22)                            AS close_1m,
        max(close) FILTER (WHERE rn = 64)                            AS close_3m,
        max(close) FILTER (WHERE rn = 251)                           AS close_1y,
        max(mktcap) FILTER (WHERE rn = 1)                            AS mktcap,
        max(volume) FILTER (WHERE rn = 1)                            AS volume,
        max(value) FILTER (WHERE rn = 1)                             AS value_1d,
        sum(value) FILTER (WHERE rn <= 5)                            AS value_1w,
        sum(value) FILTER (WHERE rn <= 21)                           AS value_1m,
        sum(value) FILTER (WHERE rn <= 63)                           AS value_3m,
        sum(value) FILTER (WHERE rn <= 250)                          AS value_1y,
        avg(volume) FILTER (WHERE rn BETWEEN 2 AND 21)               AS vol_avg20,
        max(high) FILTER (WHERE rn BETWEEN 2 AND 250)                AS high_52w_prev,
        max(high) FILTER (WHERE rn = 1)                              AS high_today,
        -- 연속 상승일: 가장 최근부터 등락률 > 0 이 끊기기 전까지의 날 수
        COALESCE(min(rn) FILTER (WHERE change_rate IS NULL OR change_rate <= 0), max(rn) + 1) - 1 AS up_streak,
        bool_or(halted) FILTER (WHERE rn = 1)                        AS halted_today,
        count(*)                                                     AS bars
    FROM win GROUP BY ticker
), fin AS (                                   -- 공시일 기준(PIT): 기준일까지 공시된 재무만
    SELECT f.*, row_number() OVER (PARTITION BY f.ticker ORDER BY f.fiscal_year DESC) AS k
    FROM analytics.fin_annual f
    WHERE f.disclosed_at IS NULL OR f.disclosed_at <= (SELECT d FROM last)
), fin_now AS (
    SELECT a.ticker, a.fiscal_year, a.revenue, a.gross_profit, a.operating_profit, a.net_income,
           a.total_equity, a.total_liabilities, a.capital_stock,
           b.revenue AS revenue_3y, b.net_income AS net_income_3y, c.net_income AS net_income_1y
    FROM fin a
    LEFT JOIN fin b ON b.ticker = a.ticker AND b.fiscal_year = a.fiscal_year - 3
    LEFT JOIN fin c ON c.ticker = a.ticker AND c.fiscal_year = a.fiscal_year - 1
    WHERE a.k = 1
), ni_streak AS (                             -- 순이익 연속 증가 연수(최근 연도부터 거꾸로)
    SELECT ticker, COALESCE(min(k) FILTER (WHERE NOT grew), max(k) + 1) - 1 AS years
    FROM (SELECT f.ticker, f.k,
                 COALESCE(f.net_income > lead(f.net_income) OVER (PARTITION BY f.ticker ORDER BY f.k), false) AS grew
          FROM fin f) g
    GROUP BY ticker
), div_ttm AS (                               -- 최근 1년(기준일로부터 365일) 주당배당금 합 — 배당수익률 분자
    SELECT ticker, sum(dps) AS dps_ttm
    FROM analytics.dividend
    WHERE record_date > (SELECT d FROM last) - 365 AND record_date <= (SELECT d FROM last)
    GROUP BY ticker
), div_year AS (                              -- 사업연도별 주당배당금 합 — 연속 지급·연속 증가 판정
    -- 2023년 배당 절차 개선 이후 결산배당 기준일이 다음 해 2~4월로 옮겨간 회사가 많다 → 그 해의 앞 연도 몫으로 본다.
    -- 기준일이 속한 사업연도가 아직 끝나지 않은 해(올해)는 빼고, 끝난 사업연도만 비교한다(중간배당만 있는 해가 '감소'로 보이지 않게).
    SELECT ticker, y, sum(dps) AS dps FROM (
        SELECT ticker, dps,
               CASE WHEN kind = '결산' AND extract(month FROM record_date) <= 4
                    THEN extract(year FROM record_date)::int - 1 ELSE extract(year FROM record_date)::int END AS y
        FROM analytics.dividend WHERE record_date <= (SELECT d FROM last)) z
    WHERE y <= extract(year FROM (SELECT d FROM last))::int - 1
    GROUP BY ticker, y
), div_seq AS (
    SELECT d.ticker, d.y, d.dps,
           row_number() OVER (PARTITION BY d.ticker ORDER BY d.y DESC) AS k,
           max(d.y) OVER (PARTITION BY d.ticker) AS ymax,
           COALESCE(d.dps > lead(d.dps) OVER (PARTITION BY d.ticker ORDER BY d.y DESC), false) AS grew
    FROM div_year d
), div_stat AS (
    SELECT ticker,
        max(y) FILTER (WHERE k = 1)                                           AS dps_year,
        -- 연속 지급: 최근 연도부터 거꾸로, 배당금 > 0 이고 연도가 끊기지 않은 해의 수
        COALESCE(min(k) FILTER (WHERE dps <= 0 OR y <> ymax - k + 1), max(k) + 1) - 1 AS paid_years,
        COALESCE(min(k) FILTER (WHERE NOT grew), max(k) + 1) - 1              AS grew_years
    FROM div_seq GROUP BY ticker
), flow AS (                                  -- 외국인·기관 순매수 수량: 최근일과 그 전날
    SELECT ticker,
        max(frgn_net_qty) FILTER (WHERE k = 1) AS frgn_now, max(frgn_net_qty) FILTER (WHERE k = 2) AS frgn_prev,
        max(orgn_net_qty) FILTER (WHERE k = 1) AS orgn_now, max(orgn_net_qty) FILTER (WHERE k = 2) AS orgn_prev,
        max(trade_date) FILTER (WHERE k = 1)   AS flow_date
    FROM (SELECT s.*, row_number() OVER (PARTITION BY s.ticker ORDER BY s.trade_date DESC) AS k
          FROM analytics.investor_flow_stock s
          WHERE s.trade_date <= (SELECT d FROM last) AND s.trade_date > (SELECT d FROM last) - 20) z
    WHERE k <= 2 GROUP BY ticker
), act AS (                                   -- 최근 1년 시장조치 공시 중 아직 '해제'되지 않은 최신 조치
    SELECT DISTINCT ON (ticker) ticker, action, rcept_dt
    FROM analytics.disclosure
    WHERE ticker IS NOT NULL AND action IS NOT NULL AND category = '시장조치' AND rcept_dt > (SELECT d FROM last) - 365
    ORDER BY ticker, rcept_dt DESC, rcept_no DESC
)
SELECT
    m.ticker, m.name, m.market, m.sector, m.sect_tp,
    px.as_of, px.mkt, px.close, px.mktcap, px.volume, px.bars,
    px.value_1d, px.value_1w, px.value_1m, px.value_3m, px.value_1y,
    round((px.close::numeric / NULLIF(px.close_1d, 0) - 1) * 100, 2) AS ret_1d,
    round((px.close::numeric / NULLIF(px.close_1w, 0) - 1) * 100, 2) AS ret_1w,
    round((px.close::numeric / NULLIF(px.close_1m, 0) - 1) * 100, 2) AS ret_1m,
    round((px.close::numeric / NULLIF(px.close_3m, 0) - 1) * 100, 2) AS ret_3m,
    round((px.close::numeric / NULLIF(px.close_1y, 0) - 1) * 100, 2) AS ret_1y,
    px.up_streak,
    (px.high_today IS NOT NULL AND px.high_52w_prev IS NOT NULL AND px.high_today > px.high_52w_prev) AS new_high_52w,
    round((px.volume / NULLIF(px.vol_avg20, 0))::numeric, 2)          AS vol_ratio20,
    -- 재무(최근 공시 연도)
    f.fiscal_year,
    CASE WHEN f.net_income > 0 THEN round((px.mktcap::numeric / f.net_income), 2) END          AS per,
    CASE WHEN f.total_equity > 0 THEN round((px.mktcap::numeric / f.total_equity), 2) END      AS pbr,
    CASE WHEN f.total_equity > 0 THEN round(f.net_income::numeric / f.total_equity * 100, 2) END AS roe,
    CASE WHEN f.revenue > 0 AND f.gross_profit IS NOT NULL
         THEN round(f.gross_profit::numeric / f.revenue * 100, 2) END                         AS gross_margin,
    CASE WHEN f.revenue > 0 THEN round(f.operating_profit::numeric / f.revenue * 100, 2) END  AS op_margin,
    CASE WHEN f.total_equity > 0 THEN round(f.total_liabilities::numeric / f.total_equity * 100, 1) END AS debt_ratio,
    CASE WHEN f.revenue > 0 AND f.revenue_3y > 0
         THEN round(((f.revenue::numeric / f.revenue_3y) ^ (1.0 / 3) - 1) * 100, 2) END       AS rev_cagr3,
    CASE WHEN f.net_income > 0 AND f.net_income_3y > 0
         THEN round(((f.net_income::numeric / f.net_income_3y) ^ (1.0 / 3) - 1) * 100, 2) END AS ni_cagr3,
    CASE WHEN f.net_income_1y > 0
         THEN round((f.net_income - f.net_income_1y)::numeric / f.net_income_1y * 100, 2) END   AS ni_yoy,
    ns.years                                                                                  AS ni_growth_years,
    (f.total_equity IS NOT NULL AND f.capital_stock IS NOT NULL AND f.total_equity < f.capital_stock) AS capital_impaired,
    -- 배당
    dt.dps_ttm, d.dps_year,
    CASE WHEN dt.dps_ttm > 0 AND px.close > 0 THEN round(dt.dps_ttm / px.close * 100, 2) END   AS div_yield,
    -- 배당성향 = 배당총액 / 순이익 (배당총액 ≈ 주당배당금 × 상장주식수, 주식수 ≈ 시총/종가)
    CASE WHEN dt.dps_ttm > 0 AND f.net_income > 0 AND px.close > 0
         THEN round(dt.dps_ttm * (px.mktcap::numeric / px.close) / f.net_income * 100, 1) END  AS payout_ratio,
    COALESCE(d.paid_years, 0)                                                                 AS div_paid_years,
    COALESCE(d.grew_years, 0)                                                                 AS div_grew_years,
    -- 수급
    fl.flow_date, fl.frgn_now, fl.frgn_prev, fl.orgn_now, fl.orgn_prev,
    -- 위험 신호(투자 유의)
    st.managed, st.warn_level, st.short_overheat, st.liquidation,
    COALESCE(px.halted_today, false) OR COALESCE(st.halted, false)                            AS halted,
    a.action AS last_action, a.rcept_dt AS last_action_dt,
    COALESCE(COALESCE(st.managed, false) OR COALESCE(st.warn_level, 0) >= 2 OR COALESCE(st.liquidation, false)
     OR COALESCE(px.halted_today, false) OR COALESCE(st.halted, false)
     OR COALESCE(m.sect_tp LIKE '관리종목%' OR m.sect_tp LIKE '투자주의환기%', false)
     OR COALESCE(a.action IN ('관리종목지정', '투자위험', '투자경고', '상장폐지사유', '정리매매'), false), false) AS risky
FROM analytics.security_master m
JOIN px ON px.ticker = m.ticker
LEFT JOIN fin_now f ON f.ticker = m.ticker
LEFT JOIN ni_streak ns ON ns.ticker = m.ticker
LEFT JOIN div_stat d ON d.ticker = m.ticker
LEFT JOIN div_ttm dt ON dt.ticker = m.ticker
LEFT JOIN flow fl ON fl.ticker = m.ticker
LEFT JOIN analytics.stock_status st ON st.ticker = m.ticker
LEFT JOIN act a ON a.ticker = m.ticker
WHERE px.as_of = (SELECT d FROM last);        -- 기준일에 시세가 있는 종목만(상장폐지·장기 정지 제외)

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_stock_snapshot ON analytics.mv_stock_snapshot (ticker);
CREATE INDEX IF NOT EXISTS ix_mv_stock_snapshot_sector ON analytics.mv_stock_snapshot (sector);

-- ── 업종 성과 — '지금 뜨는 산업' ───────────────────────────────
-- 기간 수익률 = 시가총액 가중 평균(기간 초 시총을 가중치로). 거래대금 = 기간 합.
CREATE OR REPLACE FUNCTION analytics.sector_performance(p_period text)
RETURNS TABLE (sector text, members bigint, ret numeric, value_sum numeric, mktcap_sum numeric,
               top_ticker text, top_name text, up_count bigint, down_count bigint)
LANGUAGE sql STABLE AS $$
    WITH s AS (
        SELECT v.sector, v.ticker, v.name, v.mktcap::numeric AS mktcap,
               CASE p_period WHEN '1d' THEN v.ret_1d WHEN '1w' THEN v.ret_1w WHEN '1m' THEN v.ret_1m
                             WHEN '3m' THEN v.ret_3m WHEN '1y' THEN v.ret_1y END AS r,
               CASE p_period WHEN '1d' THEN v.value_1d WHEN '1w' THEN v.value_1w WHEN '1m' THEN v.value_1m
                             WHEN '3m' THEN v.value_3m WHEN '1y' THEN v.value_1y END AS val
        FROM analytics.mv_stock_snapshot v
        WHERE v.sector IS NOT NULL
    ), w AS (
        SELECT s.*, s.mktcap / (1 + s.r / 100) AS cap0 FROM s WHERE s.r IS NOT NULL AND s.r > -100
    )
    SELECT w.sector::text, count(*),
           round(sum(w.cap0 * w.r) / NULLIF(sum(w.cap0), 0), 2),
           sum(COALESCE(w.val, 0))::numeric, sum(w.mktcap),
           (array_agg(w.ticker ORDER BY w.mktcap DESC))[1]::text,
           (array_agg(w.name ORDER BY w.mktcap DESC))[1]::text,
           count(*) FILTER (WHERE w.r > 0), count(*) FILTER (WHERE w.r < 0)
    FROM w GROUP BY w.sector
$$;

-- ── 적재 현황(날짜별 종목 수) — 누락·이상 일자 확인 ─────────────────
CREATE OR REPLACE VIEW analytics.v_daily_coverage AS
SELECT trade_date, count(*) AS tickers, count(*) FILTER (WHERE mkt = 'K') AS kospi,
       count(*) FILTER (WHERE mkt = 'Q') AS kosdaq, count(*) FILTER (WHERE halted) AS halted,
       sum(value) AS value_sum
FROM analytics.price_history GROUP BY trade_date;
