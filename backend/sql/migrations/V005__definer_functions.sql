-- V005 DB 권한 분리 대비(3번 정보보호, ADR-0005) — 앱 계정(oos_app)은 DDL 권한이 없다.
-- 그런데 배치는 ① 새 연도 파티션 만들기 ② 종목 스냅샷(MV) 새로 고침을 해야 한다(둘 다 소유자 권한 필요).
-- → 그 두 작업만 '소유자 권한으로 실행되는 함수'(SECURITY DEFINER)로 감싸고, 앱 계정에는 실행 권한만 준다.
--   search_path 를 고정(analytics, pg_temp)해 다른 스키마의 같은 이름 객체로 바꿔치기하는 공격을 막는다.

CREATE OR REPLACE FUNCTION analytics.ensure_price_partition(y integer) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = analytics, pg_temp AS $$
DECLARE
    part text := format('price_history_%s', y);
BEGIN
    IF y < 1990 OR y > 2100 THEN
        RAISE EXCEPTION '연도 범위 밖: %', y;
    END IF;
    IF to_regclass('analytics.' || part) IS NULL THEN
        EXECUTE format('CREATE TABLE analytics.%I PARTITION OF analytics.price_history '
                       'FOR VALUES FROM (%L) TO (%L)', part, make_date(y, 1, 1), make_date(y + 1, 1, 1));
    END IF;
    RETURN part;
END $$;

CREATE OR REPLACE FUNCTION analytics.refresh_snapshot(concurrent boolean DEFAULT true) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = analytics, pg_temp AS $$
BEGIN
    IF concurrent AND (SELECT ispopulated FROM pg_matviews
                       WHERE schemaname = 'analytics' AND matviewname = 'mv_stock_snapshot') THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_stock_snapshot;   -- 읽기를 막지 않음
    ELSE
        REFRESH MATERIALIZED VIEW analytics.mv_stock_snapshot;
    END IF;
    RETURN (SELECT count(*) FROM analytics.mv_stock_snapshot);
END $$;

-- 기본은 누구도 실행 못 하게 하고(PUBLIC 회수), deploy/sql/roles.sql 에서 앱 계정에만 실행 권한을 준다.
REVOKE ALL ON FUNCTION analytics.ensure_price_partition(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION analytics.refresh_snapshot(boolean) FROM PUBLIC;
