-- V007 수정주가 — 액면분할·무상증자·감자 같은 권리 이벤트를 반영해 기간 수익률·52주 신고가·변동성·낙폭·거래량 배수를 계산한다.
--
-- 왜(점검에서 찾은 결함): 기간 수익률을 '원 종가 비율'로 계산하면 액면분할(5:1) 날 종가가 1/5 이 되어
-- 1주·1개월·1년 수익률이 −80% 처럼 나온다. 업종·테마 등락률, 52주 신고가, 월말 변동성·낙폭(신용위험 피처),
-- 1년 최대낙폭, 거래량 급증 배수, 배당수익률(분할 전 주당배당금 ÷ 분할 뒤 주가)이 모두 같이 틀린다.
-- 원리: KRX 등락률(FLUC_RT)은 기준가(권리락이 반영된 전일 가격) 대비라 이벤트 날에도 '실제 수익률'이다.
--   이벤트 날 = 원 종가 비율(c_t / c_{t-1}) 과 KRX 비율(1 + 등락률/100) 이 0.5% 넘게 다른 날.
--   조정계수 f = 원 비율 ÷ KRX 비율(5:1 분할이면 약 0.2). 그 날 '이전' 가격에 f 를 곱하고 거래량은 f 로 나눈다.
--   평소에는 두 비율이 반올림 오차(0.005%) 안에서 같으므로 원 종가를 그대로 쓴다(정확).
-- 원 종가는 덮어쓰지 않는다 — 원천 보존, 같은 날을 다시 적재해도 충돌 없음. 이벤트만 따로 두고 읽을 때 곱한다.

CREATE TABLE IF NOT EXISTS analytics.price_adj_event (
    ticker      varchar(12)      NOT NULL,
    trade_date  date             NOT NULL,          -- 권리락·분할이 반영된 첫 거래일
    factor      double precision NOT NULL CHECK (factor > 0),   -- 이 날 '이전' 가격에 곱할 값
    raw_ratio   double precision NOT NULL,          -- 원 종가 비율 c_t / c_{t-1}
    krx_ratio   double precision NOT NULL,          -- 1 + KRX 등락률/100
    detected_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, trade_date)
);
COMMENT ON TABLE analytics.price_adj_event IS '권리 이벤트(액면분할·무상증자·감자 등) 조정계수 — 수정주가 = 원 가격 × 이후 이벤트 계수의 곱';

-- 이벤트 다시 찾기 — p_since 이후 거래일만 바꾼다(NULL 이면 전체). 적재기가 새로 받은 가장 이른 날을 넘긴다
-- (백필은 과거로 가므로 그날~최신 사이 이벤트가 새로 확정될 수 있다).
-- 거르는 두 가지(점검에서 찾은 오탐):
--   ① 봉이 빠진 날: 종목의 직전 행이 '직전 시장 거래일'이 아니면(그날 적재 실패·결측) 이틀 치 원 비율과 하루 치 공식 등락률을
--      비교하게 되어 가짜 이벤트가 생기고 빠진 날의 등락이 지워진다 → 시장 달력(적재된 거래일)으로 확인해 건너뛴다.
--   ② 등락률 자료가 종가와 안 맞는 종목: 실제 권리 이벤트는 한 종목에 1년 한두 번 — 앞뒤 250거래일 안에 어긋난 날이 2% 를
--      넘으면 조정하지 않는다. 판단 창을 p_since 와 상관없이 같게 둬서(최소 400일 앞부터 읽음) 증분 갱신과 전체 갱신이 같은 답.
CREATE OR REPLACE FUNCTION analytics.refresh_price_adj(p_since date DEFAULT NULL) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    lo  date := COALESCE(p_since, DATE '1900-01-01');
    glo date;
    n integer;
