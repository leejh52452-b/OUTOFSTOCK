"""백업·복구 훈련(tools/dr.py) — FORCE RLS 표까지 빠짐없이 덤프되는가 · 암호문 변조와 잘못된 키를 잡는가 ·
키 링 점검이 '키 분실'을 찾는가 · 보관 주기(GFS) · (선택) 실제 복원 훈련이 통과하고 '빈 백업'을 잡는가.

실제 복원 훈련 테스트는 데이터베이스를 만들 수 있는 계정이 필요하다 — DR_TEST_ADMIN_URL 을 주면 실행:
  DR_TEST_ADMIN_URL=postgresql+psycopg://dr_admin:…@localhost:5432/postgres pytest tests/test_dr.py
"""
import base64
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import crypto, db
from app.config import settings
from app.services import audit, auth, wallet
from conftest import TEST_DATABASE_URL

_spec = importlib.util.spec_from_file_location("dr_tool", Path(__file__).resolve().parents[1] / "tools" / "dr.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

needs_pg = pytest.mark.skipif(not (shutil.which("pg_dump") and shutil.which("pg_restore")),
                              reason="PostgreSQL 클라이언트(pg_dump·pg_restore) 필요")
needs_age = pytest.mark.skipif(not (shutil.which("age") and shutil.which("age-keygen")), reason="age 필요")
PW = "Dr-pass-2026"


@pytest.fixture
def bk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "BACKUP_AGE_RECIPIENTS", "")
    monkeypatch.setattr(settings, "BACKUP_OFFSITE_CMD", "")
    return tmp_path


def _seed():
    for u in ("dr-a", "dr-b"):
        auth.register(u, PW)
        wallet.balance(u)                         # 지갑 + 원장 + 분개(FORCE RLS 표)
    audit.record("test.seed", "dr")               # 감사 체인 머리


