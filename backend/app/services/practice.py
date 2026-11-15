"""실습(과거 사례) 엔진 — 과거 특정 구간을 하루씩(1일/5일) 넘기며, 미래를 가린 채
매수·매도를 연습한다. backtest 개념 + game 의 안전장치(원자성·멱등성·OCC)를 결합.

  · 데이터  — ohlc 테이블(실데이터 또는 생성 데이터). 시나리오 = 종목 + 기간 + 학습목표.
  · 미래 은닉 — series 는 커서(현재 시점)까지만. '현재 시점 이후 데이터는 제공하지 않아요'.
  · 계좌    — 천만원 시작. 현금/보유/평균매입가/평가손익/총자산.
  · 진행 규칙 — 한 번에 한 시나리오만 active(진행 중). 끝내야 새 실습 시작.
"""
import secrets
import logging
from datetime import date, timedelta

from sqlalchemy import text

from app import clock
from app import db
from app.services import indicators as ind

log = logging.getLogger(__name__)

START_CASH = 10_000_000       # 천만원
START_OFFSET = 20             # 시작 시 공개해 둘 초기 캔들 수(빈 차트 방지)
WARMUP_BARS = 60              # 시나리오 시작 전 봉 — SMA60 등을 첫 화면부터 그리기 위한 계산용(화면엔 안 보임)
WARMUP_DAYS = 120             # 워밍업 봉을 찾는 범위(시작일 이전 달력일)

# 6개 시나리오(디자인 '과거 사례 실습' 그대로). pattern 은 생성기 학습패턴 키.
SCENARIOS = [
    {"scenario_id": "samsung-golden", "ticker": "005930", "stock": "삼성전자",
     "name": "삼성전자 골든크로스 구간", "difficulty": "초급",
     "start_date": "2020-04-01", "end_date": "2020-08-04", "pattern": "golden_cross",
     "indicators": ["SMA5", "SMA20"],
     "objective": "SMA 5가 SMA 20을 위로 통과하는 시점을 직접 찾아 매수·매도 판단을 연습해요."},
    {"scenario_id": "naver-rsi", "ticker": "035420", "stock": "NAVER",
     "name": "NAVER RSI 과매도 반등 구간", "difficulty": "중급",
     "start_date": "2021-08-02", "end_date": "2021-12-17", "pattern": "rsi_oversold",
     "indicators": ["RSI14", "SMA20"],
     "objective": "RSI 14가 30 아래로 내려간 뒤 다시 올라오는 시점을 찾아 반등 매매를 연습해요."},
    {"scenario_id": "kakao-ma-fail", "ticker": "035720", "stock": "카카오",
     "name": "카카오 이동평균 지지 실패 구간", "difficulty": "고급",
     "start_date": "2022-03-02", "end_date": "2022-07-19", "pattern": "ma_support_fail",
     "indicators": ["SMA20", "SMA60", "VOL"],
     "objective": "이동평균선이 지지선 역할을 하지 못하는 구간에서 손실을 줄이는 판단을 연습해요."},
    {"scenario_id": "lgchem-sideways", "ticker": "051910", "stock": "LG화학",
     "name": "LG화학 이동평균 횡보 구간", "difficulty": "중급",
     "start_date": "2021-01-04", "end_date": "2021-05-20", "pattern": "sideways_cross",
     "indicators": ["SMA5", "SMA20", "SMA60"],
     "objective": "횡보 구간에서 교차 신호가 반복될 때 어떻게 걸러낼지 연습해요."},
    {"scenario_id": "skhynix-divergence", "ticker": "000660", "stock": "SK하이닉스",
     "name": "SK하이닉스 RSI 다이버전스 구간", "difficulty": "고급",
     "start_date": "2022-06-01", "end_date": "2022-10-25", "pattern": "rsi_divergence",
     "indicators": ["RSI14", "SMA20", "VOL"],
     "objective": "가격과 RSI가 다른 방향으로 움직이는 장면을 찾아 반등 시점을 연습해요."},
    {"scenario_id": "hyundai-volume", "ticker": "005380", "stock": "현대차",
     "name": "현대차 거래량 돌파 구간", "difficulty": "초급",
     "start_date": "2023-05-02", "end_date": "2023-09-04", "pattern": "volume_breakout",
     "indicators": ["SMA20", "VOL"],
     "objective": "거래량이 급증하며 박스권 상단을 넘어서는 장면에서 진입 시점을 연습해요."},
]
_BY_ID = {s["scenario_id"]: s for s in SCENARIOS}


