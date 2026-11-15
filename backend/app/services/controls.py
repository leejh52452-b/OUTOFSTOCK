"""서비스 비상 차단 — 기능별 스위치(송금·충전·꾸미기 구매·모의투자). 중대 전자금융사고의 '피해 확산 방지'.

  멈추기   보안·운영 관리자(JIT 활성·OTP 세션) 한 사람이 사유를 적고 바로 멈춘다 — 사고 때는 빨라야 한다.
           자동 차단: 원장 변조 탐지·마감 실패·FDS 차단 급증 같은 규칙이 걸리면 시스템이 멈춘다(app/security/events.py).
  다시 열기  재개 요청(사유) → **다른 관리자**가 승인해야 열린다(요청자 ≠ 승인자 — 직무 분리, 2인 통제).
           원인을 확인하지 않고 한 사람이 다시 여는 일을 막는다. 대기 중 요청은 기능마다 하나(부분 유일 인덱스), 24시간 뒤 만료.
  집행     PEP 가 동작의 control(policy.json)을 보고 멈춘 기능이면 503(code service_paused) — 조회·다른 기능은 그대로.
           상태는 워커마다 2초 캐시(바꾼 워커는 즉시 무효화) — 다른 워커에도 2초 안에 반영.
  기록     멈춤·재개 요청·승인/반려는 모두 감사 로그(해시 체인)에, 상태 변경은 보안 이벤트·운영 알림으로.
"""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import text

from app import clock, db
from app.security import pdp

CACHE_S = 2.0
REQUEST_TTL = timedelta(hours=24)
_cache = {"at": float("-inf"), "paused": {}}
_mono = time.monotonic


class ControlError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


def _features() -> dict:
    return pdp.controls()


def clear_cache() -> None:
    _cache["at"] = float("-inf")


def paused() -> dict:
    """멈춘 기능 → {reason, changed_by, changed_at} (2초 캐시). DB 를 못 읽으면 직전 값(서비스는 계속)."""
    now = _mono()
    if now - _cache["at"] > CACHE_S:
        try:
            rows = db.query("SELECT feature, reason, changed_by, changed_at FROM service_control WHERE state='paused'")
            _cache["paused"] = {r["feature"]: {"reason": r["reason"], "changed_by": r["changed_by"],
                                               "changed_at": r["changed_at"].isoformat()} for r in rows}
        except Exception:                                 # noqa: BLE001
            pass
        _cache["at"] = now
    return _cache["paused"]


def is_paused(feature: str | None) -> bool:
    return bool(feature) and feature in paused()


def message(feature: str) -> str:
    name = _features().get(feature, feature)
    return f"{name} 기능을 잠시 멈췄어요(점검 중) — 조회와 다른 기능은 그대로 쓸 수 있어요"


def _audit(action, actor, target, detail=None, conn=None):
    from app.services import audit
    audit.record(action, actor, target=target, detail=detail, conn=conn)


def pause(feature: str, reason: str, actor: str, auto: bool = False) -> dict:
    """즉시 중지 — 이미 멈췄으면 사유만 덧붙이지 않고 그대로(첫 원인을 남긴다)."""
    from app.security import events
    from app.services import alerts
    if feature not in _features():
        raise ControlError(f"없는 기능이에요({', '.join(_features())})", 404, "not_found")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ControlError("멈추는 사유를 5자 이상 적어 주세요", 400, "reason_required")
    now = clock.utc()
    with db.transaction() as conn:
        n = conn.execute(text(
            "INSERT INTO service_control(feature, state, reason, changed_by, changed_at) VALUES(:f, 'paused', :r, :a, :n) "
            "ON CONFLICT(feature) DO UPDATE SET state='paused', reason=EXCLUDED.reason, changed_by=EXCLUDED.changed_by, "
            "changed_at=EXCLUDED.changed_at WHERE service_control.state <> 'paused'"),
            {"f": feature, "r": reason[:200], "a": actor[:50], "n": now}).rowcount
        if n:
            _audit("control.pause", actor, feature, {"reason": reason[:200], "auto": auto}, conn=conn)
    clear_cache()
    if n:
        events.emit("service_paused", "high", username=None if auto else actor, respond=False,
                    feature=feature, auto=auto, reason=reason[:120])
        alerts.notify(f"control:pause:{feature}", f"[비상 차단] {_features()[feature]} 중지",
                      f"사유: {reason[:200]}\n변경: {actor}{' (자동)' if auto else ''}\n재개는 다른 관리자의 승인이 필요해요",
                      level="error", dedup_hours=0.01)
    return state()


