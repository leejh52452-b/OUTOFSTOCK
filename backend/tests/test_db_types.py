"""DB 타입 표준·재작성 마이그레이션·공간 정리(ADR-0027 · app/db.py).

저장은 맞는 타입(date·timestamp·timestamptz·정수 원)으로, 서비스 계층이 받는 값 형식은 예전 그대로(연결 로더) —
예전 문자열 열이 남은 DB 는 init_db 가 한 번 바꾼다(깨진 값은 격리소에 원값, 대사 실패면 전체 롤백)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app import db


def test_reads_keep_the_legacy_service_contract():
    """date → 'YYYY-MM-DD', 업무 시각 → 'YYYY-MM-DDTHH:MM:SS'(한국시간), 시세·뉴스 시각 → 한국시간 datetime(isoformat 이 예전 문자열)."""
    with db.transaction() as c:
        c.execute(text("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) "
                       "VALUES('TYP001', '2026-10-30', 70000.4, 71000, 69000, 70500.6, 10)"))
        c.execute(text("INSERT INTO realtime_price(ticker, price, ts, session_date) "
                       "VALUES('TYP001', 70500, '2026-10-30T15:30:00+09:00', '2026-10-30')"))
        c.execute(text("INSERT INTO alert_log(key, level, title, created_at) VALUES('typ:test', 'info', 't', :t)"),
                  {"t": "2026-10-30T15:31:02"})
    o = db.query("SELECT trade_date, open, close FROM ohlc WHERE ticker = 'TYP001'")[0]
    assert o["trade_date"] == "2026-10-30"
    assert (o["open"], o["close"]) == (70000, 70501)                     # 원화 가격 = 정수 원(반올림)
    r = db.query("SELECT ts, session_date FROM realtime_price WHERE ticker = 'TYP001'")[0]
    assert r["session_date"] == "2026-10-30"
    assert r["ts"].isoformat() == "2026-10-30T15:30:00+09:00"            # 예전 저장 문자열과 같은 표기
    assert r["ts"] == datetime(2026, 10, 30, 6, 30, tzinfo=timezone.utc)   # 같은 순간
    a = db.query("SELECT created_at FROM alert_log WHERE key = 'typ:test'")[0]
    assert a["created_at"] == "2026-10-30T15:31:02"


def test_connection_timezone_is_seoul_whatever_the_server_says():
    """시간대 없는 시각 문자열을 timestamptz 와 비교·저장하면 한국시간으로(clock.py 규칙) — 서버가 UTC 여도."""
    with db.get_engine().connect() as c:
        assert c.execute(text("SHOW TimeZone")).scalar() == "Asia/Seoul"
        v = c.execute(text("SELECT CAST('2026-10-30T09:00:00' AS timestamptz)")).scalar()
        assert v == datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
        assert c.execute(text("SELECT CAST('2026-10-30' AS date)")).scalar() == "2026-10-30"


def _legacy(c):
    """세 표를 예전 문자열·double 형식으로 되돌리고 깨진 값을 넣는다(트랜잭션 안 — 시험 뒤 롤백)."""
    c.execute(text("DROP INDEX IF EXISTS ix_news_ts"))
    c.execute(text("ALTER TABLE news ALTER COLUMN published_ts TYPE varchar(40) "
                   "USING to_char(published_ts AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD\"T\"HH24:MI:SS') || '+09:00', "
                   "ALTER COLUMN published_at TYPE varchar(40) USING published_at::text"))
    c.execute(text("ALTER TABLE access_log ALTER COLUMN at TYPE varchar(30) "
                   "USING to_char(at, 'YYYY-MM-DD\"T\"HH24:MI:SS')"))
    c.execute(text("ALTER TABLE ohlc ALTER COLUMN close TYPE double precision"))
    c.execute(text("ALTER TABLE alert_log ALTER COLUMN created_at TYPE varchar(30) "
                   "USING to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS')"))
    rows = [("https://ex.com/ty/1", "2026-09-29", "2026-09-29T08:30:00+09:00"),       # 한국시간 표기
            ("https://ex.com/ty/2", "2026-09-29", "2026-09-29T00:30:00Z"),            # UTC 표기 → 09:30 KST
            ("https://ex.com/ty/3", "2026-09-29", "2026-09-29T10:00:00"),             # 시간대 없음 = 한국시간
            ("https://ex.com/ty/4", "2026-02-30", "2026-02-30T10:00:00+09:00"),       # 달력에 없는 날
            ("https://ex.com/ty/5", "", "N/A")]
    ids = [c.execute(text("INSERT INTO news(title, url, published_at, published_ts) VALUES('t', :u, :d, :ts) "
                          "RETURNING news_id"), {"u": u, "d": d, "ts": ts}).scalar() for u, d, ts in rows]
    c.execute(text("INSERT INTO access_log(user_id, event, at) VALUES('ty-user', 'login', '2026-13-01T00:00:00')"))
    c.execute(text("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) "
                   "VALUES('TYP002', '2026-10-01', 1, 1, 1, 'NaN', 1), ('TYP002', '2026-10-02', 1, 1, 1, 70500.5, 1)"))
    c.execute(text("INSERT INTO alert_log(key, level, title, created_at) VALUES('typ:bad', 'info', 't', 'yesterday?')"))
    return ids


def test_retype_converts_legacy_values_quarantines_broken_ones_and_reconciles(as_owner):
    eng = db.get_engine()
    with eng.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("SET LOCAL app.rls = 'off'"))
            ids = _legacy(c)
            plan = db._retype_plan(c)
            assert {"news", "access_log", "ohlc", "alert_log"} <= set(plan)
            applied = db._retype(c)
            assert any(a.startswith("retype:news") for a in applied)
            ty = dict(c.execute(text("SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                                     "WHERE table_schema = current_schema() AND (table_name, column_name) IN "
                                     "(('news','published_ts'), ('news','published_at'), ('access_log','at'), "
                                     "('ohlc','close'), ('alert_log','created_at'))")).all())
            assert ty == {"news.published_ts": "timestamp with time zone", "news.published_at": "date",
                          "access_log.at": "timestamp without time zone", "ohlc.close": "integer",
                          "alert_log.created_at": "timestamp without time zone"}
            got = {r[0]: r[1:] for r in c.execute(text(
                "SELECT news_id, published_at, published_ts FROM news WHERE news_id = ANY(:i)"), {"i": ids}).all()}
            utc = timezone.utc
            assert got[ids[0]] == ("2026-09-29", datetime(2026, 9, 28, 23, 30, tzinfo=utc))
            assert got[ids[1]] == ("2026-09-29", datetime(2026, 9, 29, 0, 30, tzinfo=utc))
            assert got[ids[2]] == ("2026-09-29", datetime(2026, 9, 29, 1, 0, tzinfo=utc))
            assert got[ids[3]] == (None, None) and got[ids[4]] == (None, None)     # 깨진 값 → NULL
            q = c.execute(text("SELECT table_name, rule, detail FROM data_quality_issue WHERE rule LIKE 'retype:%' "
                               "ORDER BY issue_id")).all()
            raw = " ".join(r[2] for r in q)
            assert "2026-02-30T10:00:00+09:00" in raw and "N/A" in raw and "2026-13-01T00:00:00" in raw
            assert "NaN" in raw and "yesterday?" in raw                         # 원값은 격리소에 남는다
            closes = c.execute(text("SELECT close FROM ohlc WHERE ticker = 'TYP002' ORDER BY trade_date")).scalars().all()
            assert closes[0] is None and closes[1] in (70500, 70501)            # NaN → NULL, 소수 원 → 정수
            bad = c.execute(text("SELECT created_at FROM alert_log WHERE key = 'typ:bad'")).scalar()
            assert bad == "1970-01-01T00:00:00"                                 # NOT NULL 열은 기준 시각(원값은 격리소)
            summary = c.execute(text("SELECT value FROM app_meta WHERE key LIKE 'migration:retype:%' "
                                     "ORDER BY key DESC LIMIT 1")).scalar()
            assert '"invalid": 2' in summary and '"nonnull_after"' in summary   # 표·열별 대사 기록
            assert db._retype_plan(c) == {}                                     # 다시 돌리면 할 일 없음(멱등)
        finally:
            tx.rollback()


def test_broken_primary_key_rows_are_quarantined_and_removed_but_unique_columns_keep_the_row(as_owner):
    """기본키의 깨진 날짜는 그 행을 둘 곳이 없다(격리 후 삭제) — 유일 제약만 있는 열(NULL 허용)은 행을 지우지 않고 NULL."""
    with db.get_engine().connect() as c:
        tx = c.begin()
        try:
            c.execute(text("SET LOCAL app.rls = 'off'"))
            c.execute(text("ALTER TABLE index_daily ALTER COLUMN trade_date TYPE varchar(10) USING trade_date::text"))
            c.execute(text("ALTER TABLE attendance ALTER COLUMN attend_date TYPE varchar(10) USING attend_date::text"))
            c.execute(text("INSERT INTO index_daily(code, trade_date, close) VALUES('KOSPI', '2026-10-30', 2600), "
                           "('KOSPI', '2026-02-30', 2500)"))
            aid = c.execute(text("INSERT INTO attendance(username, attend_date, reward, streak) "
                                 "VALUES('ty-att', '2026/10/31', 100, 1) RETURNING id")).scalar()
            db._retype(c)
            assert c.execute(text("SELECT trade_date FROM index_daily WHERE code = 'KOSPI'")).scalars().all() == ["2026-10-30"]
            assert c.execute(text("SELECT attend_date FROM attendance WHERE id = :i"), {"i": aid}).first() == (None,)
            q = dict(c.execute(text("SELECT table_name, detail FROM data_quality_issue WHERE rule LIKE 'retype:%'")).all())
            assert "2026-02-30" in q["index_daily"] and "code,trade_date=KOSPI,2026-02-30" in q["index_daily"]
            assert "2026/10/31" in q["attendance"]
            summary = c.execute(text("SELECT value FROM app_meta WHERE key LIKE 'migration:retype:%' "
                                     "ORDER BY key DESC LIMIT 1")).scalar()
            assert '"rows_deleted": 1' in summary and '"row_deleted"' in summary
        finally:
            tx.rollback()


def test_reconciliation_failure_rolls_everything_back(monkeypatch, as_owner):
    """바뀐 뒤 값 있는 행 수가 '전 - 깨진 값'과 다르면 예외 — 조용히 값을 잃지 않는다."""
    eng = db.get_engine()
    real_conv = db._retype_conv
    monkeypatch.setattr(db, "_retype_conv", lambda kind, c: "NULL::date" if kind == "date" else real_conv(kind, c))
    with eng.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("SET LOCAL app.rls = 'off'"))
            _legacy(c)
            with pytest.raises(RuntimeError, match="대사 실패"):
                db._retype(c)
        finally:
            tx.rollback()
    assert db.query("SELECT data_type FROM information_schema.columns WHERE table_name = 'news' "
                    "AND column_name = 'published_at'")[0]["data_type"] == "date"


def test_news_url_is_unique_by_md5_key_and_lookups_use_it():
    have = {r["indexname"] for r in db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'news'")}
    assert "ux_news_url_md5" in have and "news_url_key" not in have
    url = "https://news.google.com/rss/articles/CBMi" + "x" * 160 + "?oc=5"
    with db.transaction() as c:
        for ts in (None, "2026-10-30T09:00:00+09:00"):
            c.execute(text("INSERT INTO news(title, url, published_at, published_ts) VALUES('t', :u, '2026-10-30', :ts) "
                           f"{db.NEWS_URL_CONFLICT} DO UPDATE SET published_ts = EXCLUDED.published_ts "
                           "WHERE news.published_ts IS NULL"), {"u": url, "ts": ts})
        nid = c.execute(text(db.NEWS_ID_BY_URL), {"u": url}).scalar()
    rows = db.query("SELECT news_id, published_ts FROM news WHERE url = :u", {"u": url})
    assert len(rows) == 1 and rows[0]["news_id"] == nid and rows[0]["published_ts"] is not None
    with db.get_engine().connect() as c:
        c.execute(text("SET LOCAL enable_seqscan = off"))                 # 시험 DB 는 작아서 순차 읽기를 고를 수 있다
        plan = "\n".join(r[0] for r in c.execute(text("EXPLAIN " + db.NEWS_ID_BY_URL), {"u": url}).all())
    assert "ux_news_url_md5" in plan


def test_news_time_expression_is_indexed_and_typed():
    idx = db.query("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_news_ts'")[0]["indexdef"]
    assert "Asia/Seoul" in idx and "published_at" in idx
    with db.get_engine().connect() as c:
        c.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(r[0] for r in c.execute(text(
            f"EXPLAIN SELECT news_id FROM news n WHERE {db.NEWS_TS.format(p='n.')} >= '2026-10-01T00:00:00+09:00' "
            f"ORDER BY {db.NEWS_TS.format(p='n.')} DESC LIMIT 5")).all())
    assert "ix_news_ts" in plan


def test_redundant_indexes_are_gone_and_storage_params_are_set():
    have = {r["indexname"] for r in db.query("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert not (set(db._REDUNDANT_INDEXES) & have)
    assert {"ohlc_pkey", "uq_ledger_idem", "uq_attendance_day", "uq_friendship"} <= have
    opts = {r["relname"]: set(r["reloptions"] or []) for r in db.query(
        "SELECT relname, reloptions FROM pg_class WHERE relname = ANY(:t)",
        {"t": ["realtime_price", "news", "wallet", "search_log"]})}
    assert "fillfactor=70" in opts["realtime_price"] and "fillfactor=80" in opts["wallet"]
    assert "autovacuum_vacuum_scale_factor=0.02" in opts["news"] and "autovacuum_vacuum_scale_factor=0.02" in opts["search_log"]
    an = db.query("SELECT indexname FROM pg_indexes WHERE schemaname = 'analytics' AND tablename = 'credit_score'")
    assert "ix_credit_score_ticker" not in {r["indexname"] for r in an}   # V018 — 기본키가 같은 조회를 처리


def test_metadata_has_no_string_dates_outside_documented_exceptions():
    """새 열을 문자열 날짜로 만드는 일을 막는다 — 예외는 문서화한 넷(해시 체인·외부 형식·표시가·격리소 원값)."""
    allowed = {("audit_log", "at"), ("dart_corp", "modify_date"), ("data_quality_issue", "trade_date"),
               ("iap_purchase", "price")}
    import re
    bad = []
    for t in db.metadata.sorted_tables:
        for col in t.columns:
            name = col.name
            looks_time = re.search(r"(^|_)(date|day|at|ts|as_of|through|entry)$", name) or name.endswith("_at")
            if looks_time and type(col.type).__name__ in ("String", "VARCHAR", "Text") and (t.name, name) not in allowed:
                bad.append(f"{t.name}.{name}")
    assert not bad, f"문자열 날짜·시각 열: {bad}"