def _copy_rows(dump: Path, table: str) -> int:
    """덤프 안 COPY 블록의 행 수(복원 없이 덤프 내용을 직접 센다)."""
    out = subprocess.run(["pg_restore", "--data-only", f"--table={table}", "-f", "-", str(dump)],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    i = next(k for k, ln in enumerate(out) if ln.startswith(f"COPY public.{table} "))
    return out.index("\\.", i) - i - 1


def _keypair(d: Path, name: str):
    key = d / f"{name}.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    pub = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout.strip()
    return key, pub


@needs_pg
def test_backup_captures_force_rls_tables_with_a_consistent_manifest(bk):
    _seed()
    r = dr.backup(plaintext=True)
    dump = Path(r["path"])
    m = json.loads(dr._manifest_path(dump).read_text(encoding="utf-8"))
    assert {"wallet", "wallet_ledger", "watchlist"} <= set(m["facts"]["rls_forced"])
    assert m["tables"]["public.wallet"] == _copy_rows(dump, "wallet") == 2           # 개인 데이터 표도 전부
    assert m["tables"]["public.wallet_ledger"] == _copy_rows(dump, "wallet_ledger") > 0
    assert m["facts"]["journal_sum"] == 0 and m["facts"]["audit_head"]
    assert "public.rate_event" not in m["tables"]                                     # UNLOGGED(휘발성) 표는 제외
    assert oct(dump.stat().st_mode & 0o777) == "0o600"
    v = dr.verify(dump, None)
    assert v["ok"] and v["toc_entries"] > 50


@needs_pg
def test_naive_pg_dump_fails_or_silently_drops_force_rls_rows(bk, tmp_path):
    """왜 백업 전용 계정(BYPASSRLS)이나 시스템 문맥이 필요한가 — 이 위험을 문서로 고정한다."""
    if db.query("SELECT rolsuper OR rolbypassrls AS b FROM pg_roles WHERE rolname = current_user")[0]["b"]:
        pytest.skip("테스트 DB 계정이 RLS 를 우회한다(슈퍼유저 등)")
    _seed()
    env = dr._env(dr._pg(TEST_DATABASE_URL))
    r = subprocess.run(["pg_dump", "-Fc", "-f", str(tmp_path / "n1.dump")], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "row-level security" in r.stderr            # 그냥 덤프: 실패(매일 실패하던 그 오류)
    subprocess.run(["pg_dump", "-Fc", "--enable-row-security", "-f", str(tmp_path / "n2.dump")], env=env, check=True)
    assert _copy_rows(tmp_path / "n2.dump", "wallet") == 0                    # 옵션만 켜면: '조용히 빈 표'


@needs_pg
@needs_age
def test_encrypted_backup_detects_tampering_and_wrong_keys(bk, monkeypatch):
    ops, pub1 = _keypair(bk, "ops")
    vault, pub2 = _keypair(bk, "vault")
    rec = bk / "recipients.txt"
    rec.write_text(f"# 공개키만\n{pub1}\n{pub2}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "BACKUP_AGE_RECIPIENTS", str(rec))
    _seed()
    r = dr.backup()
    p = Path(r["path"])
    assert r["encrypted"] and p.name.endswith(".dump.age")
    assert b"PGDMP" not in p.read_bytes()[:4096]                               # 평문 덤프 머리말이 보이지 않는다
    assert dr.verify(p, str(ops))["ok"] and dr.verify(p, str(vault))["ok"]     # 두 키 중 어느 것으로도(키 분실 대비)
    other, _ = _keypair(bk, "other")
    with pytest.raises(dr.DrError, match="복호화 실패"):
        dr.verify(p, str(other))
    with pytest.raises(dr.DrError, match="복호화 키"):
        dr.verify(p, None)
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0x01
    p.write_bytes(bytes(b))
    assert dr.verify(p, str(ops))["cipher_sha256"] is False                    # manifest 해시가 먼저 잡고
    mp = dr._manifest_path(p)
    mj = json.loads(mp.read_text(encoding="utf-8"))
    mj["cipher"]["sha256"] = dr._sha256_file(p)                                 # manifest 까지 맞춰 바꿔도
    mp.write_text(json.dumps(mj), encoding="utf-8")
    with pytest.raises(dr.DrError, match="복호화 실패"):                        # 인증 암호(AEAD) 태그가 잡는다
        dr.verify(p, str(ops))


def test_backup_refuses_plaintext_unless_explicit(bk):
    with pytest.raises(dr.DrError, match="BACKUP_AGE_RECIPIENTS"):
        dr.backup()


def test_retention_keeps_recent_backups_and_the_first_of_each_month():
    names = [f"oos-2026{m:02d}{d:02d}T000000Z.dump.age" for m in (7, 8, 9, 10) for d in (1, 15, 28)]
    names += ["outofstock-20260101-000000.dump", "oos-20261029T000000Z.dump.age.part"]   # 남의 파일·쓰다 만 파일은 무시
    keep, drop = dr.plan_retention(names, keep_daily=2, keep_monthly=3)
    assert keep == ["oos-20261028T000000Z.dump.age", "oos-20261015T000000Z.dump.age",
                    "oos-20261001T000000Z.dump.age", "oos-20260901T000000Z.dump.age",
                    "oos-20260801T000000Z.dump.age"]
    assert len(drop) == 7 and "oos-20260701T000000Z.dump.age" in drop


def test_key_ring_check_finds_values_locked_by_a_missing_key(monkeypatch):
    k1, k2 = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", k1)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", "")
    db.execute("INSERT INTO app_meta(key, value, updated_at) VALUES('dr-test', :v, '2026-10-01T09:00:00+09:00')",
               {"v": crypto.encrypt("secret", aad="app_meta:dr-test")})
    assert crypto.verify_all()["ok"]
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", k2)                  # 키를 바꾸며 예전 키를 빠뜨린 사고
    r = crypto.verify_all()
    col = r["columns"]["app_meta.value"]
    assert not r["ok"] and col["failed"] == 1 and col["missing_kids"] == [crypto._kid(base64.b64decode(k1))]
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", k1)              # 예전 키를 읽기 전용으로 되살리면
    assert crypto.verify_all()["ok"]


def test_verify_process_cannot_fall_back_to_production_db_urls(tmp_path, monkeypatch):
    """복원 검증 프로세스의 다른 DB 주소는 '지우지' 않고 빈 값으로 — 지우면 앱 설정의 load_dotenv 가 운영 .env 값으로
    되살려, 검증의 init_db(마이그레이션·시작 잔액 분개)가 운영 DB 에서 돌았다(결함 31)."""
    from dotenv import load_dotenv
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://owner:x@prod-db:5432/outofstock")
    env = dr._isolated_env(dr._pg("postgresql://dr:dr@127.0.0.1:5432/oos_drill_x"))
    assert all(env[k] == "" for k in dr._OTHER_DB_URLS) and "oos_drill_x" in env["DATABASE_URL"]
    f = tmp_path / ".env"                                       # python-dotenv 의 규칙: '있는' 키(빈 값 포함)는 덮어쓰지 않는다
    f.write_text("MIGRATION_DATABASE_URL=postgresql://owner@prod-db/outofstock\n"
                 "ANALYTICS_DATABASE_URL=postgresql://ro@prod-db/outofstock\n", encoding="utf-8")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "")
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "placeholder")  # 원래 상태를 기록해 두고(정리용) 지운다
    monkeypatch.delenv("ANALYTICS_DATABASE_URL")
    load_dotenv(f)
    assert os.environ["MIGRATION_DATABASE_URL"] == ""           # 빈 값 → 그대로
    assert "prod-db" in os.environ["ANALYTICS_DATABASE_URL"]    # 지운 키 → .env 가 되살린다(예전 방식의 문제)


