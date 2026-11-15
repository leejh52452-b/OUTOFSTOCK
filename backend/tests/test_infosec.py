"""정보보호(3번) — 커뮤니티 보호 장치·감사 로그 해시 체인·암호화 저장·접근 통제·입력 검증.
docs/security/owasp_checklist.md 의 각 항목이 어떤 테스트로 확인되는지 이 파일 이름으로 찾을 수 있다."""
import base64
import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, crypto, db
from app.api import app
from app.config import settings
from app.services import audit

client = TestClient(app)


def _login(u, pw="pass1234"):
    client.post("/auth/register", json={"username": u, "password": pw})
    tok = client.post("/auth/login", json={"username": u, "password": pw}).json()["token"]
    return {"Authorization": "Bearer " + tok}


@pytest.fixture
def key(monkeypatch):
    k = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", k)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", "")
    return k


# ── A03 주입·XSS · 커뮤니티 입력 검증 ─────────────────────────


# ── A01 접근 통제 ───────────────────────────────────────────


# ── A08·A09 감사 로그(해시 체인) ─────────────────────────────
def test_audit_chain_detects_tampering(as_owner):
    _login("audited@oos.io")
    client.post("/auth/login", json={"username": "audited@oos.io", "password": "wrong"})
    acts = [r["action"] for r in audit.recent(10)]
    assert {"auth.register", "auth.login"} <= set(acts)
    assert db.query("SELECT result FROM audit_log WHERE action = 'auth.login' ORDER BY audit_id DESC LIMIT 1")[0]["result"] == "fail"
    assert audit.verify()["ok"]
    mid = db.query("SELECT audit_id FROM audit_log ORDER BY audit_id LIMIT 1 OFFSET 1")[0]["audit_id"]
    db.execute("UPDATE audit_log SET actor = 'mallory' WHERE audit_id = :i", {"i": mid})       # 위·변조
    v = audit.verify()
    assert not v["ok"] and v["broken_at"] == mid


def test_audit_purge_keeps_chain_verifiable(monkeypatch, as_owner):
    for i in range(4):
        audit.record("probe", f"u{i}")
    db.execute("UPDATE audit_log SET at = '2020-01-01T00:00:00' WHERE audit_id <= 2")
    # 시각을 바꾸면 그 행의 해시가 어긋나므로, 테스트에서는 체인을 다시 계산해 '오래된 정상 행'을 만든다
    rows = db.query("SELECT * FROM audit_log ORDER BY audit_id")
    prev = audit.GENESIS
    for r in rows:
        h = audit._hash(prev, audit._canon(r["at"], r["actor"], r["action"], r["target"], r["detail"], r["ip_hash"],
                                           r["result"]))
        db.execute("UPDATE audit_log SET prev_hash = :p, row_hash = :h WHERE audit_id = :i", {"p": prev, "h": h,
                                                                                              "i": r["audit_id"]})
        prev = h
    assert audit.verify()["ok"]
    assert audit.purge(days=365) == 2
    assert audit.verify()["ok"]                                     # 시작점(anchor)으로 이어서 검증
    audit.record("after", "u9")
    assert audit.verify()["ok"]


def test_ip_is_pseudonymized_in_audit():
    audit.record("probe", "x", ip="203.0.113.7")
    row = db.query("SELECT ip_hash, detail FROM audit_log ORDER BY audit_id DESC LIMIT 1")[0]
    assert row["ip_hash"] and "203.0.113.7" not in str(row)


# ── A02 암호화 저장 ─────────────────────────────────────────
def test_aes_gcm_roundtrip_binding_and_rotation(key, monkeypatch):
    enc = crypto.encrypt("kis-access-token-abc", aad="app_meta:kis_token")
    assert enc.startswith("enc:v2:a256gcm:") and "kis-access-token" not in enc
    assert crypto.decrypt(enc, aad="app_meta:kis_token") == "kis-access-token-abc"
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(enc, aad="payment_order:1")                  # 다른 위치에 붙인 암호문은 거부
    assert crypto.decrypt("plain-old-value") == "plain-old-value"   # 도입 전 평문 호환
    new = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", key)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", new)
    assert crypto.decrypt(enc, aad="app_meta:kis_token") == "kis-access-token-abc"   # 예전 키로도 읽힘
    assert crypto.needs_rotation(enc) and not crypto.needs_rotation(crypto.encrypt("x"))


