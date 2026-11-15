"""데이터 수명주기(app/services/ilm.py · ADR-0027) — 보관 기한 지난 달 → 압축 보관·검증 → DB 정리 → 복원.

파일이 검증되기 전에는 한 행도 지우지 않는다 · 해시가 다르면 복원 거부 · 보관 폴더 밖 파일은 들이지 않는다."""
import gzip
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app import db
from app.config import settings
from app.services import ilm

TODAY = date(2026, 11, 3)


@pytest.fixture()
def archive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ILM_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(settings, "NEWS_RETENTION_DAYS", 60)            # 기한 2026-09-04 → 8월까지 보관 대상
    monkeypatch.setattr(settings, "SEARCH_LOG_RETENTION_DAYS", 60)
    return tmp_path / "archive"


def _seed():
    """7·8·10월 기사(종목 태깅·감성 포함) + 검색 기록."""
    ids = {}
    with db.transaction() as c:
        for i, d in enumerate(["2026-07-10", "2026-07-25", "2026-08-03", "2026-10-20"]):
            nid = c.execute(text("INSERT INTO news(title, url, source, published_at, published_ts) "
                                 "VALUES(:t, :u, '샘플일보', :d, :ts) RETURNING news_id"),
                            {"t": f"기사 {i} — 삼성전자", "u": f"https://ex.com/ilm/{i}", "d": d,
                             "ts": f"{d}T09:0{i}:00+09:00"}).scalar()
            c.execute(text("INSERT INTO news_stock(news_id, ticker) VALUES(:n, '005930'), (:n, '000660')"), {"n": nid})
            c.execute(text("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) "
                           "VALUES(:n, 'POS', 0.8, 'lexicon-v1', :c)"), {"n": nid, "c": f"{d}T10:00:00"})
            ids[d] = nid
        for ts in ("2026-07-31T23:30:00+09:00", "2026-08-01T00:10:00+09:00", "2026-10-01T12:00:00+09:00"):
            c.execute(text("INSERT INTO search_log(keyword, ticker, searched_at) VALUES('삼성전자', '005930', :t)"), {"t": ts})
    return ids


def test_plan_takes_only_whole_months_older_than_the_cutoff(archive_dir):
    _seed()
    months = {(m["dataset"], m["start"]) for m in ilm.plan(today=TODAY)}
    assert ("news", "2026-07-01") in months and ("news", "2026-08-01") in months
    assert ("news", "2026-09-01") not in months and ("news", "2026-10-01") not in months   # 9월은 일부가 기한 안
    assert ("search", "2026-07-01") in months and ("search", "2026-08-01") in months
    assert ilm.archive(today=TODAY, dry_run=True)["dry_run"] is True
    assert db.query("SELECT count(*) AS n FROM news WHERE url LIKE 'https://ex.com/ilm/%'")[0]["n"] == 4   # 아무것도 안 바뀜