class PracticeError(Exception):
    """실습 규칙 위반(데이터 부족·현금 부족·진행 중 실습 존재 등)."""


def _now():
    return clock.iso_now()


def warmup_start(scenario):
    """워밍업 봉 수집 시작일(시나리오 시작 WARMUP_DAYS 일 전) — SMA60 등을 첫 화면부터 그리기 위한 계산용 봉."""
    return (date.fromisoformat(scenario["start_date"]) - timedelta(days=WARMUP_DAYS)).isoformat()


def scenario_meta(scenario_id):
    return _BY_ID.get(scenario_id)


def _dates(ticker, start, end):
    return [r["trade_date"] for r in db.query(
        "SELECT trade_date FROM ohlc WHERE ticker=:t AND trade_date>=:s AND trade_date<=:e "
        "ORDER BY trade_date", {"t": ticker, "s": start, "e": end})]


def _latest_session(user_id, scenario_id):
    rows = db.query("SELECT * FROM practice_session WHERE user_id=:u AND scenario_id=:s "
                    "ORDER BY session_id DESC LIMIT 1", {"u": user_id, "s": scenario_id})
    return rows[0] if rows else None


def _active_session(user_id):
    rows = db.query("SELECT * FROM practice_session WHERE user_id=:u AND status='active' "
                    "ORDER BY session_id DESC LIMIT 1", {"u": user_id})
    return rows[0] if rows else None


def list_scenarios(user_id="guest"):
    """시나리오 목록 + 유저 상태(미시작/진행중/완료) + 요약 통계."""
    active = _active_session(user_id)
    items, done_cnt, done_by_diff = [], 0, {"초급": 0, "중급": 0, "고급": 0}
    for sc in SCENARIOS:
        sess = _latest_session(user_id, sc["scenario_id"])
        status = "none"
        progress = None
        best = None
        attempts = 0
        if sess:
            attempts = sess["attempts"] or 0
            best = sess["best_return"]
            if sess["status"] == "active":
                status = "active"
                dates = _dates(sc["ticker"], sc["start_date"], sc["end_date"])
                total = len(dates)
                # 커서까지 공개된 봉 수(get_state 와 같은 기준) — 예전엔 커서 날짜 봉이 지워지면 목록만 0%로 보였다
                cur = sum(1 for d in dates if d <= sess["cursor_date"])
                progress = {"current": cur, "total": total,
                            "pct": round(cur / total * 100) if total else 0}
            elif sess["status"] == "done":
                status = "done"
                done_cnt += 1
                done_by_diff[sc["difficulty"]] = done_by_diff.get(sc["difficulty"], 0) + 1
        items.append({
            "scenario_id": sc["scenario_id"], "name": sc["name"], "stock": sc["stock"],
            "ticker": sc["ticker"], "difficulty": sc["difficulty"],
            "start_date": sc["start_date"], "end_date": sc["end_date"],
            "objective": sc["objective"], "indicators": sc["indicators"], "pattern": sc["pattern"],
            "status": status, "progress": progress, "best_return": best, "attempts": attempts,
            "session_id": sess["session_id"] if sess else None,
            # 진행 중 실습이 있으면 다른 미시작 실습은 잠금(디자인 규칙)
            "locked": bool(active and status == "none"),
        })
    return {
        "stats": {"done": done_cnt, "total": len(SCENARIOS),
                  "by_difficulty": done_by_diff},
        "active_scenario": active["scenario_id"] if active else None,
        "items": items,
    }


def start(user_id, scenario_id):
    sc = _BY_ID.get(scenario_id)
    if not sc:
        raise PracticeError("존재하지 않는 실습입니다")
    with db.transaction() as conn:          # 같은 사용자의 '시작'을 줄 세운다 — 예전엔 연타 2번에 진행 중 실습이 2개 생겼다
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "practice:" + str(user_id)})
        sid = _start_locked(conn, user_id, scenario_id, sc)
    return get_state(sid)


