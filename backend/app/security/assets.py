"""자산 목록·중요도 — 자율보안 첫 단계 '자산 식별·중요도 평가'를 코드에서 만든다(손으로 적는 목록은 금방 낡는다).

자산 5종과 원천
  데이터       분류 등록부(classification.REGISTRY) — 모든 표. 새 표는 분류부터(시험이 강제)
  업무 서비스  접근 정책(policy.json) — 모든 동작과 중요도(critical·normal·low)
  외부 연동    나가는 연결 허용 목록(egress.ALLOWED) ↔ 제3자 등록부(third_party_register.md §1·§1-1)
  소프트웨어   해시 고정 잠금 파일(requirements.lock)
  구성요소     배포 구성(경계·앱·배치·실시간·DB·프록시·백업·관리 단말)과 신뢰 구역(DMZ·내부·제한)

데이터 중요도 = 기밀성 + 무결성 + 가용성(각 1~3점, 3~9)
  8~9 → 1등급(핵심) · 6~7 → 2등급(중요) · 3~5 → 3등급(일반)
  기밀성: 분류 등급(L1 1 · L2 2 · L3·L4 3)
  무결성: 돈·인증·증거(추가만) 표 3 · 운영 기록 1 · 그 밖 2
  가용성: 로그인·돈이 오가는 길에 있는 표 3 · 운영 기록 1 · 그 밖 2
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER_PATH = ROOT / "docs" / "security" / "third_party_register.md"

# 무결성·가용성 판단에 쓰는 표 묶음 — 이름이 등록부에 없으면 시험이 실패한다(오타·폐기된 표가 남지 않게)
MONEY = frozenset({"wallet", "wallet_ledger", "journal_entry", "ledger_close", "payment_order", "iap_purchase",
                   "invest_position", "attendance", "avatar_closet", "fds_decision", "outbox",
                   "vmarket_entry", "vmarket_position", "vmarket_trade"})
AUTH = frozenset({"app_user", "auth_session", "auth_token", "auth_refresh_used", "user_mfa", "user_role",
                  "role_activation", "role_request", "dpop_jti", "rate_event"})
EVIDENCE = frozenset({"audit_log", "security_event", "pii_access_log", "service_control", "service_control_request",
                      "security_assessment", "integrity_baseline", "change_record", "incident"})
OPS_LOW = frozenset({"search_log", "pipeline_run", "alert_log", "dq_result", "data_quality_issue", "capacity_report",
                     "uptime_check"})
ONLINE = AUTH | frozenset({"wallet", "wallet_ledger", "journal_entry", "payment_order", "iap_purchase", "service_control"})

GRADES = {1: "1등급(핵심)", 2: "2등급(중요)", 3: "3등급(일반)"}
ZONES = {"외부": "인터넷·이용자", "DMZ": "경계 — 바깥과 맞닿는 구성", "내부": "앱·배치(사설망/루프백)",
         "제한": "데이터·백업(앱 계정만, 평문 TCP 거부)", "관리": "운영자 단말(SSH 키·기기 인증서)"}

# 구성요소 — (키, 설명, 신뢰 구역, (기밀성, 무결성, 가용성))
COMPONENTS: tuple[tuple[str, str, str, tuple[int, int, int]], ...] = (
    ("edge", "nginx 경계(TLS·요청률·본문 상한·관리 경로 mTLS)", "DMZ", (2, 3, 3)),
    ("egress", "egress 프록시(squid — 허용 목록·내부 주소 거부)", "DMZ", (1, 3, 2)),
    ("app", "API·웹 화면(FastAPI/uvicorn — PEP·PDP·세션)", "내부", (3, 3, 3)),
    ("scheduler", "배치 스케줄러(적재·원장 마감·보안 일일 작업)", "내부", (2, 3, 2)),
    ("realtime", "실시간 시세 수집기(KIS·Finnhub 웹소켓)", "내부", (1, 2, 2)),
    ("db", "PostgreSQL 16(FORCE RLS·복식부기·감사 체인)", "제한", (3, 3, 3)),
    ("backup", "백업(age 암호화·원격 사본·WAL)", "제한", (3, 3, 2)),
    ("admin", "관리 단말(SSH 키·관리자 대역·기기 인증서·TOTP)", "관리", (3, 3, 1)),
)


def grade(c: int, i: int, a: int) -> int:
    s = c + i + a
    return 1 if s >= 8 else (2 if s >= 6 else 3)


def data_cia(name: str, table) -> tuple[int, int, int]:
    has_l4 = any(col.level == "L4" for col in (table.columns or {}).values())
    c = {"L1": 1, "L2": 2, "L3": 3, "L4": 3}[table.level]
    if has_l4:
        c = 3
    i = 3 if (name in MONEY | AUTH | EVIDENCE or "append_only" in table.protection) else (1 if name in OPS_LOW else 2)
    a = 3 if name in ONLINE else (1 if name in OPS_LOW else 2)
    return c, i, a


def data_assets() -> list[dict]:
    from app.security import classification
    out = []
    for name, t in classification.REGISTRY.items():
        c, i, a = data_cia(name, t)
        out.append({"name": name, "class": t.level, "desc": t.desc, "c": c, "i": i, "a": a, "score": c + i + a,
                    "grade": grade(c, i, a), "protection": list(t.protection), "retention": t.retention})
    return sorted(out, key=lambda r: (r["grade"], -r["score"], r["name"]))


def service_assets() -> list[dict]:
    from app.security import pdp
    order = {"critical": 1, "normal": 2, "low": 3}
    out = [{"action": k, "tier": v.get("tier", "normal"), "grade": order.get(v.get("tier", "normal"), 2),
            "control": v.get("control"), "device_bound": v.get("bind") == "device", "desc": v.get("desc", "")}
           for k, v in pdp.load()["actions"].items()]
    return sorted(out, key=lambda r: (r["grade"], r["action"]))


# ── 제3자 등록부(마크다운 표) 읽기 ─────────────────────────────────────────
def _md_table(text: str, heading: str) -> list[list[str]]:
    """heading 으로 시작하는 줄 아래의 첫 표 → 행(머리·구분선 제외) 목록."""
    lines = text.splitlines()
    try:
        k = next(n for n, line in enumerate(lines) if line.startswith(heading))
    except StopIteration:
        return []
    rows, started = [], False
    for line in lines[k + 1:]:
        if line.startswith("|"):
            started = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        elif started:
            break
        elif line.startswith("#"):
            break
    return [r for r in rows[2:]] if len(rows) >= 2 else []


SUPPLIER_COLUMNS = ("제3자", "용도", "보내는 것", "중요도", "끊기면", "대체·완화", "빠져나오기")


def suppliers(text: str | None = None) -> dict:
    """§1 공급사 표 + §1-1 호스트 표."""
    text = text if text is not None else REGISTER_PATH.read_text(encoding="utf-8")
    rows = [dict(zip(SUPPLIER_COLUMNS, r)) for r in _md_table(text, "## 1. 외부 데이터·서비스")]
    hosts = {r[0]: r[1] for r in _md_table(text, "### 1-1.") if len(r) >= 2}
    return {"suppliers": rows, "hosts": hosts}


def supplier_problems(text: str | None = None) -> list[str]:
    """선정 기준(S1~S4) 칸이 빈 공급사 · 허용 목록과 어긋난 호스트 · 표에 없는 공급사를 가리키는 호스트."""
    from app.security import egress
    reg = suppliers(text)
    out = []
    names = [r.get("제3자", "") for r in reg["suppliers"]]
    for r in reg["suppliers"]:
        for col in SUPPLIER_COLUMNS[2:]:
            v = (r.get(col) or "").strip()
            if not v or v in ("—", "-"):
                out.append(f"{r.get('제3자')}: '{col}' 빈 칸(선정 기준)")
    allowed, listed = set(egress.ALLOWED), set(reg["hosts"])
    out += [f"허용 목록에만 있음(등록부 누락): {h}" for h in sorted(allowed - listed)]
    out += [f"등록부에만 있음(허용 목록에 없음): {h}" for h in sorted(listed - allowed)]
    for h, who in reg["hosts"].items():
        if not any(n.startswith(who) for n in names):
            out.append(f"{h}: 등록부 §1 에 없는 공급사 '{who}'")
    return out


def external_assets() -> list[dict]:
    from app.security import egress
    reg = suppliers()
    by_name = {r["제3자"]: r for r in reg["suppliers"]}
    out = []
    for h, purpose in sorted(egress.ALLOWED.items()):
        who = reg["hosts"].get(h)
        row = next((r for n, r in by_name.items() if who and n.startswith(who)), None)
        level = (row or {}).get("중요도", "")
        g = 1 if level.startswith("높음") else (2 if level.startswith("중간") else 3)
        out.append({"host": h, "purpose": purpose, "supplier": who, "importance": level.split(" ")[0] if level else None,
                    "grade": g if row else None})
    return out


def software_assets() -> dict:
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.\-\[\]]+)==([^\s\\]+)", lock, re.M)
    hashed = len(re.findall(r"--hash=sha256:", lock))
    direct = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line.strip())
        if m and not line.strip().startswith("#"):
            direct.append(m.group(1))
    return {"packages": len(pins), "hashes": hashed, "direct": direct, "pinned": sorted(n for n, _ in pins)}


def component_assets() -> list[dict]:
    return [{"key": k, "desc": d, "zone": z, "c": c, "i": i, "a": a, "grade": grade(c, i, a)}
            for k, d, z, (c, i, a) in COMPONENTS]


def inventory() -> dict:
    data, svc, ext, sw, comp = data_assets(), service_assets(), external_assets(), software_assets(), component_assets()

    def count(rows):
        return {GRADES[g]: sum(1 for r in rows if r.get("grade") == g) for g in GRADES}
    return {"summary": {"data": {"total": len(data), **count(data)}, "services": {"total": len(svc), **count(svc)},
                        "external": {"total": len(ext), **count(ext)}, "software": {"packages": sw["packages"]},
                        "components": {"total": len(comp), **count(comp)}},
            "data": data, "services": svc, "external": ext, "software": sw, "components": comp}


def drift() -> dict:
    """운영 DB 의 실제 표 ↔ 분류 등록부 — 분류되지 않은 표(새 자산이 목록 밖)·등록부에만 있는 표(스키마 불일치)."""
    from app import db
    from app.security import classification
    rows = db.query("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition")
    actual, known = {r["relname"] for r in rows}, set(classification.REGISTRY)
    return {"unclassified": sorted(actual - known), "missing": sorted(known - actual), "tables": len(actual)}