BEGIN
    glo := LEAST(lo, COALESCE(analytics.latest_trade_date(), lo) - 400);
    DELETE FROM analytics.price_adj_event WHERE trade_date >= lo;
    INSERT INTO analytics.price_adj_event(ticker, trade_date, factor, raw_ratio, krx_ratio)
    WITH cal AS (                                -- 시장 거래일 달력(적재된 날) — 직전 거래일
        SELECT trade_date, lag(trade_date) OVER (ORDER BY trade_date) AS prev_day
        FROM (SELECT DISTINCT trade_date FROM analytics.price_history WHERE trade_date >= glo - 15) d
    ), x AS (
        SELECT ticker, trade_date, close, change_rate,
               lag(close) OVER w AS prev, lag(trade_date) OVER w AS prev_date
        FROM analytics.price_history
        WHERE trade_date >= glo - 15
        WINDOW w AS (PARTITION BY ticker ORDER BY trade_date)
    ), y AS (
        SELECT x.ticker, x.trade_date, x.close::float8 / NULLIF(x.prev, 0) AS rr, 1 + x.change_rate::float8 / 100 AS kr
        FROM x JOIN cal c ON c.trade_date = x.trade_date
        WHERE x.trade_date >= glo AND x.prev IS NOT NULL AND x.change_rate IS NOT NULL AND x.change_rate > -100
          AND x.prev_date = c.prev_day             -- ① 직전 행이 직전 거래일일 때만
    ), z AS (
        SELECT ticker, trade_date, rr, kr, abs(rr / kr - 1) > 0.005 AS cand
        FROM y WHERE rr IS NOT NULL
    ), g AS (
        SELECT z.*,
               count(*) FILTER (WHERE cand) OVER v AS n_ev,
               count(*) OVER v AS n_rows
        FROM z
        WINDOW v AS (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 250 PRECEDING AND 250 FOLLOWING)
    )
    SELECT ticker, trade_date, rr / kr, rr, kr
    FROM g
    WHERE cand AND trade_date >= lo
      AND n_ev <= greatest(2, floor(n_rows * 0.02));          -- ② 앞뒤 250거래일 안 어긋난 날이 2% 이하일 때만
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- 기간 안의 수정주가 재료 — adj = 이 날 '이후'(기간 끝까지) 이벤트 계수의 곱(가격 × adj = 기간 끝 기준 수정주가),
-- lr = 수정 로그수익률(이벤트 날은 KRX 등락률과 같아진다). 첫 행의 lr 은 NULL(직전 행이 기간 밖).
-- p_tickers 를 주면 그 종목만(테마 지수·응집도처럼 몇십 종목만 필요할 때 기본키 인덱스로 읽는다).
CREATE OR REPLACE FUNCTION analytics.adj_prices(p_from date, p_to date, p_tickers text[] DEFAULT NULL)
RETURNS TABLE (ticker varchar, trade_date date, mkt character, close integer, high integer, low integer,
               volume bigint, value bigint, mktcap bigint, change_rate real, halted boolean,
               adj double precision, lr double precision)
