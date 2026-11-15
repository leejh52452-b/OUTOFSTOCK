"""데이터 보호 — 봉투 암호화·키 교체(rewrap)·채팅 암호화·분류 등록부(app/crypto.py, app/security/classification.py)."""
import base64
import os

import pytest
from fastapi.testclient import TestClient

from app import crypto, db
from app.api import app
from app.config import settings
from app.security import classification
from app.services import auth

client = TestClient(app)


@pytest.fixture
def kek(monkeypatch):
    k = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", k)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", "")
    return k


def test_envelope_encryption_rewrap_changes_only_the_key_wrapping(kek, monkeypatch):
    enc = crypto.encrypt("민감한 값", aad="chat:a:b")
    alg, kid, wrapped, body = enc.split(":", 2)[2].split(":", 3)
    assert alg == "a256gcm" and crypto.decrypt(enc, aad="chat:a:b") == "민감한 값"
    assert crypto.encrypt("민감한 값", aad="chat:a:b") != enc                # 값마다 새 데이터 키·nonce
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(enc, aad="chat:a:c")                                 # 다른 대화로 옮겨 붙이면 실패
    new = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", kek)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", new)
    assert crypto.needs_rotation(enc)
    re = crypto.rewrap(enc, aad="chat:a:b")
    _alg, kid2, wrapped2, body2 = re.split(":", 2)[2].split(":", 3)
    assert kid2 != kid and wrapped2 != wrapped and body2 == body           # 데이터 암호문은 그대로, DEK 포장만 교체
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", "")           # 예전 키를 지워도
    assert crypto.decrypt(re, aad="chat:a:b") == "민감한 값" and not crypto.needs_rotation(re)


def test_legacy_v1_values_are_read_and_migrated(kek):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = base64.b64decode(kek)
    nonce = os.urandom(12)
    v1 = f"enc:v1:{crypto._kid(k)}:{base64.b64encode(nonce + AESGCM(k).encrypt(nonce, b'old', b'app_meta:x')).decode()}"
    assert crypto.decrypt(v1, aad="app_meta:x") == "old" and crypto.needs_rotation(v1)
    v2 = crypto.rewrap(v1, aad="app_meta:x")
    assert v2.startswith("enc:v2:") and crypto.decrypt(v2, aad="app_meta:x") == "old"


def test_unknown_algorithm_is_rejected(kek):
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt("x", alg="rot13")
    assert "a256gcm" in crypto.algorithms()


def _pair():
    for u in ("dp-a", "dp-b"):
        auth.register(u, "Dp-pass-2026")
    h = {u: {"Authorization": "Bearer " + client.post("/auth/login", json={"username": u, "password": "Dp-pass-2026"})
             .json()["token"]} for u in ("dp-a", "dp-b")}
    client.post("/friends/dp-b", headers=h["dp-a"])
    client.post("/friends/dp-a/accept", headers=h["dp-b"])
    return h


def test_chat_bodies_are_encrypted_at_rest_and_rotated(kek, monkeypatch):
    h = _pair()
    assert client.post("/chat/dp-b/messages", headers=h["dp-a"], json={"body": "비밀 약속 장소"}).status_code == 200
    stored = db.query("SELECT body FROM chat_message")[0]["body"]
    assert stored.startswith("enc:v2:") and "약속" not in stored
    assert client.get("/chat/dp-a/messages", headers=h["dp-b"]).json()["items"][-1]["body"] == "비밀 약속 장소"
    assert client.get("/chat/rooms", headers=h["dp-b"]).json()["items"][0]["last_body"] == "비밀 약속 장소"
    new = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", kek)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", new)
    r = crypto.rotate_all()
    assert r["chat_message.body"] == {"scanned": 1, "rewrapped": 1}
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEYS_OLD", "")
    assert client.get("/chat/dp-a/messages", headers=h["dp-b"]).json()["items"][-1]["body"] == "비밀 약속 장소"


def test_every_table_is_classified_and_protections_hold():
    tables = {t.name: t for t in db.metadata.sorted_tables}
    assert set(tables) - set(classification.REGISTRY) == set()            # 새 표는 분류부터
    assert set(classification.REGISTRY) - set(tables) == set()            # 없어진 표가 남아 있지 않게
    for name, t in classification.REGISTRY.items():
        assert t.level in classification.LEVELS
        for col, c in t.columns.items():
            assert col in tables[name].c, f"{name}.{col}"
            if c.level == "L4":
                assert set(c.protection) & {"hash", "encrypt"}, f"{name}.{col} 는 해시·암호화 필요"
        if t.level == "L4":
            assert set(t.protection) & {"hash", "encrypt"}, name
        if t.owner:
            for col in t.owner.split(","):
                assert col in tables[name].c, f"{name}.{col}"
    forced = {r["relname"] for r in db.query(
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relrowsecurity AND c.relforcerowsecurity")}
    assert classification.rls_tables() <= forced                            # 'rls' 표시 표는 DB 에서 FORCE RLS
