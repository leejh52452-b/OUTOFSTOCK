-- V011 최소 권한 보강 — 역할 분리(deploy/sql/roles.sql)를 쓰는 DB 에서만 동작(oos_app 이 없으면 아무것도 안 함).
-- 표가 다시 만들어져도(마이그레이션·복구) 감사 성격의 표는 앱 계정이 고치거나 지울 수 없게 다시 맞춘다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON public.audit_log FROM oos_app;          -- 추가·조회만(해시 체인)
    END IF;
    IF to_regclass('public.pii_access_log') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON public.pii_access_log FROM oos_app;     -- 열람 기록은 추가·조회만
        GRANT UPDATE (subject) ON public.pii_access_log TO oos_app;                -- 탈퇴 시 대상 아이디 가명화만
    END IF;
    IF to_regclass('public.security_event') IS NOT NULL THEN
        REVOKE TRUNCATE ON public.security_event FROM oos_app;
    END IF;
    IF to_regclass('analytics.schema_migrations') IS NOT NULL THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON analytics.schema_migrations FROM oos_app;
    END IF;
    -- 분석 계층 함수 실행 권한(V005 가 PUBLIC 에서 회수) — 앱·조회 계정에 필요한 것만
    PERFORM 1;
    IF to_regprocedure('analytics.ensure_price_partition(integer)') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION analytics.ensure_price_partition(integer), analytics.refresh_snapshot(boolean),
                                  analytics.refresh_market_monthly(integer), analytics.refresh_price_adj(date),
                                  analytics.adj_prices(date, date, text[]) TO oos_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_ro')
       AND to_regprocedure('analytics.latest_trade_date()') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION analytics.latest_trade_date(), analytics.sector_performance(text),
                                  analytics.sector_of(text), analytics.adj_prices(date, date, text[]) TO oos_ro;
    END IF;
END $$;

-- 분석 계층 스키마·표 권한(스키마를 새로 만든 경우에도 — 기본 권한은 스키마에 묶여 있어 함께 사라진다)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'analytics') THEN
        GRANT USAGE ON SCHEMA analytics TO oos_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics TO oos_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO oos_app;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON analytics.schema_migrations FROM oos_app;
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA analytics '
                       'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oos_app', current_user);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA analytics '
                       'GRANT USAGE, SELECT ON SEQUENCES TO oos_app', current_user);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_ro') THEN
            GRANT USAGE ON SCHEMA analytics TO oos_ro;
            GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO oos_ro;
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA analytics GRANT SELECT ON TABLES TO oos_ro',
                           current_user);
        END IF;
    END IF;
END $$;

-- 공개 데이터 표(L1)만 TRUNCATE 허용 — 팀 공유 스냅샷 들이기(run.py snapshot-import)가 표를 통째로 바꾼다.
-- 개인 데이터·감사 표는 TRUNCATE 불가(대량 삭제는 행 단위 DELETE 로만 — RLS·흔적이 남는다).
DO $$
DECLARE t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    FOREACH t IN ARRAY ARRAY['stock', 'daily_price', 'ohlc', 'market_index', 'index_daily', 'realtime_price',
                             'financial_statement', 'ranking', 'recommendation', 'reco_snapshot', 'news', 'news_stock',
                             'news_sentiment', 'overseas_stock', 'overseas_ohlc', 'overseas_quote', 'overseas_financial'] LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('GRANT TRUNCATE ON public.%I TO oos_app', t);
        END IF;
    END LOOP;
    -- 그 표들의 번호 시퀀스는 들인 데이터 뒤로 이어 붙일 수 있게(setval = 시퀀스 UPDATE 권한)
    FOR t IN SELECT seq.relname FROM pg_class seq
               JOIN pg_depend d ON d.objid = seq.oid AND d.deptype IN ('a', 'i')
               JOIN pg_class tbl ON tbl.oid = d.refobjid
               JOIN pg_namespace n ON n.oid = seq.relnamespace AND n.nspname = 'public'
              WHERE seq.relkind = 'S' AND tbl.relname = ANY(ARRAY['stock', 'daily_price', 'ohlc', 'market_index',
                        'index_daily', 'realtime_price', 'financial_statement', 'ranking', 'recommendation',
                        'reco_snapshot', 'news', 'news_stock', 'news_sentiment', 'overseas_stock', 'overseas_ohlc',
                        'overseas_quote', 'overseas_financial']) LOOP
        EXECUTE format('GRANT UPDATE ON SEQUENCE public.%I TO oos_app', t);
    END LOOP;
END $$;
