"""보안 로그 외부 관제 연동 형식 — CEF(Common Event Format) · JSON Lines.

배경: 금융위원회(2026.10) 점검에서 '24시간 보안관제 공백'이 지적됐고, 금감원(2026.3)은 금융권 통합 보안관제(FIRST) 방향을 밝혔다.
서비스 안의 탐지(보안 이벤트)·감사 기록·운영 경보를 외부 관제 시스템(SIEM·상위 관제)이 바로 읽는 표준 형식으로 내보낸다.
  · CEF:0|OUTOFSTOCK|stock-backend|버전|서명ID|이름|심각도(0~10)|확장 — ArcSight 형식(대부분의 SIEM 이 읽는다)
  · JSON Lines — 같은 내용을 한 줄 JSON 으로(수집기·로그 파이프라인용)
개인정보 최소화: 아이디는 로그 줄(events.emit)과 같은 HMAC 가명(user), IP 는 이미 가명(ip_hash)만 저장돼 있다.
감사 대상: 내보내기 자체도 감사 체인에 남는다(누가·언제·몇 건).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from app import clock, db
from app.security import keys

VENDOR, PRODUCT = "OUTOFSTOCK", "stock-backend"
SEVERITY = {"low": 3, "medium": 5, "high": 8, "critical": 10, "info": 2, "warn": 6, "error": 9}
SYSTEM_ACTORS = {"system", "cli", "deploy", "scheduler", "", None}
_OBJ = re.compile(r"^[a-z_]+:[\w.:-]+$")             # change:3 · incident:5 처럼 '대상 종류:번호'는 그대로


def _pseudo(name) -> str | None:
    if name in SYSTEM_ACTORS:
        return name or None
    return "u:" + keys.hmac_hex("user", str(name))


def _target(t) -> str | None:
    if not t:
        return None
    return t if _OBJ.match(str(t)) else _pseudo(t)


def _hdr(v) -> str:
    return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _ext(v) -> str:
    return str(v).replace("\\", "\\\\").replace("=", "\\=").replace("\n", "\\n").replace("\r", "\\r")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _kst_naive(s: str) -> datetime:
    """audit_log·alert_log 의 시각(KST 문자열, 시간대 표기 없음) → 시간대 포함."""
    return datetime.fromisoformat(s).replace(tzinfo=clock.KST)


def collect(since: datetime, until: datetime | None = None, limit: int = 5000) -> list[dict]:
    """세 원천을 시간순으로 — 보안 이벤트(탐지·자동 대응) · 감사 기록(관리 행위·인증) · 운영 경보."""
    until = until or clock.utc()
    lim = max(1, min(int(limit), 20000))
    ks, ku = since.astimezone(clock.KST).replace(tzinfo=None).isoformat(timespec="seconds"), \
        until.astimezone(clock.KST).replace(tzinfo=None).isoformat(timespec="seconds")
    out: list[dict] = []
    for r in db.query("SELECT id, at, kind, severity, username, session_id, ip_hash, request_id, response FROM security_event "
                      "WHERE at >= :s AND at <= :u ORDER BY id LIMIT :n", {"s": since, "u": until, "n": lim}):
        out.append({"source": "security_event", "id": r["id"], "at": r["at"], "signature": r["kind"], "name": r["kind"],
                    "severity": SEVERITY.get(r["severity"], 3), "level": r["severity"], "user": _pseudo(r["username"]),
                    "ip_hash": r["ip_hash"], "request_id": r["request_id"], "action": r["response"],
                    "session": (r["session_id"] or "")[:8] or None})
    for r in db.query("SELECT audit_id, at, actor, action, target, ip_hash, result FROM audit_log WHERE at >= :s AND at <= :u "
                      "ORDER BY audit_id LIMIT :n", {"s": ks, "u": ku, "n": lim}):
        adm = r["action"].split(".")[0] in ("admin", "role", "rbac", "change", "incident", "control", "integrity", "alert")
        out.append({"source": "audit_log", "id": r["audit_id"], "at": _kst_naive(r["at"]), "signature": r["action"],
                    "name": r["action"], "severity": 6 if adm else (5 if r["result"] != "ok" else 2),
                    "level": "admin" if adm else "info", "user": _pseudo(r["actor"]), "ip_hash": r["ip_hash"],
                    "target": _target(r["target"]), "outcome": r["result"]})
    for r in db.query("SELECT alert_id, key, level, title, created_at, ack_by, ack_at FROM alert_log "
                      "WHERE created_at >= :s AND created_at <= :u ORDER BY alert_id LIMIT :n", {"s": ks, "u": ku, "n": lim}):
        out.append({"source": "alert_log", "id": r["alert_id"], "at": _kst_naive(r["created_at"]),
                    "signature": "alert:" + r["key"].split(":")[0], "name": r["title"], "severity": SEVERITY.get(r["level"], 5),
                    "level": r["level"], "user": _pseudo(r["ack_by"]) if r["ack_by"] else None,
                    "acked": bool(r["ack_at"])})
    out.sort(key=lambda e: (e["at"], e["source"], e["id"]))
    return out[:lim]


def to_cef(e: dict, version: str = "1") -> str:
    ext = {"rt": _ms(e["at"]), "externalId": f"{e['source']}:{e['id']}", "cat": e["source"]}
    if e.get("user"):
        ext["suser"] = e["user"]
    if e.get("ip_hash"):
        ext["cs1Label"], ext["cs1"] = "srcIpPseudonym", e["ip_hash"]
    if e.get("request_id"):
        ext["cs2Label"], ext["cs2"] = "requestId", e["request_id"]
    if e.get("target"):
        ext["cs3Label"], ext["cs3"] = "target", e["target"]
    if e.get("action"):
        ext["act"] = e["action"]
    if e.get("outcome"):
        ext["outcome"] = e["outcome"]
    if "acked" in e:
        ext["cs4Label"], ext["cs4"] = "acknowledged", "true" if e["acked"] else "false"
    body = " ".join(f"{k}={_ext(v)}" for k, v in ext.items())
    return (f"CEF:0|{_hdr(VENDOR)}|{_hdr(PRODUCT)}|{_hdr(version)}|{_hdr(e['signature'])[:100]}|"
            f"{_hdr(e['name'])[:200]}|{int(e['severity'])}|{body}")


def to_json(e: dict) -> str:
    return json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items() if v is not None},
                      ensure_ascii=False, separators=(",", ":"))


def export(hours: float = 24, fmt: str = "cef", actor: str = "cli", limit: int = 5000) -> dict:
    """최근 hours 시간의 보안 로그를 외부 관제 형식으로 — {format, count, lines}. 내보내기는 감사 기록에 남는다."""
    if fmt not in ("cef", "jsonl"):
        raise ValueError("fmt 는 cef|jsonl")
    hours = max(0.1, min(float(hours), 24 * 31))
    until = clock.utc()
    events = collect(until - timedelta(hours=hours), until, limit)
    lines = [to_cef(e) if fmt == "cef" else to_json(e) for e in events]
    from app.services import audit
    audit.record("siem.export", actor, detail={"format": fmt, "hours": hours, "count": len(lines)})
    return {"format": fmt, "hours": hours, "count": len(lines), "lines": lines}
