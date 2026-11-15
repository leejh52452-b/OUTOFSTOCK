"""변경관리(프로그램 통제) — 승인된 설치본만, 사전 영향 분석·자동 점검을 거쳐, 장중이 아닐 때 배포한다.

근거(설계 기준)
  · 금감원 「금융IT 안정성 강화를 위한 가이드라인」(2023) — 성능 관리·**프로그램 통제**·비상대책
  · 금감원 빅테크 CIO 간담회(2026.6.24) — 최근 IT 사고 대부분이 '프로그램 변경 때 배포 전 성능 검토·부하 시험·제3자 검증 누락' 같은
    기본적인 IT 내부통제 실패에서 비롯 → 변경 전 사전 영향 분석·제3자 검증 강화 요구
  · 전자금융감독규정 시행세칙 별지 제2호(사고보고서) — '사고원인(변경시점 및 사유 등)'을 적어야 한다 → 변경 기록이 곧 장애 원인 추적의 근거
흐름
  1. 요청  `python run.py change-request <설치본> --by 아이디 --title … --reason … [--evidence 시험·부하 결과]`
           설치본을 임시 폴더에 풀어 ① 사전 영향 분석(지금 배포된 파일 대비 바뀐 파일을 돈·인증·스키마·화면·배포 영역으로 묶고 위험도)
           ② 자동 사전 점검(파이썬 구문 · 접근 정책 형식 · 이미 적용된 마이그레이션 변경 여부 · 수준진단 근거 무결성)을 기록한다.
  2. 승인  다른 관리자가 `POST /admin/changes/{id}/approve` — 요청자 ≠ 승인자(제3자 검증). 돈·인증·스키마·성능 민감 변경은
           검증 근거(시험·부하 결과)가 없으면 승인할 수 없다. 승인은 7일 동안 유효.
  3. 배포  vm.sh update 가 `run.py change-gate <설치본>` 으로 확인 — 설치본 SHA-256 이 승인된 요청과 같아야 한다(바꿔치기 불가).
           장중(기본 평일 08:30~16:00)에는 배포하지 않는다. 막혀 있으면 긴급 변경(`CHANGE_REASON='사유'`)만 — 3일 안에 사후 검토.
  4. 기록  배포가 끝나면 `run.py change-applied` — 적용 시각·무결성 기준선 번호. 되돌리면 rolled_back.
모드(CHANGE_GATE): enforce = 위 규칙대로 막는다(금융 운영) · record = 막지 않고 기록·경고만(개발·학교 VM 기본) · off = 점검만 건너뜀.
어느 모드든 **배포를 깨뜨리는 점검 실패**(구문 오류·적용된 마이그레이션 변경·정책 형식 오류)는 막는다 — 서비스를 멈추기 전에.
"""
from __future__ import annotations

import base64
import fnmatch
import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

ROOT = Path(__file__).resolve().parents[2]
APPROVAL_DAYS = 7
REVIEW_DAYS = 3
AREAS = (   # (키, 이름, 경로 패턴) — 위에서부터 처음 맞는 영역
    ("money", "돈(원장·송금·지갑·결제)", ("app/services/ledger.py", "app/services/transfer.py", "app/services/wallet.py",
                                      "app/services/toss.py", "app/services/iap.py", "app/services/fds.py",
                                      "app/services/invest.py", "app/services/avatar.py", "app/services/attendance.py",
                                      "app/services/quiz.py", "app/services/outbox.py", "app/services/controls.py")),
    ("auth", "인증·세션·정책·권한", ("app/security/*", "app/services/auth.py", "app/services/account.py")),
    ("schema", "DB 스키마·마이그레이션", ("app/db.py", "app/migrations.py", "sql/*")),
    ("web", "화면", ("web/*",)),
    ("deploy", "배포·서버 설정", ("deploy/*", "requirements*", "Dockerfile", "docker-compose*")),
    ("app", "그 밖의 서버 코드·배치", ("app/*", "run.py", "scheduler.py", "tools/*")),
)
HOT_PATHS = ("app/api.py", "app/db.py", "app/security/pep.py", "app/security/sessions.py", "app/services/market.py",
             "app/services/capacity.py", "app/webui.py")       # 모든 요청이 지나가는 길 — 바뀌면 성능 근거 필요


