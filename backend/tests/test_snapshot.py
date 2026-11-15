"""팀원 공유 스냅샷 — 공개 데이터만 왕복 · 계정/토큰/채팅은 파일에 없음 · 들여도 개인 데이터 그대로 ·
변조/잘린 파일은 거부(원자적) · 자동 증가 id 이어짐."""
import gzip
import json

import pytest

from app import db
from app.services import snapshot


def _seed_public():
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES('005930','2026-09-29',71000,1000,1.2), ('005930','2026-09-30',72000,900,1.4)")
    db.execute("INSERT INTO news(news_id, title, url, source, published_at, published_ts) VALUES "
               "(7, '삼성전자 수주', 'https://ex.com/7', 'A일보', '2026-09-30', '2026-09-30T09:00:00+09:00'), "
               "(9, '카카오 신작', 'https://ex.com/9', 'B경제', '2026-09-30', NULL)")
    db.execute("INSERT INTO news_stock(news_id, ticker) VALUES(7, '005930'), (9, '035720')")
    db.execute("INSERT INTO news_sentiment(news_id, label, score, model, classified_at) "
               "VALUES(7, 'POS', 1, 'lexicon', '2026-10-01T09:00:00')")
    db.set_meta("market_source", "krx")
    db.set_meta("kis_token", "SECRET-KIS-ACCESS-TOKEN-123")          # 절대 파일에 들어가면 안 됨
    db.set_meta("pipeline_real_status", '{"ok": false, "error": "internal path /opt/x"}')


def _seed_private():
    db.execute("INSERT INTO app_user(username, password_hash, salt, created_at) "
               "VALUES('kim@oos.io', 'HASH-SHOULD-NOT-LEAK', 'SALT', '2026-10-01T09:00:00')")
    db.execute("INSERT INTO auth_token(token, username, created_at) VALUES('TOKEN-HASH-XYZ', 'kim@oos.io', '2026-10-01T09:00:00')")
    db.execute("INSERT INTO chat_message(sender, receiver, body, created_at) "
               "VALUES('kim@oos.io', 'lee@oos.io', '비밀 메시지', '2026-10-01T09:00:00+09:00')")


def test_roundtrip_public_only_and_private_untouched(tmp_path):
    _seed_public()
    _seed_private()
    r = snapshot.export_snapshot(str(tmp_path / "s.jsonl.gz"))
    raw = gzip.open(r["path"], "rt", encoding="utf-8").read()
    for secret in ("SECRET-KIS-ACCESS-TOKEN-123", "HASH-SHOULD-NOT-LEAK", "TOKEN-HASH-XYZ", "비밀 메시지",
                   "kim@oos.io", "internal path"):
        assert secret not in raw
    assert r["tables"]["news"] == 2 and r["tables"]["daily_price"] == 2 and r["tables"]["app_meta"] == 1

    # 팀원 PC 처럼 — 데이터가 다르고 개인 데이터도 있음
    db.execute("DELETE FROM news WHERE news_id = 9")
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES('000660','2026-09-30',180000,10,0)")
    db.set_meta("market_source", "gen")
    db.set_meta("kis_token", "TEAMMATE-OWN-TOKEN")
    db.execute("INSERT INTO chat_message(sender, receiver, body, created_at) VALUES('a', 'b', '팀원 대화', '2026-10-01T09:00:00+09:00')")

    res = snapshot.import_snapshot(r["path"])
    assert res["tables"]["news"] == 2 and res["created_at"]
    assert [x["news_id"] for x in db.query("SELECT news_id FROM news ORDER BY news_id")] == [7, 9]
    assert db.query("SELECT COUNT(*) AS c FROM daily_price")[0]["c"] == 2          # 교체됨
    assert db.get_meta("market_source") == "krx"
    assert db.get_meta("kis_token") == "TEAMMATE-OWN-TOKEN"                         # 팀원 것 그대로
    assert db.query("SELECT COUNT(*) AS c FROM chat_message")[0]["c"] == 2          # 개인 데이터 그대로
    assert db.query("SELECT COUNT(*) AS c FROM app_user")[0]["c"] == 1
    nid = db.query("INSERT INTO news(title, url) VALUES('새 기사', 'https://ex.com/new') RETURNING news_id")
    assert nid[0]["news_id"] > 9                                                    # id 가 이어짐


def _write(path, lines):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def test_tampered_file_cannot_touch_account_tables(tmp_path):
    _seed_public()
    _seed_private()
    p = str(tmp_path / "bad.jsonl.gz")
    _write(p, [{"format": snapshot.FORMAT, "version": 1},
               {"table": "news", "columns": ["news_id", "title", "url"]}, [1, "x", "https://x"],
               {"table": "app_user", "columns": ["username", "password_hash", "salt", "created_at"]},
               ["kim@oos.io", "attacker", "s", "x"], {"end": {}}])
    with pytest.raises(snapshot.SnapshotError, match="허용되지 않은 표"):
        snapshot.import_snapshot(p)
    assert db.query("SELECT password_hash FROM app_user")[0]["password_hash"] == "HASH-SHOULD-NOT-LEAK"
    assert db.query("SELECT COUNT(*) AS c FROM news")[0]["c"] == 2                  # 한 트랜잭션 — 되돌려짐


def test_truncated_or_foreign_file_rejected(tmp_path):
    _seed_public()
    p = str(tmp_path / "cut.jsonl.gz")
    _write(p, [{"format": snapshot.FORMAT, "version": 1},
               {"table": "news", "columns": ["news_id", "title", "url"]}, [1, "x", "https://x"]])
    with pytest.raises(snapshot.SnapshotError, match="끊겼"):
        snapshot.import_snapshot(p)
    assert db.query("SELECT COUNT(*) AS c FROM news")[0]["c"] == 2
    q = str(tmp_path / "other.jsonl.gz")
    _write(q, [{"hello": "world"}])
    with pytest.raises(snapshot.SnapshotError, match="스냅샷 파일이 아니"):
        snapshot.import_snapshot(q)
    r = str(tmp_path / "count.jsonl.gz")
    _write(r, [{"format": snapshot.FORMAT, "version": 1},
               {"table": "news", "columns": ["news_id", "title", "url"]}, [1, "x", "https://x"],
               {"end": {"news": 5}}])
    with pytest.raises(snapshot.SnapshotError, match="행 수"):
        snapshot.import_snapshot(r)
    assert db.query("SELECT COUNT(*) AS c FROM news")[0]["c"] == 2