def test_kis_token_cached_encrypted(key, monkeypatch):
    from app.collectors import realtime as rt
    monkeypatch.setattr(settings, "KIS_APP_KEY", "key-A")
    monkeypatch.setattr(settings, "KIS_APP_SECRET", "secret")

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "TOKEN-PLAINTEXT-123", "expires_in": 86400}
    monkeypatch.setattr(rt.requests, "post", lambda url, **kw: R())
    assert rt.get_access_token() == "TOKEN-PLAINTEXT-123"
    stored = db.get_meta("kis_token")
    assert stored.startswith("enc:v2:") and "TOKEN-PLAINTEXT" not in stored
    assert rt.get_access_token() == "TOKEN-PLAINTEXT-123"           # 캐시에서 복호화


def test_crypto_rejects_bad_key(monkeypatch):
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", "too-short")
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt("x")


# ── A05 보안 설정 ───────────────────────────────────────────


def test_audit_purge_never_drops_role_history_younger_than_three_years(as_owner):
    """권한 부여·변경·말소 내역은 최소 3년(안전성 확보조치 기준 제5조③) — 보관 설정을 짧게 줘도 3년 미만 행은 남는다."""
    audit.record("rbac.grant", "admin1", target="user1")
    db.execute("UPDATE audit_log SET at = :t", {"t": (clock.now() - timedelta(days=800)).isoformat(timespec="seconds")})
    assert audit.purge(days=30) == 0
    assert audit.ROLE_HISTORY_MIN_YEARS == 3


def test_legal_minimum_retention_counts_calendar_years_not_days():
    """'1년 이상 보관'을 365일로 세면 2월 29일이 낀 해엔 하루 일찍 지운다(결함 28) — 달력 기준 하한."""
    from datetime import datetime
    from app.services import privacy
    now = datetime(2028, 3, 1, 12, 0)                                   # 2028-02-29 가 낀 1년
    assert now - timedelta(days=365) == datetime(2027, 3, 2, 12, 0)     # 365일 전 = 아직 1년이 안 된 행까지 지움
    assert privacy.retention_cut(now, 365, 1) == datetime(2027, 3, 1, 12, 0)
    assert privacy.retention_cut(now, 1095, 3) == datetime(2025, 3, 1, 12, 0)
    assert privacy.retention_cut(now, 3000, 3) == now - timedelta(days=3000)   # 설정이 더 길면 설정대로
    assert privacy.years_ago(datetime(2028, 2, 29, 9, 0), 1) == datetime(2027, 2, 28, 9, 0)


def test_role_history_is_aged_from_the_last_decision(as_owner):
    """권한 내역은 '마지막으로 바뀐 때'부터 센다 — 4년 전 요청·1년 전 승인 건은 남는다(예전: 요청 시각 기준으로 삭제)."""
    from app.services import privacy
    db.execute("INSERT INTO role_request(username, role, reason, requested_by, requested_at, status, decided_by, decided_at) "
               "VALUES ('u-old', 'admin', '점검', 'boot', now() - interval '4 years', 'approved', 'boss', "
               "now() - interval '1 year')")
    out = privacy.retention_purge()
    assert out["role_request"] == 0 and db.query("SELECT 1 FROM role_request WHERE username='u-old'")


def test_purge_records_its_audit_even_when_a_step_fails(monkeypatch, as_owner):
    """한 단계 실패가 파기 기록(감사)과 다른 단계를 막지 않는다 — 실패는 다 돌린 뒤 알린다(결함 30)."""
    from app.services import alerts, privacy
    monkeypatch.setattr(alerts, "purge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError, match="alert_log"):
        privacy.retention_purge()
    row = db.query("SELECT result, detail FROM audit_log WHERE action='privacy.retention_purge'")[0]
    assert row["result"] == "partial" and "outbox" in row["detail"] and "실패" in row["detail"]


def test_daily_security_job_still_writes_the_witness_when_purge_fails(monkeypatch):
    import scheduler
    from app.services import privacy
    monkeypatch.setattr(privacy, "retention_purge", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="purge"):
        scheduler.job_security_daily()
    assert db.get_meta("audit_witness_last")                            # 외부 증인은 그날도 남았다


def test_account_deletion_keeps_role_history_but_pseudonymized():
    """탈퇴해도 관리 권한 활성 내역은 남는다(3년) — 아이디만 가명으로. 현재 자격·세션은 지운다."""
    from app.services import privacy
    db.execute("INSERT INTO role_activation(username, role, session_id, reason, activated_at, expires_at) "
               "VALUES ('adminx', 'admin', 's1', '정기 점검', now(), now())")
    db.execute("INSERT INTO user_role(username, role, granted_by, granted_at) VALUES ('adminx', 'admin', 'boot', now())")
    with db.transaction() as c:
        privacy.on_account_deleted(c, "adminx")
    hist = db.query("SELECT username FROM role_activation")
    assert len(hist) == 1 and hist[0]["username"].startswith("del:")
    assert not db.query("SELECT 1 FROM user_role WHERE username = 'adminx'")
