"""양봉이 꾸미기 — 가상머니로 아이템(모자·옷·바지·신발·안경·펫)을 사서 입힌다(2026-10-31, 팀 결정).

카탈로그: app/data/avatar_items.json — 화면 파일 web/assets/avatar-items.js 에서 tools/build_avatar_items.py 로 꺼낸 사본.
          **가격은 서버 사본이 기준**(화면이 보낸 금액을 믿지 않는다 — 화면은 아이템 id 만 보낸다).
구매: 산 기록(avatar_closet)과 지갑 차감(원장 'avatar_buy' · 분개 상대 계정 sys:avatar_shop)을 **같은 트랜잭션**에서.
      이미 산 아이템은 다시 돈을 받지 않는다(계정·아이템 PK — 동시에 두 번 눌러도 1번). 잔액이 모자라면 기록도 취소.
착용: 부위마다 하나 — (username, slot) WHERE equipped 부분 유일 인덱스가 DB 에서 보장. 사지 않은 아이템은 입을 수 없다.
      같은 계정의 꾸미기 요청은 계정별 advisory lock 으로 줄을 세운다(두 탭에서 동시에 갈아입어도 유일 조건 충돌 없이).
되팔기·선물은 없다(원장 멱등키 avatar:<계정>:<아이템> 이 '한 번 산 아이템'을 전제로 한다 — 되팔기를 만들면 키를 바꿀 것).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text

from app import clock, db

SLOTS = ("hat", "top", "bottom", "shoes", "glasses", "pet")
_PATH = Path(__file__).resolve().parents[1] / "data" / "avatar_items.json"


class AvatarError(ValueError):
    """규칙 위반(없는 아이템·이미 가짐·안 산 아이템 착용 등) — 400."""


class AvatarPriceChanged(AvatarError):
    """화면이 보여 준 가격과 서버 가격이 다르다(예전 화면 파일이 캐시에 남은 경우 등) — 409, 돈을 빼지 않는다."""


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _by_id() -> dict:
    return {it["id"]: it for it in _data()["items"]}


def catalog() -> dict:
    d = _data()
    return {"slots": d["slots"], "tiers": d["tiers"], "classes": d["classes"],
            "items": [{k: it[k] for k in ("id", "slot", "tier", "name", "price", "desc")} for it in d["items"]]}


def item(item_id: str) -> dict | None:
    return _by_id().get(item_id) if isinstance(item_id, str) else None


def worth(equipped: dict) -> int:
    """입은 아이템 가격 합(양봉이 몸값) — 카탈로그 기준."""
    return sum((item(i) or {}).get("price", 0) for i in equipped.values())


def class_of(w: int) -> dict:
    c = _data()["classes"][0]
    for k in _data()["classes"]:
        if w >= k["min"]:
            c = k
    return {"name": c["name"], "level": _data()["classes"].index(c) + 1}


def _lock(conn, username: str) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "avatar:" + username})


def _state(conn, username: str) -> dict:
    rows = conn.execute(text("SELECT item_id, slot, equipped FROM avatar_closet WHERE username=:u ORDER BY bought_at, item_id"),
                        {"u": username}).mappings().all()
    owned = [r["item_id"] for r in rows if item(r["item_id"])]
    equipped = {r["slot"]: r["item_id"] for r in rows if r["equipped"] and item(r["item_id"])}
    w = worth(equipped)
    return {"owned": owned, "equipped": equipped, "worth": w, "class": class_of(w)}


def state(username: str) -> dict:
    with db.transaction() as conn:
        return _state(conn, username)


def _set(conn, username: str, slot: str, item_id: str | None) -> None:
    conn.execute(text("UPDATE avatar_closet SET equipped=false WHERE username=:u AND slot=:s AND equipped"),
                 {"u": username, "s": slot})
    if item_id is not None and conn.execute(text(
            "UPDATE avatar_closet SET equipped=true WHERE username=:u AND item_id=:i RETURNING item_id"),
            {"u": username, "i": item_id}).first() is None:
        raise AvatarError("아직 사지 않은 아이템이에요")


def buy(username: str, item_id: str, equip: bool = True, expect_price: int | None = None) -> dict:
    """구매(+바로 입기). 이미 가진 아이템이면 400 — 돈을 다시 받지 않는다.
    expect_price: 화면이 사용자에게 보여 준 가격. 서버 가격과 다르면 아무것도 바꾸지 않고 AvatarPriceChanged
    (사용자가 동의한 금액과 실제로 빠지는 금액이 같을 때만 결제 — 결제 금액 검증)."""
    it = item(item_id)
    if not it:
        raise AvatarError("없는 아이템이에요")
    if expect_price is not None and expect_price != it["price"]:
        raise AvatarPriceChanged(f"가격이 바뀌었어요(지금 {it['price']:,}원) — 화면을 새로고침한 뒤 다시 사 주세요")
    from app.services import wallet
    with db.transaction() as conn:
        _lock(conn, username)
        if conn.execute(text(
                "INSERT INTO avatar_closet(username, item_id, slot, price, equipped, bought_at) "
                "VALUES(:u, :i, :s, :p, false, :t) ON CONFLICT DO NOTHING RETURNING item_id"),
                {"u": username, "i": item_id, "s": it["slot"], "p": it["price"], "t": clock.utc()}).first() is None:
            raise AvatarError("이미 가지고 있는 아이템이에요")
        paid = wallet.debit(conn, username, it["price"], "avatar_buy", ref=item_id,
                            memo=f"양봉이 꾸미기 · {it['name']}", idem_key=f"avatar:{username}:{item_id}")
        if not paid["applied"]:                                 # 돈을 안 받고 아이템만 주는 일은 없게(방어)
            raise AvatarError("이미 결제된 기록이 있어요 — 새로고침해 주세요")
        if equip:
            _set(conn, username, it["slot"], item_id)
        out = _state(conn, username)
    out.update(balance=paid["balance"], bought=item_id)
    return out


def equip(username: str, changes: dict) -> dict:
    """부위별로 갈아입기 — {'hat': 'hat-3', 'pet': None(벗기)} 처럼 바꿀 부위만. 한 트랜잭션(하나라도 틀리면 그대로)."""
    if not isinstance(changes, dict) or not changes:
        raise AvatarError("바꿀 부위를 보내 주세요")
    for slot, item_id in changes.items():
        if slot not in SLOTS:
            raise AvatarError("없는 부위예요(hat·top·bottom·shoes·glasses·pet)")
        if item_id is not None and ((item(item_id) or {}).get("slot") != slot):
            raise AvatarError("그 부위에 맞지 않는 아이템이에요")
    with db.transaction() as conn:
        _lock(conn, username)
        for slot in SLOTS:                                      # 순서 고정(같은 계정은 잠금으로 이미 한 줄)
            if slot in changes:
                _set(conn, username, slot, changes[slot])
        return _state(conn, username)


def delete_user(conn, username: str) -> None:
    conn.execute(text("DELETE FROM avatar_closet WHERE username=:u"), {"u": username})