def request_resume(feature: str, reason: str, actor: str) -> dict:
    if feature not in _features():
        raise ControlError("없는 기능이에요", 404, "not_found")
    if not is_paused_db(feature):
        raise ControlError("멈춘 기능이 아니에요", 409, "not_paused")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ControlError("재개 사유(원인 확인 결과)를 5자 이상 적어 주세요", 400, "reason_required")
    now = clock.utc()
    with db.transaction() as conn:
        conn.execute(text("UPDATE service_control_request SET status='expired', decided_at=:n "
                          "WHERE feature=:f AND status='pending' AND requested_at < :c"),
                     {"n": now, "f": feature, "c": now - REQUEST_TTL})
        rid = conn.execute(text(
            "INSERT INTO service_control_request(feature, reason, requested_by, requested_at, status) "
            "VALUES(:f, :r, :a, :n, 'pending') ON CONFLICT DO NOTHING RETURNING id"),
            {"f": feature, "r": reason[:200], "a": actor[:50], "n": now}).scalar()
        if rid is None:
            raise ControlError("이미 대기 중인 재개 요청이 있어요 — 다른 관리자의 승인을 기다려 주세요", 409, "pending_exists")
        _audit("control.resume_request", actor, feature, {"request_id": rid, "reason": reason[:200]}, conn=conn)
    return {"request_id": rid, "status": "pending", "feature": feature}


def decide(request_id: int, approve: bool, actor: str) -> dict:
    """승인/반려 — 승인자는 요청자와 달라야 한다. 승인되면 그 순간 재개."""
    from app.security import events
    from app.services import alerts
    now = clock.utc()
    with db.transaction() as conn:
        req = conn.execute(text("SELECT * FROM service_control_request WHERE id=:i FOR UPDATE"),
                           {"i": request_id}).mappings().first()
        if not req or req["status"] != "pending":
            raise ControlError("대기 중인 재개 요청이 아니에요", 404, "not_found")
        if actor == req["requested_by"]:
            raise ControlError("본인이 낸 재개 요청은 승인할 수 없어요(2인 통제)", 403, "four_eyes")
        if now - req["requested_at"] > REQUEST_TTL:
            conn.execute(text("UPDATE service_control_request SET status='expired', decided_at=:n WHERE id=:i"),
                         {"n": now, "i": request_id})
            raise ControlError("재개 요청이 만료됐어요(24시간) — 다시 요청해 주세요", 409, "expired")
        status = "approved" if approve else "rejected"
        conn.execute(text("UPDATE service_control_request SET status=:s, decided_by=:a, decided_at=:n WHERE id=:i"),
                     {"s": status, "a": actor[:50], "n": now, "i": request_id})
        if approve:
            conn.execute(text("UPDATE service_control SET state='on', reason=:r, changed_by=:a, changed_at=:n "
                              "WHERE feature=:f"),
                         {"r": f"재개 승인(요청 #{request_id}: {req['reason']})"[:200], "a": actor[:50], "n": now,
                          "f": req["feature"]})
        _audit(f"control.resume_{status}", actor, req["feature"],
               {"request_id": request_id, "requested_by": req["requested_by"]}, conn=conn)
    clear_cache()
    if approve:
        events.emit("service_resumed", "medium", username=actor, respond=False, feature=req["feature"],
                    requested_by=req["requested_by"])
        alerts.notify(f"control:resume:{req['feature']}", f"[비상 차단] {_features()[req['feature']]} 재개",
                      f"요청: {req['requested_by']} · 승인: {actor}\n사유: {req['reason']}", level="warn", dedup_hours=0.01)
    return {"request_id": request_id, "status": status, "feature": req["feature"]}


def is_paused_db(feature: str) -> bool:
    return bool(db.query("SELECT 1 FROM service_control WHERE feature=:f AND state='paused'", {"f": feature}))


def state() -> dict:
    """관리 화면 — 기능별 상태 + 대기 중 재개 요청."""
    rows = {r["feature"]: r for r in db.query("SELECT feature, state, reason, changed_by, changed_at FROM service_control")}
    pending = db.query("SELECT id, feature, reason, requested_by, requested_at FROM service_control_request "
                       "WHERE status='pending' ORDER BY id")
    out = []
    for f, name in _features().items():
        r = rows.get(f)
        out.append({"feature": f, "name": name, "state": (r or {}).get("state", "on"),
                    "reason": (r or {}).get("reason"), "changed_by": (r or {}).get("changed_by"),
                    "changed_at": r["changed_at"].isoformat() if r else None})
    return {"features": out, "pending": [{**p, "requested_at": p["requested_at"].isoformat()} for p in pending]}


def auto_pause(features: list[str], why: str) -> list[str]:
    """자동 대응 — 규칙이 걸리면 시스템이 멈춘다(사람이 확인 후 2인 승인으로 재개)."""
    done = []
    for f in features:
        if f in _features() and not is_paused_db(f):
            pause(f, f"자동 차단: {why}", "system", auto=True)
            done.append(f)
    return done