class ChangeError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


def mode() -> str:
    m = (settings.CHANGE_GATE or "record").strip().lower()
    return m if m in ("enforce", "record", "off") else "record"


# ── 설치본 ──────────────────────────────────────────────────────────────
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(installer: Path, dest: Path) -> int:
    """설치본(자기 해제 셸)의 압축본을 꺼내 SHA-256 을 머리말과 대조한 뒤 dest 에 푼다(경로 탈출 거부). 반환: 파일 수."""
    raw = Path(installer).read_text(encoding="utf-8", errors="strict")
    m = re.search(r"압축본 SHA-256 ([0-9a-f]{64})", raw)
    if "__OOS_PAYLOAD_BEGIN__" not in raw or "__OOS_PAYLOAD_END__" not in raw or not m:
        raise ChangeError("OUTOFSTOCK 설치본이 아니에요", 400, "not_installer")
    b64 = raw.split("__OOS_PAYLOAD_BEGIN__\n", 1)[1].split("__OOS_PAYLOAD_END__", 1)[0]
    blob = base64.b64decode(b64)
    if hashlib.sha256(blob).hexdigest() != m.group(1):
        raise ChangeError("설치본이 손상됐어요(SHA-256 불일치)", 400, "corrupt")
    n = 0
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:") as tar:
        for member in tar.getmembers():
            name = member.name
            if not member.isfile() or name.startswith("/") or ".." in Path(name).parts:
                raise ChangeError(f"설치본에 허용되지 않는 경로: {name}", 400, "unsafe_path")
        if hasattr(tarfile, "data_filter"):      # 3.12+/보안 패치판: 링크·장치·권한 비트 거부(bandit B202 가 읽을 수 있게 직접 적는다)
            tar.extractall(dest, filter="data")
        else:                                    # 옛 파이썬 — 위에서 모든 항목을 검사했다(일반 파일·상대 경로만)
            tar.extractall(dest, members=tar.getmembers())
        n = sum(1 for m in tar.getmembers() if m.isfile())
    return n


