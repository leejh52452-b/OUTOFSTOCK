"""계정 정보 — 로그인 유저에 귀속되는 데이터의 기록/조회.
성향(설문결과) · 학습로그 · 접속로그 를 기록하고, /me 로 한 번에 모아 본다(마이페이지)."""

from app import clock
from app import db


def _now():
    return clock.iso_now()


# ── 기록 ──────────────────────────────────────────────────
def record_survey(user_id, result):
    if not user_id:
        return
    db.execute("INSERT INTO survey_result(user_id, type, score, description, created_at) "
               "VALUES(:u, :t, :s, :d, :at)",
               {"u": user_id, "t": result["type"], "s": result.get("score"),
                "d": result.get("description"), "at": _now()})


def _clip(s, n):
    """로그용 문자열 정리 — NUL 제거 + 컬럼 길이로 자름(열람 기록은 잘라서라도 남기는 편이 낫다)."""
    return str(s or "").replace("\x00", "")[:n]


def record_learning(user_id, kind, item_key, item_name):
    if not user_id:
        return
    db.execute("INSERT INTO learning_log(user_id, kind, item_key, item_name, viewed_at) "
               "VALUES(:u, :k, :ik, :inm, :at)",
               {"u": user_id, "k": _clip(kind, 20), "ik": _clip(item_key, 60), "inm": _clip(item_name, 100),
                "at": _now()})


def record_access(user_id, event, detail=""):
    if not user_id:
        return
    db.execute("INSERT INTO access_log(user_id, event, detail, at) VALUES(:u, :e, :d, :at)",
               {"u": user_id, "e": _clip(event, 20), "d": _clip(detail, 200), "at": _now()})


# ── 조회(마이페이지) ──────────────────────────────────────
def latest_survey(user_id):
    rows = db.query("SELECT type, score, description, created_at FROM survey_result "
                    "WHERE user_id=:u ORDER BY result_id DESC LIMIT 1", {"u": user_id})
    return rows[0] if rows else None


def public_profile(username):
    """공개 프로필 — 친구/타 사용자에게 보여줄 안전 부분집합(데이터 분류: 공개 L1).
    성향·실습 성과·투자 수익률만. 잔액·거래원장·접속로그·관심종목은 제외(프라이버시)."""
    prof = db.query("SELECT username, created_at FROM app_user WHERE username=:u", {"u": username})
    from app.services import invest as invest_svc
    inv = invest_svc.positions(username)["summary"]
    pr = db.query("SELECT status FROM practice_session WHERE user_id=:u", {"u": username})
    practice_done = sum(1 for p in pr if p["status"] == "done")
    sv = latest_survey(username)
    return {
        "username": username,
        "created_at": prof[0]["created_at"] if prof else None,
        "survey_type": sv["type"] if sv else None,
        "practice_done": practice_done,
        "invest": {"open_count": inv["open_count"], "pnl_pct": inv["pnl_pct"]},
    }


