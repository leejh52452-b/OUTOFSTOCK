"""파일 무결성 감시(FIM) — 배포 기준선 대비 바뀐·사라진·새 파일(웹셸 의심) 탐지(app/security/integrity.py).
기준선은 소유자 계정만 쓸 수 있다(V016) — 앱 권한을 얻은 공격자가 기준선을 고쳐 심은 파일을 '정상'으로 만들 수 없게."""
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import db
from app.security import integrity

ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "web" / "assets").mkdir(parents=True)
    (tmp_path / "app" / "api.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (tmp_path / "web" / "index.html").write_text("<!doctype html><title>t</title>\n", encoding="utf-8")
    (tmp_path / "web" / "assets" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("print('cli')\n", encoding="utf-8")
    return tmp_path


def _owner():
    url = os.getenv("TEST_DATABASE_URL") if os.getenv("TEST_APP_DATABASE_URL") else None
    return (create_engine(url, future=True), True) if url else (db.get_engine(), False)


def test_changed_missing_and_new_files_are_detected_against_the_baseline(tmp_path):
    root = _tree(tmp_path)
    assert integrity.verify(root)["state"] == "no_baseline"
    b = integrity.record_baseline(root=root, actor="deploy", release="install_test.sh")
    assert b["files"] == 4
    assert integrity.verify(root)["state"] == "ok"
    # 캐시·운영 값은 감시 밖 — 생겨도 위반이 아니다
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "api.cpython-312.pyc").write_bytes(b"\x00")
    (root / ".env").write_text("X=1\n", encoding="utf-8")
    (root / "web" / "server.key").write_text("k\n", encoding="utf-8")
    assert integrity.verify(root)["state"] == "ok"
    (root / "web" / "assets" / "shell.php").write_text("drill\n", encoding="utf-8")          # 몰래 심은 파일
    (root / "app" / "api.py").write_text("def ok():\n    return 2\n", encoding="utf-8")      # 승인 안 된 변경
    (root / "run.py").unlink()                                                                # 사라진 파일
    r = integrity.verify(root)
    assert r["state"] == "violation" and r["unexpected"] == ["web/assets/shell.php"]
    assert r["changed"] == ["app/api.py"] and r["missing"] == ["run.py"]
    assert r["counts"] == {"changed": 1, "missing": 1, "unexpected": 1} and r["baseline_id"] == b["id"]
    au = db.query("SELECT actor, detail FROM audit_log WHERE action='integrity.baseline'")
    assert au and au[-1]["actor"] == "deploy" and "install_test.sh" in au[-1]["detail"]


def test_violation_emits_one_critical_event_until_it_changes(tmp_path, monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda key, title, msg="", **k: sent.append(title) or {"sent": []})
    root = _tree(tmp_path)
    integrity.record_baseline(root=root)
    assert integrity.check(root=root)["state"] == "ok"
    (root / "web" / "x.php").write_text("x\n", encoding="utf-8")
    integrity.check(root=root)
    integrity.check(root=root)                                       # 같은 위반 — 다시 울리지 않는다(10분마다 경보 폭주 방지)
    ev = db.query("SELECT severity, response, detail FROM security_event WHERE kind='file_integrity'")
    assert len(ev) == 1 and ev[0]["severity"] == "critical" and "alert" in ev[0]["response"] and "web/x.php" in ev[0]["detail"]
    assert sent == ["[보안] 배포 기준선과 다른 코드·화면 파일(변조·웹셸 의심) — 즉시 확인"]
    (root / "web" / "y.php").write_text("y\n", encoding="utf-8")       # 위반이 달라지면 다시 알린다
    integrity.check(root=root)
    assert db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='file_integrity'")[0]["n"] == 2
    (root / "web" / "x.php").unlink()
    (root / "web" / "y.php").unlink()
    assert integrity.check(root=root)["state"] == "ok" and not db.get_meta("integrity_last_sig")
    integrity.status_reset()


def test_app_account_cannot_rewrite_the_baseline(tmp_path):
    root = _tree(tmp_path)
    integrity.record_baseline(root=root)
    forge = ("INSERT INTO integrity_baseline(created_at, created_by, files, digest, manifest) "
             "VALUES(now(), 'attacker', 1, 'x', '{}')")
    if os.getenv("TEST_APP_DATABASE_URL"):                   # 최소 권한 모드: 앱 계정으로는 기준선을 쓸 수 없다
        with pytest.raises(Exception) as e:
            db.execute(forge)
        assert getattr(getattr(e.value, "orig", None), "sqlstate", None) == "42501"
        with pytest.raises(Exception):
            db.execute("UPDATE integrity_baseline SET manifest='{}'")
    sql = (ROOT / "sql" / "migrations" / "V016__assurance_least_privilege.sql").read_text(encoding="utf-8")
    roles = (ROOT / "deploy" / "sql" / "roles.sql").read_text(encoding="utf-8")
    for doc in (sql, roles):
        assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.integrity_baseline FROM oos_app" in doc
        assert "REVOKE UPDATE, DELETE, TRUNCATE ON public.security_assessment FROM oos_app" in doc


def test_tampered_baseline_row_is_itself_a_violation(tmp_path):
    root = _tree(tmp_path)
    b = integrity.record_baseline(root=root)
    eng, own = _owner()
    try:                                                     # DB 소유자 권한을 가진 내부자가 기준선에 자기 파일을 끼워 넣으면
        with eng.begin() as c:
            c.execute(text("UPDATE integrity_baseline SET manifest = replace(manifest, '}', ',\"web/evil.js\":\"00\"}') "
                           "WHERE id=:i"), {"i": b["id"]})
    finally:
        if own:
            eng.dispose()
    r = integrity.verify(root)
    assert r["state"] == "violation" and r["baseline_tampered"] is True     # 파일 목록 다이제스트가 맞지 않는다


def test_scan_follows_the_installer_rules():
    """감시 범위 = 설치본이 싣는 파일 중 실행·내보내는 것 — 운영 값(.env·인증서·키)·캐시는 둘 다 뺀다."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("oos_make_installer", ROOT / "tools" / "make_installer.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    shipped = {f for f in mk.files() if f.split("/")[0] in integrity.WATCH_DIRS or f in integrity.WATCH_FILES}
    scanned = set(integrity.scan(ROOT))
    assert scanned == shipped, {"감시만": sorted(scanned - shipped)[:5], "설치본만": sorted(shipped - scanned)[:5]}
    assert "app/security/integrity.py" in scanned and not any(p.endswith(".pyc") for p in scanned)
