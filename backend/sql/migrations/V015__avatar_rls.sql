-- V015 양봉이 꾸미기(2026-10-31) — 산 아이템·착용 상태 표의 행 수준 보안(V010·V014 와 같은 주인 정책).
--   avatar_closet : 계정이 산 아이템(산 가격)과 부위별 착용 상태(옷장)
--   (이름이 avatar_owned 가 아닌 까닭: V009 가 정리한 예전 아바타 상점 표 이름이라, 새로 설치하는 DB 에서 V009 가 지워 버린다)
-- 지금 요청의 주체(app.user) 행만 보이고 쓸 수 있다. 시스템 문맥(app.rls=off — 탈퇴·관리 배치)만 예외.

DO $$
BEGIN
    IF to_regclass('public.avatar_closet') IS NULL THEN
        RETURN;
    END IF;
    ALTER TABLE public.avatar_closet ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.avatar_closet FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS rls_owner ON public.avatar_closet;
    CREATE POLICY rls_owner ON public.avatar_closet USING (public.app_rls_ok(username))
        WITH CHECK (public.app_rls_ok(username));
END $$;