def delete_account(user_id):
    """회원 탈퇴 — 계정에 엮인 모든 데이터를 한 트랜잭션으로 삭제(관심종목·성향·학습·접속·실습·웹 실습 기록·퀴즈·토큰·계정)."""
    if not user_id:
        return {"ok": False}
    from sqlalchemy import text as _text
    from app.security import rls
    with rls.elevated("account_delete"), db.transaction() as conn:
        # 계정 행부터 잠근다 — 진행 중인 송금·선물(받는 계정을 FOR SHARE 로 확인)과 순서를 맞춰 교착 없이 줄 세움
        conn.execute(_text("SELECT 1 FROM app_user WHERE username=:u FOR UPDATE"), {"u": user_id})
        sids = [r[0] for r in conn.execute(
            _text("SELECT session_id FROM practice_session WHERE user_id=:u"), {"u": user_id})]
        for sid in sids:
            conn.execute(_text("DELETE FROM practice_trade WHERE session_id=:s"), {"s": sid})
        conn.execute(_text("DELETE FROM practice_session WHERE user_id=:u"), {"u": user_id})
        for tbl in ("watchlist", "survey_result", "learning_log", "access_log", "practice_record", "quiz_attempt"):
            conn.execute(_text(f"DELETE FROM {tbl} WHERE user_id=:u"), {"u": user_id})
        # 가상머니 지갑 생태계 데이터도 함께 삭제(단일 트랜잭션 — 전부 삭제 또는 롤백).
        # 남은 잔액은 먼저 '소멸' 분개로 옮긴다 — 분개는 지우지 않으므로 원장은 계속 맞고, 계좌번호와 사람의 연결만 사라진다
        from app.services import ledger as _ledger
        if conn.execute(_text("SELECT 1 FROM wallet WHERE username=:u"), {"u": user_id}).first():
            _ledger.forfeit(conn, user_id)
        from app.services import vmarket as _vmarket                  # 모의투자 시즌 참가·보유·체결
        _vmarket.delete_user(conn, user_id)
        for tbl in ("wallet_ledger", "attendance", "invest_position", "avatar_closet", "wallet"):
            conn.execute(_text(f"DELETE FROM {tbl} WHERE username=:u"), {"u": user_id})
        # 결제 영수증은 계정과 끊고(이름 지움) 남긴다 — 지우면 같은 영수증을 새 계정(같은 아이디 재가입 포함)에서
        # 다시 써서 가상머니를 또 받을 수 있었다(재사용 방지가 영수증 UNIQUE 하나뿐)
        conn.execute(_text("UPDATE iap_purchase SET username=NULL WHERE username=:u"), {"u": user_id})
        # 친구 관계(양방향)·친구 요청·채팅(주고받은 메시지 전부)·결제 주문도 함께 삭제
        from app.services import friends as _friends
        _friends.delete_user(conn, user_id)
        from app.services import chat as _chat
        _chat.delete_user(conn, user_id)
        conn.execute(_text("DELETE FROM payment_order WHERE username=:u"), {"u": user_id})
        conn.execute(_text("DELETE FROM auth_token WHERE username=:u"), {"u": user_id})
        # 세션·2단계 인증·역할 행 삭제, 보안 이벤트의 아이디는 가명으로(보관 기간 동안 분석용으로만 남김)
        from app.services import privacy as _privacy
        _privacy.on_account_deleted(conn, user_id)
        conn.execute(_text("DELETE FROM app_user WHERE username=:u"), {"u": user_id})
    return {"ok": True}


MY_PREVIEW = 5            # 마이페이지 각 섹션에 보이는 최근 건수(나머지는 '전체 보기' 전용 페이지)


def my_overview(user_id):
    """마이페이지 — 프로필 + 성향 + 각 로그의 '최근 5건'과 전체 건수(counts).
    전체 목록은 /me/logs/{kind} (해당 로그만 모인 전용 페이지, 페이지네이션)."""
    prof = db.query("SELECT username, created_at FROM app_user WHERE username=:u", {"u": user_id})
    counts = log_counts(user_id)
    practice = logs(user_id, "practice", MY_PREVIEW)["items"]
    pc = db.query("SELECT COUNT(*) FILTER (WHERE status='done') AS done, "
                  "COUNT(*) FILTER (WHERE status='active') AS active FROM practice_session "
                  "WHERE user_id=:u", {"u": user_id})[0]
    learning = logs(user_id, "learning", MY_PREVIEW)["items"]
    access = logs(user_id, "access", MY_PREVIEW)["items"]
    from app.services import watchlist as watchlist_svc      # 지연 import(순환참조 회피)
    watch = watchlist_svc.items(user_id)                     # 전체(별표 상태용) — 화면은 5개만 보여줌
    # 가상머니 지갑 생태계(지연 import) — 지갑·출석·장기투자 요약
    from app.services import wallet as wallet_svc
    from app.services import attendance as attendance_svc
    from app.services import invest as invest_svc
    from app.services import avatar as avatar_svc
    w = wallet_svc.overview(user_id, ledger_limit=MY_PREVIEW)
    att = attendance_svc.status(user_id)
    inv = invest_svc.positions(user_id)
    av = avatar_svc.state(user_id)                           # 꾸민 양봉이(프로필 그림) — 착용·몸값·수저
    return {
        "user": user_id,
        "profile": (prof[0] if prof else {"username": user_id, "created_at": None}),
        "survey": latest_survey(user_id),
        "practice": {"done": int(pc["done"] or 0), "active": int(pc["active"] or 0),
                     "total": counts["practice"], "sessions": practice},
        "learning": {"count": counts["learning"], "items": learning},
        "access": {"count": counts["access"], "items": access},
        "watchlist": {"count": len(watch), "items": watch},
        "wallet": w,
        "attendance": {"streak": att["streak"], "total_days": att["total_days"],
                       "checked_in_today": att["checked_in_today"],
                       "daily_reward": att["daily_reward"]},
        "invest": inv["summary"],
        "avatar": {"equipped": av["equipped"], "worth": av["worth"], "class": av["class"], "owned": len(av["owned"])},
        "counts": counts,
        "preview": MY_PREVIEW,
    }