def _start_locked(conn, user_id, scenario_id, sc):
    """진행할 세션 id — 이어하기면 기존 세션, 아니면 새로 만든다(호출한 쪽 트랜잭션·잠금 안에서)."""
    active = _active_session(user_id)
    if active and active["scenario_id"] != scenario_id:
        raise PracticeError("진행 중인 실습을 먼저 끝내야 새 실습을 시작할 수 있어요")
    existing = _latest_session(user_id, scenario_id)
    if existing and existing["status"] == "active":
        return existing["session_id"]                      # 이어하기
    dates = _dates(sc["ticker"], sc["start_date"], sc["end_date"])
    if len(dates) < START_OFFSET + 2:
        raise PracticeError("해당 구간의 시세 데이터가 부족합니다 (fetch-practice 필요)")
    cursor = dates[min(START_OFFSET, len(dates) - 1)]
    attempts = (existing["attempts"] or 0) + 1 if existing else 1
    best = existing["best_return"] if existing else None
    sid = conn.execute(db.practice_session.insert().values(
        user_id=user_id, scenario_id=scenario_id, cursor_date=cursor,
        cash=START_CASH, holding_qty=0, holding_avg=0.0, start_cash=START_CASH,
        version=0, status="active", started_at=_now(),
        best_return=best, attempts=attempts)).inserted_primary_key[0]
    return sid


def _price_at(conn, sc, cursor_date):
    """커서 시점 가격 = 시나리오 구간에서 커서 이하 가장 최근 종가(화면 get_state 와 같은 기준).
    예전엔 커서 날짜의 봉만 찾아, 실데이터로 다시 받으며 생성 데이터의 휴장일 봉이 지워진 세션은
    주문이 '현재 시점 가격이 없습니다'로 막히고 종료 정산은 보유 주식을 0원으로 평가했다."""
    row = conn.execute(text("SELECT close FROM ohlc WHERE ticker=:t AND trade_date>=:s AND trade_date<=:d "
                            "AND close > 0 ORDER BY trade_date DESC LIMIT 1"),
                       {"t": sc["ticker"], "s": sc["start_date"], "d": cursor_date}).mappings().first()
    return row["close"] if row else None


def _get(sid):
    rows = db.query("SELECT * FROM practice_session WHERE session_id=:s", {"s": sid})
    if not rows:
        raise PracticeError("실습 세션 없음")
    return rows[0]


def owner_of(sid):
    rows = db.query("SELECT user_id FROM practice_session WHERE session_id=:s", {"s": sid})
    return rows[0]["user_id"] if rows else None


