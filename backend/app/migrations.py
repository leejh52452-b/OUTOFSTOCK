"""SQL 마이그레이션 실행기 — sql/migrations/V001__이름.sql 을 번호 순서대로 한 번씩 적용한다(Flyway 방식).

왜 따로 두나(ADR-0002):
  · 화면용 표는 SQLAlchemy 메타데이터(create_all + _migrate)로 관리해 왔다. 분석 계층(analytics 스키마)은
    파티션 표·함수·머티리얼라이즈드 뷰처럼 ORM 으로 표현하기 어려운 DDL 이 중심이라 SQL 파일로 관리한다.
  · 파일마다 체크섬을 남긴다 — 이미 적용한 파일을 고치면(운영 DB 와 코드의 스키마가 갈라짐) 시작을 멈추고 알린다.
    바꿀 것은 항상 새 파일(V00N)로 추가한다.
  · init_db 의 advisory lock(같은 트랜잭션) 안에서 돌아 여러 프로세스가 동시에 켜져도 한 번만 적용된다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "sql" / "migrations"
FILE_RE = re.compile(r"^V(\d{3})__([a-z0-9_]+)\.sql$")
# 테스트가 매번 비우지 않는 '참조 데이터' 표(마이그레이션이 채운 값)
REFERENCE_TABLES = {"schema_migrations", "ksic_sector", "indicator_series"}


class MigrationError(RuntimeError):
    """적용된 파일의 내용이 바뀌었거나 번호가 겹침 — 운영 스키마와 코드가 갈라질 수 있어 시작을 멈춘다."""


def files():
    """[(version, description, path)] — 번호 순. 번호 중복이면 오류."""
    out, seen = [], {}
    for p in sorted(MIGRATIONS_DIR.glob("V*.sql")):
        m = FILE_RE.match(p.name)
        if not m:
            raise MigrationError(f"마이그레이션 파일 이름 형식 오류: {p.name} (V001__소문자_이름.sql)")
        v = m.group(1)
        if v in seen:
            raise MigrationError(f"마이그레이션 번호 중복: {seen[v]} / {p.name}")
        seen[v] = p.name
        out.append((v, m.group(2), p))
    return out


def checksum(path: Path) -> str:
    # 줄바꿈 차이(CRLF — 윈도에서 편집·복사)로 체크섬이 달라지지 않게 정규화
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _bootstrap(conn):
    # 이미 있으면 DDL 을 보내지 않는다 — DB 권한을 분리해 앱 계정(DDL 권한 없음)으로 켜도 실패하지 않게
    if not conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = 'analytics'")).first():
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS analytics"))
    if conn.execute(text("SELECT to_regclass('analytics.schema_migrations')")).scalar() is None:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS analytics.schema_migrations ("
            " version varchar(3) PRIMARY KEY, description varchar(100) NOT NULL,"
            " checksum varchar(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(),"
            " execution_ms integer NOT NULL)"))


def applied(conn):
    return {r[0]: r[1] for r in conn.execute(text("SELECT version, checksum FROM analytics.schema_migrations"))}


def apply_all(conn):
    """대기 중인 마이그레이션 적용 → 적용한 파일 이름 목록. conn 은 init_db 의 트랜잭션(잠금 보유)."""
    _bootstrap(conn)
    done = applied(conn)
    out = []
    for v, desc, path in files():
        cs = checksum(path)
        if v in done:
            if done[v] != cs:
                raise MigrationError(f"이미 적용된 마이그레이션 V{v}__{desc}.sql 의 내용이 바뀌었습니다 — "
                                     "적용된 파일은 고치지 말고 새 번호의 파일로 변경 사항을 추가하세요")
            continue
        sql = path.read_text(encoding="utf-8")
        t0 = time.perf_counter()
        raw = conn.connection.driver_connection          # psycopg — 파라미터 없는 execute 는 여러 문장을 한 번에 실행
        with raw.cursor() as cur:
            cur.execute(sql)
        ms = int((time.perf_counter() - t0) * 1000)
        conn.execute(text("INSERT INTO analytics.schema_migrations(version, description, checksum, execution_ms) "
                          "VALUES(:v, :d, :c, :ms)"), {"v": v, "d": desc, "c": cs, "ms": ms})
        log.info("마이그레이션 적용: V%s__%s (%dms)", v, desc, ms)
        out.append(f"V{v}__{desc}")
    return out


def status(conn=None):
    """적용 현황 — [{version, description, applied_at, execution_ms, state}] (state: applied|pending|changed)."""
    from app import db
    rows = {}
    try:
        for r in db.query("SELECT version, description, checksum, applied_at, execution_ms "
                          "FROM analytics.schema_migrations"):
            rows[r["version"]] = r
    except Exception:                                    # noqa: BLE001 — 아직 한 번도 적용 안 됨
        rows = {}
    out = []
    for v, desc, path in files():
        r = rows.get(v)
        state = "pending" if not r else ("applied" if r["checksum"] == checksum(path) else "changed")
        out.append({"version": v, "description": desc, "state": state,
                    "applied_at": str(r["applied_at"]) if r else None,
                    "execution_ms": r["execution_ms"] if r else None})
    return out


def data_tables(conn):
    """analytics 스키마의 '데이터' 표(참조 표·파티션 자식 제외) — 테스트 격리용."""
    rows = conn.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'analytics' AND c.relkind IN ('r', 'p') AND NOT c.relispartition")).all()
    return [r[0] for r in rows if r[0] not in REFERENCE_TABLES]