LANGUAGE sql STABLE AS $$
    SELECT p.ticker, p.trade_date, p.mkt, p.close, p.high, p.low, p.volume, p.value, p.mktcap, p.change_rate, p.halted,
           exp(COALESCE(sum(ln(e.factor)) OVER (PARTITION BY p.ticker ORDER BY p.trade_date DESC
                                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)),
           ln(p.close::float8 / NULLIF(lag(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date), 0))
             - COALESCE(ln(e.factor), 0)
    FROM analytics.price_history p
    LEFT JOIN analytics.price_adj_event e ON e.ticker = p.ticker AND e.trade_date = p.trade_date
    WHERE p.trade_date BETWEEN p_from AND p_to AND (p_tickers IS NULL OR p.ticker = ANY(p_tickers))
$$;

-- ── 종목 스냅샷 다시 만들기(수정주가 기준) ──────────────────────────
-- 열 이름·뜻은 V003 과 같다(화면·API 그대로). 바뀐 계산: ret_*, new_high_52w, vol_ratio20, 배당 지표.
DROP MATERIALIZED VIEW IF EXISTS analytics.mv_stock_snapshot;
CREATE MATERIALIZED VIEW analytics.mv_stock_snapshot AS
WITH last AS (
    SELECT analytics.latest_trade_date() AS d
), win AS (                                   -- 최근 약 1년(달력 400일) — 파티션 2개만 읽는다
    SELECT p.ticker, p.trade_date, p.close, p.high, p.volume, p.value, p.mktcap, p.change_rate, p.halted, p.mkt,
           row_number() OVER (PARTITION BY p.ticker ORDER BY p.trade_date DESC) AS rn,
           -- 이 날 '이후'(기준일까지) 권리 이벤트 계수의 곱 — 이 날 가격 × adj = 기준일 기준 수정주가
           exp(COALESCE(sum(ln(e.factor)) OVER (PARTITION BY p.ticker ORDER BY p.trade_date DESC
                                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)) AS adj
    FROM analytics.price_history p
    LEFT JOIN analytics.price_adj_event e ON e.ticker = p.ticker AND e.trade_date = p.trade_date
    -- 스칼라 서브쿼리(InitPlan)로 넘겨야 실행 시점 파티션 제외(runtime pruning)가 된다 — CTE 를 조인하면 전 파티션을 훑는다
    WHERE p.trade_date > (SELECT d FROM last) - 400 AND p.trade_date <= (SELECT d FROM last)
), px AS (
    SELECT ticker,
        max(trade_date) FILTER (WHERE rn = 1)                        AS as_of,
        max(mkt) FILTER (WHERE rn = 1)                               AS mkt,
        max(close) FILTER (WHERE rn = 1)                             AS close,
        max(close * adj) FILTER (WHERE rn = 2)                       AS close_1d,
        max(close * adj) FILTER (WHERE rn = 6)                       AS close_1w,
        max(close * adj) FILTER (WHERE rn = 22)                      AS close_1m,
        max(close * adj) FILTER (WHERE rn = 64)                      AS close_3m,
        max(close * adj) FILTER (WHERE rn = 251)                     AS close_1y,
        max(mktcap) FILTER (WHERE rn = 1)                            AS mktcap,
        max(volume) FILTER (WHERE rn = 1)                            AS volume,
        max(value) FILTER (WHERE rn = 1)                             AS value_1d,
        sum(value) FILTER (WHERE rn <= 5)                            AS value_1w,
        sum(value) FILTER (WHERE rn <= 21)                           AS value_1m,
        sum(value) FILTER (WHERE rn <= 63)                           AS value_3m,
        sum(value) FILTER (WHERE rn <= 250)                          AS value_1y,
        avg(volume / adj) FILTER (WHERE rn BETWEEN 2 AND 21)         AS vol_avg20,    -- 분할 전 거래량은 주식 수 기준으로 늘린다
        max(high * adj) FILTER (WHERE rn BETWEEN 2 AND 250)          AS high_52w_prev,
        max(high) FILTER (WHERE rn = 1)                              AS high_today,
        -- 연속 상승일: 가장 최근부터 등락률 > 0 이 끊기기 전까지의 날 수(등락률은 이미 기준가 대비)
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
), dps_adj AS (                               -- 주당배당금도 수정(기준일 기준) — 분할 전 배당금 ÷ 분할 뒤 주가가 되지 않게
    SELECT dv.ticker, dv.record_date, dv.kind,
           dv.dps * exp(COALESCE((SELECT sum(ln(e.factor)) FROM analytics.price_adj_event e
                                  WHERE e.ticker = dv.ticker AND e.trade_date > dv.record_date
                                    AND e.trade_date <= (SELECT d FROM last)), 0)) AS dps
    FROM analytics.dividend dv
    WHERE dv.record_date <= (SELECT d FROM last)
), div_ttm AS (                               -- 최근 1년(기준일로부터 365일) 주당배당금 합 — 배당수익률 분자
    SELECT ticker, sum(dps) AS dps_ttm
    FROM dps_adj
    WHERE record_date > (SELECT d FROM last) - 365
    GROUP BY ticker
), div_year AS (                              -- 사업연도별 주당배당금 합 — 연속 지급·연속 증가 판정
    -- 2023년 배당 절차 개선 이후 결산배당 기준일이 다음 해 2~4월로 옮겨간 회사가 많다 → 그 해의 앞 연도 몫으로 본다.
    -- 기준일이 속한 사업연도가 아직 끝나지 않은 해(올해)는 빼고, 끝난 사업연도만 비교한다(중간배당만 있는 해가 '감소'로 보이지 않게).
    SELECT ticker, y, sum(dps) AS dps FROM (
        SELECT ticker, dps,
               CASE WHEN kind = '결산' AND extract(month FROM record_date) <= 4
                    THEN extract(year FROM record_date)::int - 1 ELSE extract(year FROM record_date)::int END AS y
        FROM dps_adj) z
    WHERE y <= extract(year FROM (SELECT d FROM last))::int - 1
    GROUP BY ticker, y
), div_seq AS (
    SELECT d.ticker, d.y, d.dps,
           row_number() OVER (PARTITION BY d.ticker ORDER BY d.y DESC) AS k,
           max(d.y) OVER (PARTITION BY d.ticker) AS ymax,
           -- 반올림 차(수정 계수)로 같은 배당이 '증가'로 보이지 않게 0.5% 넘게 늘어야 증가
           COALESCE(d.dps > lead(d.dps) OVER (PARTITION BY d.ticker ORDER BY d.y DESC) * 1.005, false) AS grew
    FROM div_year d
), div_stat AS (
    SELECT ticker,
        max(y) FILTER (WHERE k = 1)                                           AS dps_year,
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
    round((px.close::numeric / NULLIF(px.close_1d, 0)::numeric - 1) * 100, 2) AS ret_1d,
    round((px.close::numeric / NULLIF(px.close_1w, 0)::numeric - 1) * 100, 2) AS ret_1w,
    round((px.close::numeric / NULLIF(px.close_1m, 0)::numeric - 1) * 100, 2) AS ret_1m,
    round((px.close::numeric / NULLIF(px.close_3m, 0)::numeric - 1) * 100, 2) AS ret_3m,
    round((px.close::numeric / NULLIF(px.close_1y, 0)::numeric - 1) * 100, 2) AS ret_1y,
    px.up_streak,
    (px.high_today IS NOT NULL AND px.high_52w_prev IS NOT NULL AND px.high_today > px.high_52w_prev * 1.0001) AS new_high_52w,
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
    -- 배당(수정 주당배당금)
    round(dt.dps_ttm::numeric, 2) AS dps_ttm, d.dps_year,
    CASE WHEN dt.dps_ttm > 0 AND px.close > 0 THEN round((dt.dps_ttm / px.close * 100)::numeric, 2) END AS div_yield,
    -- 배당성향 = 배당총액 / 순이익 (배당총액 ≈ 주당배당금 × 상장주식수, 주식수 ≈ 시총/종가)
    CASE WHEN dt.dps_ttm > 0 AND f.net_income > 0 AND px.close > 0
         THEN round((dt.dps_ttm * (px.mktcap::float8 / px.close) / f.net_income * 100)::numeric, 1) END AS payout_ratio,
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

-- ── 월말 시장 피처(신용위험 모형) — 수정 로그수익률로 ───────────────────
-- 예전: ln(종가/전일 종가) 로 변동성, 종가/최고 종가로 낙폭 → 분할 날이 '−80% 폭락'으로 들어가 변동성·낙폭이 커지고
-- 부실 확률이 올라갔다. 이제 누적 수정 로그수익률(cum)로 1년 수익률·최고점·낙폭을 계산한다.
CREATE OR REPLACE FUNCTION analytics.refresh_market_monthly(months integer DEFAULT 1200) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = analytics, pg_temp AS $$
DECLARE
    since date := (SELECT max(trade_date) FROM analytics.price_history) - (months * 31 + 400);
    n bigint;
BEGIN
    DELETE FROM analytics.market_monthly WHERE month_end >= since + 400;
    INSERT INTO analytics.market_monthly(ticker, month_end, close, mktcap, ret_1y, vol_1y, dd_1y)
    SELECT ticker, trade_date, close, mktcap,
           CASE WHEN n_obs >= 200 THEN exp(cum - cum_1y) - 1 END,
           CASE WHEN n_obs >= 200 THEN sd * sqrt(252) END,
           exp(cum - peak) - 1
    FROM (
        SELECT ticker, trade_date, close, mktcap, cum,
               stddev_samp(lr) OVER w AS sd, max(cum) OVER w AS peak, first_value(cum) OVER w AS cum_1y,
               count(*) OVER w AS n_obs,
               row_number() OVER (PARTITION BY ticker, date_trunc('month', trade_date) ORDER BY trade_date DESC) AS k
        FROM (
            SELECT ticker, trade_date, close, mktcap, lr,
                   sum(COALESCE(lr, 0)) OVER (PARTITION BY ticker ORDER BY trade_date) AS cum
            FROM (
                SELECT p.ticker, p.trade_date, p.close, p.mktcap,
                       ln(p.close::float8 / NULLIF(lag(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date), 0))
                         - COALESCE(ln(e.factor), 0) AS lr
                FROM analytics.price_history p
                LEFT JOIN analytics.price_adj_event e ON e.ticker = p.ticker AND e.trade_date = p.trade_date
                WHERE p.trade_date >= since AND NOT p.halted
            ) r
        ) c
        WINDOW w AS (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 249 PRECEDING AND CURRENT ROW)
    ) s
    WHERE k = 1 AND trade_date >= since + 400
    ON CONFLICT (ticker, month_end) DO UPDATE SET close = EXCLUDED.close, mktcap = EXCLUDED.mktcap,
        ret_1y = EXCLUDED.ret_1y, vol_1y = EXCLUDED.vol_1y, dd_1y = EXCLUDED.dd_1y;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION analytics.refresh_market_monthly(integer) FROM PUBLIC;

-- ── DB 계정이 분리돼 있으면(deploy/sql/roles.sql) 다시 만든 객체의 권한을 다시 준다 ─────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_ro') THEN
        EXECUTE 'GRANT SELECT ON analytics.mv_stock_snapshot, analytics.price_adj_event TO oos_ro';
        EXECUTE 'GRANT EXECUTE ON FUNCTION analytics.adj_prices(date, date, text[]) TO oos_ro';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON analytics.price_adj_event TO oos_app';
        EXECUTE 'GRANT SELECT ON analytics.mv_stock_snapshot TO oos_app';
        EXECUTE 'GRANT EXECUTE ON FUNCTION analytics.refresh_market_monthly(integer) TO oos_app';
    END IF;
END $$;
