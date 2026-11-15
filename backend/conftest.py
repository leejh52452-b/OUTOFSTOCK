"""pytest 공통 픽스처 — PostgreSQL 테스트 DB 사용(SQLite 미사용).

테스트 DB(기본 outofstock_test)에 스키마를 한 번 만들고, 매 테스트 전 전체 TRUNCATE 로 격리한다.
compose 의 db 를 그대로 쓰거나(권장), TEST_DATABASE_URL 로 다른 PostgreSQL 을 지정할 수 있다.
  docker compose up -d db   # 그러면 outofstock_test 도 자동 생성됨(initdb 스크립트)
  pytest -q
"""
import os
import pytest

os.environ.setdefault("WARMUP_ON_START", "false")            # 서버 시작 예열 스레드가 테스트 DB 정리(TRUNCATE)와 겹치지 않게
os.environ.setdefault("PASSWORD_HASH_ITERATIONS", "1000")    # 테스트 속도용(운영 기본 600,000) — 형식·상향 로직은 같음
from sqlalchemy import text

from app import db
from app.config import CURATED

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://oos:oos@localhost:5432/outofstock_test")
# 최소 권한 모드(선택) — 운영처럼 앱은 소유자가 아닌 계정(oos_app: DDL 없음·RLS 적용·감사 로그 수정 불가)으로,
# 스키마 생성·테스트 간 초기화만 소유자 계정(TEST_DATABASE_URL)으로. deploy/sql/roles.sql 로 만든 DB 에서:
#   TEST_DATABASE_URL=…oos_owner…/outofstock_lp TEST_APP_DATABASE_URL=…oos_app…/outofstock_lp pytest -q
TEST_APP_DATABASE_URL = os.getenv("TEST_APP_DATABASE_URL", "")
_owner = {}


def _owner_engine():
    if "eng" not in _owner:
        from sqlalchemy import create_engine
        _owner["eng"] = create_engine(TEST_DATABASE_URL, future=True) if TEST_APP_DATABASE_URL else db.get_engine()
    return _owner["eng"]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from app.config import settings
    db.reset_engine(TEST_APP_DATABASE_URL or TEST_DATABASE_URL)
    if TEST_APP_DATABASE_URL:
        settings.MIGRATION_DATABASE_URL = TEST_DATABASE_URL     # 스키마 변경은 소유자 계정으로만(운영과 같은 분리)
    owner = _owner_engine()
    db.metadata.drop_all(owner)                # 이전 실행 잔재 제거 → 현재 스키마로 재생성
    with owner.begin() as conn:                # 분석 계층(SQL 마이그레이션)도 처음부터 다시 적용
        conn.execute(text("DROP SCHEMA IF EXISTS analytics CASCADE"))
    db.init_db()
    yield


@pytest.fixture
def as_owner():
    """DBA 권한이 필요한 시뮬레이션(옛 스키마 재현·감사 로그 변조·보관 기간 파기)을 소유자 계정으로.
    기본 모드에선 아무것도 하지 않는다. 최소 권한 모드에서는 앱 계정이 이런 일을 '할 수 없다'는 것 자체가 검증 대상."""
    if not TEST_APP_DATABASE_URL:
        yield
        return
    prev = db.get_engine()
    db._engine = _owner_engine()
    try:
        yield
    finally:
        db._engine = prev
        # 시뮬레이션이 표를 다시 만들었을 수 있다 — 최소 권한 설정(V011·V016·V017)을 다시 맞춘다(멱등)
        from pathlib import Path
        mig = Path(__file__).parent / "sql" / "migrations"
        with _owner_engine().begin() as conn:
            with conn.connection.driver_connection.cursor() as cur:   # 파라미터 없는 실행(마이그레이션 실행기와 같은 방식)
                for name in ("V011__least_privilege.sql", "V016__assurance_least_privilege.sql",
                             "V017__internal_control_least_privilege.sql", "V018__db_operations.sql"):
                    cur.execute((mig / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_db(_schema):
    """각 테스트 전 전체 TRUNCATE(+ID 리셋)로 격리."""
    from app import migrations
    tables = ", ".join(f'"{t.name}"' for t in db.metadata.sorted_tables)
    with _owner_engine().begin() as conn:      # TRUNCATE·MV 새로 고침은 소유자 권한(최소 권한 모드에서도 같은 경로)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        an = ", ".join(f"analytics.{t}" for t in migrations.data_tables(conn))
        if an:
            conn.execute(text(f"TRUNCATE {an} RESTART IDENTITY CASCADE"))
        conn.execute(text("REFRESH MATERIALIZED VIEW analytics.mv_stock_snapshot"))
    db.seed_stocks(CURATED)
    from app.services import credit, ratelimit, search, signals
    ratelimit.clear()                          # 로그인·가입 시도 제한 기록도 테스트마다 비움
    search.invalidate()                        # 검색 색인·추천 캐시도 테스트마다 새로
    signals.clear_cache()
    credit.clear_rf_cache()                    # 무위험이자율(국고채 3년) 캐시
    from app.security import events
    events.clear_cache()                       # 자동 대응 IP 차단 목록 캐시
    from app.services import vmarket, yangbong
    yangbong.invalidate()                      # 양봉이 주간 추천 응답 캐시(10분)
    vmarket.clear_cache()                      # 모의투자 시즌 데이터 캐시
    yield


@pytest.fixture(autouse=True)
def no_external_index_calls(monkeypatch):
    """테스트는 외부 네트워크를 쓰지 않는다 — 지수 소스(KIS·야후·FDR)는 막아 두고, 필요한 테스트만 가짜로 바꾼다.
    (.env 에 KIS 키가 있어도 pytest 가 실제 KIS 토큰 발급·시세 호출을 하지 않게)"""
    from app.config import settings
    from app.collectors import indexes

    def blocked(*_a, **_k):
        raise ConnectionError("테스트에서는 외부 지수 소스 호출 안 함")
    for name in ("fetch_kis", "fetch_yahoo", "fetch_fdr"):
        monkeypatch.setattr(indexes, name, blocked)
    monkeypatch.setattr(settings, "INDEX_LIVE_REFRESH", False)
    # 모형 입력 원천(ECOS)도 테스트에서는 외부로 나가지 않게 — 필요한 테스트는 가짜 fetch 를 넘긴다
    from app.collectors import ecos
    monkeypatch.setattr(ecos, "_get", blocked)
    for k in ("ECOS_API_KEY",):
        monkeypatch.setattr(settings, k, "")
    yield
