-- V012 복식부기 원장 무결성 — DB 가 직접 지키는 규칙(앱 코드에 실수가 있어도).
--   1) 거래(txn_id)마다 금액 합 = 0 (차변 = 대변). 커밋 시점에 확인(DEFERRABLE INITIALLY DEFERRED 제약 트리거) —
--      한 트랜잭션 안에서 여러 줄을 넣는 동안은 잠시 맞지 않아도 되고, 커밋할 때 맞지 않으면 전체가 롤백된다.
--   2) 분개는 추가만 — 수정·삭제는 트리거가 거부(소유자 계정 포함). 틀린 분개는 반대 분개로 바로잡는다(회계 원칙).
--   3) 일 마감(ledger_close)도 추가만 — 지난 마감 결과를 고치지 못한다.

CREATE OR REPLACE FUNCTION public.journal_balanced() RETURNS trigger
    LANGUAGE plpgsql AS
$$
DECLARE s bigint;
BEGIN
    SELECT COALESCE(SUM(amount), 0) INTO s FROM public.journal_entry WHERE txn_id = NEW.txn_id;
    IF s <> 0 THEN
        RAISE EXCEPTION '분개 불균형 txn=% 합계=% (차변·대변 합이 0 이어야 함)', NEW.txn_id, s
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION public.append_only() RETURNS trigger
    LANGUAGE plpgsql AS
$$
BEGIN
    RAISE EXCEPTION '% 는 추가만 할 수 있어요(수정·삭제 금지 — 반대 분개로 정정)', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END
$$;

DO $$
BEGIN
    IF to_regclass('public.journal_entry') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_journal_balanced ON public.journal_entry;
        CREATE CONSTRAINT TRIGGER trg_journal_balanced AFTER INSERT ON public.journal_entry
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.journal_balanced();
        DROP TRIGGER IF EXISTS trg_journal_append_only ON public.journal_entry;
        CREATE TRIGGER trg_journal_append_only BEFORE UPDATE OR DELETE ON public.journal_entry
            FOR EACH ROW EXECUTE FUNCTION public.append_only();
    END IF;
    IF to_regclass('public.ledger_close') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_close_append_only ON public.ledger_close;
        CREATE TRIGGER trg_close_append_only BEFORE UPDATE OR DELETE ON public.ledger_close
            FOR EACH ROW EXECUTE FUNCTION public.append_only();
    END IF;
END $$;
