"""개인정보 보호 — 관리자 열람 기록·점검, 보관 기간 파기, 탈퇴 시 보안 기록 가명화.

  열람(subject_view)  목적을 받아 '누가·언제·누구의·무엇을·왜'를 pii_access_log 에 남긴다. 같은 내용을 보안 이벤트
                      (pii_access)로도 남겨 과다 열람 규칙(하루 50명 이상 → 경보)이 자동으로 본다.
  점검(access_review) 운영자별·일별 건수, 야간(22~07시 KST) 열람, 같은 대상 반복 열람을 표시 — 월 1회 점검
                      (안전성 확보조치 기준 제8조② 2025 개정: 주기·방법·사후조치는 내부 관리계획으로 — 계획 §8).
  파기(retention)     보관 기간이 지난 접속 기록·보안 이벤트·세션·알림 기록을 지운다(보관 기간은 설정, 기본은
                      접속 기록 1년·감사 3년 — 권한 내역 3년 하한). 감사 로그는 해시 체인 시작점을 옮겨 검증을 이어 간다.
  가명화(on_account_deleted)  탈퇴하면 보안 이벤트의 아이디를 가명(HMAC)으로 바꾼다 — 보안 분석용 기록은 기간 동안
                      남기되, 더 이상 그 사람을 직접 가리키지 않게(목적 달성 후 식별성 제거).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings
from app.security import context, events, keys

FIELDS = "아이디·가입일·상태·최근 로그인·지갑 잔액·투자성향·최근 접속 기록 10건·활성 세션 수"
NIGHT = (22, 7)                                  # 야간 열람 판정(KST 시)
DAILY_FLAG = 30                                  # 운영자 1명이 하루에 이 인원 이상 열람하면 표시
REVIEW_MAX = 20_000                              # 한 번에 점검하는 열람 기록 상한(넘으면 truncated 로 알린다)


def subject_view(username: str, operator: str, purpose: str) -> dict | None:
    from app.security import rls
    with rls.elevated("admin_pii"):                          # 권한·목적·기록을 거친 관리자 열람만 RLS 를 연다
        return _subject_view(username, operator, purpose)


def _subject_view(username: str, operator: str, purpose: str) -> dict | None:
    u = db.query("SELECT username, created_at, status, last_login_at, locked_until FROM app_user WHERE username=:u",
                 {"u": username})
    if not u:
        return None
    ctx = context.current()
    db.execute("INSERT INTO pii_access_log(at, operator, subject, fields, purpose, ip_hash, request_id) "
               "VALUES(:at, :op, :s, :f, :p, :ip, :rid)",
               {"at": clock.utc(), "op": operator, "s": username, "f": FIELDS[:200], "p": purpose.strip()[:200],
                "ip": events.ip_hash(ctx.ip), "rid": ctx.request_id[:64]})
    events.emit("pii_access", "low", username=operator, subject=keys.hmac_hex("user", username))
    from app.services import audit
    audit.record("admin.pii_view", operator, target=username, detail={"purpose": purpose.strip()[:200]})
    row = u[0]
    bal = db.query("SELECT balance FROM wallet WHERE username=:u", {"u": username})
    sv = db.query("SELECT type FROM survey_result WHERE user_id=:u ORDER BY result_id DESC LIMIT 1", {"u": username})
    acc = db.query("SELECT event, detail, at FROM access_log WHERE user_id=:u ORDER BY log_id DESC LIMIT 10",
                   {"u": username})
    sess = db.query("SELECT COUNT(*) AS n FROM auth_session WHERE username=:u AND revoked_at IS NULL "
                    "AND expires_at > now()", {"u": username})[0]["n"]
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v           # noqa: E731
    return {"username": row["username"], "created_at": row["created_at"], "status": row["status"],
            "last_login_at": iso(row["last_login_at"]), "locked_until": iso(row["locked_until"]),
            "wallet_balance": bal[0]["balance"] if bal else None, "survey_type": sv[0]["type"] if sv else None,
            "recent_access": acc, "active_sessions": int(sess),
            "logged": {"fields": FIELDS, "purpose": purpose.strip()[:200]}}


def access_review(days: int = 30) -> dict:
    since = clock.utc() - timedelta(days=days)
    total = int(db.query("SELECT COUNT(*) AS n FROM pii_access_log WHERE at >= :t", {"t": since})[0]["n"])
    rows = db.query("SELECT id, at, operator, subject, fields, purpose, request_id FROM pii_access_log "
                    "WHERE at >= :t ORDER BY id DESC LIMIT :n", {"t": since, "n": REVIEW_MAX})
    per_day: dict = defaultdict(set)
    repeat = Counter()
    flags = []
    for r in rows:
        kst = r["at"].astimezone(clock.KST)
        per_day[(r["operator"], kst.date().isoformat())].add(r["subject"])
        repeat[(r["operator"], r["subject"])] += 1
        if kst.hour >= NIGHT[0] or kst.hour < NIGHT[1]:
            flags.append({"type": "night", "operator": r["operator"], "at": kst.isoformat(timespec="minutes"),
                          "id": r["id"]})
    summary = [{"operator": op, "date": d, "subjects": len(s), "flag": len(s) >= DAILY_FLAG}
               for (op, d), s in sorted(per_day.items(), key=lambda kv: kv[0][1], reverse=True)]
    flags += [{"type": "bulk", "operator": x["operator"], "date": x["date"], "subjects": x["subjects"]}
              for x in summary if x["flag"]]
    flags += [{"type": "repeat", "operator": op, "subject_hash": keys.hmac_hex("user", sub), "count": n}
              for (op, sub), n in repeat.items() if n >= 5]
    items = [{**r, "at": r["at"].isoformat()} for r in rows[:200]]
    # 점검 대상이 상한을 넘으면 숨기지 않고 알린다 — 예전엔 2,000건에서 조용히 잘려 그 밖의 야간·대량 열람은 점검표에 없었다
    return {"days": days, "total": total, "reviewed": len(rows), "truncated": total > len(rows),
            "items": items, "summary": summary, "flags": flags}


def on_account_deleted(conn, username: str) -> None:
    """탈퇴 — 보안 이벤트의 아이디를 가명으로(기록은 보관 기간 동안 유지), 세션·MFA·현재 역할 자격은 삭제.
    관리 권한 활성 내역(role_activation)은 지우지 않고 가명으로 — 권한 내역 3년 보관(안전성 확보조치 기준 제5조③)."""
    alias = "del:" + keys.hmac_hex("user", username)
    conn.execute(text("UPDATE security_event SET username=:a WHERE username=:u"), {"a": alias, "u": username})
    for tbl in ("auth_session", "user_mfa", "user_role"):
        conn.execute(text(f"DELETE FROM {tbl} WHERE username=:u"), {"u": username})
    conn.execute(text("UPDATE role_activation SET username=:a WHERE username=:u"), {"a": alias, "u": username})
    conn.execute(text("UPDATE pii_access_log SET subject=:a WHERE subject=:u"), {"a": alias, "u": username})
    conn.execute(text("UPDATE fds_decision SET username=:a WHERE username=:u"), {"a": alias, "u": username})
    conn.execute(text("UPDATE fds_decision SET counterparty=:a WHERE counterparty=:u"), {"a": alias, "u": username})


def years_ago(now, years: int):
    """달력 기준 n년 전 같은 월·일·시각 — 2월 29일은 28일로(하루 더 오래 보관하는 쪽)."""
    try:
        return now.replace(year=now.year - years)
    except ValueError:
        return now.replace(year=now.year - years, day=28)


def retention_cut(now, days: int, min_years: int = 0):
    """보관 기한(이 시각보다 오래된 행만 지운다) — 설정 일수와 법정·내부 최소 연수(달력) 중 '더 오래 보관하는' 쪽.
    '1년'을 365일로 세면 윤년이 낀 해엔 하루 일찍 지워 '1년 이상 보관' 의무를 어긴다(결함 28)."""
    cut = now - timedelta(days=int(days))
    return min(cut, years_ago(now, min_years)) if min_years else cut


def _privilege_error(e: Exception) -> bool:
    """앱 계정에 DELETE 권한이 없는 표(소유자 배치 몫) — 그 경우만 '건너뜀'. 다른 오류를 권한 탓으로 숨기지 않는다."""
    return getattr(getattr(e, "orig", None), "sqlstate", None) == "42501"


def retention_purge() -> dict:
    """보관 기간 파기 배치(스케줄러가 매일) — 지운 건수를 돌려준다(감사 로그에 남김).
    단계마다 따로 — 한 단계가 실패해도 나머지는 계속하고, 파기 기록(감사 로그)은 항상 남긴 뒤 실패를 알린다(결함 30:
    예전엔 세션·알림·아웃박스 정리 중 하나가 예외를 내면 앞서 지운 건수의 감사 기록이 빠졌다)."""
    from app.security import sessions
    from app.services import alerts, audit, outbox
    now, know = clock.utc(), clock.now()
    out, errors = {}, []
    hist = retention_cut(now, settings.AUDIT_RETENTION_DAYS, audit.ROLE_HISTORY_MIN_YEARS)   # 권한 내역 — 3년(달력) 하한
    jobs = [
        ("access_log", "DELETE FROM access_log WHERE at < :c",                                   # 접속 기록 — 1년(달력) 하한
         retention_cut(know, settings.ACCESS_LOG_RETENTION_DAYS, 1).isoformat(timespec="seconds")),
        ("security_event", "DELETE FROM security_event WHERE at < :c",
         retention_cut(now, settings.SECURITY_EVENT_RETENTION_DAYS, 1)),
        ("pii_access_log", "DELETE FROM pii_access_log WHERE at < :c",                           # 열람 기록 — 2년(달력)
         retention_cut(now, settings.PII_LOG_RETENTION_DAYS, 2)),
        ("fds_decision", "DELETE FROM fds_decision WHERE at < :c", retention_cut(now, 365)),
        ("dq_result", "DELETE FROM dq_result WHERE run_at < :c", retention_cut(now, 365)),
        # 권한 내역은 '마지막으로 바뀐 때'부터 센다 — 예전엔 요청 시각으로 세어, 늦게 결정된 건은 결정 기록이 3년 전에 지워질 수 있었다
        ("role_request", "DELETE FROM role_request WHERE status <> 'pending' "
         "AND COALESCE(decided_at, requested_at) < :c", hist),
        ("role_activation", "DELETE FROM role_activation WHERE expires_at < :c", hist),           # JIT 활성 내역 3년↑
        ("service_control_request", "DELETE FROM service_control_request WHERE status <> 'pending' "
         "AND COALESCE(decided_at, requested_at) < :c", hist),                                    # 재개 승인 내역 3년↑
        ("capacity_report", "DELETE FROM capacity_report WHERE at < :c", retention_cut(now, 365)),
        ("security_assessment", "DELETE FROM security_assessment WHERE at < :c", years_ago(now, 5)),   # 진단 증빙 5년
        ("incident", "DELETE FROM incident WHERE status = 'closed' AND COALESCE(ended_at, started_at) < :c",
         years_ago(now, 5)),                                                                            # 사고 보고 근거 5년
        ("uptime_check", "DELETE FROM uptime_check WHERE at < :c", now - timedelta(days=35)),
        ("dpop_jti", "DELETE FROM dpop_jti WHERE seen_at < :c", now - timedelta(minutes=10)),
    ]
    for name, sql, cut in jobs:                      # 표마다 따로 — 앱 계정에 DELETE 권한이 없는 표(열람 기록)는 소유자 배치로
        try:
            with db.transaction() as c:
                out[name] = c.execute(text(sql), {"c": cut}).rowcount
        except Exception as e:                       # noqa: BLE001
            out[name] = "skipped(권한)" if _privilege_error(e) else f"실패: {type(e).__name__}"
    steps = [("sessions", lambda: {f"session_{k}": v for k, v in sessions.purge().items()}),
             ("alert_log", alerts.purge), ("outbox", outbox.purge), ("audit_log", audit.purge)]
    for name, fn in steps:
        try:
            r = fn()
            if isinstance(r, dict):
                out.update(r)
            else:
                out[name] = r
        except Exception as e:                       # noqa: BLE001
            out[name] = "skipped(권한)" if _privilege_error(e) else f"실패: {type(e).__name__}"
    errors = [k for k, v in out.items() if isinstance(v, str) and v.startswith("실패")]
    audit.record("privacy.retention_purge", "system", detail=out, result="partial" if errors else "ok")
    if errors:
        raise RuntimeError(f"보관 기간 파기 일부 실패: {errors} (나머지는 처리·기록됨)")
    return out


