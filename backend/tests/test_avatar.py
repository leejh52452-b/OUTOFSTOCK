"""양봉이 꾸미기 — 가격은 서버 카탈로그(화면 파일과 해시로 묶음) · 구매 = 지갑 차감과 같은 트랜잭션(두 번 눌러도 1번) ·
부위마다 하나만 착용(DB 부분 유일 인덱스) · 계정별 격리 · 탈퇴 시 삭제 · 위험 신호가 있으면 추가 인증."""
import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.security import pdp
from app.security.context import Principal
from app.services import account, auth, avatar, wallet

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
WEB_ITEMS = ROOT / "web" / "assets" / "avatar-items.js"
PW = "Avatar-pass-2026"


def _user(name, topup=0):
    auth.register(name, PW)
    if topup:
        wallet.credit_once(name, topup, "iap", memo="시험 충전")
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _bought(u):
    return db.query("SELECT amount, memo, ref, ledger_id FROM wallet_ledger WHERE username=:u AND reason='avatar_buy' "
                    "ORDER BY ledger_id", {"u": u})


def test_catalog_is_the_screen_file_six_slots_five_tiers():
    data = avatar._data()
    assert data["source_sha256"] == hashlib.sha256(WEB_ITEMS.read_bytes()).hexdigest(), \
        "화면의 avatar-items.js 가 바뀌었다 — python tools/build_avatar_items.py 로 서버 사본을 다시 만든다"
    cat = client.get("/avatar/catalog").json()
    assert [s["id"] for s in cat["slots"]] == list(avatar.SLOTS)
    for slot in avatar.SLOTS:
        items = sorted((i for i in cat["items"] if i["slot"] == slot), key=lambda i: i["tier"])
        assert [i["tier"] for i in items] == [1, 2, 3, 4, 5], slot
        assert [i["price"] for i in items] == [10_000, 100_000, 1_000_000, 10_000_000, 100_000_000], slot   # 등급마다 10배
    assert "art" not in cat["items"][0]                                          # 그림은 화면에만


def test_buy_charges_the_server_price_in_one_transaction_and_equips():
    h = _user("av-buy")
    before = wallet.balance("av-buy")
    r = client.post("/avatar/buy", headers=h, json={"item_id": "hat-2"}).json()
    assert r["balance"] == before - 100_000 and r["equipped"] == {"hat": "hat-2"} and r["owned"] == ["hat-2"]
    assert r["worth"] == 100_000 and r["class"]["name"] == "흙수저"
    rows = _bought("av-buy")
    assert len(rows) == 1 and rows[0]["amount"] == -100_000 and rows[0]["memo"] == "양봉이 꾸미기 · 빨간 야구모자"
    je = db.query("SELECT account, amount FROM journal_entry WHERE txn_id = "
                  "(SELECT txn_id FROM journal_entry WHERE ledger_id=:l)", {"l": rows[0]["ledger_id"]})
    assert sum(j["amount"] for j in je) == 0 and "sys:avatar_shop" in {j["account"] for j in je}    # 상대 계정 = 꾸미기 매출
    assert wallet.reconcile("av-buy")["ok"]
    me = client.get("/avatar/me", headers=h).json()
    assert me["equipped"] == {"hat": "hat-2"} and me["balance"] == before - 100_000
    logs = client.get("/me/logs/wallet?filter=shop", headers=h).json()["items"]                     # 마이페이지 '내 활동'
    assert logs and logs[0]["reason_ko"] == "양봉이 꾸미기"


def test_no_second_charge_and_no_item_without_money():
    h = _user("av-twice")
    assert client.post("/avatar/buy", headers=h, json={"item_id": "pet-1"}).status_code == 200
    bal = wallet.balance("av-twice")
    r = client.post("/avatar/buy", headers=h, json={"item_id": "pet-1"})
    assert r.status_code == 400 and "이미" in r.json()["detail"] and wallet.balance("av-twice") == bal
    r = client.post("/avatar/buy", headers=h, json={"item_id": "hat-5"})                            # 1억 — 잔액 부족
    assert r.status_code == 400 and "잔액" in r.json()["detail"]
    assert db.query("SELECT COUNT(*) AS n FROM avatar_closet WHERE username='av-twice' AND item_id='hat-5'")[0]["n"] == 0
    assert wallet.balance("av-twice") == bal and len(_bought("av-twice")) == 1
    for bad in ({"item_id": "hat-9"}, {"item_id": "x" * 21}, {}):
        assert client.post("/avatar/buy", headers=h, json=bad).status_code in (400, 422)


def test_price_shown_on_screen_must_match_the_server_price():
    h = _user("av-price")
    bal = wallet.balance("av-price")
    r = client.post("/avatar/buy", headers=h, json={"item_id": "top-1", "price": 1})            # 예전 화면(캐시)이 보여 준 값
    assert r.status_code == 409 and "새로고침" in r.json()["detail"]
    assert wallet.balance("av-price") == bal and not _bought("av-price")                        # 아무것도 바뀌지 않음
    assert client.post("/avatar/buy", headers=h, json={"item_id": "top-1", "price": 10_000}).status_code == 200
    me = client.get("/me", headers=h).json()["avatar"]                                          # 마이페이지 프로필 그림
    assert me["equipped"] == {"top": "top-1"} and me["worth"] == 10_000 and me["owned"] == 1