# ── 로그 전용 페이지(/me/logs/{kind}) — '더보기'를 누르면 그 로그만 모아 페이지 단위로 ─────────
LOG_KINDS = {
    "wallet": "지갑 거래 내역",
    "practice": "실습 이력",
    "learning": "학습 로그",
    "access": "접속 로그",
    "survey": "투자성향 분석 이력",
    "watchlist": "관심종목",
}
# 지갑 거래 내역 필터(사유 묶음) — 전용 페이지의 칩
WALLET_FILTERS = {
    "all": ("전체", None),
    "reward": ("출석·퀴즈·환영금", ["welcome", "attendance", "quiz_reward"]),
    "charge": ("충전", ["iap"]),
    "transfer": ("송금", ["transfer_in", "transfer_out"]),
    "invest": ("모의투자", ["invest_buy", "invest_sell", "vmarket_entry", "vmarket_settle"]),
    "shop": ("양봉이 꾸미기", ["avatar_buy"]),
}
# kind → (FROM/WHERE 절, SELECT 컬럼, ORDER BY) — 사용자 조건은 항상 바인딩(:u)
_LOG_SQL = {
    "wallet": ("FROM wallet_ledger WHERE username=:u",
               "ledger_id AS id, reason, amount, balance_after, ref, memo, created_at", "ledger_id DESC"),
    "practice": ("FROM practice_session WHERE user_id=:u",
                 "session_id AS id, session_id, scenario_id, status, final_return, best_return, attempts, "
                 "started_at, ended_at", "session_id DESC"),
    "learning": ("FROM learning_log WHERE user_id=:u",
                 "log_id AS id, kind, item_key, item_name, viewed_at", "log_id DESC"),
    "access": ("FROM access_log WHERE user_id=:u", "log_id AS id, event, detail, at", "log_id DESC"),
    "survey": ("FROM survey_result WHERE user_id=:u",
               "result_id AS id, type, score, description, created_at", "result_id DESC"),
}


class LogError(ValueError):
    """알 수 없는 로그 종류/필터."""


def log_counts(user_id):
    """각 로그의 전체 건수(마이페이지 '전체 N건' 표시용) — 쿼리 1번."""
    parts = ", ".join(f"(SELECT COUNT(*) {frm}) AS {k}" for k, (frm, _c, _o) in _LOG_SQL.items())
    parts += ", (SELECT COUNT(*) FROM watchlist WHERE user_id=:u) AS watchlist"
    row = db.query(f"SELECT {parts}", {"u": user_id})[0]
    return {k: int(row[k] or 0) for k in LOG_KINDS}


def _decorate(kind, items):
    if kind == "wallet":
        from app.services.wallet import REASON_KO
        for r in items:
            r["reason_ko"] = REASON_KO.get(r["reason"], r["reason"])
    elif kind == "practice":
        from app.services import practice as practice_svc     # 시나리오 표시명(순환참조 회피 지연 import)
        for p in items:
            meta = practice_svc.scenario_meta(p["scenario_id"])
            p["name"] = meta["name"] if meta else p["scenario_id"]
            p["difficulty"] = meta["difficulty"] if meta else ""
    return items


def logs(user_id, kind, limit=20, offset=0, flt="all"):
    """kind 로그 한 페이지 — 최신순. total·has_more 로 페이지네이션. 지갑은 사유 필터(flt) 지원."""
    if kind not in LOG_KINDS:
        raise LogError(f"알 수 없는 로그 종류: {kind}")
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, min(int(offset or 0), 1_000_000))      # 터무니없이 큰 offset(64비트 초과) → DB 오류 방지
    out = {"kind": kind, "title": LOG_KINDS[kind], "limit": limit, "offset": offset}
    if kind == "watchlist":
        from app.services import watchlist as watchlist_svc
        allw = watchlist_svc.items(user_id)
        out.update(total=len(allw), items=allw[offset:offset + limit])
    else:
        frm, cols, order = _LOG_SQL[kind]
        params = {"u": user_id, "n": limit, "o": offset}
        if kind == "wallet":
            if flt not in WALLET_FILTERS:
                raise LogError(f"알 수 없는 필터: {flt}")
            reasons = WALLET_FILTERS[flt][1]
            if reasons:
                frm += " AND reason = ANY(:rs)"
                params["rs"] = reasons
            out["filter"] = flt
            out["filters"] = [{"key": k, "label": v[0]} for k, v in WALLET_FILTERS.items()]
        total = db.query(f"SELECT COUNT(*) AS n {frm}", params)[0]["n"]
        items = db.query(f"SELECT {cols} {frm} ORDER BY {order} LIMIT :n OFFSET :o", params)
        out.update(total=int(total), items=_decorate(kind, items))
    out["has_more"] = offset + len(out["items"]) < out["total"]
    return out
