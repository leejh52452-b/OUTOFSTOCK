-- DB 계정 분리(최소 권한) — 3번 정보보호, ADR-0005
--
--   oos_owner : 스키마 소유자. 표·함수·마이그레이션(run.py initdb). 서비스 프로세스는 이 계정을 쓰지 않는다.
--   oos_app   : 서비스·배치. 데이터 읽기/쓰기(DML)만. DDL 없음. 감사 로그는 INSERT·SELECT 만(UPDATE·DELETE 없음).
--   oos_ro    : 분석·조회. analytics 스키마와 공개 데이터 표만 SELECT. 개인정보·계정·지갑 표는 못 읽음.
--
-- 실행(관리자, VM):
--   sudo -u postgres psql -d outofstock \
--        -v owner_pw="'소유자비번'" -v app_pw="'앱비번'" -v ro_pw="'조회비번'" -f deploy/sql/roles.sql
-- 그 뒤 .env:  DATABASE_URL=postgresql+psycopg://oos_app:앱비번@localhost:5432/outofstock
--             ANALYTICS_DATABASE_URL=postgresql+psycopg://oos_ro:조회비번@localhost:5432/outofstock
--   마이그레이션(업데이트 때만): MIGRATION_DATABASE_URL=…oos_owner… .venv/bin/python run.py initdb
--   (MIGRATION_DATABASE_URL 은 서비스 .env 가 아니라 root 만 읽는 파일에 두는 것을 권장)
-- 확인: .venv/bin/python tools/db_roles_check.py
-- 여러 번 실행해도 된다(이미 있으면 비밀번호·권한만 다시 맞춤).

\set ON_ERROR_STOP on
SELECT current_database() AS db \gset

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_owner') THEN CREATE ROLE oos_owner LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN CREATE ROLE oos_app LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_ro') THEN CREATE ROLE oos_ro LOGIN; END IF;
END $$;
ALTER ROLE oos_owner PASSWORD :owner_pw;
ALTER ROLE oos_app PASSWORD :app_pw;
ALTER ROLE oos_ro PASSWORD :ro_pw;
-- 앱·조회 계정은 슈퍼유저·DB 생성·역할 생성 권한 없음(기본값이지만 명시)
ALTER ROLE oos_app NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE oos_ro NOSUPERUSER NOCREATEDB NOCREATEROLE;
-- 조회 계정은 오래 걸리는 쿼리를 스스로 끊는다
ALTER ROLE oos_ro SET statement_timeout = '10s';
ALTER ROLE oos_ro SET default_transaction_read_only = on;

-- 소유권: 데이터베이스·스키마·표·시퀀스·함수·뷰를 oos_owner 로(기존 소유자가 누구든)
ALTER DATABASE :"db" OWNER TO oos_owner;
ALTER SCHEMA public OWNER TO oos_owner;
CREATE SCHEMA IF NOT EXISTS analytics AUTHORIZATION oos_owner;
ALTER SCHEMA analytics OWNER TO oos_owner;
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ('public', 'analytics') AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition LOOP
    -- 시퀀스(serial)는 표를 따라 소유자가 함께 바뀐다
    EXECUTE format('ALTER %s %I.%I OWNER TO oos_owner',
                   CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
                   r.nspname, r.relname);
  END LOOP;
  FOR r IN SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'analytics' AND c.relispartition AND c.relkind IN ('r', 'p') LOOP   -- 파티션 자식 표도
    EXECUTE format('ALTER TABLE %I.%I OWNER TO oos_owner', r.nspname, r.relname);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS f FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'analytics' LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO oos_owner', r.f);
  END LOOP;
END $$;

-- 아무나 public 스키마에 객체를 만들지 못하게(PostgreSQL 15 이전 기본값 보완)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO oos_app;      -- TEMPORARY: 대량 적재(COPY 임시 표)
GRANT CONNECT ON DATABASE :"db" TO oos_ro;

-- 앱: 데이터 읽기/쓰기만
GRANT USAGE ON SCHEMA public, analytics TO oos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics TO oos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public, analytics TO oos_app;
-- 함수 권한은 함수가 있을 때만(빈 DB 에 initdb 전에 실행해도 실패하지 않게 — initdb 의 V011 이 다시 맞춘다)
DO $$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY['analytics.ensure_price_partition(integer)', 'analytics.refresh_snapshot(boolean)',
                           'analytics.refresh_market_monthly(integer)', 'analytics.refresh_price_adj(date)',
                           'analytics.adj_prices(date, date, text[])'] LOOP
    IF to_regprocedure(f) IS NOT NULL THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO oos_app', f); END IF;
  END LOOP;
END $$;
-- 감사 로그는 추가·조회만 — 지우거나 고칠 수 없다(보관 기간 정리는 소유자 계정으로)
DO $$ BEGIN
  IF to_regclass('public.audit_log') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON public.audit_log FROM oos_app;
  END IF;
END $$;
-- 마이그레이션 이력은 앱이 바꾸지 못하게
DO $$ BEGIN
  IF to_regclass('analytics.schema_migrations') IS NOT NULL THEN
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON analytics.schema_migrations FROM oos_app;
  END IF;
