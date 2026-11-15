-- V014 웹 화면(팀 확정 디자인) 연동으로 생긴 개인 활동 표 — 행 수준 보안(V010 과 같은 주인 정책).
--   practice_record : 웹 '학습 및 실습하기' 한 판씩의 기록(브라우저 계산값 — 표시용, 보상과 무관)
--   quiz_attempt    : Daily Stock Quiz 결과(계정·날짜마다 1행, 서버 채점)
-- 지금 요청의 주체(app.user) 행만 보이고 쓸 수 있다. 시스템 문맥(app.rls=off — 탈퇴·관리 배치)만 예외.

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES ('practice_record', 'user_id'), ('quiz_attempt', 'user_id')) AS v(tbl, col) LOOP
        IF to_regclass('public.' || t.tbl) IS NULL THEN CONTINUE; END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_owner ON public.%I', t.tbl);
        EXECUTE format('CREATE POLICY rls_owner ON public.%I USING (public.app_rls_ok(%I)) '
                       'WITH CHECK (public.app_rls_ok(%I))', t.tbl, t.col, t.col);
    END LOOP;
END $$;

-- 앱 계정(roles.sql 을 쓰는 DB 에서만): 퀴즈 결과는 한 번 쓰면 고치지 않는다(같은 날 두 번째 제출은 처음 결과를 돌려줌)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.quiz_attempt') IS NOT NULL THEN
        REVOKE UPDATE ON public.quiz_attempt FROM oos_app;
    END IF;
    IF to_regclass('public.practice_record') IS NOT NULL THEN
        REVOKE UPDATE ON public.practice_record FROM oos_app;
    END IF;
END $$;
