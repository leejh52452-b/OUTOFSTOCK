"""웹 실습 기록 — 브라우저 계산값이라 '범위·종류만 확인하고 보상과 잇지 않는다'는 신뢰 경계 · 집계 · 시도 제한 · 격리 · 탈퇴."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings
from app.services import account, auth, wallet

client = TestClient(app)
PW = "Prac-pass-2026"


def _user(name):
    auth.register(name, PW)
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_records_and_summary_follow_the_screen_shape():
    h = _user("pr-a")
    post = lambda **b: client.post("/practice/records", headers=h, json=b)   # noqa: E731
    assert post(tech="golden", mode="practice", traded=True, ret=0.05).status_code == 200
    assert post(tech="golden", mode="practice", traded=True, ret=-0.02).status_code == 200
    assert post(tech="golden", mode="practice", traded=False, ret=0.9).status_code == 200   # 매매 안 한 판의 수익률은 버린다
    r = post(tech="rsi", mode="real", traded=True, ret=0.12, entry_date="2019-05-21").json()
    s = r["summary"]
    assert s["golden"] == {"tries": 3, "best": 0.05, "real_tries": 0, "real_best": None, "traded": 2}
    assert s["rsi"] == {"tries": 0, "best": None, "real_tries": 1, "real_best": 0.12, "traded": 1}
    ov = client.get("/practice/records?limit=2", headers=h).json()
    assert ov["total"] == 4 and len(ov["items"]) == 2 and ov["has_more"]
    assert ov["items"][0]["tech"] == "rsi" and ov["items"][0]["real_entry"] == "2019-05-21"
    assert db.query("SELECT ret FROM practice_record WHERE user_id='pr-a' AND traded=false")[0]["ret"] is None


@pytest.mark.parametrize("body", [
    {"tech": "evil", "mode": "practice", "traded": True, "ret": 0.1},
    {"tech": "golden", "mode": "turbo", "traded": True, "ret": 0.1},
    {"tech": "golden", "mode": "practice", "traded": True, "ret": 10.5},       # +1000% 초과
    {"tech": "golden", "mode": "practice", "traded": True, "ret": -1.5},       # 원금보다 많이 잃을 수 없다
    {"tech": "golden", "mode": "practice", "traded": True},                    # 매매했는데 수익률 없음
    {"tech": "golden", "mode": "real", "traded": False, "entry_date": "2019-5-21"},
    {"tech": "golden", "mode": "real", "traded": False, "entry_date": "2009-01-01"},
])
def test_out_of_range_or_unknown_values_are_rejected(body):
    h = _user("pr-bad")
    assert client.post("/practice/records", headers=h, json=body).status_code in (400, 422)
    assert client.get("/practice/records", headers=h).json()["total"] == 0


def test_records_never_touch_the_wallet():
    h = _user("pr-money")
    before = wallet.balance("pr-money")
    client.post("/practice/records", headers=h, json={"tech": "support", "mode": "practice", "traded": True, "ret": 9.9})
    assert wallet.balance("pr-money") == before


def test_hourly_cap_per_account(monkeypatch):
    monkeypatch.setattr(settings, "PRACTICE_RECORD_MAX_PER_HOUR", 3)
    h = _user("pr-cap")
    codes = [client.post("/practice/records", headers=h, json={"tech": "rsi", "traded": False}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_login_required_and_per_user():
    assert client.get("/practice/records").status_code == 401
    a, b = _user("pr-p1"), _user("pr-p2")
    client.post("/practice/records", headers=a, json={"tech": "pullback", "traded": False})
    assert client.get("/practice/records", headers=b).json()["total"] == 0
    assert client.get("/practice/records", headers=a).json()["total"] == 1


def test_account_deletion_removes_records():
    h = _user("pr-del")
    client.post("/practice/records", headers=h, json={"tech": "golden", "traded": False})
    account.delete_account("pr-del")
    assert db.query("SELECT COUNT(*) AS n FROM practice_record WHERE user_id='pr-del'")[0]["n"] == 0
