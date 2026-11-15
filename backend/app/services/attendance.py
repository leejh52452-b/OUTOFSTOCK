"""출석 체크 — 매일 1회 출석하면 가상머니(기본 100만원)를 지갑에 지급.

  · 하루 1회 — (username, attend_date) UNIQUE + 지갑 멱등키로 이중 지급을 이중으로 차단.
  · 연속 출석(streak) — 어제도 출석했으면 +1, 아니면 1로 리셋.
  · 날짜 기준 — 한국(KST). 테스트는 today 인자로 특정 날짜를 주입해 연속 출석을 검증한다.
"""
from datetime import datetime, timedelta, timezone, date

from sqlalchemy import text

from app import clock
from app import db
from app.config import settings
from app.services import wallet

KST = timezone(timedelta(hours=9))


def _now():
    return clock.iso_now()


def _today():
    return datetime.now(KST).date().isoformat()


def _yesterday(day):
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def status(username, today=None):
    """출석 현황 — 오늘 출석 여부·연속 출석·총 출석일·최근 이력."""
    day = today or _today()
    rows = db.query("SELECT attend_date, reward, streak FROM attendance "
                    "WHERE username=:u ORDER BY attend_date DESC LIMIT 60", {"u": username})
    checked_today = any(r["attend_date"] == day for r in rows)
    cur_streak = 0
    if rows:
        top = rows[0]
        if top["attend_date"] in (day, _yesterday(day)):
            cur_streak = top["streak"]          # 오늘 출석했거나, 오늘은 아직이지만 어제까지 연속
    total = db.query("SELECT COUNT(*) AS c FROM attendance WHERE username=:u",
                     {"u": username})[0]["c"]
    return {"today": day, "checked_in_today": checked_today, "streak": cur_streak,
            "total_days": total, "daily_reward": settings.ATTENDANCE_DAILY_REWARD,
            "recent": rows[:14]}


def check_in(username, today=None):
    """오늘 출석 처리 — 최초면 보상 지급, 이미 했으면 재지급 없이 현황만(멱등)."""
    day = today or _today()
    yday = _yesterday(day)
    claimed = False
    reward = 0
    with db.transaction() as conn:
        wallet.ensure(conn, username)
        exists = conn.execute(text("SELECT 1 FROM attendance WHERE username=:u AND attend_date=:d"),
                              {"u": username, "d": day}).first()
        if not exists:
            last = conn.execute(text("SELECT attend_date, streak FROM attendance "
                                     "WHERE username=:u AND attend_date < :d ORDER BY attend_date DESC LIMIT 1"),
                                {"u": username, "d": day}).mappings().first()
            streak = (last["streak"] + 1) if (last and last["attend_date"] == yday) else 1
            reward = settings.ATTENDANCE_DAILY_REWARD
            # 같은 날 동시 출석(두 기기·두 탭) — 늦게 온 쪽은 '이미 출석'으로 답한다(예전: 제약 위반 409 알림창)
            ins = conn.execute(text("INSERT INTO attendance(username, attend_date, reward, streak, created_at) "
                                    "VALUES(:u, :d, :r, :s, :t) ON CONFLICT (username, attend_date) DO NOTHING"),
                               {"u": username, "d": day, "r": reward, "s": streak, "t": _now()})
            if ins.rowcount == 1:
                if reward > 0:                           # 보상을 0 으로 설정한 운영에서도 출석 기록은 남게
                    wallet.credit(conn, username, reward, "attendance", ref=day,
                                  memo=f"{day} 출석 (연속 {streak}일)", idem_key=f"attend:{username}:{day}")
                claimed = True
            else:
                reward = 0
    st = status(username, day)
    st["claimed"] = claimed
    st["reward_given"] = reward if claimed else 0
    st["balance"] = wallet.balance(username)
    return st
