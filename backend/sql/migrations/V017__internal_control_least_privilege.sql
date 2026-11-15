-- V017 IT 내부통제(2026-11-02) — 변경·장애 기록의 앱 계정 권한을 '필요한 만큼'으로.
--   change_record : 변경 기록 — 앱은 추가·조회 + 상태·승인·적용·검토 칸만 수정. 지울 수 없다.
--                   (요청 때 기록한 설치본 해시·사전 영향 분석·자동 점검·사유는 앱 권한으로 고칠 수 없다 — 승인된 설치본 바꿔치기 방지)
--   incident      : 장애·사고 사건 — 앱은 추가·조회·수정(원인·조치·보고 단계). 지울 수 없다(보고 근거, 5년 보관은 소유자 배치).
--   uptime_check  : 가용성 확인 기록 — 앱이 35일 지난 행을 지운다(제한 없음).
-- oos_app 역할이 없는 설치(권한 분리 전 — 개발 DB)에서는 아무것도 하지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.change_record') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON public.change_record FROM oos_app;
        GRANT UPDATE (status, approved_by, approved_at, applied_at, applied_by, baseline_id,
                      reviewed_by, reviewed_at, review_note) ON public.change_record TO oos_app;
    END IF;
    IF to_regclass('public.incident') IS NOT NULL THEN
        REVOKE DELETE, TRUNCATE ON public.incident FROM oos_app;
    END IF;
END $$;
