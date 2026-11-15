-- V016 자율보안(2026-11-02) — 증빙 표의 앱 계정 권한을 '필요한 만큼'으로.
--   security_assessment : 수준진단 결과 — 앱은 추가·조회만(지난 진단을 고쳐 등급을 꾸밀 수 없다). 보관 기간 정리는 소유자 배치.
--   integrity_baseline  : 파일 무결성 기준선 — 앱은 조회만. 기준선은 배포 때 소유자(마이그레이션) 계정이 쓴다
--                         (앱 권한을 얻은 공격자가 기준선을 고쳐 심은 파일을 '정상'으로 만들지 못하게).
-- oos_app 역할이 없는 설치(권한 분리 전 — 개발 DB)에서는 아무것도 하지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.security_assessment') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON public.security_assessment FROM oos_app;
    END IF;
    IF to_regclass('public.integrity_baseline') IS NOT NULL THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.integrity_baseline FROM oos_app;
    END IF;
END $$;
