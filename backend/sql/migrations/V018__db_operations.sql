-- V018 DB 운영(2026-11-03 · ADR-0027) — 수명주기 보관 기록의 앱 계정 권한 · 겹치는 인덱스 정리.
--   archive_log      : 보관 파일 목록(기간·경로·SHA-256·행 수) — 앱은 추가·조회 + 정리·검증·복원 시각 칸만 수정.
--                      지울 수 없고 파일·해시·행 수를 고칠 수 없다(보관 파일 무결성의 근거 — 해시를 바꿔 위조 파일을 들이는 경로 차단).
--   db_size_snapshot : DB 크기 일일 기록 — 앱이 하루 1행 추가·갱신, 2년 지난 행 삭제(제한 없음).
--   analytics.ix_credit_score_ticker : 기본키(ticker, as_of, model_version)가 'ticker = ? ORDER BY as_of DESC' 를
--                      거꾸로 훑어 같은 비용으로 처리한다 — 쓰기·저장·백업 비용만 드는 인덱스(public 쪽 6개는 init_db 가 정리).
DROP INDEX IF EXISTS analytics.ix_credit_score_ticker;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oos_app') THEN
        RETURN;
    END IF;
    IF to_regclass('public.archive_log') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON public.archive_log FROM oos_app;
        GRANT UPDATE (deleted_at, deleted_rows, verified_at, restored_at) ON public.archive_log TO oos_app;
    END IF;
END $$;
