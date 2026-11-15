-- V019 모의투자 — 가상 시장 시즌(2026-11-09) 개인 표의 행 수준 보안(V010·V015 와 같은 주인 정책).
--   vmarket_entry    : 시즌 참가(참가금·시즌 현금·정산 금액)
--   vmarket_position : 종목별 보유(수량·평균단가·실현손익)
--   vmarket_trade    : 체결(매수·매도·정산 자동 매도)
-- 지금 요청의 주체(app.user) 행만 보이고 쓸 수 있다. 시스템 문맥(app.rls=off — 주말 정산 배치·탈퇴)만 예외.
-- 시즌 데이터(vmarket_season)는 개인 표가 아니지만 진행 중 시즌의 '미래 봉'을 담는다 — 조회 계정(oos_ro)에 주지 않는다
-- (roles.sql 의 공개 표 목록에 넣지 않음). 화면에는 API 가 공개된 봉까지만 잘라 준다(app/services/vmarket.py).

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['vmarket_entry', 'vmarket_position', 'vmarket_trade'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS rls_owner ON public.%I', t);
        EXECUTE format('CREATE POLICY rls_owner ON public.%I USING (public.app_rls_ok(username)) '
                       'WITH CHECK (public.app_rls_ok(username))', t);
    END LOOP;
END $$;
