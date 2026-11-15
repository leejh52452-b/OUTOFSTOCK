-- V010 행 수준 보안(RLS) — 개인 데이터 표는 '지금 요청의 주체' 행만 보이고 쓸 수 있다(app/security/rls.py).
--
--   app_rls_ok(주인) = app.rls 가 'off'(시스템 문맥: 배치·스케줄러·코드에 이름이 드러난 상승 구간) 이거나
--                      주인 = app.user(커넥션을 꺼낼 때 앱이 요청 주체로 설정).
--   아무 설정 없는 세션(사람이 앱 계정으로 직접 접속)은 NULL → 거짓 → 개인 데이터가 보이지 않는다(기본 거부).
--   FORCE: 표 소유자 계정으로 붙어도 정책을 따른다(개발·테스트 DB 처럼 소유자 계정으로 서비스하는 경우까지).
--   TRUNCATE·외래키·유일 제약은 RLS 와 무관하게 동작한다(중복 영수증 차단 등은 그대로).
--
-- 두 사람이 함께 쓰는 행(친구·친구 요청·채팅)은 양쪽 모두에게 보인다. 실습 체결(practice_trade)은 소속 실습의 주인 기준.
-- 공용 데모 실습(user_id='guest')은 누구에게나 보인다(예전 데모 시드 — 화면이 그대로 동작하게).

CREATE OR REPLACE FUNCTION public.app_rls_ok(owner text) RETURNS boolean
    LANGUAGE sql STABLE PARALLEL SAFE AS
$$ SELECT current_setting('app.rls', true) = 'off'
          OR (owner IS NOT NULL AND owner <> '' AND owner = current_setting('app.user', true)) $$;

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES
            ('watchlist', 'user_id'), ('survey_result', 'user_id'), ('learning_log', 'user_id'),
            ('access_log', 'user_id'), ('wallet', 'username'), ('wallet_ledger', 'username'),
            ('attendance', 'username'), ('invest_position', 'username'), ('payment_order', 'username'),
            ('iap_purchase', 'username')) AS v(tbl, col) LOOP
        IF to_regclass('public.' || t.tbl) IS NULL THEN CONTINUE; END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_owner ON public.%I', t.tbl);
        EXECUTE format('CREATE POLICY rls_owner ON public.%I USING (public.app_rls_ok(%I)) '
                       'WITH CHECK (public.app_rls_ok(%I))', t.tbl, t.col, t.col);
    END LOOP;

    FOR t IN SELECT * FROM (VALUES
            ('friendship', 'owner', 'friend'), ('friend_request', 'sender', 'receiver'),
            ('chat_message', 'sender', 'receiver')) AS v(tbl, a, b) LOOP
        IF to_regclass('public.' || t.tbl) IS NULL THEN CONTINUE; END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_party ON public.%I', t.tbl);
        EXECUTE format('CREATE POLICY rls_party ON public.%I USING (public.app_rls_ok(%I) OR public.app_rls_ok(%I)) '
                       'WITH CHECK (public.app_rls_ok(%I) OR public.app_rls_ok(%I))', t.tbl, t.a, t.b, t.a, t.b);
    END LOOP;

    IF to_regclass('public.practice_session') IS NOT NULL THEN
        ALTER TABLE public.practice_session ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.practice_session FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS rls_owner ON public.practice_session;
        CREATE POLICY rls_owner ON public.practice_session
            USING (public.app_rls_ok(user_id) OR user_id = 'guest')
            WITH CHECK (public.app_rls_ok(user_id) OR user_id = 'guest');
    END IF;
    IF to_regclass('public.practice_trade') IS NOT NULL THEN
        ALTER TABLE public.practice_trade ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.practice_trade FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS rls_parent ON public.practice_trade;
        CREATE POLICY rls_parent ON public.practice_trade
            USING (EXISTS (SELECT 1 FROM public.practice_session s WHERE s.session_id = practice_trade.session_id))
            WITH CHECK (EXISTS (SELECT 1 FROM public.practice_session s WHERE s.session_id = practice_trade.session_id));
    END IF;
END $$;