def test_double_click_from_two_tabs_charges_once():
    _user("av-race", topup=0)
    out, errs = [None] * 5, []
    gate = threading.Barrier(5)

    def run(i):
        gate.wait()
        try:
            out[i] = avatar.buy("av-race", "top-2")
        except Exception as e:                              # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for o in out if o) == 1 and len(errs) == 4 and all(isinstance(e, avatar.AvatarError) for e in errs)
    assert len(_bought("av-race")) == 1 and wallet.reconcile("av-race")["ok"]


def test_equip_only_owned_one_per_slot_all_or_nothing():
    h = _user("av-equip", topup=50_000_000)
    for it in ("hat-1", "hat-3", "glasses-2"):
        client.post("/avatar/buy", headers=h, json={"item_id": it, "equip": False})
    assert client.get("/avatar/me", headers=h).json()["equipped"] == {}
    r = client.post("/avatar/equip", headers=h, json={"items": {"hat": "hat-1", "glasses": "glasses-2"}}).json()
    assert r["equipped"] == {"hat": "hat-1", "glasses": "glasses-2"} and r["worth"] == 110_000
    r = client.post("/avatar/equip", headers=h, json={"items": {"hat": "hat-3"}}).json()              # 같은 부위는 갈아입기
    assert r["equipped"]["hat"] == "hat-3"
    assert db.query("SELECT COUNT(*) AS n FROM avatar_closet WHERE username='av-equip' AND equipped AND slot='hat'")[0]["n"] == 1
    for bad in ({"hat": "hat-2"},                            # 안 산 아이템
                {"hat": "glasses-2"},                        # 다른 부위 아이템
                {"wings": "hat-1"},                          # 없는 부위
                {"glasses": None, "hat": "hat-4"}):          # 하나라도 틀리면 전부 그대로
        assert client.post("/avatar/equip", headers=h, json={"items": bad}).status_code == 400, bad
    assert client.get("/avatar/me", headers=h).json()["equipped"] == {"hat": "hat-3", "glasses": "glasses-2"}
    r = client.post("/avatar/equip", headers=h, json={"items": {"hat": None, "glasses": None}}).json()  # 벗기
    assert r["equipped"] == {} and r["worth"] == 0
    with pytest.raises(Exception):                           # DB 도 부위당 하나만 — 앱을 거치지 않아도
        with db.transaction() as conn:
            conn.execute(db.text("UPDATE avatar_closet SET equipped=true WHERE username='av-equip' AND slot='hat'"))


def test_login_required_and_isolated_per_user():
    assert client.get("/avatar/me").status_code == 401
    assert client.post("/avatar/buy", json={"item_id": "hat-1"}).status_code in (401, 403)
    a, b = _user("av-a"), _user("av-b")
    client.post("/avatar/buy", headers=a, json={"item_id": "shoes-1"})
    assert client.get("/avatar/me", headers=b).json()["owned"] == []
    assert client.post("/avatar/equip", headers=b, json={"items": {"shoes": "shoes-1"}}).status_code == 400


def test_buying_needs_step_up_only_when_the_session_looks_risky():
    calm = Principal(kind="user", name="u", aal=1)
    risky = Principal(kind="user", name="u", aal=1, risk=frozenset({"ip_change"}))
    assert pdp.decide("avatar.buy", calm).allow
    assert pdp.decide("avatar.buy", risky).code == "step_up_required"
    assert pdp.decide("avatar.buy", risky, fresh=True).allow


def test_account_deletion_removes_avatar_rows():
    h = _user("av-del")
    client.post("/avatar/buy", headers=h, json={"item_id": "glasses-1"})
    account.delete_account("av-del")
    assert db.query("SELECT COUNT(*) AS n FROM avatar_closet WHERE username='av-del'")[0]["n"] == 0


def test_screen_sends_only_the_item_id_and_the_price_it_showed():
    page = (ROOT / "web" / "avatar.html").read_text(encoding="utf-8")
    assert "body: { item_id: id, equip: true, price: it.price }" in page          # 금액은 '확인용'으로만 — 차감은 서버 가격
    assert "/avatar/me" in page and "/avatar/equip" in page
    for name in ("avatar", "mypage", "index"):                                   # 그림 파일은 공용 런타임 다음에
        src = (ROOT / "web" / f"{name}.html").read_text(encoding="utf-8")
        assert src.index('src="assets/oos/runtime.js"') < src.index('src="assets/avatar-items.js"'), name
    for name in ("index", "learn", "news", "practice", "mypage", "stocks", "avatar"):   # 모든 화면 메뉴에 '양봉이 꾸미기'
        assert 'href="avatar.html"' in (ROOT / "web" / f"{name}.html").read_text(encoding="utf-8"), name
