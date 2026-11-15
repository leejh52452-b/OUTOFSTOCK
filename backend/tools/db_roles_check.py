"""DB 계정 권한 점검(읽기 전용 — 아무것도 바꾸지 않음). 서비스가 '필요한 만큼만' 권한을 갖고 있는지 확인한다.

    .venv/bin/python tools/db_roles_check.py            # 결과만 보기
    .venv/bin/python tools/db_roles_check.py --strict   # 권장 상태와 다르면 종료 코드 1(CI 최소 권한 작업)
보는 것: 접속 계정 · 슈퍼유저/DB 생성 권한 · 스키마 생성(DDL) 권한 · 감사 로그 수정·삭제 권한 ·
        조회 계정(ANALYTICS_DATABASE_URL)이 개인정보 표를 읽을 수 있는지 · 암호화 키 설정 여부.
권장 상태: 앱 계정은 DDL·감사 로그 수정 불가, 조회 계정은 계정·지갑·채팅 표 접근 불가. (deploy/sql/roles.sql)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

PRIVATE = ("app_user", "auth_token", "auth_session", "user_mfa", "wallet", "wallet_ledger", "chat_message",
           "payment_order", "audit_log", "access_log", "security_event", "pii_access_log")
RLS_TABLES = ("watchlist", "wallet", "wallet_ledger", "chat_message", "friendship", "practice_session", "access_log")


def check(url, label):
    eng = create_engine(url, future=True)
    out = []
    with eng.connect() as c:
        role = c.execute(text("SELECT current_user")).scalar()
        su, cdb = c.execute(text("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user")).one()
        ddl_public = c.execute(text("SELECT has_schema_privilege('public', 'CREATE')")).scalar()
        ddl_an = c.execute(text("SELECT has_schema_privilege('analytics', 'CREATE')")).scalar() if c.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = 'analytics'")).first() else None
        owner = c.execute(text("SELECT pg_get_userbyid(datdba) = current_user FROM pg_database "
                               "WHERE datname = current_database()")).scalar()
        out.append((f"[{label}] 접속 계정", role, None))
        out.append(("슈퍼유저", su, False))
        out.append(("DB 생성 권한", cdb, False))
        out.append(("DB 소유자", owner, False))
        out.append(("public 스키마 DDL", ddl_public, False))
        out.append(("analytics 스키마 DDL", ddl_an, False))
        if c.execute(text("SELECT to_regclass('public.audit_log')")).scalar():
            out.append(("감사 로그 UPDATE 권한", c.execute(text(
                "SELECT has_table_privilege('public.audit_log', 'UPDATE')")).scalar(), False))
            out.append(("감사 로그 DELETE 권한", c.execute(text(
                "SELECT has_table_privilege('public.audit_log', 'DELETE')")).scalar(), False))
        out.append(("RLS 우회(BYPASSRLS)", c.execute(text(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar(), False))
        for t in RLS_TABLES:
            if c.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{t}"}).scalar():
                out.append((f"RLS 강제 {t}", c.execute(text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
                    {"t": f"public.{t}"}).scalar(), True))
        if c.execute(text("SELECT to_regclass('public.pii_access_log')")).scalar():
            out.append(("열람 기록 DELETE 권한", c.execute(text(
                "SELECT has_table_privilege('public.pii_access_log', 'DELETE')")).scalar(), False))
        if c.execute(text("SELECT to_regclass('public.integrity_baseline')")).scalar():     # V016 — 기준선은 소유자만
            out.append(("무결성 기준선 쓰기 권한", c.execute(text(
                "SELECT has_table_privilege('public.integrity_baseline', 'INSERT') "
                "OR has_table_privilege('public.integrity_baseline', 'UPDATE')")).scalar(), False))
        if c.execute(text("SELECT to_regclass('public.security_assessment')")).scalar():    # V016 — 진단 결과는 추가만
            out.append(("수준진단 기록 수정·삭제 권한", c.execute(text(
                "SELECT has_table_privilege('public.security_assessment', 'UPDATE') "
                "OR has_table_privilege('public.security_assessment', 'DELETE')")).scalar(), False))
        if c.execute(text("SELECT to_regclass('public.change_record')")).scalar():          # V017 — 변경 기록은 지울 수 없다
            out.append(("변경 기록 삭제·해시 수정 권한", c.execute(text(
                "SELECT has_table_privilege('public.change_record', 'DELETE') "
                "OR has_column_privilege('public.change_record', 'release_sha256', 'UPDATE')")).scalar(), False))
        if c.execute(text("SELECT to_regclass('public.archive_log')")).scalar():            # V018 — 보관 기록은 해시를 고칠 수 없다
            out.append(("보관 기록 삭제·해시 수정 권한", c.execute(text(
                "SELECT has_table_privilege('public.archive_log', 'DELETE') "
                "OR has_column_privilege('public.archive_log', 'sha256', 'UPDATE')")).scalar(), False))
        if c.execute(text("SELECT to_regclass('public.incident')")).scalar():               # V017 — 사고 기록은 지울 수 없다
            out.append(("장애·사고 기록 삭제 권한", c.execute(text(
                "SELECT has_table_privilege('public.incident', 'DELETE')")).scalar(), False))
        if label == "조회":
            for t in PRIVATE:
                if c.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{t}"}).scalar():
                    out.append((f"개인정보 표 {t} 읽기", c.execute(text(
                        "SELECT has_table_privilege(:t, 'SELECT')"), {"t": f"public.{t}"}).scalar(), False))
    eng.dispose()
    return out


def main():
    strict = "--strict" in sys.argv
    rows = check(settings.DATABASE_URL, "서비스")
    if settings.ANALYTICS_DATABASE_URL:
        rows += check(settings.ANALYTICS_DATABASE_URL, "조회")
    else:
        rows.append(("[조회] ANALYTICS_DATABASE_URL", "없음(분석 CLI 도 서비스 계정 + READ ONLY 트랜잭션)", None))
    rows.append(("데이터 암호화 키(DATA_ENCRYPTION_KEY)", "설정됨" if settings.DATA_ENCRYPTION_KEY else "없음", None))
    warn = 0
    for name, val, want in rows:
        bad = want is not None and bool(val) != want
        warn += bad
        print(f"{'⚠' if bad else '✓'} {name}: {val}")
    print("\n권장 상태와 다른 항목:", warn, "— deploy/sql/roles.sql 참고" if warn else "")
    if not settings.DATA_ENCRYPTION_KEY:
        print("⚠ DATA_ENCRYPTION_KEY 가 없어 KIS 토큰·결제 키가 평문으로 저장됩니다(.env.example 참고)")
    return 1 if (strict and warn) else 0


if __name__ == "__main__":
    sys.exit(main())
