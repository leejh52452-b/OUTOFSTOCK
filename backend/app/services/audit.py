"""감사 로그(audit trail) — 누가·언제·무엇을 했는지를 '고칠 수 없게' 남긴다(3번 정보보호).

설계
  · 해시 체인: 각 행은 직전 행의 해시(prev_hash)와 자기 내용을 합친 SHA-256(row_hash)을 가진다.
    중간 행 하나를 고치거나 지우면 그 뒤 체인이 전부 어긋나 verify() 가 '몇 번 행부터 깨졌는지'를 알려 준다.
  · 직렬화: 체인이 갈라지지 않게 쓰기는 advisory lock 으로 한 번에 하나(같은 트랜잭션 안).
  · 개인정보 최소화: IP 는 원문 대신 HMAC(서버 비밀값)으로 가명 처리 — 같은 IP 인지는 비교할 수 있지만 되돌릴 수 없다.
    비밀번호·토큰·카드번호 같은 값은 detail 에 넣지 않는다(넣어도 redact 로 한 번 더 가림).
  · DB 권한(deploy/sql/roles.sql): 앱 계정은 audit_log 에 INSERT·SELECT 만 — UPDATE·DELETE 권한 없음.
  · 보관: AUDIT_RETENTION_DAYS(기본 1095일) 지난 행은 purge() — 체인의 시작점(anchor)을 기록해 검증은 계속 가능.
    달력 3년 미만으로는 지우지 않는다: 관리 역할 부여·변경·말소 내역(감사 기록)은 최소 3년 보관
    (개인정보의 안전성 확보조치 기준 제5조③). 체인은 '앞에서부터 연속으로'만 지울 수 있어 동작별 보관은 하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import text

from app import clock, db
from app.config import settings
from app.redact import redact

log = logging.getLogger(__name__)
_LOCK = 0x6F6F6175                      # 'ooau'
GENESIS = "0" * 64


def _ip_hash(ip):
    """IP 가명값 — 용도별 하위 키(keys.subkey('ip'))로 HMAC. 보안 이벤트·접근 로그와 같은 값이라 서로 잇는다."""
    if not ip:
        return None
    from app.security import keys
    return keys.hmac_hex("ip", ip)


def _canon(at, actor, action, target, detail, ip_hash, result):
    return json.dumps([at, actor, action, target, detail, ip_hash, result], ensure_ascii=False, separators=(",", ":"))


def _hash(prev, body):
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def record(action, actor=None, target=None, detail=None, ip=None, result="ok", conn=None):
    """감사 기록 1건. 실패해도 업무 처리를 막지 않는다(로그만) — 단, conn 을 넘기면 그 트랜잭션에 포함.
    요청 안에서 부르면 IP(가명)·요청 ID 를 문맥에서 채운다 — 접근 로그·보안 이벤트와 같은 요청 ID 로 잇는다."""
    from app.security import context
    ctx = context.current()
    ip = ip or ctx.ip or None
    if not ctx.request_id.startswith("local-"):
        detail = dict(detail or {}, rid=ctx.request_id) if isinstance(detail, dict) or detail is None else detail
    detail_s = redact(json.dumps(detail, ensure_ascii=False, default=str))[:1000] if detail is not None else None

    def write(c):
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})
        prev = c.execute(text("SELECT row_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1")).scalar() or \
            (db.get_meta("audit_anchor") or GENESIS)
        at = clock.iso_now()
        a = (actor or "")[:50] or None
        t = (str(target)[:100] if target is not None else None)
        iph = _ip_hash(ip)
        h = _hash(prev, _canon(at, a, action, t, detail_s, iph, result))
        c.execute(text("INSERT INTO audit_log(at, actor, action, target, detail, ip_hash, result, prev_hash, row_hash) "
                       "VALUES(:at, :a, :ac, :t, :d, :ip, :r, :p, :h)"),
                  {"at": at, "a": a, "ac": action[:40], "t": t, "d": detail_s, "ip": iph, "r": result[:10],
                   "p": prev, "h": h})
    try:
        if conn is not None:
            write(conn)
        else:
            with db.transaction() as c:
                write(c)
    except Exception as e:                               # noqa: BLE001 — 감사 기록 실패가 로그인·결제를 막지 않게
        log.error("감사 로그 기록 실패(%s): %s", action, type(e).__name__)


def verify(limit=None):
    """체인 검증 → {ok, rows, broken_at(처음 어긋난 audit_id 또는 None)}."""
    prev = db.get_meta("audit_anchor") or GENESIS
    rows = db.query("SELECT audit_id, at, actor, action, target, detail, ip_hash, result, prev_hash, row_hash "
                    "FROM audit_log ORDER BY audit_id" + (" LIMIT :n" if limit else ""), {"n": limit} if limit else {})
    for r in rows:
        body = _canon(r["at"], r["actor"], r["action"], r["target"], r["detail"], r["ip_hash"], r["result"])
        if r["prev_hash"] != prev or _hash(prev, body) != r["row_hash"]:
            return {"ok": False, "rows": len(rows), "broken_at": r["audit_id"]}
        prev = r["row_hash"]
    return {"ok": True, "rows": len(rows), "broken_at": None}


def witness(note="daily"):
    """외부 증인 — 체인 머리(마지막 행 ID·해시·행 수)를 DB 밖(AUDIT_WITNESS_FILE, 운영 알림 채널)에 남긴다.
    DB 소유자 권한으로 체인 전체를 다시 계산해 바꿔치기해도, 밖에 남은 머리 해시와 어긋나 드러난다.
    마스터 키가 있으면 HMAC 서명을 붙여 증인 파일 자체의 위조도 가려낸다."""
    import hmac as _hmac
    from app.security import keys
    last = db.query("SELECT audit_id, row_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1")
    rows = db.query("SELECT COUNT(*) AS n FROM audit_log")[0]["n"]
    rec = {"at": clock.utc().isoformat(timespec="seconds"), "last_id": last[0]["audit_id"] if last else 0,
           "head": last[0]["row_hash"] if last else (db.get_meta("audit_anchor") or GENESIS), "rows": int(rows),
           "note": note}
    sk = keys.subkey("audit-witness")
    if sk:
        rec["kid"], rec["sig"] = sk[0], _hmac.new(sk[1], _canon_w(rec).encode(), hashlib.sha256).hexdigest()
    path = settings.AUDIT_WITNESS_FILE
    if path:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    db.set_meta("audit_witness_last", json.dumps(rec, ensure_ascii=False))
    from app.services import alerts
    alerts.notify(f"audit:witness:{rec['at'][:10]}", "[감사] 체인 머리 기록",
                  f"last_id={rec['last_id']} rows={rec['rows']}\nhead={rec['head']}", level="info", dedup_hours=20)
    return rec


def _canon_w(rec):
    return json.dumps([rec["at"], rec["last_id"], rec["head"], rec["rows"], rec.get("note")], separators=(",", ":"))


def verify_witnesses(path=None):
    """증인 파일의 각 기록이 지금 DB 의 체인과 맞는지 — 그 시점 마지막 행의 해시가 같아야 한다(보관 기간으로 지운 행은 건너뜀).
    서명이 있으면 서명도 확인. 반환 {ok, checked, mismatches[]}."""
    import hmac as _hmac
    from app.security import keys
    path = path or settings.AUDIT_WITNESS_FILE
    if not path:
        return {"ok": True, "checked": 0, "mismatches": [], "note": "AUDIT_WITNESS_FILE 미설정"}
    import os
    if not os.path.exists(path):
        return {"ok": True, "checked": 0, "mismatches": [], "note": "증인 파일 없음"}
    bad, n = [], 0
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        n += 1
        if rec.get("sig"):
            sk = keys.subkey("audit-witness", rec.get("kid"))
            if not sk or not _hmac.compare_digest(
                    _hmac.new(sk[1], _canon_w(rec).encode(), hashlib.sha256).hexdigest(), rec["sig"]):
                bad.append({"at": rec["at"], "reason": "signature"})
                continue
        if not rec["last_id"]:
            continue
        row = db.query("SELECT row_hash FROM audit_log WHERE audit_id=:i", {"i": rec["last_id"]})
        if row and row[0]["row_hash"] != rec["head"]:
            bad.append({"at": rec["at"], "last_id": rec["last_id"], "reason": "head_mismatch"})
    return {"ok": not bad, "checked": n, "mismatches": bad}


def recent(limit=50, actor=None, action=None):
    q, p = "SELECT audit_id, at, actor, action, target, detail, result FROM audit_log WHERE 1=1", {}
    if actor:
        q += " AND actor = :a"
        p["a"] = actor
    if action:
        q += " AND action = :ac"
        p["ac"] = action
    p["n"] = max(1, min(int(limit), 500))
    return db.query(q + " ORDER BY audit_id DESC LIMIT :n", p)


ROLE_HISTORY_MIN_YEARS = 3          # 권한 부여·변경·말소 내역 최소 보관(안전성 확보조치 기준 제5조③) — 설정이 더 짧아도 이만큼은(달력 3년)


def purge(days=None):
    """보관 기간이 지난 행 삭제 — 남은 첫 행의 prev_hash 를 새 시작점(anchor)으로 기록해 검증을 이어 간다.
    (DB 권한 분리 시 이 작업은 소유자 계정으로만 — 앱 계정엔 DELETE 권한이 없다)
    보관 기간은 달력 3년(ROLE_HISTORY_MIN_YEARS)보다 짧아지지 않는다 — 법정 최소 보관이 데이터 최소화보다 앞선다.
    (예전엔 1095일 — 윤년이 낀 3년은 1096일이라 하루 일찍 지울 수 있었다)"""
    from app.services.privacy import retention_cut
    cut = retention_cut(clock.now(), int(days or settings.AUDIT_RETENTION_DAYS),
                        ROLE_HISTORY_MIN_YEARS).isoformat(timespec="seconds")
    with db.transaction() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})
        last_deleted = c.execute(text("SELECT row_hash FROM audit_log WHERE at < :c ORDER BY audit_id DESC LIMIT 1"),
                                 {"c": cut}).scalar()
        n = c.execute(text("DELETE FROM audit_log WHERE at < :c"), {"c": cut}).rowcount or 0
        first = c.execute(text("SELECT prev_hash FROM audit_log ORDER BY audit_id LIMIT 1")).scalar()
        anchor = first or last_deleted                   # 남은 첫 행의 앞 해시(없으면 지운 마지막 행) = 새 시작점
        if n and anchor:
            db.set_meta("audit_anchor", anchor, c)
    return n
