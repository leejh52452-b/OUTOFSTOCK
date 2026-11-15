"""가용성 감시 · IT 장애 자동 판정 · 사고보고서 초안 — 전자금융감독규정 시행세칙 제7조의4(사고보고)를 설계 기준으로.

  감시   스케줄러가 1분마다 API 헬스(`UPTIME_URL`, auto = http://127.0.0.1:$PORT/health)를 직접 부른다(합성 모니터링 —
         서버 안의 지표는 서버가 죽으면 같이 사라진다). 실패(5xx·연결 불가)나 지연(UPTIME_SLOW_MS 초과)을 uptime_check 에 남긴다.
  판정   실패·지연이 10분 이어지면 장애 사건(incident)을 연다. 30분을 넘기면(또는 10분 + 가입자 1만 명 이상) 그 순간
         **보고 대상** — 시행세칙 제7조의4 ①1호. 최초 보고 기한 = 인지 후 24시간(종결까지 길어지면 2개월 안 중간 보고, 그 뒤 6개월마다).
         정상 응답이 3번 이어지면 닫고 지속 시간·영향 이용자를 남긴다.
  안전   한 번도 정상 응답을 받은 적이 없으면 장애로 판정하지 않고 '주소 확인' 경고만 — 잘못된 설정이 가짜 장애를 만들지 않게.
         같은 서버 안의 감시는 서버 전체가 꺼진 동안을 볼 수 없다 → 확인 기록 사이가 10분 넘게 비면 '감시 공백' 경고(사람이 판단).
  보고   보고서 초안은 별지 제2호 서식 항목 순서대로 — 특히 '사고원인(변경시점 및 사유 등)'에 사건 전 24시간의 변경 기록
         (change_record)·심각 보안 이벤트·용량 보고서·비상 차단을 자동으로 붙인다(원인 추적의 출발점).
  침해   사람이 여는 보안 침해 사건(kind=security)은 처음부터 보고 대상 — 전자금융거래법 제21조의5(지체 없이 통지), 설계 기준 24시간.
  중대   5개 지표(①영향도 高 ②데이터 훼손 ③평판 저하 ④장애시간 長 ⑤손실금액 高) 중 2개 이상이면 '중대 IT 장애'.
         지표의 수치 기준은 공개되지 않아 이 서비스의 기준을 정해 적었다(THRESHOLDS — 자체 기준).
  가용률 하루·30일 정상 응답 비율과 목표(UPTIME_SLO, 기본 99.5%) 대비 · 감시 범위(확인 기록이 있는 비율)를 함께.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger("incidents")
OPEN_AFTER_MIN = 10                # 이만큼 이어지면 장애 사건으로 연다
CLOSE_AFTER_OK = 3                 # 정상 응답이 이만큼 이어지면 닫는다
REPORT_MIN = 30                    # 시행세칙 제7조의4 ①1호 — 지연·중단 30분 이상
REPORT_MIN_WITH_USERS = (10, 10_000)   # 또는 10분 이상 + 가입자 1만 명 이상
FIRST_REPORT_H = 24
INTERIM_DAYS, PERIODIC_DAYS = 61, 183
KINDS = ("outage", "security", "data", "other")
THRESHOLDS = {   # 중대 IT 장애 5개 지표 — 자체 기준(수치는 공개되지 않음)
    "impact": "핵심 거래(로그인·송금·결제) 불가 30분 이상 또는 가입자 30% 이상 영향",
    "data": "원장·감사 체인 무결성 점검 실패, 데이터 손실·훼손(사람이 표시)",
    "reputation": "언론 보도·민원 다수(사람이 표시)",
    "duration": "장애 3시간 이상(전자금융감독규정 제23조 핵심업무 복구목표시간)",
    "loss": "가상머니 오지급·손실 1억 원 이상(사람이 입력)",
}
REPORT_FIELDS = ("보고형태", "사고인지일시", "사고발생일시", "사고종료일시", "사고발생 업무(시스템)명", "사고금액",
                 "사고 영향 이용자수", "사고내용(현상 및 결과)", "사고원인(변경시점 및 사유 등)", "파급영향", "처리결과", "대책")


def target_url() -> str:
    """감시할 주소 — auto 면 같은 서버의 API 포트, off·빈 값이면 감시하지 않는다."""
    u = (settings.UPTIME_URL or "").strip()
    if u.lower() in ("", "off", "none", "false", "0"):
        return ""
    if u.lower() == "auto":
        return f"http://127.0.0.1:{os.getenv('PORT') or 8000}/health"
    return u


# ── 합성 모니터링 ─────────────────────────────────────────────────────────
def probe(url: str | None = None, timeout_s: float = 10.0) -> dict:
    """헬스 1회 — 같은 서버 안의 호출이라 프록시·egress 통제를 거치지 않는다(바깥으로 나가는 연결이 아님)."""
    import httpx
    url = url or target_url()
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s, trust_env=False, follow_redirects=False) as c:
            r = c.get(url, headers={"User-Agent": "oos-uptime"})
        code, ok = r.status_code, r.status_code < 500
    except Exception as e:                                  # noqa: BLE001 — 연결 불가 = 중단
        code, ok = 0, False
        log.warning("가용성 확인 실패: %s", type(e).__name__)
    ms = int((time.perf_counter() - t0) * 1000)
    slow = ok and ms > settings.UPTIME_SLOW_MS
    return {"ok": ok and not slow, "code": code, "ms": ms, "slow": slow}


def record_check(res: dict, at=None) -> None:
    db.execute("INSERT INTO uptime_check(at, ok, ms, code) VALUES(:t, :ok, :ms, :c)",
               {"t": at or clock.utc(), "ok": bool(res["ok"]), "ms": int(res["ms"]), "c": int(res["code"])})


def run_check() -> dict:
    """스케줄러 1분 작업 — 확인 → 기록 → 판정. 한 일이 없으면 빈 dict(로그를 남기지 않는다)."""
    if not target_url():
        return {}
    res = probe()
    record_check(res)
    ev = evaluate()
    return {} if res["ok"] and not ev else {**res, **ev}


# ── 판정 ────────────────────────────────────────────────────────────────
def _open_incident():
    rows = db.query("SELECT * FROM incident WHERE kind='outage' AND opened_by IS NULL AND ended_at IS NULL "
                    "ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def _notify(key, title, body, level="error", dedup_hours=0):
    from app.services import alerts
    return alerts.notify(key, title, body, level=level, dedup_hours=dedup_hours)


def _gap_check() -> dict:
    """방금 기록과 그 앞 기록 사이가 10분 넘게 비었으면(서버·스케줄러 중단) 한 번 경고 — 그동안의 가용성은 알 수 없다."""
    last2 = db.query("SELECT at FROM uptime_check ORDER BY at DESC LIMIT 2")
    if len(last2) < 2:
        return {}
    gap = last2[0]["at"] - last2[1]["at"]
    if gap < timedelta(minutes=OPEN_AFTER_MIN):
        return {}
    mins = round(gap.total_seconds() / 60)
    _notify(f"uptime:gap:{last2[1]['at']:%Y%m%d%H%M}", "[관제] 가용성 감시 공백",
            f"{last2[1]['at'].astimezone(clock.KST):%m-%d %H:%M} KST 부터 {mins}분 동안 확인 기록이 없어요(서버·스케줄러 중단?). "
            "그동안 서비스가 멈췄다면 사람이 장애 사건으로 등록해 주세요(POST /admin/incidents).", level="warn",
            dedup_hours=24)
    return {"gap_min": mins}


def evaluate(now=None) -> dict:
    """최근 확인 기록으로 장애 사건을 열고(10분), 보고 대상으로 표시하고(30분), 닫는다(정상 3번)."""
    now = now or clock.utc()
    out = _gap_check()
    rows = db.query("SELECT at, ok FROM uptime_check WHERE at >= :t ORDER BY at", {"t": now - timedelta(hours=6)})
    cur = _open_incident()
    if cur is None:
        streak = []
        for r in reversed(rows):                           # 마지막부터 거꾸로 — 이어진 실패
            if r["ok"]:
                break
            streak.append(r)
        if not streak or (now - streak[-1]["at"]) < timedelta(minutes=OPEN_AFTER_MIN):
            return out
        if not db.query("SELECT 1 FROM uptime_check WHERE ok LIMIT 1"):
            _notify("uptime:unverified", "[관제] 가용성 감시 주소에 한 번도 연결되지 않았어요",
                    f"감시 주소 {target_url() or '(없음)'} — 주소·포트(UPTIME_URL)를 확인해 주세요. "
                    "정상 응답을 한 번 받기 전에는 장애로 판정하지 않습니다.", level="warn", dedup_hours=None)
            return {**out, "unverified": True}
        start = streak[-1]["at"]
        got = db.write_query(
            "INSERT INTO incident(kind, title, started_at, detected_at, status, severity, details) "
            "VALUES('outage', :ti, :s, :d, 'open', 'major', :det) ON CONFLICT DO NOTHING RETURNING id",
            {"ti": "서비스 응답 실패·지연(합성 모니터링)", "s": start, "d": now,
             "det": json.dumps({"failed_checks": len(streak)}, ensure_ascii=False)})
        if not got:                                        # 다른 스케줄러가 방금 열었다
            return out
        iid = got[0]["id"]
        from app.services import audit
        audit.record("incident.open", "system", target=f"incident:{iid}", detail={"kind": "outage", "auto": True})
        _notify(f"incident:{iid}", "[장애] 서비스 응답 실패·지연 10분 이상",
                f"사건 #{iid} · 시작 {start.astimezone(clock.KST):%m-%d %H:%M} KST · 30분을 넘기면 감독규정상 보고 대상"
                "(인지 후 24시간 안 최초 보고) · 원인 후보: /admin/incidents/" + str(iid))
        return {**out, "opened": iid}
    oks, first_ok = 0, None
    for r in rows:
        if r["at"] <= cur["started_at"]:
            continue
        if r["ok"]:
            oks += 1
            first_ok = first_ok or r["at"]
        else:
            oks, first_ok = 0, None
    if oks >= CLOSE_AFTER_OK and first_ok is not None:
        return {**out, "closed": close(cur["id"], first_ok)}
    minutes = (now - cur["started_at"]).total_seconds() / 60
    if not cur["reportable"] and reportable(minutes, subscribers()):
        _mark_reportable(cur["id"], minutes)
        return {**out, "open": cur["id"], "reportable": True}
    return {**out, "open": cur["id"]}


def _mark_reportable(iid: int, minutes: float) -> None:
    n = db.execute("UPDATE incident SET reportable=true, report_due=detected_at + make_interval(hours => :h) "
                   "WHERE id=:i AND NOT reportable", {"h": FIRST_REPORT_H, "i": iid})
    if n:
        r = db.query("SELECT report_due FROM incident WHERE id=:i", {"i": iid})[0]
        from app.services import audit
        audit.record("incident.reportable", "system", target=f"incident:{iid}", detail={"minutes": round(minutes, 1)})
        _notify(f"incident:report:{iid}", "[장애] 감독규정상 보고 대상 — 24시간 안 최초 보고",
                f"사건 #{iid} · {minutes:.0f}분째 · 최초 보고 기한 {r['report_due'].astimezone(clock.KST):%m-%d %H:%M} KST · "
                f"보고서 초안: /admin/incidents/{iid}?fmt=md")


def subscribers() -> int:
    return int(db.query("SELECT COUNT(*) AS n FROM app_user WHERE status <> 'disabled'")[0]["n"])


def affected_users(start, end) -> int:
    """영향 이용자 추정 — 장애 시간에 살아 있던(만료·폐기·유휴 만료 전) 로그인 세션의 사용자 수."""
    from app.security import rls
    with rls.elevated("incident_report"):
        return int(db.query(
            "SELECT COUNT(DISTINCT username) AS n FROM auth_session WHERE created_at <= :e "
            "AND (revoked_at IS NULL OR revoked_at >= :s) AND expires_at >= :s AND last_seen_at >= :idle",
            {"s": start, "e": end, "idle": start - timedelta(seconds=settings.SESSION_IDLE_SECONDS)})[0]["n"])


def reportable(minutes: float, subs: int) -> bool:
    return minutes >= REPORT_MIN or (minutes >= REPORT_MIN_WITH_USERS[0] and subs >= REPORT_MIN_WITH_USERS[1])


def close(iid: int, ended_at, actor: str = "system") -> int:
    """사건 종료 — 지속 시간·가입자·영향 이용자를 남기고, 장애(outage)면 보고 대상인지 다시 판정한다."""
    rows = db.query("SELECT * FROM incident WHERE id=:i", {"i": iid})
    if not rows or rows[0]["ended_at"] is not None:
        return iid
    r = rows[0]
    if ended_at < r["started_at"]:
        raise ValueError("종료 시각이 시작보다 빨라요")
    minutes = (ended_at - r["started_at"]).total_seconds() / 60
    subs, users = subscribers(), affected_users(r["started_at"], ended_at)
    rep = bool(r["reportable"]) or (r["kind"] == "outage" and reportable(minutes, subs))
    det = json.loads(r["details"] or "{}") | {"minutes": round(minutes, 1), "subscribers": subs}
    with db.transaction() as conn:
        n = conn.execute(text(
            "UPDATE incident SET ended_at=:e, status='resolved', affected_users=:u, reportable=:rep, "
            "report_due=COALESCE(report_due, CASE WHEN :rep THEN detected_at + make_interval(hours => :h) END), details=:d "
            "WHERE id=:i AND ended_at IS NULL"),
            {"e": ended_at, "u": users, "rep": rep, "h": FIRST_REPORT_H, "d": json.dumps(det, ensure_ascii=False),
             "i": iid}).rowcount
    if not n:                                              # 다른 쪽이 먼저 닫았다
        return iid
    from app.services import audit
    audit.record("incident.close", actor, target=f"incident:{iid}",
                 detail={"minutes": round(minutes, 1), "reportable": rep, "affected": users})
    if rep and not r["reportable"]:
        _notify(f"incident:report:{iid}", "[장애] 감독규정상 보고 대상 — 24시간 안 최초 보고",
                f"사건 #{iid} · {minutes:.0f}분 · 영향 이용자 약 {users}명 · 보고서 초안: /admin/incidents/{iid}?fmt=md")
    elif r["kind"] == "outage" and actor == "system":
        _notify(f"incident:closed:{iid}", "[장애] 복구 확인", f"사건 #{iid} · {minutes:.0f}분 · 영향 이용자 약 {users}명",
                level="info")
    return iid


def open_manual(kind: str, title: str, actor: str, started_at=None) -> int:
    """사람이 여는 사건(보안 침해·데이터 오류·감시 공백 동안의 장애 등). 보안 침해는 처음부터 보고 대상(24시간)."""
    title = (title or "").strip()
    if kind not in KINDS or len(title) < 5:
        raise ValueError("kind 는 outage|security|data|other, 제목 5자 이상")
    now = clock.utc()
    started_at = started_at or now
    if started_at > now:
        raise ValueError("시작 시각이 미래예요")
    sec = kind == "security"
    iid = db.write_query(
        "INSERT INTO incident(kind, title, started_at, detected_at, status, severity, opened_by, reportable, report_due) "
        "VALUES(:k, :t, :s, :d, 'open', 'major', :by, :rep, :due) RETURNING id",
        {"k": kind, "t": title[:200], "s": started_at, "d": now, "by": actor[:50], "rep": sec,
         "due": now + timedelta(hours=FIRST_REPORT_H) if sec else None})[0]["id"]
    from app.services import audit
    audit.record("incident.open", actor, target=f"incident:{iid}", detail={"kind": kind})
    if sec:
        _notify(f"incident:report:{iid}", "[침해] 보안 침해 사건 등록 — 지체 없이 보고(설계 기준 24시간)",
                f"사건 #{iid} · {title[:120]} · 등록 {actor}")
    return iid


UPDATABLE = ("cause", "action", "measures", "impact_note", "loss_amount", "reputation", "data_damage", "reportable")


def update(iid: int, actor: str, **fields) -> dict:
    """사람이 채우는 칸(원인·처리·대책·평판·손실·보고 대상) + 보고 단계(최초·중간·종결) + 종료 시각."""
    sets, p = [], {"i": iid}
    for k in UPDATABLE:
        if k in fields and fields[k] is not None:
            sets.append(f"{k}=:{k}")
            p[k] = fields[k] if not isinstance(fields[k], str) else fields[k][:1000]
    if fields.get("reportable"):
        sets.append("report_due=COALESCE(report_due, detected_at + make_interval(hours => :h))")
        p["h"] = FIRST_REPORT_H
    stage = fields.get("reported")
    if stage:
        if stage not in ("최초", "중간", "종결"):
            raise ValueError("보고 단계는 최초|중간|종결")
        col = {"최초": "first_reported_at", "중간": "interim_reported_at", "종결": "final_reported_at"}[stage]
        sets.append(f"{col}=:now")
        p["now"] = clock.utc()
        if stage == "종결":
            sets.append("status='closed'")
    ended = fields.get("ended_at")
    if not sets and ended is None:
        raise ValueError("바꿀 칸이 없어요")
    cur = db.query("SELECT status, ended_at FROM incident WHERE id=:i", {"i": iid})
    if not cur:
        raise LookupError("없는 사건")
    if stage == "종결" and cur[0]["ended_at"] is None and ended is None:
        raise ValueError("끝나지 않은 사건은 종결 보고를 할 수 없어요(ended_at 을 함께)")
    if ended is not None:
        close(iid, ended, actor)
    if sets:
        db.execute(f"UPDATE incident SET {', '.join(sets)} WHERE id=:i", p)     # noqa: S608 — 열 이름은 상수 목록에서
    from app.services import audit
    audit.record("incident.update", actor, target=f"incident:{iid}",
                 detail={"fields": sorted(k for k in fields if k in UPDATABLE and fields[k] is not None), "reported": stage,
                         "ended": ended is not None})
    return get(iid)


# ── 원인 후보(변경시점 등) · 중대 판정 · 보고서 초안 ───────────────────────────
def cause_candidates(start, end) -> dict:
    """사건 전 24시간 ~ 종료까지의 변경·이상 신호 — '사고원인(변경시점 및 사유 등)'의 출발점."""
    since = start - timedelta(hours=24)
    until = end or clock.utc()
    out: dict = {}
    try:
        from app.services import changes
        out["changes"] = [{"id": c["id"], "kind": c["kind"], "title": c["title"], "risk": c["risk"],
                           "applied_at": c["applied_at"], "status": c["status"]}
                          for c in changes.recent(since=since, until=until, limit=20)]
    except Exception:                                      # noqa: BLE001 — 표가 없는 구버전
        out["changes"] = []
    q = [("security", "SELECT at, kind, severity, response FROM security_event WHERE at BETWEEN :s AND :e "
                      "AND severity IN ('high', 'critical') ORDER BY at LIMIT 30"),
         ("capacity", "SELECT at, level FROM capacity_report WHERE at BETWEEN :s AND :e ORDER BY at LIMIT 10"),
         ("controls", "SELECT feature, state, reason, changed_by, changed_at FROM service_control WHERE changed_at "
                      "BETWEEN :s AND :e")]
    for key, sql in q:
        try:
            rows = db.query(sql, {"s": since, "e": until})
            out[key] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]
        except Exception:                                  # noqa: BLE001
            out[key] = []
    return out


def major(inc: dict) -> dict:
    """중대 IT 장애 5개 지표(자체 기준) — 2개 이상이면 중대."""
    det = inc.get("details") or {}
    minutes = det.get("minutes") or 0
    subs = det.get("subscribers") or 0
    outage = inc.get("kind") == "outage"
    flags = {
        "impact": outage and (minutes >= REPORT_MIN or (inc.get("affected_users") or 0) >= max(1, subs * 0.3)),
        "data": bool(inc.get("data_damage")),
        "reputation": bool(inc.get("reputation")),
        "duration": minutes >= 180,
        "loss": (inc.get("loss_amount") or 0) >= 100_000_000,
    }
    return {"flags": flags, "count": sum(flags.values()), "major": sum(flags.values()) >= 2, "criteria": THRESHOLDS}


def deadlines(r: dict) -> dict:
    """보고 기한 — 최초(인지 후 24시간) · 중간(종결까지 2개월 넘으면 2개월 안, 그 뒤 6개월마다) · 기한 초과 여부."""
    due = r.get("report_due")
    out: dict = {"first": due.isoformat() if (r.get("reportable") and due) else None}
    if r.get("reportable") and r.get("status") != "closed":
        out["interim"] = (r["detected_at"] + timedelta(days=INTERIM_DAYS)).isoformat()
        out["periodic_days"] = PERIODIC_DAYS
    out["first_overdue"] = bool(out["first"] and not r.get("first_reported_at") and clock.utc() > due)
    out["interim_overdue"] = bool(out.get("interim") and not r.get("interim_reported_at") and not r.get("final_reported_at")
                                  and clock.utc() > r["detected_at"] + timedelta(days=INTERIM_DAYS))
    return out


def get(iid: int) -> dict | None:
    rows = db.query("SELECT * FROM incident WHERE id=:i", {"i": iid})
    if not rows:
        return None
    r = dict(rows[0])
    try:
        r["details"] = json.loads(r["details"]) if r.get("details") else {}
    except ValueError:
        r["details"] = {}
    if r["ended_at"] is None:                              # 진행 중 — 지금까지의 시간
        r["details"]["minutes_so_far"] = round((clock.utc() - r["started_at"]).total_seconds() / 60, 1)
    r["major"] = major(r)
    r["causes"] = cause_candidates(r["started_at"], r["ended_at"])
    r["deadlines"] = deadlines(r)
    for k, v in list(r.items()):
        if hasattr(v, "isoformat"):
            r[k] = v.isoformat()
    return r


def _kst(v) -> str:
    return "—" if not v else clock.parse_utc(v).astimezone(clock.KST).strftime("%Y.%m.%d %H:%M")


def report_md(iid: int, stage: str = "최초") -> str:
    """사고보고서 초안(마크다운) — 별지 제2호 서식 항목 순서. 사람이 원인·조치·대책을 채운 뒤 보고한다."""
    r = get(iid)
    if r is None:
        raise LookupError("없는 사건")
    det, causes = r["details"], r["causes"]
    ch = "; ".join(f"#{c['id']} {c['title']}({c['kind']}·위험 {c['risk']}·적용 {_kst(c['applied_at'])})"
                   for c in causes["changes"]) or "사건 전 24시간 변경 없음"
    mins = det.get("minutes", det.get("minutes_so_far", "—"))
    vals = {
        "보고형태": stage, "사고인지일시": _kst(r["detected_at"]), "사고발생일시": _kst(r["started_at"]),
        "사고종료일시": _kst(r["ended_at"]) if r["ended_at"] else "(진행 중)",
        "사고발생 업무(시스템)명": f"OUTOFSTOCK API·웹({r['title']})",
        "사고금액": f"{int(r.get('loss_amount') or 0):,}원(가상머니)",
        "사고 영향 이용자수": f"약 {r.get('affected_users') or 0}명(장애 시간에 로그인 세션이 있던 사용자 추정 — 가입자 "
                       f"{det.get('subscribers', '—')}명)",
        "사고내용(현상 및 결과)": f"{r['title']} — {mins}분" + (f" · {r['impact_note']}" if r.get("impact_note") else ""),
        "사고원인(변경시점 및 사유 등)": (r.get("cause") or "(조사 중)") + f" · 변경시점 후보: {ch}",
        "파급영향": f"심각 보안 이벤트 {len(causes['security'])}건 · 성능관리보고서 {len(causes['capacity'])}건 · "
                f"비상 차단 {len(causes['controls'])}건(사건 전 24시간~종료)",
        "처리결과": r.get("action") or "(작성 필요)", "대책": r.get("measures") or "(작성 필요)",
    }
    m, dl = r["major"], r["deadlines"]
    cell = lambda v: str(v).replace("|", "/").replace("\n", " ")                      # noqa: E731 — 표 칸 깨짐 방지
    lines = [f"# 전자금융사고 보고서 초안 — 사건 #{iid}", "",
             "> 전자금융감독규정 시행세칙 별지 제2호 서식의 항목 순서(설계 기준 — 이 서비스는 보고 의무 대상이 아니다). "
             f"보고 대상: **{'예' if r.get('reportable') else '아니오'}**(지연·중단 30분 이상, 또는 10분 이상 + 가입자 1만 명 이상, "
             f"또는 보안 침해) · 최초 보고 기한 {_kst(dl.get('first'))}"
             + (" · **기한 초과**" if dl.get("first_overdue") else ""), "",
             "| 항목 | 내용 |", "|---|---|"]
    lines += [f"| {k} | {cell(vals[k])} |" for k in REPORT_FIELDS]
    lines += ["", f"중대 IT 장애 판정(자체 기준, 5개 지표 중 2개 이상): **{'중대' if m['major'] else '일반'}** — "
              + ", ".join(f"{k} {'●' if v else '○'}" for k, v in m["flags"].items())]
    return "\n".join(lines) + "\n"


def listing(limit: int = 50) -> list[dict]:
    rows = db.query("SELECT id FROM incident ORDER BY id DESC LIMIT :n", {"n": max(1, min(int(limit), 200))})
    return [get(r["id"]) for r in rows]


def availability(days: int = 30) -> dict:
    """가용률 — 하루별 정상 응답 비율, 기간 전체, 목표 대비, 감시 범위(1분 확인이 실제로 있었던 비율)."""
    now = clock.utc()
    since = now - timedelta(days=days)
    rows = db.query("SELECT (at AT TIME ZONE 'Asia/Seoul')::date AS d, COUNT(*) AS n, SUM(CASE WHEN ok THEN 1 ELSE 0 END) "
                    "AS ok, PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ms) AS p95, MIN(at) AS first FROM uptime_check "
                    "WHERE at >= :s GROUP BY 1 ORDER BY 1", {"s": since})
    n, ok = sum(int(r["n"]) for r in rows), sum(int(r["ok"]) for r in rows)
    ratio = round(ok / n * 100, 3) if n else None
    first = min((r["first"] for r in rows), default=None)
    expected = max(1, int((now - first).total_seconds() // 60)) if first else 0
    return {"days": days, "url": target_url() or None, "checks": n, "ok": ok, "availability": ratio,
            "target": settings.UPTIME_SLO, "met": None if ratio is None else ratio >= settings.UPTIME_SLO,
            "downtime_min": n - ok, "coverage": round(min(1.0, n / expected) * 100, 1) if expected else None,
            "daily": [{"date": str(r["d"]), "checks": int(r["n"]), "availability": round(int(r["ok"]) / int(r["n"]) * 100, 2),
                       "p95_ms": int(r["p95"] or 0)} for r in rows]}


def remind_deadlines(now=None) -> dict:
    """관제 5분 — 최초 보고 기한이 6시간 안으로 다가왔거나 지난 보고 대상 사건(아직 최초 보고 안 함)을 알린다."""
    now = now or clock.utc()
    rows = db.query("SELECT id, title, report_due FROM incident WHERE reportable AND first_reported_at IS NULL "
                    "AND final_reported_at IS NULL AND report_due IS NOT NULL AND report_due < :soon ORDER BY report_due",
                    {"soon": now + timedelta(hours=6)})
    for r in rows:
        late = r["report_due"] < now
        _notify(f"incident:due:{r['id']}:{'late' if late else 'soon'}",
                f"[보고] 사건 #{r['id']} 최초 보고 {'기한 초과' if late else '기한 6시간 이내'}",
                f"{r['title'][:120]} · 기한 {r['report_due'].astimezone(clock.KST):%m-%d %H:%M} KST · 보고 후 "
                f"POST /admin/incidents/{r['id']} {{\"reported\": \"최초\"}}", level="error" if late else "warn",
                dedup_hours=6)
    return {"due": [r["id"] for r in rows]} if rows else {}


def overview() -> dict:
    """관리 화면 — 진행 중·보고 기한 임박/초과 사건, 최근 사건, 30일 가용률."""
    items = listing(30)
    return {"open": [i for i in items if i["status"] == "open"],
            "report_overdue": [i["id"] for i in items if i["deadlines"].get("first_overdue")],
            "recent": items, "availability": availability(30), "criteria": THRESHOLDS,
            "rules": {"open_after_min": OPEN_AFTER_MIN, "report_min": REPORT_MIN, "report_min_with_users": REPORT_MIN_WITH_USERS,
                      "first_report_h": FIRST_REPORT_H, "interim_days": INTERIM_DAYS, "periodic_days": PERIODIC_DAYS}}