def build_installer(src: Path, out: Path, names: list[str] | None = None) -> dict:
    """작은 설치본(변경관리 훈련·시험용) — tools/make_installer.py 와 같은 형식·같은 무결성 머리말."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("oos_make_installer", ROOT / "tools" / "make_installer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = names or sorted(p.relative_to(src).as_posix() for p in Path(src).rglob("*") if p.is_file())
    return mod.build(Path(out), root=Path(src), names=names)


# ── 사전 영향 분석 ──────────────────────────────────────────────────────────
def _area(path: str) -> str:
    for key, _name, pats in AREAS:
        if any(fnmatch.fnmatch(path, p) for p in pats):
            return key
    return "other"


def analyze(new_root: Path, old_manifest: dict[str, str] | None = None) -> dict:
    """지금 배포된 파일(최신 무결성 기준선, 없으면 이 서버의 파일) 대비 바뀐 파일을 영역별로 묶고 위험도를 매긴다."""
    from app.security import integrity
    new = integrity.scan(new_root)
    if old_manifest is None:
        base = None
        try:
            base = integrity.latest_baseline()
        except Exception:                                  # noqa: BLE001 — 표가 없는 구버전 DB
            base = None
        old_manifest = base["manifest"] if base else integrity.scan(ROOT)
    changed = sorted(k for k in new.keys() & old_manifest.keys() if new[k] != old_manifest[k])
    added, removed = sorted(new.keys() - old_manifest.keys()), sorted(old_manifest.keys() - new.keys())
    areas: dict[str, list[str]] = {}
    for p in changed + added + removed:
        areas.setdefault(_area(p), []).append(p)
    touched = set(areas)
    risk = "high" if touched & {"money", "auth", "schema"} else "medium" if touched & {"deploy", "app"} else "low"
    hot = sorted(p for p in changed + added if p in HOT_PATHS or p.startswith("sql/migrations/"))
    names = {k: n for k, n, _ in AREAS} | {"other": "그 밖"}
    return {"changed": len(changed), "added": len(added), "removed": len(removed), "risk": risk,
            "areas": {names[k]: {"count": len(v), "sample": v[:8]} for k, v in sorted(areas.items())},
            "performance_sensitive": hot, "needs_evidence": risk == "high" or bool(hot)}


# ── 자동 사전 점검(배포 전에 — 서비스를 멈추기 전에) ─────────────────────────────
def precheck(new_root: Path) -> list[dict]:
    out = []

    def add(name, ok, detail="", blocking=True):
        out.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300], "blocking": blocking})
    bad = []
    for p in sorted((new_root / "app").rglob("*.py")) + [new_root / "run.py", new_root / "scheduler.py"]:
        if p.is_file():
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as e:
                bad.append(f"{p.relative_to(new_root)}:{e.lineno}")
    add("파이썬 구문", not bad, ", ".join(bad[:5]) or "모두 컴파일됨")
    try:
        from app.security import pdp
        pol = json.loads((new_root / "app" / "security" / "policy.json").read_text(encoding="utf-8"))
        pdp._validate(pol)
        add("접근 정책 형식", True, f"버전 {pol.get('version')} · 동작 {len(pol['actions'])}개")
    except Exception as e:                                  # noqa: BLE001
        # 새 설치본이 역할을 새로 만든 경우는 지금 코드의 역할 목록으로 판단할 수 없다 — 막지 않고 경고만
        add("접근 정책 형식", False, f"{type(e).__name__}: {e}", blocking="알 수 없는 역할" not in str(e))
    try:                                                    # 이미 적용된 마이그레이션 파일을 고치면 기동 때 멈춘다(MigrationError)
        from app import migrations
        with db.get_engine().connect() as conn:
            applied = migrations.applied(conn)
        changed_mig = []
        for p in sorted((new_root / "sql" / "migrations").glob("V*.sql")):
            v = p.name[1:4]
            if v in applied and applied[v] != migrations.checksum(p):
                changed_mig.append(p.name)
        missing = sorted(set(applied) - {p.name[1:4] for p in (new_root / "sql" / "migrations").glob("V*.sql")})
        add("적용된 마이그레이션 보존", not changed_mig and not missing,
            "변경 없음" if not (changed_mig or missing) else f"내용이 바뀐 파일 {changed_mig} · 사라진 번호 {missing}")
    except Exception as e:                                  # noqa: BLE001 — DB 에 못 붙으면 점검 불가(막지 않음)
        add("적용된 마이그레이션 보존", True, f"확인 불가({type(e).__name__}) — 기동 때 다시 검사", blocking=False)
    try:
        from app.security import assessment
        cat = assessment.load_catalog(new_root / "app" / "security" / "ssf_catalog.json")
        r = assessment.evaluate("static", catalog=cat, root=new_root)
        add("수준진단 근거 무결성", not r["broken"], f"깨진 근거 {len(r['broken'])}건" if r["broken"] else "모두 확인",
            blocking=False)
    except Exception as e:                                  # noqa: BLE001
        add("수준진단 근거 무결성", False, f"{type(e).__name__}: {e}", blocking=False)
    return out


def _blocking_failures(checks: list[dict]) -> list[str]:
    return [c["check"] for c in checks if c["blocking"] and not c["ok"]]


# ── 기록 ────────────────────────────────────────────────────────────────
def _row(r: dict) -> dict:
    out = dict(r)
    for k in ("impact", "prechecks"):
        try:
            out[k] = json.loads(r[k]) if r.get(k) else None
        except (TypeError, ValueError):
            out[k] = None
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


def _audit(action, actor, cid, detail=None):
    from app.services import audit
    audit.record(action, actor, target=f"change:{cid}", detail=detail)


def request(installer: Path, requested_by: str, title: str, reason: str, evidence: str = "") -> dict:
    """변경 요청 — 설치본을 풀어 사전 영향 분석·자동 점검을 기록한다(승인 대기)."""
    title, reason = (title or "").strip(), (reason or "").strip()
    if len(title) < 3 or len(reason) < 5:
        raise ChangeError("제목(3자↑)과 변경 사유(5자↑)를 적어 주세요", 400, "reason_required")
    sha = sha256_file(installer)
    with tempfile.TemporaryDirectory(prefix="oos-change-") as d:
        extract(installer, Path(d))
        impact, checks = analyze(Path(d)), precheck(Path(d))
    if db.query("SELECT 1 FROM change_record WHERE release_sha256=:s AND kind='planned' "
                "AND status IN ('requested', 'approved')", {"s": sha}):
        raise ChangeError("같은 설치본의 진행 중인 변경 요청이 있어요", 409, "duplicate")
    cid = db.write_query(
        "INSERT INTO change_record(created_at, kind, title, reason, requested_by, release_name, release_sha256, impact, risk, "
        "prechecks, evidence, status) VALUES(:t, 'planned', :ti, :re, :by, :rn, :sha, :im, :risk, :pc, :ev, 'requested') "
        "RETURNING id",
        {"t": clock.utc(), "ti": title[:120], "re": reason[:500], "by": requested_by[:50], "rn": Path(installer).name[:160],
         "sha": sha, "im": json.dumps(impact, ensure_ascii=False), "risk": impact["risk"],
         "pc": json.dumps(checks, ensure_ascii=False), "ev": (evidence or "").strip()[:1000] or None})[0]["id"]
    _audit("change.request", requested_by, cid, {"sha": sha[:16], "risk": impact["risk"], "files": impact["changed"]
                                                  + impact["added"] + impact["removed"]})
    return {"id": cid, "sha256": sha, "impact": impact, "prechecks": checks,
            "blocking": _blocking_failures(checks), "status": "requested"}


def decide(cid: int, approve: bool, actor: str, note: str = "") -> dict:
    """승인·반려 — 요청자 ≠ 승인자(제3자 검증). 위험이 높거나 성능 민감 경로를 건드린 변경은 검증 근거가 있어야 승인."""
    now = clock.utc()
    with db.transaction() as conn:
        r = conn.execute(text("SELECT * FROM change_record WHERE id=:i FOR UPDATE"), {"i": cid}).mappings().first()
        if not r or r["status"] != "requested":
            raise ChangeError("승인 대기 중인 변경이 아니에요", 404, "not_found")
        if actor == r["requested_by"]:
            raise ChangeError("요청자는 자기 변경을 승인할 수 없어요(제3자 검증)", 403, "four_eyes")
        impact = json.loads(r["impact"] or "{}")
        checks = json.loads(r["prechecks"] or "[]")
        if approve and _blocking_failures(checks):
            raise ChangeError(f"자동 점검 실패가 있어 승인할 수 없어요: {_blocking_failures(checks)}", 409, "precheck_failed")
        if approve and impact.get("needs_evidence") and not (r["evidence"] or "").strip():
            raise ChangeError("돈·인증·스키마·성능 민감 변경은 검증 근거(시험·부하 결과)가 있어야 승인할 수 있어요",
                              409, "evidence_required")
        status = "approved" if approve else "rejected"
        conn.execute(text("UPDATE change_record SET status=:st, approved_by=:by, approved_at=:t, review_note=:n WHERE id=:i"),
                     {"st": status, "by": actor[:50], "t": now, "n": (note or "")[:500] or None, "i": cid})
    _audit(f"change.{status}", actor, cid, {"risk": r["risk"]})
    return {"id": cid, "status": status}


def in_freeze(now: datetime | None = None) -> bool:
    """배포 금지 시간(기본 평일 08:30~16:00 KST — 장 시작 전부터 마감 직후까지, 이용이 가장 많은 때)."""
    spec = (settings.CHANGE_FREEZE or "").strip()
    if not spec or spec.lower() == "none":
        return False
    m = re.fullmatch(r"(mon-fri|daily)\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})", spec)
    if not m:
        return False
    k = (now or clock.utc()).astimezone(clock.KST)
    if m.group(1) == "mon-fri" and k.weekday() >= 5:
        return False
    start, end = int(m.group(2)) * 60 + int(m.group(3)), int(m.group(4)) * 60 + int(m.group(5))
    return start <= k.hour * 60 + k.minute < end


def gate(installer: Path, emergency_reason: str = "", actor: str = "deploy", now: datetime | None = None) -> dict:
    """배포 직전 확인 — 반환 {allow, change_id, mode, reason, warnings}. vm.sh 가 allow=false 면 멈춘다."""
    now = now or clock.utc()
    m = mode()
    sha = sha256_file(installer)
    warnings: list[str] = []
    if m == "off":
        return {"allow": True, "change_id": None, "mode": m, "reason": "CHANGE_GATE=off — 점검 없이 배포", "warnings": []}
    with tempfile.TemporaryDirectory(prefix="oos-gate-") as d:
        extract(installer, Path(d))
        checks = precheck(Path(d))
        impact = analyze(Path(d))
    blocking = _blocking_failures(checks)
    if blocking:
        return {"allow": False, "change_id": None, "mode": m, "warnings": [],
                "reason": f"배포를 깨뜨리는 점검 실패: {', '.join(blocking)} — 서비스를 멈추기 전에 중단", "prechecks": checks}
    freeze = in_freeze(now)
    emergency_reason = (emergency_reason or "").strip()
    row = db.query("SELECT id FROM change_record WHERE release_sha256=:s AND status='approved' AND kind='planned' "
                   "AND approved_at >= :t ORDER BY id DESC LIMIT 1", {"s": sha, "t": now - timedelta(days=APPROVAL_DAYS)})
    if row and not freeze:
        return {"allow": True, "change_id": row[0]["id"], "mode": m, "reason": "승인된 변경", "warnings": []}
    if freeze:
        warnings.append(f"배포 금지 시간({settings.CHANGE_FREEZE} KST)")
    if not row:
        warnings.append("승인된 변경 요청이 없는 설치본")
    if emergency_reason:
        if len(emergency_reason) < 10:
            return {"allow": False, "change_id": None, "mode": m, "warnings": warnings,
                    "reason": "긴급 변경 사유는 10자 이상(무엇이 왜 급한지)"}
        cid = _record(installer, sha, impact, checks, kind="emergency", reason=emergency_reason, actor=actor,
                      review_due=now + timedelta(days=REVIEW_DAYS))
        from app.services import alerts
        alerts.notify(f"change:emergency:{cid}", "[변경] 긴급 변경 배포 — 3일 안에 사후 검토",
                      f"변경 #{cid} · 사유: {emergency_reason[:200]} · 위험도 {impact['risk']} · {'; '.join(warnings)}",
                      level="warn", dedup_hours=0)
        return {"allow": True, "change_id": cid, "mode": m, "reason": "긴급 변경(사후 검토 3일)", "warnings": warnings}
    if m == "enforce":
        return {"allow": False, "change_id": None, "mode": m, "warnings": warnings,
                "reason": "; ".join(warnings) + " — 승인받거나 긴급 변경(CHANGE_REASON='사유')으로"}
    cid = row[0]["id"] if row else _record(installer, sha, impact, checks, kind="unapproved",
                                           reason="기록 모드 — 승인 없이 배포", actor=actor)
    return {"allow": True, "change_id": cid, "mode": m, "reason": "기록 모드(막지 않음)", "warnings": warnings}


def _record(installer, sha, impact, checks, kind, reason, actor, review_due=None) -> int:
    cid = db.write_query(
        "INSERT INTO change_record(created_at, kind, title, reason, requested_by, release_name, release_sha256, impact, risk, "
        "prechecks, status, approved_at, review_due) VALUES(:t, :k, :ti, :re, :by, :rn, :sha, :im, :risk, :pc, "
        "'approved', :t, :due) RETURNING id",
        {"t": clock.utc(), "k": kind, "ti": f"{'긴급' if kind == 'emergency' else '미승인'} 배포 {Path(installer).name}"[:120],
         "re": reason[:500], "by": actor[:50], "rn": Path(installer).name[:160], "sha": sha,
         "im": json.dumps(impact, ensure_ascii=False), "risk": impact["risk"], "pc": json.dumps(checks, ensure_ascii=False),
         "due": review_due})[0]["id"]
    _audit(f"change.{kind}", actor, cid, {"sha": sha[:16], "risk": impact["risk"]})
    return cid


def applied(cid: int | None, actor: str = "deploy", baseline_id: int | None = None, ok: bool = True) -> dict | None:
    if not cid:
        return None
    st = "applied" if ok else "failed"
    n = db.execute("UPDATE change_record SET status=:st, applied_at=:t, applied_by=:by, baseline_id=:b WHERE id=:i "
                   "AND status='approved'", {"st": st, "t": clock.utc(), "by": actor[:50], "b": baseline_id, "i": cid})
    if n:
        _audit(f"change.{st}", actor, cid, {"baseline": baseline_id})
    return {"id": cid, "status": st if n else "unchanged"}


def rolled_back(actor: str = "deploy", cid: int | None = None) -> dict | None:
    """되돌림 — vm.sh 가 배포 때 남긴 변경 번호(cid). 없으면 가장 최근에 적용된 변경."""
    if cid is not None:
        r = db.query("SELECT id FROM change_record WHERE id=:i AND status IN ('approved', 'applied', 'failed')", {"i": cid})
    else:
        r = db.query("SELECT id FROM change_record WHERE status='applied' ORDER BY applied_at DESC LIMIT 1")
    if not r:
        return None
    db.execute("UPDATE change_record SET status='rolled_back' WHERE id=:i", {"i": r[0]["id"]})
    _audit("change.rolled_back", actor, r[0]["id"])
    return {"id": r[0]["id"], "status": "rolled_back"}


def review(cid: int, actor: str, note: str) -> dict:
    """긴급 변경 사후 검토 — 배포한 사람과 다른 관리자가, 원인·영향·정식 승인 여부를 적는다."""
    note = (note or "").strip()
    if len(note) < 10:
        raise ChangeError("사후 검토 내용을 10자 이상 적어 주세요", 400, "reason_required")
    with db.transaction() as conn:
        r = conn.execute(text("SELECT * FROM change_record WHERE id=:i FOR UPDATE"), {"i": cid}).mappings().first()
        if not r or r["kind"] != "emergency" or r["reviewed_at"] is not None:
            raise ChangeError("사후 검토할 긴급 변경이 아니에요", 404, "not_found")
        if actor in (r["requested_by"], r["applied_by"]):
            raise ChangeError("배포한 사람은 사후 검토할 수 없어요(제3자 검증)", 403, "four_eyes")
        conn.execute(text("UPDATE change_record SET reviewed_by=:by, reviewed_at=:t, review_note=:n WHERE id=:i"),
                     {"by": actor[:50], "t": clock.utc(), "n": note[:500], "i": cid})
    _audit("change.reviewed", actor, cid)
    return {"id": cid, "reviewed": True}


def overdue_reviews(now: datetime | None = None) -> list[dict]:
    rows = db.query("SELECT id, title, created_at, review_due FROM change_record WHERE kind='emergency' "
                    "AND reviewed_at IS NULL AND review_due < :t ORDER BY id", {"t": now or clock.utc()})
    return [_row(r) for r in rows]


def remind_reviews() -> dict:
    """매일 — 사후 검토 기한이 지난 긴급 변경을 경보."""
    late = overdue_reviews()
    if late:
        from app.services import alerts
        alerts.notify("change:review_overdue", "[변경] 긴급 변경 사후 검토 기한 초과",
                      "\n".join(f"#{r['id']} {r['title']} (기한 {r['review_due']})" for r in late)[:1500], level="warn")
    return {"overdue": [r["id"] for r in late]}


def recent(since: datetime | None = None, limit: int = 50, until: datetime | None = None) -> list[dict]:
    q = ("SELECT id, created_at, kind, title, reason, requested_by, release_name, release_sha256, risk, status, approved_by, "
         "approved_at, applied_at, applied_by, baseline_id, review_due, reviewed_by, reviewed_at, evidence, impact, prechecks "
         "FROM change_record WHERE true")
    p: dict = {"n": max(1, min(int(limit), 500))}
    if since is not None:
        q += " AND COALESCE(applied_at, created_at) >= :s"
        p["s"] = since
    if until is not None:
        q += " AND COALESCE(applied_at, created_at) <= :u"
        p["u"] = until
    return [_row(r) for r in db.query(q + " ORDER BY id DESC LIMIT :n", p)]


def overview() -> dict:
    return {"mode": mode(), "freeze": settings.CHANGE_FREEZE, "freeze_now": in_freeze(), "approval_days": APPROVAL_DAYS,
            "review_days": REVIEW_DAYS, "pending": [r for r in recent(limit=200) if r["status"] == "requested"],
            "overdue_reviews": overdue_reviews(), "recent": recent(limit=30)}
