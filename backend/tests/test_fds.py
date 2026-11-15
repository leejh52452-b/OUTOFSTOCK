"""이상거래탐지(FDS) — 규칙 단위·송금 연동·합성 평가 회귀(app/services/fds.py, app/models/fds_eval.py)."""
import threading

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.models import fds_eval
from app.services import auth, fds

client = TestClient(app)
PW = "Fd-pass-2026"


def _h(u):
    auth.register(u, PW)
    tok = client.post("/auth/login", json={"username": u, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _age(u):
    db.execute("UPDATE auth_session SET auth_time = now() - interval '1 hour' WHERE username=:u", {"u": u})


def _par(n, fn):
    barrier, out, errs = threading.Barrier(n), [], []

    def run(i):
        barrier.wait()
        try:
            out.append(fn(i))
        except Exception as e:                       # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out, errs


def test_rules_are_individually_testable():
    base = {"amount": 50_000, "history": 10, "median_amount": 40_000, "balance_before": 5_000_000,
            "balance_after": 4_950_000, "known_payee": True}
    assert fds.score(base) == (0, [])
    assert "velocity" in fds.score({**base, "sent_10m": 6})[1]
    assert "spike" in fds.score({**base, "amount": 400_000, "balance_after": 4_600_000})[1]
    assert "spike" not in fds.score({**base, "amount": 200_000})[1]                 # 작은 금액은 배수가 커도 제외
    drain = {**base, "amount": 2_000_000, "balance_before": 2_000_000, "balance_after": 0, "known_payee": False}
    pts, hits = fds.score({**drain, "ip_change": True, "pw_changed_24h": True})
    assert {"drain", "new_payee", "ato_session", "recent_pw"} <= set(hits) and fds.decide(pts) == "block"
    assert "charge_out" in fds.score({**base, "amount": 1_000_000, "charged_1h": 1_050_000})[1]
    assert "charge_out" not in fds.score({**base, "amount": 1_000_000, "charged_1h": 50_000})[1]


def test_account_takeover_drain_is_blocked_and_recorded():
    h = _h("fd-victim")
    _h("fd-thief")
    client.get("/wallet", headers=h)
    _age("fd-victim")
    db.execute("UPDATE app_user SET password_changed_at = now() WHERE username='fd-victim'")
    db.execute("UPDATE auth_session SET last_ip_net='198.51.100.0/24' WHERE username='fd-victim'")
    client.get("/me", headers=h)                                              # 다른 대역 → 위험 신호
    client.post("/auth/step-up", headers=h, json={"password": PW})            # 정상 사용자도 재인증은 했다고 가정
    db.execute("UPDATE auth_session SET risk='ip_change' WHERE username='fd-victim'")
    r = client.post("/transfer", headers=h, json={"to": "fd-thief", "amount": 1_000_000})
    assert r.status_code == 403 and r.json()["code"] == "fds_blocked"
    row = db.query("SELECT decision, score FROM fds_decision WHERE username='fd-victim'")[0]
    assert row["decision"] == "block" and row["score"] >= fds.BLOCK
    assert db.query("SELECT 1 FROM security_event WHERE kind='fds_block'")
    assert client.get("/wallet", headers=h).json()["balance"] == 1_000_000      # 돈은 그대로


def test_velocity_requires_step_up_then_passes():
    h = _h("fd-fast")
    _h("fd-friend")
    client.get("/wallet", headers=h)
    _age("fd-fast")
    for _ in range(5):
        assert client.post("/transfer", headers=h, json={"to": "fd-friend", "amount": 1000}).status_code == 200
    r = client.post("/transfer", headers=h, json={"to": "fd-friend", "amount": 1000})
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    assert client.post("/auth/step-up", headers=h, json={"password": PW}).status_code == 200
    assert client.post("/transfer", headers=h, json={"to": "fd-friend", "amount": 1000}).status_code == 200
    ds = [r["decision"] for r in db.query("SELECT decision FROM fds_decision WHERE username='fd-fast' ORDER BY id")]
    assert ds[-2:] == ["step_up", "allow_after_step_up"]


def test_synthetic_evaluation_regression():
    r = fds_eval.evaluate(n=3000)
    assert r["fraud_caught_rate"] >= 0.95 and r["fraud_blocked_rate"] >= 0.7
    assert r["normal_block_rate"] <= 0.005 and r["normal_step_up_rate"] <= 0.08


# ── 판정과 실행 사이(결함 20~22) ─────────────────────────
def test_parallel_transfers_cannot_slip_past_velocity():
    """판정이 지갑 잠금 밖에 있으면 동시에 보낸 송금이 모두 '최근 10분 0건'을 보고 통과했다(TOCTOU) —
    판정을 잠금 뒤·같은 트랜잭션으로 옮겨, 몇 건을 동시에 보내도 6건째부터는 추가 인증."""
    h = _h("fd-burst")
    _h("fd-sink")
    client.get("/wallet", headers=h)
    codes, errs = _par(10, lambda i: client.post("/transfer", headers=h,
                                                json={"to": "fd-sink", "amount": 1000}).status_code)
    assert not errs and sorted(codes) == [200] * 5 + [401] * 5
    sent = db.query("SELECT COUNT(*) AS n FROM wallet_ledger WHERE username='fd-burst' AND reason='transfer_out'")[0]["n"]
    assert sent == 5
    ds = [r["decision"] for r in db.query("SELECT decision FROM fds_decision WHERE username='fd-burst'")]
    assert sorted(ds) == ["allow"] * 5 + ["step_up"] * 5                    # 막힌 판정도 남는다(롤백돼도)


def test_retry_of_completed_transfer_returns_original_result_without_rescoring():
    """같은 키 재시도(응답 유실)는 판정보다 먼저 '이미 처리'를 확인한다 — 예전엔 처리된 송금을 다시 채점해
    추가 인증·차단(4xx)을 돌려줬고, 화면은 4xx 면 새 키를 만들어 같은 돈을 한 번 더 보냈다."""
    h = _h("fd-retry")
    _h("fd-shop")
    client.get("/wallet", headers=h)
    for i in range(4):
        assert client.post("/transfer", headers=h, json={"to": "fd-shop", "amount": 1000}).status_code == 200
    k = {**h, "Idempotency-Key": "lost-response-1"}
    first = client.post("/transfer", headers=k, json={"to": "fd-shop", "amount": 1000})
    assert first.status_code == 200 and first.json()["duplicate"] is False
    again = client.post("/transfer", headers=k, json={"to": "fd-shop", "amount": 1000})   # 이제 '최근 10분 5건'
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["balance"] == first.json()["balance"]
    assert db.query("SELECT COUNT(*) AS n FROM fds_decision WHERE username='fd-retry'")[0]["n"] == 5   # 재시도는 채점 안 함
    conflict = client.post("/transfer", headers=k, json={"to": "fd-shop", "amount": 2000})
    assert conflict.status_code == 409


def test_payee_is_normalized_once_for_screening_and_execution():
    """받는 사람 앞뒤 공백 — 예전엔 송금은 공백을 지운 계정으로 갔는데 FDS 는 공백 붙은 이름(행 없음)으로 채점해
    모집책 계좌 규칙(fan_in)·맞송금 규칙(round_trip)을 비켜 갔다."""
    _h("fd-mule")
    for i in range(5):
        hs = _h(f"fd-src{i}")
        client.get("/wallet", headers=hs)
        assert client.post("/transfer", headers=hs, json={"to": "fd-mule", "amount": 1000}).status_code == 200
    h = _h("fd-src5")
    client.get("/wallet", headers=h)
    r = client.post("/transfer", headers=h, json={"to": "  fd-mule ", "amount": 1000})
    assert r.status_code == 200 and r.json()["to"] == "fd-mule"
    row = db.query("SELECT counterparty, score, rules FROM fds_decision WHERE username='fd-src5'")[0]
    assert row["counterparty"] == "fd-mule" and "fan_in" in row["rules"] and row["score"] >= 35   # 공백이 규칙을 못 비켜 간다