ADMIN = os.getenv("DR_TEST_ADMIN_URL", "")


@needs_pg
@pytest.mark.skipif(not ADMIN, reason="DR_TEST_ADMIN_URL(데이터베이스 생성 권한) 필요")
def test_full_restore_drill_passes_and_catches_an_empty_backup(bk):
    _seed()
    r = dr.backup(plaintext=True)
    rep = dr.drill(Path(r["path"]), None, ADMIN, report_dir=bk / "reports", record=False)
    assert rep["ok"], rep
    names = {c["name"]: c["ok"] for c in rep["checks"]}
    assert names["표별 행 수 = manifest"] and names["개인 흐름(가입 → 로그인 → 지갑)"]
    assert Path(rep["report"]["md"]).read_text(encoding="utf-8").count("✓") >= 10
    # '빈 백업' — RLS 에 막혀 개인 데이터가 0행인 덤프를 같은 manifest 로 바꿔치기하면 행 수 대조가 잡는다
    if db.query("SELECT rolsuper OR rolbypassrls AS b FROM pg_roles WHERE rolname = current_user")[0]["b"]:
        return
    dump = Path(r["path"])
    env = dr._env(dr._pg(TEST_DATABASE_URL))
    subprocess.run(["pg_dump", "-Fc", "--enable-row-security", "--extension=plpgsql", "--no-unlogged-table-data",
                    "-f", str(dump)], env=env, check=True)
    mp = dr._manifest_path(dump)
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["cipher"] = {"bytes": dump.stat().st_size, "sha256": dr._sha256_file(dump)}
    m["dump"] = dict(m["cipher"])
    mp.write_text(json.dumps(m), encoding="utf-8")
    bad = dr.drill(dump, None, ADMIN, report_dir=bk / "reports", record=False)
    assert not bad["ok"]
    rows = next(c for c in bad["checks"] if c["name"] == "표별 행 수 = manifest")
    assert not rows["ok"] and "public.wallet: 2→0" in rows["detail"]


def test_admin_health_tracks_backup_age_and_drills_but_public_health_hides_them():
    from datetime import datetime, timedelta, timezone

    from app.services import monitoring as mon
    assert mon.dr_status()["issues"] == []                                   # 기록이 없으면(개발) 경고 없음
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    db.set_meta("backup_last", json.dumps({"status": "ok", "last_ok_at": old, "encrypted": False}))
    st = mon.dr_status()
    assert set(st["issues"]) == {"backup_stale", "backup_unencrypted", "dr_drill_overdue"}
    assert st["backup_age_hours"] >= 30
    assert {"backup_stale", "dr_drill_overdue"} <= set(mon.health()["issues"])
    assert not set(mon.public_health()["issues"]) & set(mon.DR_ISSUES)      # 공개 응답에는 백업 상태가 없다
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.set_meta("backup_last", json.dumps({"status": "ok", "last_ok_at": now, "encrypted": True}))
    db.set_meta("dr_drill_last", json.dumps({"ok": True, "at": now, "rto_seconds": 2.3}))
    assert mon.dr_status()["issues"] == []
    db.set_meta("dr_drill_last", json.dumps({"ok": False, "at": now}))
    assert mon.dr_status()["issues"] == ["dr_drill_failed"]