def get_state(sid):
    s = _get(sid)
    sc = _BY_ID[s["scenario_id"]]
    rows = db.query("SELECT trade_date,open,high,low,close,volume FROM ohlc WHERE ticker=:t "
                    "AND trade_date>=:st AND trade_date<=:c ORDER BY trade_date",
                    {"t": sc["ticker"], "st": sc["start_date"], "c": s["cursor_date"]})
    closes = [r["close"] for r in rows]
    warm = [r["close"] for r in db.query(
        "SELECT close FROM (SELECT trade_date, close FROM ohlc WHERE ticker=:t AND trade_date<:st "
        "AND trade_date>=:ws ORDER BY trade_date DESC LIMIT :n) w ORDER BY trade_date",
        {"t": sc["ticker"], "st": sc["start_date"], "ws": warmup_start(sc), "n": WARMUP_BARS})]
    calc = warm + closes                    # 지표는 워밍업 봉까지 넣어 계산하고 화면 구간만 잘라 보낸다
    all_dates = _dates(sc["ticker"], sc["start_date"], sc["end_date"])
    total = len(all_dates)
    cur_idx = all_dates.index(s["cursor_date"]) + 1 if s["cursor_date"] in all_dates else len(rows)
    price = closes[-1] if closes else 0
    prev = closes[-2] if len(closes) >= 2 else price
    day_chg = round((price / prev - 1) * 100, 2) if prev else 0.0
    holding_val = int(price * s["holding_qty"])
    total_asset = s["cash"] + holding_val
    pnl = holding_val - int(s["holding_avg"] * s["holding_qty"])
    pnl_pct = round((price / s["holding_avg"] - 1) * 100, 2) if s["holding_avg"] else 0.0
    ret = round((total_asset / s["start_cash"] - 1) * 100, 2) if s["start_cash"] else 0.0
    overlays = {}
    if "SMA5" in sc["indicators"]:
        overlays["SMA5"] = ind.sma(calc, 5)
    if "SMA20" in sc["indicators"]:
        overlays["SMA20"] = ind.sma(calc, 20)
    if "SMA60" in sc["indicators"]:
        overlays["SMA60"] = ind.sma(calc, 60)
    if "RSI14" in sc["indicators"]:
        overlays["RSI14"] = ind.rsi(calc, 14)
    overlays = {k: v[len(warm):] for k, v in overlays.items()}
    return {
        "session_id": sid, "scenario_id": sc["scenario_id"], "name": sc["name"],
        "stock": sc["stock"], "ticker": sc["ticker"], "difficulty": sc["difficulty"],
        "objective": sc["objective"], "indicators": sc["indicators"],
        "status": s["status"], "cursor_date": s["cursor_date"],
        "start_date": sc["start_date"], "end_date": sc["end_date"],
        "progress": {"current": cur_idx, "total": total,
                     "pct": round(cur_idx / total * 100) if total else 0},
        "has_next": cur_idx < total,
        "price": price, "day_change": day_chg,
        "series": rows, "overlays": overlays,
        "account": {
            "cash": s["cash"], "holding_qty": s["holding_qty"],
            "holding_avg": round(s["holding_avg"], 1), "holding_value": holding_val,
            "pnl": pnl, "pnl_pct": pnl_pct, "total_asset": total_asset,
            "return_rate": ret, "start_cash": s["start_cash"],
            "buyable": int(s["cash"] // price) if price else 0,
        },
        "final_return": s["final_return"], "best_return": s["best_return"],
    }


def _lock(conn, sid):
    """세션 행 잠금(FOR UPDATE) — 같은 세션의 진행·매매·종료 요청을 한 줄로 세운다."""
    s = conn.execute(text("SELECT * FROM practice_session WHERE session_id=:s FOR UPDATE"),
                     {"s": sid}).mappings().first()
    if not s:
        raise PracticeError("실습 세션 없음")
    return s


def step(sid, days=1):
    """커서를 거래일 기준 days 만큼 전진(1일/5일). 끝에 닿으면 자동 종료."""
    days = 5 if int(days) >= 5 else 1
    with db.transaction() as conn:
        s = _lock(conn, sid)
        if s["status"] != "active":
            raise PracticeError("이미 종료된 실습입니다")
        sc = _BY_ID[s["scenario_id"]]
        dates = _dates(sc["ticker"], sc["start_date"], sc["end_date"])
        if not dates:
            raise PracticeError("해당 구간의 시세 데이터가 없습니다")
        if s["cursor_date"] in dates:
            i = dates.index(s["cursor_date"])
        else:                                   # 데이터 소스 교체(gen→실데이터) 시 커서 스냅
            i = max(0, sum(1 for x in dates if x <= s["cursor_date"]) - 1)
        new_i = min(i + days, len(dates) - 1)
        r = conn.execute(text("UPDATE practice_session SET cursor_date=:d, version=version+1 "
                              "WHERE session_id=:s AND version=:v AND status='active'"),
                         {"d": dates[new_i], "s": sid, "v": s["version"]})
        if r.rowcount == 0:
            raise PracticeError("다른 요청과 겹쳤어요 — 잠시 후 다시 시도해 주세요")
        if new_i >= len(dates) - 1:
            _finalize(conn, sid, sc)
    return get_state(sid)


def _finalize(conn, sid, sc):
    """현재 커서 시점 자산으로 정산. 행을 잠그고(FOR UPDATE) 다시 읽는다 — 예전엔 잠그지 않아, 진행 중이던 주문이
    커밋되기 전 잔고로 수익률을 확정한 뒤 그 주문이 '종료된 실습'에 반영될 수 있었다(기록과 잔고 불일치).
    이미 끝난 세션(동시에 온 '종료'·마지막 '다음')은 다시 정산하지 않는다."""
    s = conn.execute(text("SELECT * FROM practice_session WHERE session_id=:s FOR UPDATE"),
                     {"s": sid}).mappings().first()
    if not s or s["status"] != "active":
        return
    price = _price_at(conn, sc, s["cursor_date"]) or s["holding_avg"] or 0   # 가격이 전혀 없으면 매입가로(0원 평가 방지)
    total = s["cash"] + int(price * s["holding_qty"])
    ret = round((total / s["start_cash"] - 1) * 100, 2) if s["start_cash"] else 0.0
    best = max(ret, s["best_return"]) if s["best_return"] is not None else ret
    conn.execute(text("UPDATE practice_session SET status='done', ended_at=:e, version=version+1, "
                      "final_return=:r, best_return=:b WHERE session_id=:s"),
                 {"e": _now(), "r": ret, "b": best, "s": sid})


def end(sid):
    """지금 종료 — 현재 시점 자산으로 정산(이미 끝났으면 그대로)."""
    s = _get(sid)
    if s["status"] != "active":
        return get_state(sid)
    sc = _BY_ID[s["scenario_id"]]
    with db.transaction() as conn:
        _finalize(conn, sid, sc)
    return get_state(sid)


def trade(sid, side, qty, idem_key=None):
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        raise PracticeError("side 는 BUY 또는 SELL 이어야 합니다")
    key = db.idem_key("k", idem_key) if idem_key else secrets.token_hex(8)
    with db.transaction() as conn:
        s = conn.execute(text("SELECT * FROM practice_session WHERE session_id=:s FOR UPDATE"),
                         {"s": sid}).mappings().first()     # 먼저 잠그고 → 멱등키 확인(동시 중복 주문 1회만)
        seen = conn.execute(text("SELECT 1 FROM practice_trade WHERE session_id=:s AND idem_key=:k"),
                            {"s": sid, "k": key}).first()
        if not s:
            raise PracticeError("실습 세션 없음")
        if not seen:
            if s["status"] != "active":
                raise PracticeError("종료된 실습에는 주문할 수 없습니다")
            sc = _BY_ID[s["scenario_id"]]
            price = _price_at(conn, sc, s["cursor_date"])
            if not price:
                raise PracticeError("현재 시점 가격이 없습니다")
            qty = int(qty)
            if qty <= 0:
                raise PracticeError("수량은 1 이상이어야 합니다")
            if side == "BUY":
                cost = int(qty * price)
                if cost > s["cash"]:
                    raise PracticeError("현금이 부족합니다")
                new_hold = s["holding_qty"] + qty
                new_avg = (s["holding_avg"] * s["holding_qty"] + cost) / new_hold
                new_cash = s["cash"] - cost
            else:
                if qty > s["holding_qty"]:
                    raise PracticeError("보유 수량이 부족합니다")
                new_hold = s["holding_qty"] - qty
                new_cash = s["cash"] + int(qty * price)
                new_avg = 0.0 if new_hold == 0 else s["holding_avg"]
            r = conn.execute(text("UPDATE practice_session SET cash=:c, holding_qty=:h, holding_avg=:a, "
                                  "version=version+1 WHERE session_id=:s AND version=:v AND status='active'"),
                             {"c": new_cash, "h": new_hold, "a": new_avg, "s": sid, "v": s["version"]})
            if r.rowcount == 0:
                raise PracticeError("다른 요청과 겹쳤어요 — 잠시 후 다시 시도해 주세요")
            conn.execute(db.practice_trade.insert().values(
                session_id=sid, trade_date=s["cursor_date"], side=side, quantity=qty,
                price=price, idem_key=key, created_at=_now()))
    return get_state(sid)


def seed_demo_progress(user_id="guest"):
    """데모: 현대차 실습 1개 완료 + 삼성전자 실습 진행 중(홈 배너·통계용).
    디자인의 '완료한 실습 1/6개' 및 '진행 중인 실습' 상태를 재현한다.
    이미 진행 중인 실습이 있으면(= demo 를 두 번 실행) 그대로 두고 건너뛴다(멱등)."""
    if db.query("SELECT 1 FROM practice_session WHERE user_id=:u AND status='active' LIMIT 1",
                {"u": user_id}):
        return {"active": None, "skipped": True}
    # 1) 현대차(거래량 돌파) — 매수 후 끝까지 진행해 완료 처리
    st = start(user_id, "hyundai-volume")
    sid = st["session_id"]
    buyable = st["account"]["buyable"]
    if buyable > 0:
        trade(sid, "BUY", max(1, buyable // 3), idem_key="demo-hy-buy")
    for _ in range(60):
        st = step(sid, 5)
        if st["status"] == "done" or not st["has_next"]:
            break
    if st["status"] != "done":
        end(sid)
    # 2) 삼성전자(골든크로스) — 매수 후 절반쯤 진행(active, 홈 배너)
    st = start(user_id, "samsung-golden")
    sid = st["session_id"]
    buyable = st["account"]["buyable"]
    if buyable > 0:
        trade(sid, "BUY", max(1, buyable // 3), idem_key="demo-ss-buy")
    target = int(st["progress"]["total"] * 0.45)
    while st["has_next"] and st["progress"]["current"] < target:
        st = step(sid, 5)
    return {"active": st["scenario_id"]}


def home_active(user_id="guest"):
    """홈 상단 '진행 중인 실습' 배너용 요약(없으면 None)."""
    active = _active_session(user_id)
    if not active:
        return None
    st = get_state(active["session_id"])
    return {"session_id": st["session_id"], "scenario_id": st["scenario_id"],
            "name": st["name"], "cursor_date": st["cursor_date"],
            "progress": st["progress"], "total_asset": st["account"]["total_asset"],
            "return_rate": st["account"]["return_rate"]}
