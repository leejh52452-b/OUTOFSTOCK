"""웹 '학습 및 실습하기' 기록 — 매매는 화면(브라우저)이 차트를 그리며 계산하고, 서버는 한 판씩 받아 남긴다.
마이페이지 '실습 이력'·'실습 완료' 와 실습 목록의 '나의 실습 기록' 이 같은 기록을 본다.

신뢰 경계(KDB 관점: 클라이언트 값은 믿지 않는다 — 그러면 무엇을 지키나)
  · 수익률은 브라우저 계산값이라 서버가 다시 셀 수 없다(예시 차트·실제 차트(SOXL·국내 대형주) 모두 화면이 가진 데이터).
    그래서 '기록'으로만 쓰고 가상머니·보상과 잇지 않는다 — 값을 조작해도 얻는 것이 없게 설계.
  · 받는 값은 허용 목록·범위로만: 기법 4종 · 방식 2종 · 수익률 −100%~+1000% · 실전 사례 날짜 형식.
  · 계정당 1시간 PRACTICE_RECORD_MAX_PER_HOUR 건(api 의 시도 제한) — 기록 부풀리기·저장 공간 소모 방지.
  · 표는 행 수준 보안(주인 user_id) · 탈퇴 시 함께 삭제.
"""
from __future__ import annotations

import math
from datetime import date

from sqlalchemy import text

from app import clock, db

TECHS = ("golden", "rsi", "pullback", "support")
MODES = ("practice", "real")
RET_MIN, RET_MAX = -1.0, 10.0                    # 전액 매수·매도 — 원금 이상 잃을 수 없다(−100%), 위는 넉넉히 +1000%


class RecordError(ValueError):
    """형식·범위 오류 — 400."""


def _entry(v) -> str | None:
    if v in (None, ""):
        return None
    try:
        if not isinstance(v, str) or len(v) != 10:
            raise ValueError(v)
        d = date.fromisoformat(v)
    except ValueError:
        raise RecordError("실전 사례 날짜는 YYYY-MM-DD 형식이어야 해요") from None
    if not (date(2010, 1, 1) <= d <= clock.today()):
        raise RecordError("실전 사례 날짜가 범위를 벗어났어요")
    return d.isoformat()


def add(user_id: str, tech: str, mode: str, traded: bool, ret: float | None, entry_date: str | None = None) -> dict:
    if tech not in TECHS:
        raise RecordError("알 수 없는 실습이에요")
    if mode not in MODES:
        raise RecordError("알 수 없는 실습 방식이에요")
    if traded:
        if ret is None or not isinstance(ret, (int, float)) or isinstance(ret, bool) or not math.isfinite(ret) \
                or not (RET_MIN <= ret <= RET_MAX):
            raise RecordError("수익률이 범위를 벗어났어요")
        ret = round(float(ret), 6)
    else:
        ret = None                                   # 매매하지 않은 판은 수익률 없음(보낸 값은 버린다)
    entry = _entry(entry_date) if mode == "real" else None
    db.execute("INSERT INTO practice_record(user_id, tech, mode, traded, ret, real_entry, created_at) "
               "VALUES(:u, :t, :m, :tr, :r, :e, :at)",
               {"u": user_id, "t": tech, "m": mode, "tr": bool(traded), "r": ret, "e": entry, "at": clock.utc()})
    return summary(user_id)


def summary(user_id: str) -> dict:
    """기법별 { tries(연습), best(연습 최고), real_tries, real_best, traded(매매까지 마친 판 — 마이페이지 '완료') }."""
    rows = db.query(
        "SELECT tech, COUNT(*) FILTER (WHERE mode='practice') AS tries, "
        "MAX(ret) FILTER (WHERE mode='practice' AND traded) AS best, "
        "COUNT(*) FILTER (WHERE mode='real') AS real_tries, MAX(ret) FILTER (WHERE mode='real' AND traded) AS real_best, "
        "COUNT(*) FILTER (WHERE traded) AS traded "
        "FROM practice_record WHERE user_id=:u GROUP BY tech", {"u": user_id})
    return {r["tech"]: {"tries": int(r["tries"]), "best": r["best"], "real_tries": int(r["real_tries"]),
                        "real_best": r["real_best"], "traded": int(r["traded"])} for r in rows}


def overview(user_id: str, limit: int = 20, offset: int = 0) -> dict:
    total = db.query("SELECT COUNT(*) AS n FROM practice_record WHERE user_id=:u", {"u": user_id})[0]["n"]
    items = db.query("SELECT id, tech, mode, traded, ret, real_entry, created_at FROM practice_record "
                     "WHERE user_id=:u ORDER BY id DESC LIMIT :n OFFSET :o", {"u": user_id, "n": limit, "o": offset})
    for it in items:
        if hasattr(it.get("created_at"), "astimezone"):
            it["created_at"] = it["created_at"].astimezone(clock.KST).replace(tzinfo=None).isoformat(timespec="seconds")
    return {"summary": summary(user_id), "items": items, "total": int(total),
            "has_more": offset + len(items) < int(total)}


def delete_user(conn, user_id: str) -> None:
    conn.execute(text("DELETE FROM practice_record WHERE user_id=:u"), {"u": user_id})
