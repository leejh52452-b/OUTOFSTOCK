"""모의투자 — 가상 시장 시즌(팀 invest.html · 2026-11-09). 돈과 시세는 서버가 정한다.

규칙(팀 화면과 같다 — 화면은 같은 시계를 그리기만 한다)
  · 시즌 = 한 주(월~금). 시즌 1 = 2026-11-02(월) 주. 가상 종목 5개(종목 A~E), 종목마다 사전 봉 120개(이동평균용, 처음부터
    공개) + 시즌 봉 65개(평일 09:00·09:30 … 15:00 에 하나씩 = 하루 13개). 15:30 장마감, 토·일은 결과 발표.
  · 참가: 시즌 중(월~금) 언제든, 참가금 VMARKET_MIN_ENTRY(100만 원) 이상 ~ 지갑 잔액 이하. 참가금은 지갑에서 빠져
    시즌 현금이 된다(원장 vmarket_entry · 분개 상대 sys:vmarket_custody). 시즌마다 한 번.
  · 주문: 장중에만, 지금 공개된 마지막 봉의 종가(= 화면의 현재가)로 바로 체결. 현금은 5종목이 함께 쓴다. 수수료 없음.
  · 정산: 시즌이 끝나면(토요일 00:00 KST 이후) 남은 주식을 종목마다 마지막 봉 종가로 자동 매도 → 최종 평가금액을
    지갑으로(vmarket_settle). 매일 00:05 스케줄러가 끝난 시즌을 모두 정산하고, 화면도 주말에 POST /vmarket/settle 로
    바로 정산을 요청할 수 있다(같은 시즌은 한 번만 — 상태 + 원장 멱등키).
보안·무결성(팀 시안은 브라우저 localStorage 에 잔액·체결·정산을 두고, 시즌 파일(미래 봉 포함)을 통째로 내려받았다)
  · 미래 봉 비공개 — 시즌 데이터는 DB(vmarket_season)에만 두고, 화면에는 서버 시계로 '지금까지 공개된 봉'까지만 준다.
    끝난 시즌만 전체·장세(시나리오)를 공개. 정적 파일로는 내보내지 않는다(web/ 에 시즌 파일 없음).
  · 체결가는 서버가 그 봉 종가로 정한다. 화면이 본 봉(bar)과 서버의 봉이 다르면 409(새 봉이 나온 사이 옛 가격으로 체결 방지).
  · 금액은 정수 원 — 체결가(정수) × 수량이라 현금이 늘 정수. 잔액 부족·보유 초과는 서버가 거부.
  · 동시성 — 참가 행을 FOR UPDATE 로 잠그고 처리(같은 계정의 동시 주문은 줄을 선다). 주문 재전송은 Idempotency-Key 로 1번.
  · 잠금 순서(교착 방지): 계정 행(KEY SHARE) → 참가·보유 행 → 지갑 — wallet.hold 규칙과 같다.
  · 개인 표(참가·보유·체결)는 FORCE RLS(주인 = username, V019). 탈퇴하면 함께 지운다(account.delete_account).
  · 시즌 데이터 가져오기는 형식·가격(시≤고·저≤종 범위·양수·정수)을 검사하고 SHA-256 을 남긴다. 이미 시작한 시즌은
    내용이 바뀐 파일로 덮어쓸 수 없다(사용자가 거래한 가격이 바뀌지 않게).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)

HIST, SEASON, PER_DAY, STOCKS = 120, 65, 13, 5
OPEN_MIN, CLOSE_MIN = 9 * 60, 15 * 60 + 30
EPOCH = date(2026, 11, 2)                       # 시즌 1 = 2026-11-02(월) 주
SCENARIOS = {"bull": "상승장", "bear": "하락장", "sideways": "횡보장", "crash_rebound": "급락 후 반등"}
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "vmarket"
MAX_QTY = 1_000_000_000
_lock = threading.Lock()
_payloads: dict[int, tuple[str, dict]] = {}     # season_id → (sha256, 데이터) — 시즌 데이터는 바뀌지 않으므로 해시가 같으면 재사용


class VMarketError(Exception):
    """규칙 위반(400) — 참가 조건·잔액·보유 수량·장 시간 등."""


class VMarketConflict(VMarketError):
    """상태가 바뀜(409) — 새 봉이 나왔거나 시즌이 바뀜. 화면을 새로 고친 뒤 다시."""


class VMarketNotFound(VMarketError):
    """없는·아직 공개되지 않은 시즌(404)."""


# ── 시계(한국시간) ─────────────────────────────────────────
def week_start(season_id: int) -> date:
    return EPOCH + timedelta(days=7 * (season_id - 1))


def clock_of(now: datetime | None = None) -> dict:
    """지금 시즌·요일·장 상태·공개된 봉 수 — 팀 화면 clockOf() 와 같은 규칙(서버는 한국시간)."""
    now = now or clock.now()
    wd = now.weekday()
    mins = now.hour * 60 + now.minute + now.second / 60
    sid = (now.date() - timedelta(days=wd) - EPOCH).days // 7 + 1
    if wd >= 5:
        phase, today = "weekend", PER_DAY
    elif mins < OPEN_MIN:
        phase, today = "pre", 0
    elif mins < CLOSE_MIN:
        phase, today = "open", min(PER_DAY, int((mins - OPEN_MIN) // 30) + 1)
    else:
        phase, today = "closed", PER_DAY
    revealed = SEASON if wd >= 5 else wd * PER_DAY + today
    return {"id": sid, "wd": wd, "phase": phase, "today": today, "revealed": revealed}


def _key(c: dict) -> tuple:
    return c["id"], c["phase"], c["revealed"]


def next_change(now: datetime | None = None) -> datetime:
    """다음에 시계(시즌·장 상태·공개 봉 수)가 바뀌는 시각 — 화면이 그때 다시 묻는다."""
    now = (now or clock.now()).replace(microsecond=0)
    cur = _key(clock_of(now))
    day = now.replace(hour=0, minute=0, second=0)
    for add in range(0, 9):
        base = day + timedelta(days=add)
        marks = [base + timedelta(minutes=m) for m in range(OPEN_MIN, CLOSE_MIN + 1, 30)] + [base + timedelta(days=1)]
        for t in marks:
            if t > now and _key(clock_of(t)) != cur:
                return t
    return now + timedelta(days=1)


def ended(season_id: int, cl: dict) -> bool:
    return season_id < cl["id"] or (season_id == cl["id"] and cl["phase"] == "weekend")


# ── 시즌 데이터 ────────────────────────────────────────────
def _canonical(raw: dict) -> dict:
    """팀 생성기 형식(season_XX.json · season_XX.js) → 검사한 표준 형식. 틀리면 VMarketError."""
    try:
        sid = int(raw["season"])
        stocks = raw["stocks"]
    except (KeyError, TypeError, ValueError):
        raise VMarketError("시즌 파일 형식이 아니에요(season·stocks 필요)") from None
    if sid < 1 or not isinstance(stocks, list) or len(stocks) != STOCKS:
        raise VMarketError(f"시즌 {sid}: 종목 {STOCKS}개가 필요해요")
    out = []
    for k, s in enumerate(stocks):
        if not isinstance(s, dict):
            raise VMarketError(f"시즌 {sid} 종목 {k + 1}: 형식 오류")
        name = str(s.get("name") or "").strip()
        if not name or len(name) > 40 or re.search(r"[<>\"'&]", name):
            raise VMarketError(f"시즌 {sid} 종목 {k + 1}: 이름이 비었거나 허용되지 않는 글자")
        if s.get("scenario") not in SCENARIOS:
            raise VMarketError(f"시즌 {sid} {name}: 알 수 없는 장세 {s.get('scenario')!r}")
        if int(s.get("warmup", -1)) != HIST:
            raise VMarketError(f"시즌 {sid} {name}: 사전 봉은 {HIST}개여야 해요")
        cs = s.get("candles")
        if not isinstance(cs, list) or len(cs) != HIST + SEASON:
            raise VMarketError(f"시즌 {sid} {name}: 봉 {HIST + SEASON}개가 필요해요")
        rows = []
        for i, c in enumerate(cs):
            try:
                o, h, lo, cl, v = (c["o"], c["h"], c["l"], c["c"], c["v"]) if isinstance(c, dict) else c
                vals = [int(o), int(h), int(lo), int(cl), int(v)]
            except (KeyError, TypeError, ValueError):
                raise VMarketError(f"시즌 {sid} {name} {i + 1}번째 봉: 형식 오류") from None
            if [o, h, lo, cl, v] != vals:
                raise VMarketError(f"시즌 {sid} {name} {i + 1}번째 봉: 가격·거래량은 정수여야 해요")
            o, h, lo, cl, v = vals
            if not (0 < lo <= min(o, cl) and max(o, cl) <= h < 2_000_000_000 and 0 <= v < 10 ** 13):
                raise VMarketError(f"시즌 {sid} {name} {i + 1}번째 봉: 시·고·저·종 범위가 맞지 않아요")
            rows.append(vals)
        out.append({"id": str(s.get("id") or f"S{sid:02d}-{k + 1}")[:20], "name": name,
                    "sector": str(s.get("sector") or "")[:40], "size": str(s.get("size") or "")[:20],
                    "scenario": s["scenario"], "warmup": HIST, "candles": rows})
    return {"season": sid, "stocks": out}


def parse_file(path: Path) -> dict:
    """season_XX.json 또는 팀 화면용 season_XX.js(window.SEASON_DATA[n] = {...};) — 코드 실행 없이 JSON 부분만 읽는다."""
    body = Path(path).read_text(encoding="utf-8-sig")
    if not body.lstrip().startswith("{"):
        m = re.search(r"SEASON_DATA\[\s*\d+\s*\]\s*=\s*(\{.*\})\s*;?\s*$", body, re.S)
        if not m:
            raise VMarketError(f"{Path(path).name}: JSON 이나 팀 형식(window.SEASON_DATA[n] = {{…}};)이 아니에요")
        body = m.group(1)
    try:
        return _canonical(json.loads(body))
    except json.JSONDecodeError as e:
        raise VMarketError(f"{Path(path).name}: JSON 오류 {e}") from None


def import_season(data: dict, source: str, now: datetime | None = None) -> str:
    """검사한 시즌 하나를 넣는다. 반환: 'new' | 'same' | 'replaced'. 시작한 시즌(또는 참가자가 있는 시즌)의 내용 변경은 거부."""
    data = _canonical(data)
    sid = data["season"]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    today = (now or clock.now()).date()
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"vmarket:season:{sid}"})
        cur = conn.execute(text("SELECT sha256 FROM vmarket_season WHERE season_id = :s"), {"s": sid}).scalar()
        if cur == sha:
            return "same"
        if cur is not None:
            from app.security import rls
            with rls.elevated("vmarket.import — 참가자 수 확인(시즌 데이터 교체 전)"):
                joined = conn.execute(text("SELECT count(*) FROM vmarket_entry WHERE season_id = :s"), {"s": sid}).scalar()
            if week_start(sid) <= today or joined:
                raise VMarketError(f"시즌 {sid} 은(는) 이미 시작했거나 참가자가 있어 내용을 바꿀 수 없어요")
        conn.execute(text(
            "INSERT INTO vmarket_season(season_id, week_start, payload, sha256, source, imported_at) "
            "VALUES(:s, :w, :p, :h, :src, now()) ON CONFLICT(season_id) DO UPDATE SET payload = EXCLUDED.payload, "
            "sha256 = EXCLUDED.sha256, source = EXCLUDED.source, imported_at = EXCLUDED.imported_at"),
            {"s": sid, "w": week_start(sid), "p": payload, "h": sha, "src": str(source)[:60]})
    with _lock:
        _payloads.pop(sid, None)
    return "new" if cur is None else "replaced"


def seed() -> dict:
    """app/data/vmarket/season_*.json 을 모두 넣는다(같으면 통과 — 멱등). 문제가 있는 파일은 건너뛰고 기록."""
    out = {"new": 0, "same": 0, "replaced": 0, "errors": []}
    for p in sorted(DATA_DIR.glob("season_*.json")):
        try:
            out[import_season(parse_file(p), p.name)] += 1
        except VMarketError as e:
            out["errors"].append(f"{p.name}: {e}")
            log.warning("모의투자 시즌 파일 건너뜀 — %s: %s", p.name, e)
    return out


def payload(season_id: int) -> dict | None:
    """시즌 데이터(서버 안에서만). 없으면 None."""
    rows = db.query("SELECT sha256 FROM vmarket_season WHERE season_id = :s", {"s": season_id})
    if not rows:
        return None
    sha = rows[0]["sha256"]
    with _lock:
        hit = _payloads.get(season_id)
        if hit and hit[0] == sha:
            return hit[1]
    row = db.query("SELECT payload, sha256 FROM vmarket_season WHERE season_id = :s", {"s": season_id})[0]
    data = json.loads(row["payload"])
    if hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:      # 저장 뒤 바뀜(변조·손상) — 쓰지 않는다
        log.error("모의투자 시즌 %s 데이터 해시 불일치 — 사용 중지", season_id)
        return None
    with _lock:
        _payloads[season_id] = (row["sha256"], data)
    return data


def clear_cache() -> None:
    with _lock:
        _payloads.clear()


def _price(data: dict, k: int, revealed: int) -> int:
    return int(data["stocks"][k]["candles"][HIST + revealed - 1][3])


# ── 공개 조회 ──────────────────────────────────────────────
def season_view(season_id: int | None = None, now: datetime | None = None) -> dict:
    """시즌 차트 — 진행 중이면 지금까지 공개된 봉까지만, 끝난 시즌이면 전부 + 장세. 다음 시즌 이후는 404."""
    now = now or clock.now()
    cl = clock_of(now)
    sid = cl["id"] if season_id is None else int(season_id)
    if sid < 1:
        raise VMarketNotFound("첫 시즌은 2026년 11월 2일(월) 09:00에 시작해요")
    if sid > cl["id"]:
        raise VMarketNotFound("아직 공개되지 않은 시즌이에요")
    done = ended(sid, cl)
    revealed = SEASON if done else cl["revealed"]
    nxt = bool(db.query("SELECT 1 FROM vmarket_season WHERE season_id = :s", {"s": cl["id"] + 1}))
    base = {"season": sid, "week_start": week_start(sid).isoformat(), "ended": done, "revealed": revealed,
            "clock": {**cl, "next_change_at": next_change(now).isoformat(timespec="seconds")},
            "next_ready": nxt,                         # 다음 시즌 데이터가 준비됐는지(내용은 시작 전까지 비공개)
            "hist": HIST, "bars": SEASON, "per_day": PER_DAY}
    data = payload(sid)
    if not data:
        return {**base, "available": False, "stocks": []}
    stocks = []
    for k, s in enumerate(data["stocks"]):
        item = {"idx": k, "name": s["name"], "candles": s["candles"][:HIST + revealed]}
        if done:
            item.update(scenario=s["scenario"], sector=s["sector"], size=s["size"])
        stocks.append(item)
    return {**base, "available": True, "stocks": stocks}


# ── 내 시즌 ────────────────────────────────────────────────
def _state(conn, username: str, sid: int) -> dict | None:
    """화면의 시즌 기록과 같은 모양({id, entry, cash, realized, joinedAt, settled, final, pos[5], trades[]})."""
    e = conn.execute(text("SELECT * FROM vmarket_entry WHERE season_id = :s AND username = :u"),
                     {"s": sid, "u": username}).mappings().first()
    if not e:
        return None
    pos = [{"shares": 0, "avg": 0, "realized": 0} for _ in range(STOCKS)]
    for p in conn.execute(text("SELECT stock_idx, shares, avg_price, realized FROM vmarket_position "
                               "WHERE season_id = :s AND username = :u"), {"s": sid, "u": username}).mappings():
        pos[p["stock_idx"]] = {"shares": int(p["shares"]), "avg": float(p["avg_price"]), "realized": float(p["realized"])}
    trades = [{"type": t["side"], "qty": int(t["qty"]), "price": int(t["price"]), "i": HIST + t["bar"] - 1,
               "k": t["stock_idx"], "auto": bool(t["auto"])}
              for t in conn.execute(text("SELECT side, qty, price, bar, stock_idx, auto FROM vmarket_trade "
                                         "WHERE season_id = :s AND username = :u ORDER BY trade_id"),
                                    {"s": sid, "u": username}).mappings()]
    return {"id": sid, "entry": int(e["entry"]), "cash": int(e["cash"]), "realized": float(e["realized"]),
            "joinedAt": int(e["joined_bar"]), "settled": e["status"] == "settled",
            "final": None if e["final"] is None else int(e["final"]),
            "autoSold": int(e["auto_sold"] or 0), "autoStocks": int(e["auto_stocks"] or 0), "pos": pos, "trades": trades}


def me(username: str, now: datetime | None = None) -> dict:
    """내 모의투자 — 이번 시즌 기록·끝난 시즌 목록(정산 금액)·정산 대기·지갑 잔액. 읽기만 한다(정산은 POST)."""
    from app.services import wallet
    cl = clock_of(now)
    with db.transaction() as conn:
        rows = conn.execute(text("SELECT season_id, entry, final, status FROM vmarket_entry WHERE username = :u "
                                 "ORDER BY season_id DESC"), {"u": username}).mappings().all()
        cur = _state(conn, username, cl["id"])
    history = [{"id": r["season_id"], "entry": int(r["entry"]), "final": int(r["final"])}
               for r in rows if r["status"] == "settled" and r["final"] is not None]
    unsettled = [r["season_id"] for r in rows if r["status"] == "open" and ended(r["season_id"], cl)]
    return {"season": cl["id"], "current": cur, "history": history, "unsettled": unsettled,
            "balance": wallet.balance(username), "min_entry": settings.VMARKET_MIN_ENTRY}


def join(username: str, season_id: int, entry: int, now: datetime | None = None) -> dict:
    """이번 시즌 참가 — 참가 행 + 종목 5개 보유 행 + 지갑 출금을 한 트랜잭션에서."""
    from app.services import wallet
    cl = clock_of(now)
    if int(season_id) != cl["id"] or cl["phase"] == "weekend":
        raise VMarketConflict("지금은 참가할 수 없는 시즌이에요 — 화면을 새로 고쳐 주세요")
    if not payload(cl["id"]):
        raise VMarketError(f"시즌 {cl['id']} 데이터 준비 중이라 참가할 수 없어요")
    entry = int(entry)
    if entry < settings.VMARKET_MIN_ENTRY:
        raise VMarketError(f"참가금은 {settings.VMARKET_MIN_ENTRY:,}원 이상이어야 해요")
    if entry > settings.VMARKET_MAX_ENTRY:
        raise VMarketError("참가금이 너무 커요")
    sid = cl["id"]
    with db.transaction() as conn:
        wallet.hold(conn, username)                                     # 계정 → 참가 행 → 지갑 순서로 잠근다
        made = conn.execute(text(
            "INSERT INTO vmarket_entry(season_id, username, entry, cash, realized, joined_bar, status, joined_at) "
            "VALUES(:s, :u, :e, :e, 0, :b, 'open', now()) ON CONFLICT DO NOTHING RETURNING season_id"),
            {"s": sid, "u": username, "e": entry, "b": cl["revealed"]}).first()
        if made is None:
            raise VMarketError(f"시즌 {sid}에는 이미 참가했어요")
        conn.execute(text("INSERT INTO vmarket_position(season_id, username, stock_idx, shares, avg_price, realized) "
                          "SELECT :s, :u, k, 0, 0, 0 FROM generate_series(0, :n - 1) AS k"),
                     {"s": sid, "u": username, "n": STOCKS})
        try:
            paid = wallet.debit(conn, username, entry, "vmarket_entry", ref=f"S{sid}",
                                memo=f"모의투자 시즌 {sid} 참가금", idem_key=f"vmarket_entry:{sid}:{username}")
        except wallet.WalletError as e:
            raise VMarketError("가상머니가 부족해서 참가할 수 없어요" if "부족" in str(e) else str(e)) from None
        if not paid["applied"]:
            raise VMarketError("이미 처리된 참가예요 — 화면을 새로 고쳐 주세요")
        state = _state(conn, username, sid)
    return {"season": sid, "current": state, "balance": paid["balance"]}


def order(username: str, season_id: int, stock: int, side: str, qty: int, bar: int,
          idem_key: str | None = None, now: datetime | None = None) -> dict:
    """현재가(지금 공개된 마지막 봉의 종가) 즉시 체결. bar = 화면이 본 공개 봉 수 — 서버와 다르면 409."""
    cl = clock_of(now)
    sid = int(season_id)
    if sid != cl["id"]:
        raise VMarketConflict("시즌이 바뀌었어요 — 화면을 새로 고쳐 주세요")
    if cl["phase"] != "open" or cl["revealed"] < 1:
        raise VMarketError("장 시간(평일 09:00–15:30)에만 거래할 수 있어요")
    if int(bar) != cl["revealed"]:
        raise VMarketConflict("새 봉이 나와 현재가가 바뀌었어요. 가격을 확인하고 다시 주문해 주세요.")
    k, qty = int(stock), int(qty)
    if not 0 <= k < STOCKS:
        raise VMarketError("없는 종목이에요")
    if side not in ("buy", "sell"):
        raise VMarketError("매수 또는 매도만 할 수 있어요")
    if not 1 <= qty <= MAX_QTY:
        raise VMarketError("1주 이상 입력해 주세요")
    data = payload(sid)
    if not data:
        raise VMarketError("시즌 데이터 준비 중이에요")
    price = _price(data, k, cl["revealed"])
    idem = db.idem_key("vm", idem_key) if idem_key else None
    with db.transaction() as conn:
        e = conn.execute(text("SELECT cash, realized, status FROM vmarket_entry WHERE season_id = :s AND username = :u "
                              "FOR UPDATE"), {"s": sid, "u": username}).mappings().first()
        if not e:
            raise VMarketError("먼저 이번 시즌에 참가해 주세요")
        if idem and conn.execute(text("SELECT 1 FROM vmarket_trade WHERE username = :u AND idem_key = :k"),
                                 {"u": username, "k": idem}).first():
            return {"season": sid, "current": _state(conn, username, sid), "duplicate": True}
        if e["status"] != "open":
            raise VMarketError("이미 정산된 시즌이에요")
        p = conn.execute(text("SELECT shares, avg_price, realized FROM vmarket_position "
                              "WHERE season_id = :s AND username = :u AND stock_idx = :k FOR UPDATE"),
                         {"s": sid, "u": username, "k": k}).mappings().first()
        shares, avg, p_real = int(p["shares"]), float(p["avg_price"]), float(p["realized"])
        cash, real = int(e["cash"]), float(e["realized"])
        if side == "buy":
            if qty > cash // price:
                raise VMarketError("보유 현금으로 살 수 있는 수량을 넘었어요" if cash >= price else "현금이 부족해 살 수 없어요")
            avg = (avg * shares + price * qty) / (shares + qty)
            shares += qty
            cash -= price * qty
        else:
            if qty > shares:
                raise VMarketError("보유 수량보다 많이 팔 수 없어요" if shares else "팔 수 있는 주식이 없어요")
            gain = (price - avg) * qty
            p_real += gain
            real += gain
            shares -= qty
            cash += price * qty
            if shares == 0:
                avg = 0.0
        conn.execute(text("UPDATE vmarket_position SET shares = :sh, avg_price = :a, realized = :r "
                          "WHERE season_id = :s AND username = :u AND stock_idx = :k"),
                     {"sh": shares, "a": avg, "r": p_real, "s": sid, "u": username, "k": k})
        conn.execute(text("UPDATE vmarket_entry SET cash = :c, realized = :r WHERE season_id = :s AND username = :u"),
                     {"c": cash, "r": real, "s": sid, "u": username})
        conn.execute(text("INSERT INTO vmarket_trade(season_id, username, stock_idx, side, qty, price, bar, auto, idem_key, "
                          "created_at) VALUES(:s, :u, :k, :d, :q, :p, :b, false, :i, now())"),
                     {"s": sid, "u": username, "k": k, "d": side, "q": qty, "p": price, "b": cl["revealed"], "i": idem})
        state = _state(conn, username, sid)
    return {"season": sid, "current": state, "filled": {"stock": k, "side": side, "qty": qty, "price": price,
                                                         "name": data["stocks"][k]["name"]}}


# ── 정산 ───────────────────────────────────────────────────
def _settle_one(username: str, sid: int) -> dict | None:
    """끝난 시즌 하나 — 남은 주식을 종목마다 마지막 봉 종가로 팔고 최종 평가금액을 지갑으로. 이미 정산했으면 None."""
    from app.services import wallet
    data = payload(sid)
    with db.transaction() as conn:
        try:
            wallet.hold(conn, username)
        except wallet.WalletError:                        # 탈퇴 중·탈퇴함 — 탈퇴가 참가 기록을 함께 지운다
            return None
        e = conn.execute(text("SELECT cash, realized, status FROM vmarket_entry WHERE season_id = :s AND username = :u "
                              "FOR UPDATE"), {"s": sid, "u": username}).mappings().first()
        if not e or e["status"] != "open":
            return None
        cash, real, sold, stocks = int(e["cash"]), float(e["realized"]), 0, 0
        for p in conn.execute(text("SELECT stock_idx, shares, avg_price, realized FROM vmarket_position "
                                   "WHERE season_id = :s AND username = :u AND shares > 0 ORDER BY stock_idx FOR UPDATE"),
                              {"s": sid, "u": username}).mappings().all():
            k, sh, avg = p["stock_idx"], int(p["shares"]), float(p["avg_price"])
            last = _price(data, k, SEASON) if data else int(round(avg))     # 데이터가 없으면(손상) 원가로 — 손익 0
            gain = (last - avg) * sh
            real += gain
            cash += last * sh
            sold += sh
            stocks += 1
            conn.execute(text("UPDATE vmarket_position SET shares = 0, avg_price = 0, realized = realized + :g "
                              "WHERE season_id = :s AND username = :u AND stock_idx = :k"),
                         {"g": gain, "s": sid, "u": username, "k": k})
            conn.execute(text("INSERT INTO vmarket_trade(season_id, username, stock_idx, side, qty, price, bar, auto, "
                              "idem_key, created_at) VALUES(:s, :u, :k, 'sell', :q, :p, :b, true, NULL, now())"),
                         {"s": sid, "u": username, "k": k, "q": sh, "p": max(1, last), "b": SEASON})
        conn.execute(text("UPDATE vmarket_entry SET cash = :c, realized = :r, status = 'settled', final = :c, "
                          "auto_sold = :a, auto_stocks = :n, settled_at = now() WHERE season_id = :s AND username = :u"),
                     {"c": cash, "r": real, "a": sold, "n": stocks, "s": sid, "u": username})
        bal = None
        if cash > 0:
            bal = wallet.credit(conn, username, cash, "vmarket_settle", ref=f"S{sid}", memo=f"모의투자 시즌 {sid} 정산",
                                idem_key=f"vmarket_settle:{sid}:{username}")["balance"]
    return {"season": sid, "final": cash, "auto_sold": sold, "auto_stocks": stocks, "balance": bal}


def settle_user(username: str, now: datetime | None = None) -> dict:
    """내 끝난 시즌을 모두 정산(화면 — 주말·다음 시즌 첫 접속). 반환: 정산한 시즌들 + 이번 시즌 상태."""
    cl = clock_of(now)
    ids = [r["season_id"] for r in db.query(
        "SELECT season_id FROM vmarket_entry WHERE username = :u AND status = 'open' ORDER BY season_id", {"u": username})
        if ended(r["season_id"], cl)]
    done = [r for r in (_settle_one(username, s) for s in ids) if r]
    from app.services import wallet
    return {"settled": done, "balance": wallet.balance(username)}


def settle_all(now: datetime | None = None) -> dict:
    """끝난 시즌의 열린 참가를 모두 정산(스케줄러 — 매일 00:05, 시스템 문맥). 하나가 실패해도 나머지는 계속."""
    cl = clock_of(now)
    rows = db.query("SELECT season_id, username FROM vmarket_entry WHERE status = 'open' ORDER BY season_id, username")
    todo = [(r["username"], r["season_id"]) for r in rows if ended(r["season_id"], cl)]
    ok, failed = 0, []
    for u, s in todo:
        try:
            if _settle_one(u, s):
                ok += 1
        except Exception as e:                             # noqa: BLE001 — 한 사람의 문제로 다른 사람 정산이 멈추지 않게
            log.exception("모의투자 정산 실패 season=%s", s)
            failed.append(f"S{s}:{type(e).__name__}")
    if failed:
        from app.services import alerts
        alerts.notify("vmarket:settle", "모의투자 정산 실패", f"{len(failed)}건 — {', '.join(failed[:5])}", level="error")
    return {"settled": ok, "failed": len(failed)}


def delete_user(conn, username: str) -> None:
    """탈퇴 — 참가·보유·체결 기록을 지운다(시즌 현금은 이미 지갑 밖 — 남은 참가금은 sys:vmarket_custody 에 남는다)."""
    for t in ("vmarket_trade", "vmarket_position", "vmarket_entry"):
        conn.execute(text(f"DELETE FROM {t} WHERE username = :u"), {"u": username})


# ── 운영 ───────────────────────────────────────────────────
def status(now: datetime | None = None) -> dict:
    """운영 확인 — 이번·다음 시즌 데이터 준비, 마지막 준비된 시즌, 열린 참가, 정산 대기(끝났는데 열린 참가)."""
    cl = clock_of(now)
    have = [r["season_id"] for r in db.query("SELECT season_id FROM vmarket_season ORDER BY season_id")]
    rows = db.query("SELECT season_id, status, count(*) AS n FROM vmarket_entry GROUP BY season_id, status")
    open_now = sum(r["n"] for r in rows if r["status"] == "open" and not ended(r["season_id"], cl))
    pending = sum(r["n"] for r in rows if r["status"] == "open" and ended(r["season_id"], cl))
    nxt = cl["id"] + 1
    return {"clock": cl, "season": cl["id"], "current_ready": cl["id"] in have, "next_ready": nxt in have,
            "last_ready": max(have) if have else None, "seasons": len(have),
            "weeks_left": max(0, (max(have) if have else 0) - cl["id"]),
            "open_entries": open_now, "unsettled": pending}


def check_upcoming(now: datetime | None = None) -> dict:
    """다음 시즌 데이터가 없으면 경보(금요일 18:00) — 월요일 09:00 에 '데이터 준비 중'으로 멈추지 않게 미리 알린다."""
    st = status(now)
    if not st["next_ready"]:
        from app.services import alerts
        alerts.notify("vmarket:data", "모의투자 다음 시즌 데이터 없음",
                      f"시즌 {st['season'] + 1}({week_start(st['season'] + 1)} 주) 파일을 run.py vmarket-import 로 넣어 주세요",
                      level="warn")
    return st
