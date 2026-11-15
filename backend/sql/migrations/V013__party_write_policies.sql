-- V013 두 사람이 함께 쓰는 표(채팅·친구 요청)의 '쓰기' 정책을 좁힌다 — 보는 것은 양쪽, 새로 쓰는 행은 보내는 사람만.
--
-- V010 의 rls_party 는 USING·WITH CHECK 모두 '보낸 사람 또는 받는 사람이 나'였다. 읽기에는 맞지만, 새 행을 쓸 때도 같은 조건이라
-- 앱 코드에 실수가 생기면 RLS 가 '남을 보낸 사람으로 적은 메시지·친구 요청'(받는 사람 = 나)을 막지 못했다(2026-10-23 독립 점검).
-- RLS 는 앱 인가가 뚫렸을 때의 2차 방어이므로 명령마다 정책을 나눈다.
--   SELECT·DELETE : 양쪽(대화 보기·탈퇴·요청 정리)
--   INSERT        : 보내는 사람 = 지금 주체(app.user) — 시스템 문맥(app.rls=off)은 예외(마이그레이션·배치)
--   UPDATE        : 양쪽(읽음 표시는 받는 사람, 요청 수락·거절은 받는 사람, 취소는 보낸 사람) — 어떤 열을 바꿀 수 있는지는
--                   앱 계정의 열 단위 권한으로 한 번 더 좁힌다(아래 · deploy/sql/roles.sql: read_at·body / status·responded_at).
-- friendship 은 수락할 때 (나→상대, 상대→나) 두 행을 함께 쓰므로 V010 의 양쪽 정책을 유지한다(쓰는 경로는 수락 하나뿐).

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES ('chat_message'), ('friend_request')) AS v(tbl) LOOP
        IF to_regclass('public.' || t.tbl) IS NULL THEN CONTINUE; END IF;
        EXECUTE format('DROP POLICY IF EXISTS rls_party ON public.%I', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_party_read ON public.%I', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_party_insert ON public.%I', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_party_update ON public.%I', t.tbl);
        EXECUTE format('DROP POLICY IF EXISTS rls_party_delete ON public.%I', t.tbl);
        EXECUTE format('CREATE POLICY rls_party_read ON public.%I FOR SELECT '
                       'USING (public.app_rls_ok(sender) OR public.app_rls_ok(receiver))', t.tbl);
        EXECUTE format('CREATE POLICY rls_party_insert ON public.%I FOR INSERT '
                       'WITH CHECK (public.app_rls_ok(sender))', t.tbl);
        EXECUTE format('CREATE POLICY rls_party_update ON public.%I FOR UPDATE '
                       'USING (public.app_rls_ok(sender) OR public.app_rls_ok(receiver)) '
                       'WITH CHECK (public.app_rls_ok(sender) OR public.app_rls_ok(receiver))', t.tbl);
        EXECUTE format('CREATE POLICY rls_party_delete ON public.%I FOR DELETE '
                       'USING (public.app_rls_ok(sender) OR public.app_rls_ok(receiver))', t.tbl);
    END LOOP;
END $$;

-- 앱 계정(roles.sql 을 쓰는 DB 에서만)은 바꿀 수 있는 열만 — 보낸 사람·받는 사람(누가 누구에게)은 쓴 뒤 고칠 수 없다.
-- 채팅 본문은 키 교체 때 같은 평문을 새 키로 다시 포장해야 해서 열어 둔다(AAD 가 보낸·받는 사람에 묶여 있어 다른 대화로 옮기면 복호화 실패).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.chat_message') IS NOT NULL THEN
        REVOKE UPDATE ON public.chat_message FROM oos_app;
        GRANT UPDATE (read_at, body) ON public.chat_message TO oos_app;             -- 읽음 표시 · 키 교체(crypto-rotate 재포장)만
    END IF;
    IF to_regclass('public.friend_request') IS NOT NULL THEN
        REVOKE UPDATE ON public.friend_request FROM oos_app;
        GRANT UPDATE (status, responded_at) ON public.friend_request TO oos_app;   -- 수락·거절·취소만
    END IF;
    IF to_regclass('public.friendship') IS NOT NULL THEN
        REVOKE UPDATE ON public.friendship FROM oos_app;                             -- 맺기(INSERT)·끊기(DELETE)만
    END IF;
END $$;
