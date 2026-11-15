"""서비스 비상 차단 — 기능별 즉시 중지 · 다른 관리자 승인(2인)으로만 재개 · 원장 이상 시 자동 중지(app/services/controls.py).
근거: 2026 디지털·IT 감독 업무설명회 「중대 전자금융사고 대응」 — 피해 확산 방지·신속 복구·재발 방지."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.security import events, rbac, totp
from app.services import auth, controls, wallet

client = TestClient(app)
PW = "Ctl-pass-2026"


@pytest.fixture(autouse=True)
def _fresh_cache():
    controls.clear_cache()
    yield
    controls.clear_cache()


def _user(name, topup=0):
    auth.register(name, PW)
    if topup:
        wallet.credit_once(name, topup, "iap", memo="시험 충전")
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _admin(name, monkeypatch, clock_box):
    monkeypatch.setattr(settings, "ADMIN_USERS", set(settings.ADMIN_USERS) | {name})
    h = _user(name)
    rbac.bootstrap()
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    s = client.post("/auth/mfa/setup", headers=h).json()["secret"]
    client.post("/auth/mfa/confirm", headers=h, json={"code": totp.code_at(s, totp.current_step())})
    clock_box[0] += 30
    r = client.post("/admin/elevate", headers=h, json={"role": "security_admin", "reason": "장애 대응 당번 근무"})
    assert r.status_code == 200, r.text
    return h


def test_pause_stops_only_that_feature_and_returns_503():
    a = _user("ct-a", topup=200_000)
    _user("ct-b")
    controls.pause("transfer", "송금 이상 징후 조사", "ops-1")
    r = client.post("/transfer", headers=a, json={"to": "ct-b", "amount": 1000})
    assert r.status_code == 503 and r.json()["code"] == "service_paused" and r.headers["Retry-After"] == "60"
    assert "송금" in r.json()["detail"]
    assert client.get("/wallet", headers=a).status_code == 200                       # 조회는 그대로
    assert client.post("/avatar/buy", headers=a, json={"item_id": "hat-1"}).status_code == 200   # 다른 기능도 그대로
    assert wallet.reconcile("ct-a")["ok"]


def test_resume_needs_a_second_admin_and_is_audited():
    _user("ct-c", topup=50_000)
    controls.pause("shop", "꾸미기 가격 오류 의심", "ops-1")
    req = controls.request_resume("shop", "가격표 확인 완료 — 정상", "ops-1")
    with pytest.raises(controls.ControlError) as e:
        controls.request_resume("shop", "중복 요청 시험입니다", "ops-2")            # 대기 중 요청은 기능마다 하나
    assert e.value.code == "pending_exists"
    with pytest.raises(controls.ControlError) as e:
        controls.decide(req["request_id"], True, "ops-1")                         # 요청자 본인 승인 불가
    assert e.value.code == "four_eyes"
    assert controls.is_paused("shop")
    assert controls.decide(req["request_id"], True, "ops-2")["status"] == "approved"
    assert not controls.is_paused("shop")
    acts = [r["action"] for r in db.query("SELECT action FROM audit_log WHERE target='shop' ORDER BY audit_id")]
    assert acts[-3:] == ["control.pause", "control.resume_request", "control.resume_approved"]


def test_ledger_tamper_pauses_money_features_automatically(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: sent.append(a[1]) or {"sent": []})
    events.emit("ledger_tamper", "critical", day="2026-11-01")
    st = {f["feature"]: f for f in controls.state()["features"]}
    assert all(st[f]["state"] == "paused" for f in ("transfer", "charge", "shop", "invest"))
    assert st["transfer"]["changed_by"] == "system" and "원장 변조" in st["transfer"]["reason"]
    resp = db.query("SELECT response FROM security_event WHERE kind='ledger_tamper' ORDER BY id DESC LIMIT 1")[0]
    assert resp["response"].startswith("pause(")
    assert any("비상 차단" in t for t in sent)


def test_fds_block_surge_pauses_transfer(monkeypatch):
    from app.services import alerts
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: {"sent": []})
    for i in range(9):
        events.emit("fds_block", "high", username=f"ct-x{i}", score=80)
    assert not controls.is_paused_db("transfer")
    events.emit("fds_block", "high", username="ct-x9", score=80)                # 10분 10건
    assert controls.is_paused_db("transfer") and not controls.is_paused_db("shop")


def test_admin_api_pause_and_two_person_resume(monkeypatch):
    clock_box = [1_800_000_000.0]
    monkeypatch.setattr(totp, "_now", lambda: clock_box[0])
    h1 = _admin("ct-ops1", monkeypatch, clock_box)
    h2 = _admin("ct-ops2", monkeypatch, clock_box)
    assert client.post("/admin/controls/invest/pause", headers=h1, json={"reason": "x"}).status_code == 422
    r = client.post("/admin/controls/invest/pause", headers=h1, json={"reason": "시세 원천 장애로 체결가 이상"})
    assert r.status_code == 200 and {f["feature"]: f["state"] for f in r.json()["features"]}["invest"] == "paused"
    rid = client.post("/admin/controls/invest/resume", headers=h1, json={"reason": "원천 복구 확인"}).json()["request_id"]
    assert client.post(f"/admin/controls/requests/{rid}/approve", headers=h1).json()["code"] == "four_eyes"
    assert client.post(f"/admin/controls/requests/{rid}/approve", headers=h2).json()["status"] == "approved"
    assert client.get("/admin/controls", headers=h2).json()["pending"] == []
    assert client.get("/admin/capacity", headers=h2).json()["level_name"] in ("정상", "주의", "경계", "심각")
    plain = _user("ct-plain")
    assert client.post("/admin/controls/transfer/pause", headers=plain, json={"reason": "권한 없는 시도"}).status_code == 403