def test_archive_verifies_then_deletes_and_restore_brings_rows_back(archive_dir):
    ids = _seed()
    r = ilm.archive(today=TODAY)
    assert r["files"] == 4 and r["rows_deleted"] > 0                     # 뉴스 7·8월 + 검색 7·8월
    left = {x["published_at"] for x in db.query("SELECT published_at FROM news WHERE url LIKE 'https://ex.com/ilm/%'")}
    assert left == {"2026-10-20"}                                        # 기한 안의 달은 그대로
    assert not db.query("SELECT 1 FROM news_stock WHERE news_id = ANY(:i)", {"i": [ids["2026-07-10"], ids["2026-08-03"]]})
    assert not db.query("SELECT 1 FROM news_sentiment WHERE news_id = :i", {"i": ids["2026-07-25"]})
    srch = db.query("SELECT searched_at FROM search_log WHERE keyword = '삼성전자' ORDER BY searched_at")
    assert [s["searched_at"].isoformat() for s in srch] == ["2026-10-01T12:00:00+09:00"]   # 한국시간 달 경계
    logs = db.query("SELECT dataset, period_start, path, sha256, bytes, rows, deleted_at FROM archive_log ORDER BY id")
    assert len(logs) == 4 and all(x["deleted_at"] is not None for x in logs)
    jul = next(x for x in logs if x["dataset"] == "news" and x["period_start"] == "2026-07-01")
    assert json.loads(jul["rows"]) == {"news": 2, "news_stock": 4, "news_sentiment": 2}
    gz = archive_dir / jul["path"]
    man = json.loads(gz.with_name(gz.name.replace(".jsonl.gz", ".manifest.json")).read_text(encoding="utf-8"))
    assert man["sha256"] == jul["sha256"] and man["period"] == ["2026-07-01", "2026-08-01"]
    assert man["columns"]["news"]["published_ts"].startswith("timestamp(0) with time zone")
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        first = json.loads(f.readline())
    assert first["t"] == "news" and first["r"]["published_ts"].endswith("+09:00") and "삼성전자" in first["r"]["title"]
    assert ilm.verify() == {"files": 4, "ok": 4, "bad": []}

    mrel = Path(jul["path"]).with_name(Path(jul["path"]).name.replace(".jsonl.gz", ".manifest.json"))
    back = ilm.restore(str(mrel))
    assert back["inserted"] == {"news": 2, "news_stock": 4, "news_sentiment": 2}
    row = db.query("SELECT title, published_at, published_ts FROM news WHERE news_id = :i", {"i": ids["2026-07-10"]})[0]
    assert row["published_at"] == "2026-07-10" and row["published_ts"].isoformat() == "2026-07-10T09:00:00+09:00"
    again = ilm.restore(str(mrel))                                       # 이미 있는 행은 건너뛴다
    assert again["inserted"] == {"news": 0, "news_stock": 0, "news_sentiment": 0}
    assert db.query("SELECT restored_at FROM archive_log WHERE sha256 = :h", {"h": jul["sha256"]})[0]["restored_at"]


def test_tampered_or_foreign_archives_are_refused(archive_dir, tmp_path):
    _seed()
    ilm.archive("news", today=TODAY, max_months=1)
    rec = db.query("SELECT path FROM archive_log ORDER BY id DESC LIMIT 1")[0]
    gz = archive_dir / rec["path"]
    man = gz.with_name(gz.name.replace(".jsonl.gz", ".manifest.json"))
    data = bytearray(gz.read_bytes())
    data[-12] ^= 0xFF                                                    # 한 바이트 변조
    gz.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="해시"):
        ilm.restore(str(man))
    assert ilm.verify()["bad"][0]["problem"] == "sha256_mismatch"
    outside = tmp_path / "evil.manifest.json"
    outside.write_text(man.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="보관 폴더"):
        ilm.restore(str(outside))
    os.remove(gz)
    assert ilm.verify()["bad"][0]["problem"] == "missing"


def test_retention_zero_keeps_everything_and_status_reports_cutoffs(archive_dir, monkeypatch):
    _seed()
    monkeypatch.setattr(settings, "NEWS_RETENTION_DAYS", 0)
    assert all(m["dataset"] != "news" for m in ilm.plan(today=TODAY))
    st = ilm.status(today=TODAY)["datasets"]
    assert st["search"]["cutoff"] == (TODAY - timedelta(days=60)).isoformat()
    assert st["search"]["rows_past_cutoff"] >= 2 and st["search"]["oldest"] <= "2026-07-31"


def test_archive_log_is_append_only_for_the_app_role():
    """V018 — 앱 계정은 보관 기록을 지우거나 해시를 고칠 수 없다(최소 권한 모드에서만 확인)."""
    if not os.getenv("TEST_APP_DATABASE_URL"):
        pytest.skip("최소 권한 모드에서만")
    with db.transaction() as c:
        aid = c.execute(text("INSERT INTO archive_log(dataset, period_start, period_end, path, sha256, bytes, rows, "
                             "created_at) VALUES('news', '2020-01-01', '2020-02-01', 'x', :h, 1, '{}', now()) "
                             "RETURNING id"), {"h": "0" * 64}).scalar()
    with pytest.raises(Exception, match="permission denied"):
        db.execute("UPDATE archive_log SET sha256 = :h WHERE id = :i", {"h": "1" * 64, "i": aid})
    with pytest.raises(Exception, match="permission denied"):
        db.execute("DELETE FROM archive_log WHERE id = :i", {"i": aid})
    db.execute("UPDATE archive_log SET verified_at = now() WHERE id = :i", {"i": aid})   # 시각 칸은 된다
