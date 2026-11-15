"""보안 이벤트 · 탐지 규칙 · 자동 대응 — 제로트러스트의 '가시성·분석'과 '자동화·대응'.

emit(kind, severity, …) 한 번으로
  1. security_event 표에 남기고(요청 ID·IP 가명값 포함 — 로그·감사 기록과 같은 ID 로 이어진다)
  2. 'security' 로거로 구조화 로그를 내보내고(수집기·SIEM 연동 지점)
  3. 규칙(RULES)을 평가해 문턱을 넘으면 자동 대응한다 — 세션 폐기·계정 잠금·IP 차단·운영 알림.
     대응 결과는 이벤트 행(response)과 감사 로그(security.auto_response)에 함께 남는다.

규칙은 '같은 주체가 짧은 시간에 반복'을 본다(단건으로는 오탐이 많다). 대응은 되돌릴 수 있는 것만 자동으로 —
세션 폐기(다시 로그인하면 됨)·일시 잠금(시간이 지나면 풀림)·일시 IP 차단. 영구 조치는 사람이 판단한다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta


from app import clock, db
from app.security import context, keys

log = logging.getLogger("security")
SEVERITIES = ("low", "medium", "high", "critical")
_mono = time.monotonic             # 테스트에서 바꿔 끼운다


@dataclass(frozen=True)
class Rule:
    kind: str                 # 대상 이벤트
    scope: str                # 묶는 기준: username | session_id | ip_hash | none
    threshold: int            # 창 안에서 이 횟수 이상이면
    window_s: int
    response: str             # revoke_session | revoke_user | lock_account | block_ip | alert | pause:<기능,…>
    severity: str             # 대응 시 올릴 경보 수준
    title: str


RULES: tuple[Rule, ...] = (
    Rule("refresh_reuse", "username", 1, 3600, "revoke_user", "critical", "갱신 토큰 재사용(토큰 탈취 의심) — 전 세션 종료"),
    Rule("device_mismatch", "username", 1, 3600, "alert", "high", "다른 기기에서 같은 세션 토큰 사용"),
    Rule("csrf_fail", "session_id", 5, 600, "revoke_session", "medium", "CSRF 검증 반복 실패 — 세션 종료"),
    Rule("authz_deny", "session_id", 20, 300, "revoke_session", "medium", "권한 없는 자원 접근 반복(IDOR 탐색 의심)"),
    Rule("step_up_fail", "username", 5, 900, "lock_account", "high", "추가 인증 반복 실패 — 계정 일시 잠금"),
    Rule("login_fail", "ip_hash", 30, 600, "block_ip", "high", "한 IP 에서 로그인 실패 급증(크리덴셜 스터핑 의심)"),
    Rule("pii_access", "username", 50, 86400, "alert", "high", "관리자 개인정보 열람 급증(내부자 위협 점검)"),
    Rule("mfa_fail", "username", 5, 900, "lock_account", "high", "OTP 반복 실패 — 계정 일시 잠금"),
    Rule("csp_violation", "none", 20, 600, "alert", "medium", "CSP·신뢰 타입 위반 급증 — XSS 시도 또는 깨진 배포 점검"),
    # 피해 확산 방지(중대 전자금융사고 대응) — 돈의 정합성이 깨졌거나 이상 송금이 몰리면 그 기능을 자동으로 멈춘다.
    # 다시 여는 것은 사람이 원인을 확인한 뒤 2인 승인(app/services/controls.py)
    Rule("ledger_tamper", "none", 1, 86400, "pause:transfer,charge,shop,invest", "critical",
         "원장 변조 탐지 — 돈이 오가는 기능 자동 중지"),
    Rule("ledger_close_fail", "none", 1, 86400, "pause:transfer,charge,shop,invest", "critical",
         "원장 마감 실패(대사 불일치) — 돈이 오가는 기능 자동 중지"),
    Rule("fds_block", "none", 10, 600, "pause:transfer", "critical", "FDS 차단 급증(10분 10건) — 송금 자동 중지"),
    Rule("dpop_mismatch", "username", 1, 3600, "revoke_user", "critical",
         "다른 기기 키로 세션 사용(쿠키 탈취 의심) — 전 세션 종료"),
    # 배포 기준선과 다른 코드·화면 파일(변조·웹셸 의심) — 사람이 즉시 확인(app/security/integrity.py — 같은 위반은 한 번만 낸다)
    Rule("file_integrity", "none", 1, 3600, "alert", "critical", "배포 기준선과 다른 코드·화면 파일(변조·웹셸 의심) — 즉시 확인"),
)
IP_BLOCK_SECONDS = 900


def ip_hash(ip: str | None) -> str | None:
    return keys.hmac_hex("ip", ip) if ip else None


def emit(kind: str, severity: str = "low", username: str | None = None, session_id: str | None = None,
         respond: bool = True, **detail) -> int | None:
    """보안 이벤트 1건. 실패해도 업무 요청을 막지 않는다(로그만)."""
    ctx = context.current()
    sev = severity if severity in SEVERITIES else "low"
    iph = detail.pop("ip_hash", None) or ip_hash(ctx.ip)
    sid = session_id or ctx.principal.session_id
    payload = {k: v for k, v in detail.items() if v is not None}
    if ctx.route or ctx.path:
        payload.setdefault("path", ctx.route or ctx.path)
    try:
        eid = db.write_query("INSERT INTO security_event(at, kind, severity, username, session_id, ip_hash, request_id, detail) "
                       "VALUES(:at, :k, :s, :u, :sid, :ip, :rid, :d) RETURNING id",
                       {"at": clock.utc(), "k": kind[:40], "s": sev, "u": (username or None) and username[:50],
                        "sid": sid, "ip": iph, "rid": ctx.request_id[:64],
                        "d": json.dumps(payload, ensure_ascii=False, default=str)[:2000]})[0]["id"]
    except Exception as e:                                # noqa: BLE001
        log.error("보안 이벤트 기록 실패 %s: %s", kind, type(e).__name__)
        return None
    log.log(logging.WARNING if sev in ("high", "critical") else logging.INFO,
            json.dumps({"event": kind, "severity": sev, "user": keys.hmac_hex("user", username) if username else None,
                        "session": sid[:8] if sid else None, "ip": iph, "rid": ctx.request_id}, ensure_ascii=False))
    if respond:
        for rule in RULES:
            if rule.kind == kind:
                _evaluate(rule, eid, username, sid, iph)
    return eid


def _evaluate(rule: Rule, eid: int, username, session_id, iph):
    key = {"username": username, "session_id": session_id, "ip_hash": iph}.get(rule.scope)
    if rule.scope != "none" and not key:
        return
    since = clock.utc() - timedelta(seconds=rule.window_s)
    q = "SELECT COUNT(*) AS n FROM security_event WHERE kind=:k AND at >= :t"
    p = {"k": rule.kind, "t": since}
    if rule.scope != "none":
        q += f" AND {rule.scope} = :v"
        p["v"] = key
    n = db.query(q, p)[0]["n"]
    if n < rule.threshold:
        return
    # 같은 대상에 같은 대응을 창 안에서 이미 했으면 반복하지 않는다(알림 폭주·잠금 연장 방지)
    already = db.query("SELECT 1 FROM security_event WHERE kind=:k AND at >= :t AND response IS NOT NULL"
                       + (f" AND {rule.scope} = :v" if rule.scope != "none" else "") + " LIMIT 1", p)
    if already and rule.threshold > 1:
        return
    result = _respond(rule, username, session_id, iph)
    db.execute("UPDATE security_event SET response=:r WHERE id=:id", {"r": result[:60], "id": eid})


def _respond(rule: Rule, username, session_id, iph) -> str:
    from app.security import sessions                   # 순환 import 회피
    from app.services import alerts, audit
    done = []
    try:
        if rule.response == "revoke_session" and session_id:
            sessions.revoke(session_id, f"auto:{rule.kind}")
            done.append("revoke_session")
        elif rule.response == "revoke_user" and username:
            n = sessions.revoke_user(username, f"auto:{rule.kind}")
            done.append(f"revoke_user({n})")
        elif rule.response == "lock_account" and username:
            lock_account(username, f"auto:{rule.kind}")
            done.append("lock_account")
        elif rule.response == "block_ip" and iph:
            block_ip(iph, rule.kind, IP_BLOCK_SECONDS)
            done.append("block_ip")
        elif rule.response.startswith("pause:"):
            from app.services import controls
            stopped = controls.auto_pause(rule.response[6:].split(","), rule.title)
            done.append(f"pause({'+'.join(stopped) or '이미 중지'})")
    except Exception as e:                                # noqa: BLE001
        log.error("자동 대응 실패 %s: %s", rule.kind, type(e).__name__)
        done.append("failed")
    if rule.severity in ("high", "critical") or rule.response == "alert":
        alerts.notify(f"sec:{rule.kind}:{username or session_id or iph}", f"[보안] {rule.title}",
                      f"대상: {'사용자 가명 ' + keys.hmac_hex('user', username) if username else '세션/IP'}\n"
                      f"조치: {', '.join(done) or '알림만'}\n요청 ID: {context.request_id() or '-'}",
                      level="error" if rule.severity in ("high", "critical") else "warn")
        done.append("alert")
    audit.record("security.auto_response", actor="system", target=rule.kind,
                 detail={"response": done, "scope": rule.scope})
    return ",".join(done)


def lock_account(username: str, reason: str, seconds: int | None = None) -> None:
    """일시 잠금 + 전 세션 종료. 잠금 시간은 같은 계정이 반복될수록 두 배(최대 24시간)."""
    from app.config import settings
    from app.security import sessions
    base = seconds or settings.ACCOUNT_LOCK_SECONDS
    prior = db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='account_locked' AND username=:u "
                     "AND at >= :t", {"u": username, "t": clock.utc() - timedelta(days=1)})[0]["n"]
    secs = min(base * (2 ** int(prior)), 86400)
    db.execute("UPDATE app_user SET locked_until=:t WHERE username=:u",
               {"t": clock.utc() + timedelta(seconds=secs), "u": username})
    sessions.revoke_user(username, reason)
    emit("account_locked", "medium", username=username, respond=False, reason=reason, seconds=secs)


# ── 일시 IP 차단(자동 대응) — PEP 가 요청 처음에 확인(30초 캐시) ─────────────────────
_block_cache: dict[str, float] = {}
_NEVER = float("-inf")             # '읽은 적 없음' — 0.0 이면 부팅 30초 안에는 monotonic 이 작아 목록을 안 읽는다(결함 19)
_block_loaded = [_NEVER]


def block_ip(iph: str, reason: str, seconds: int) -> None:
    until = clock.utc() + timedelta(seconds=seconds)
    db.execute("INSERT INTO app_meta(key, value) VALUES(:k, :v) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
               {"k": f"ipblock:{iph}", "v": json.dumps({"until": until.isoformat(), "reason": reason})})
    _block_loaded[0] = _NEVER                             # 캐시 즉시 무효화


def unblock_ip(iph: str, actor: str = "system") -> bool:
    """일시 IP 차단 해제(관리자·훈련 복구) — 감사 기록. 반환: 차단이 있었는가."""
    from app.services import audit
    n = db.execute("DELETE FROM app_meta WHERE key = :k", {"k": f"ipblock:{iph}"}) or 0
    _block_cache.pop(iph, None)
    _block_loaded[0] = _NEVER
    if n:
        audit.record("security.ip_unblock", actor, target=f"ip:{iph[:16]}")
    return bool(n)


def blocked(iph: str | None) -> bool:
    if not iph:
        return False
    now = _mono()
    if now - _block_loaded[0] > 30:
        cache = {}
        try:
            for r in db.query("SELECT key, value FROM app_meta WHERE key LIKE 'ipblock:%'"):
                v = json.loads(r["value"])
                cache[r["key"][8:]] = datetime.fromisoformat(v["until"]).timestamp()
        except Exception:                                 # noqa: BLE001 — 차단 목록을 못 읽어도 서비스는 계속
            cache = dict(_block_cache)
        _block_cache.clear()
        _block_cache.update(cache)
        _block_loaded[0] = now
    until = _block_cache.get(iph)
    return bool(until and until > clock.utc().timestamp())


def clear_cache() -> None:
    _block_cache.clear()
    _block_loaded[0] = _NEVER


def recent(limit: int = 100, kind: str | None = None, severity: str | None = None) -> list[dict]:
    q, p = ("SELECT id, at, kind, severity, username, session_id, ip_hash, request_id, detail, response "
            "FROM security_event WHERE 1=1"), {}
    if kind:
        q += " AND kind=:k"
        p["k"] = kind
    if severity in SEVERITIES:
        q += " AND severity = ANY(:sv)"
        p["sv"] = list(SEVERITIES[SEVERITIES.index(severity):])
    p["n"] = max(1, min(int(limit), 500))
    rows = db.query(q + " ORDER BY id DESC LIMIT :n", p)
    for r in rows:
        r["at"] = r["at"].isoformat()
        r["session_id"] = (r["session_id"] or "")[:8] or None    # 세션 ID 는 앞부분만(재사용 불가 값이지만 최소 노출)
    return rows


def digest(hours: int = 24, notify: bool = True) -> dict:
    """일일 보안 요약 — 종류·심각도별 건수, 자동 대응 건수, 재검토가 다가온 역할 자격.
    high/critical 이 있으면 운영 알림(같은 날 중복 없이). 스케줄러가 매일 아침 실행."""
    from app.services import alerts
    since = clock.utc() - timedelta(hours=hours)
    rows = db.query("SELECT kind, severity, COUNT(*) AS n, COUNT(response) AS responded FROM security_event "
                    "WHERE at >= :t GROUP BY kind, severity ORDER BY n DESC", {"t": since})
    due = db.query("SELECT username, role, expires_at FROM user_role WHERE expires_at IS NOT NULL "
                   "AND expires_at < now() + interval '14 days' ORDER BY expires_at")
    serious = sum(r["n"] for r in rows if r["severity"] in ("high", "critical"))
    out = {"hours": hours, "by_kind": rows, "serious": serious, "auto_responses": sum(r["responded"] for r in rows),
           "recertify_due": [{"username": d["username"], "role": d["role"], "expires_at": d["expires_at"].isoformat()}
                             for d in due]}
    if notify and (serious or due):
        body = "\n".join(f"· {r['kind']}({r['severity']}) {r['n']}건" for r in rows[:15])
        if due:
            body += f"\n역할 자격 재검토 필요 {len(due)}건(14일 안 만료)"
        alerts.notify(f"sec:digest:{clock.today_str()}", f"[보안] 일일 요약 — 심각 {serious}건",
                      body, level="error" if serious else "warn", dedup_hours=20)
    return out