END $$;
-- 제로트러스트(2026-10): 개인정보 열람 기록은 추가·조회만 — 탈퇴 시 대상 아이디 가명화(subject 열)만 허용
DO $$ BEGIN
  IF to_regclass('public.pii_access_log') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON public.pii_access_log FROM oos_app;
    GRANT UPDATE (subject) ON public.pii_access_log TO oos_app;
  END IF;
  IF to_regclass('public.security_event') IS NOT NULL THEN
    REVOKE TRUNCATE ON public.security_event FROM oos_app;     -- 보관 기간 파기는 행 단위 DELETE 로만(대량 삭제 흔적)
  END IF;
  -- 두 사람 표(V013): 쓴 뒤 바꿀 수 있는 열만 — 보낸 사람·받는 사람·본문은 고칠 수 없다
  IF to_regclass('public.chat_message') IS NOT NULL THEN
    REVOKE UPDATE ON public.chat_message FROM oos_app;
    GRANT UPDATE (read_at, body) ON public.chat_message TO oos_app;   -- body: 키 교체 재포장(crypto-rotate)
  END IF;
  IF to_regclass('public.friend_request') IS NOT NULL THEN
    REVOKE UPDATE ON public.friend_request FROM oos_app;
    GRANT UPDATE (status, responded_at) ON public.friend_request TO oos_app;
  END IF;
  IF to_regclass('public.friendship') IS NOT NULL THEN
    REVOKE UPDATE ON public.friendship FROM oos_app;
  END IF;
  -- 자율보안(V016): 수준진단 결과는 추가·조회만, 파일 무결성 기준선은 조회만(기준선은 배포 때 소유자 계정이 쓴다)
  IF to_regclass('public.security_assessment') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON public.security_assessment FROM oos_app;
  END IF;
  IF to_regclass('public.integrity_baseline') IS NOT NULL THEN
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.integrity_baseline FROM oos_app;
  END IF;
  -- IT 내부통제(V017): 변경 기록은 지울 수 없고 상태·승인·적용·검토 칸만 수정, 장애·사고 사건은 지울 수 없다
  IF to_regclass('public.change_record') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON public.change_record FROM oos_app;
    GRANT UPDATE (status, approved_by, approved_at, applied_at, applied_by, baseline_id,
                  reviewed_by, reviewed_at, review_note) ON public.change_record TO oos_app;
  END IF;
  IF to_regclass('public.incident') IS NOT NULL THEN
    REVOKE DELETE, TRUNCATE ON public.incident FROM oos_app;
  END IF;
  -- DB 운영(V018): 보관 기록은 지울 수 없고 파일·해시·행 수를 고칠 수 없다(정리·검증·복원 시각만)
  IF to_regclass('public.archive_log') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON public.archive_log FROM oos_app;
    GRANT UPDATE (deleted_at, deleted_rows, verified_at, restored_at) ON public.archive_log TO oos_app;
  END IF;
END $$;
-- 행 수준 보안: 앱 계정은 정책을 우회하지 못한다(BYPASSRLS 없음 — 기본값이지만 명시). 정책은 V010 마이그레이션.
ALTER ROLE oos_app NOBYPASSRLS;
ALTER ROLE oos_ro NOBYPASSRLS;

-- 백업 전용(선택, -v backup_pw="'백업비번'" 을 주면 만든다) — tools/dr.py backup 의 BACKUP_DATABASE_URL
--   개인 데이터 표는 FORCE RLS 라 일반 계정의 pg_dump 는 실패한다(V010 이후). 그래서 '읽기 전용 + RLS 우회'인 계정을 따로 둔다:
--   pg_read_all_data(모든 표 SELECT) + BYPASSRLS. 쓰기·DDL·DB 생성 권한 없음, 트랜잭션은 기본 읽기 전용, 동시 접속 2.
--   BYPASSRLS 는 강한 권한 — 이 계정은 백업 서버(또는 로컬 타이머)에서만 쓰고, pg_hba 에서 그 주소로만 허용한다.
\if :{?backup_pw}
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_backup') THEN CREATE ROLE oos_backup LOGIN; END IF;
END $$;
ALTER ROLE oos_backup PASSWORD :backup_pw;
ALTER ROLE oos_backup NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS CONNECTION LIMIT 2;
ALTER ROLE oos_backup SET default_transaction_read_only = on;
GRANT pg_read_all_data TO oos_backup;
GRANT CONNECT ON DATABASE :"db" TO oos_backup;
\endif

-- 조회: 분석 계층 전체 + 공개 시세·종목 표만(계정·지갑·채팅·결제·감사·토큰 표는 제외)
GRANT USAGE ON SCHEMA public, analytics TO oos_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO oos_ro;
DO $$ DECLARE t text; f text; BEGIN
  FOREACH t IN ARRAY ARRAY['stock', 'daily_price', 'ohlc', 'financial_statement', 'market_index', 'index_daily',
                           'ranking', 'news', 'news_stock', 'news_sentiment', 'reco_snapshot', 'yangbong_basket'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN EXECUTE format('GRANT SELECT ON public.%I TO oos_ro', t); END IF;
  END LOOP;
  FOREACH f IN ARRAY ARRAY['analytics.latest_trade_date()', 'analytics.sector_performance(text)',
                           'analytics.sector_of(text)', 'analytics.adj_prices(date, date, text[])'] LOOP
    IF to_regprocedure(f) IS NOT NULL THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO oos_ro', f); END IF;
  END LOOP;
END $$;

-- 앞으로 oos_owner 가 만드는 표·시퀀스에도 같은 권한이 자동으로(새 마이그레이션 대비)
ALTER DEFAULT PRIVILEGES FOR ROLE oos_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE oos_owner IN SCHEMA analytics GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE oos_owner IN SCHEMA public, analytics GRANT USAGE, SELECT ON SEQUENCES TO oos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE oos_owner IN SCHEMA analytics GRANT SELECT ON TABLES TO oos_ro;
