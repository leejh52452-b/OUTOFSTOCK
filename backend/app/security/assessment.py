"""자율보안 수준진단 — 금융보안원 「금융보안 수준진단 프레임워크」 v1.0(2026.2 — 7개 분야 45개 항목 127개 세부원칙)을
이 서비스에 적용한 자가진단. 규제 점검표(Y/N)가 아니라 '현재(As-is) → 목표(To-be)'를 성숙도 4단계로 본다.

성숙도: 초기(Initial) · 기반(Defined) · 발전(Managed) · 고도화(Advanced)
  (금융보안원 발표: 평균적인 금융회사 2단계 '기반', 국내 대형 금융회사 평균 3단계 '발전' — 2026.2)

하는 일
  1. 카탈로그(ssf_catalog.json) — 45개 항목마다 NIST CSF 2.0 하위 항목, 목표 등급, 근거(evidence) 목록.
  2. 근거를 기계로 확인 — 문서·코드·시험이 실제로 있는지(파일·함수·시험 이름까지), 운영 점검(프로브)을 돌리고,
     자동 대응(탐지 규칙·스케줄 작업)·실측 기록이 있는지. 근거가 사라지면(시험 삭제·파일 이동) 등급이 내려간다.
  3. 등급은 사람이 매기지 않는다 — 근거 '종류'로 판정한다(아래 규칙). 사람이 정하는 것은 어떤 근거를 어느 항목에
     거는지와 범위 상한(cap)뿐이고, 둘 다 카탈로그에 이유와 함께 적는다.
  4. 결과는 security_assessment 표에 추가만 하고(앱 계정은 수정·삭제 권한 없음), 결과 다이제스트를 감사 체인에 고정한다
     — 증빙 위변조 방지. 이력으로 '진단 → 개선 → 재평가'의 추이를 본다.

판정 규칙(자체 기준 — 금융보안원 127개 세부원칙·구현기준을 대신하는 단순화. 결과를 금융회사 진단과 직접 비교하지 않는다)
  초기(1)   근거 부족 — 기준(문서)과 이행 근거가 함께 있지 않다
  기반(2)   문서(정책·절차) + 구현(코드)·시험·이행 기록 중 하나          "정해져 있고 만들어져 있다"
  발전(3)   기반 + 자동 시험 + 운영 점검(프로브) 또는 자동 대응          "깨지면 안다"
  고도화(4) 발전 + 자동 대응 + 실측·훈련 기록                          "깨지면 스스로 막고, 효과를 숫자로 안다"
  운영 모드에서 그 항목의 프로브가 실패하면 기반(2)으로 내린다 — 지금 깨져 있는 것을 '안정적 운영'으로 볼 수 없다.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("security.assessment")

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(__file__).with_name("ssf_catalog.json")
DOC_PATH = ROOT / "docs" / "security" / "self_assessment.md"
RADAR_PATH = ROOT / "docs" / "security" / "self_assessment_radar.svg"
HISTORY_DIR = ROOT / "docs" / "security" / "assessment"
BASELINE_PATH = HISTORY_DIR / "2026-11-02_baseline.json"

LEVELS = {1: ("초기", "Initial"), 2: ("기반", "Defined"), 3: ("발전", "Managed"), 4: ("고도화", "Advanced")}
KINDS = {"doc": "문서", "code": "구현", "test": "자동 시험", "probe": "운영 점검", "auto": "자동 대응",
         "record": "실측·훈련 기록"}
STALE_DAYS = 92                                     # 재진단 주기(분기) — 넘으면 경보

# NIST CSF 2.0(2024.2) — 6개 기능 · 22개 범주 · 106개 하위 항목(2.0 에서 철회된 번호는 뺐다)
CSF_FUNCTIONS = {"GV": "거버넌스", "ID": "식별", "PR": "보호", "DE": "탐지", "RS": "대응", "RC": "복구"}
CSF_CATEGORIES: dict[str, tuple[str, tuple[int, ...]]] = {
    "GV.OC": ("조직 맥락", (1, 2, 3, 4, 5)),
    "GV.RM": ("위험관리 전략", (1, 2, 3, 4, 5, 6, 7)),
    "GV.RR": ("역할·책임·권한", (1, 2, 3, 4)),
    "GV.PO": ("정책", (1, 2)),
    "GV.OV": ("감독", (1, 2, 3)),
    "GV.SC": ("공급망 위험관리", tuple(range(1, 11))),
    "ID.AM": ("자산 관리", (1, 2, 3, 4, 5, 7, 8)),
    "ID.RA": ("위험 평가", tuple(range(1, 11))),
    "ID.IM": ("개선", (1, 2, 3, 4)),
    "PR.AA": ("신원·인증·접근통제", (1, 2, 3, 4, 5, 6)),
    "PR.AT": ("인식·교육", (1, 2)),
    "PR.DS": ("데이터 보안", (1, 2, 10, 11)),
    "PR.PS": ("플랫폼 보안", (1, 2, 3, 4, 5, 6)),
    "PR.IR": ("기술 인프라 복원력", (1, 2, 3, 4)),
    "DE.CM": ("지속 모니터링", (1, 2, 3, 6, 9)),
    "DE.AE": ("이상 이벤트 분석", (2, 3, 4, 6, 7, 8)),
    "RS.MA": ("사고 관리", (1, 2, 3, 4, 5)),
    "RS.AN": ("사고 분석", (3, 6, 7, 8)),
    "RS.CO": ("사고 대응 보고·소통", (2, 3)),
    "RS.MI": ("사고 완화", (1, 2)),
    "RC.RP": ("복구 계획 실행", (1, 2, 3, 4, 5, 6)),
    "RC.CO": ("복구 소통", (3, 4)),
}
CSF_SUBCATEGORIES: tuple[str, ...] = tuple(f"{c}-{n:02d}" for c, (_, ns) in CSF_CATEGORIES.items() for n in ns)


class CatalogError(ValueError):
    """카탈로그 형식 오류 — 진단을 시작하지 않는다(fail closed)."""


# ── 카탈로그 ─────────────────────────────────────────────────────────────
def load_catalog(path: Path | None = None) -> dict:
    data = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    _validate(data)
    return data


def catalog_digest(path: Path | None = None) -> str:
    return hashlib.sha256((path or CATALOG_PATH).read_bytes()).hexdigest()


def _validate(c: dict) -> None:
    domains = {d["id"]: d for d in c.get("domains") or []}
    if not domains:
        raise CatalogError("분야(domains)가 없다")
    seen: set[str] = set()
    per_domain: dict[str, int] = {}
    for it in c.get("items") or []:
        iid = it.get("id", "")
        if iid in seen:
            raise CatalogError(f"항목 번호 중복: {iid}")
        seen.add(iid)
        dom = iid.split(".")[0]
        if dom not in domains:
            raise CatalogError(f"{iid}: 알 수 없는 분야")
        per_domain[dom] = per_domain.get(dom, 0) + 1
        if not it.get("name"):
            raise CatalogError(f"{iid}: 이름 없음")
        for s in it.get("csf") or ():
            if s not in CSF_SUBCATEGORIES:
                raise CatalogError(f"{iid}: CSF 2.0 에 없는 하위 항목 {s}")
        if not it.get("csf"):
            raise CatalogError(f"{iid}: CSF 매핑 없음")
        if it.get("excluded"):
            continue
        if it.get("target") not in (1, 2, 3, 4):
            raise CatalogError(f"{iid}: 목표 등급은 1~4")
        if "cap" in it and (it["cap"] not in (1, 2, 3, 4) or not it.get("cap_reason")):
            raise CatalogError(f"{iid}: 범위 상한(cap)은 1~4 + 이유(cap_reason)")
        for ref in it.get("evidence") or ():
            if ref.split(":", 1)[0] not in KINDS:
                raise CatalogError(f"{iid}: 알 수 없는 근거 종류 {ref}")
    for did, d in domains.items():
        if per_domain.get(did, 0) != d.get("items"):
            raise CatalogError(f"분야 {did} 항목 수 {per_domain.get(did, 0)} ≠ 선언 {d.get('items')}")


# ── 근거 확인 ────────────────────────────────────────────────────────────
_SYMBOLS: dict = {}


def _py_symbols(path: Path) -> set[str]:
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _SYMBOLS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        _SYMBOLS[key] = names
    return _SYMBOLS[key]


def _file_check(spec: str, root: Path) -> tuple[bool, str]:
    """'경로' · '경로#본문에 있어야 할 글' · '경로::함수·클래스·변수 이름'(파이썬은 구문 트리로, 그 밖은 본문 검색)."""
    path, sym, needle = spec, None, None
    if "::" in spec:
        path, sym = spec.split("::", 1)
    elif "#" in spec:
        path, needle = spec.split("#", 1)
    p = root / path
    if not p.is_file():
        return False, "파일 없음"
    if sym:
        if p.suffix == ".py":
            try:
                found = sym in _py_symbols(p)
            except SyntaxError:
                return False, "구문 오류"
        else:
            found = sym in p.read_text(encoding="utf-8", errors="ignore")
        if not found:
            return False, f"'{sym}' 없음"
    if needle and needle not in p.read_text(encoding="utf-8", errors="ignore"):
        return False, f"'{needle}' 없음"
    return True, ""


def _scheduler_jobs(root: Path) -> set[str]:
    try:
        return set(re.findall(r'\bid="([\w-]+)"', (root / "scheduler.py").read_text(encoding="utf-8")))
    except OSError:
        return set()


def resolve(ref: str, root: Path = ROOT) -> dict:
    """근거 1건 → {ref, kind, ok, detail}."""
    kind, _, rest = ref.partition(":")
    ok, detail = False, ""
    if kind in ("doc", "code", "record"):
        ok, detail = _file_check(rest, root)
    elif kind == "test":
        path, _, name = rest.partition("::")
        if not name.startswith("test_"):
            ok, detail = False, "시험 이름 필요(파일::test_…)"
        else:
            ok, detail = _file_check(rest, root)
    elif kind == "probe":
        ok = rest in PROBES
        detail = PROBES[rest][0] if ok else "등록되지 않은 운영 점검"
    elif kind == "auto":
        sub, _, target = rest.partition(":")
        if sub == "rule":
            from app.security import events
            ok = target in {r.kind for r in events.RULES}
            detail = "" if ok else "탐지 규칙 없음"
        elif sub == "job":
            ok = target in _scheduler_jobs(root)
            detail = "" if ok else "스케줄 작업 없음"
        elif sub == "code":
            ok, detail = _file_check(target, root)
        else:
            detail = "auto:rule|job|code 만"
    else:
        detail = "알 수 없는 근거 종류"
    return {"ref": ref, "kind": kind, "ok": bool(ok), "detail": detail}


# ── 운영 점검(프로브) — 런타임 모드에서만 실행. ("ok"|"fail"|"na"|"error", 설명) ─────────────
PROBES: dict[str, tuple[str, object]] = {}


def probe(name: str, title: str):
    def deco(fn):
        PROBES[name] = (title, fn)
        return fn
    return deco


@probe("audit_chain", "감사 로그 해시 체인 무결")
def _p_audit():
    from app.services import audit
    r = audit.verify()
    return ("ok" if r["ok"] else "fail"), f"{r['rows']}행" + ("" if r["ok"] else f" — audit_id {r['broken_at']} 부터 불일치")


@probe("ledger", "원장 시산표 합 0 · 일 마감 사슬 무결")
def _p_ledger():
    from app.services import ledger
    v, tb = ledger.verify_closes(), ledger.trial_balance()
    ok = bool(v.get("ok")) and tb.get("total") == 0
    return ("ok" if ok else "fail"), f"시산표 합 {tb.get('total')} · 마감 {v.get('days')}일 · 깨진 날 {len(v.get('broken') or [])}"


@probe("rls_forced", "개인 데이터 표 FORCE RLS")
def _p_rls():
    from app import db
    from app.security import classification
    want = classification.rls_tables()
    rows = db.query("SELECT c.relname, (c.relrowsecurity AND c.relforcerowsecurity) AS forced FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = ANY(:t)",
                    {"t": sorted(want)})
    forced = {r["relname"] for r in rows if r["forced"]}
    missing = sorted(want - forced)
    return ("ok" if not missing else "fail"), f"{len(forced)}/{len(want)}개" + (f" — 빠짐: {', '.join(missing)}" if missing else "")


@probe("policy", "접근 정책 로드 · 기본 거부")
def _p_policy():
    from app.security import pdp
    from app.security.context import Principal
    info = pdp.info()
    deny = not pdp.decide("__assessment_unknown_action__", Principal(kind="user", name="probe", aal=2)).allow
    return ("ok" if deny else "fail"), f"정책 {info['version']} · 동작 {info['actions']}개 · 모르는 동작 {'거부' if deny else '허용(!)'}"


@probe("security_baseline", "운영 보안 설정 기준선(tools/security_check — FAIL 0)")
def _p_baseline():
    import importlib.util
    from app.config import settings
    spec = importlib.util.spec_from_file_location("oos_security_check", ROOT / "tools" / "security_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = ROOT / ".env"
    res = mod.checks(settings, template=False, env_text=mod._env_file_text(env), env_path=env if env.exists() else None)
    fails = [r["id"] for r in res if r["status"] == "FAIL"]
    return ("ok" if not fails else "fail"), f"점검 {len(res)} · FAIL {len(fails)}" + (f"({', '.join(fails)})" if fails else "")


@probe("encryption_at_rest", "암호화 열에 평문 없음")
def _p_encryption():
    from app import crypto, db
    if not crypto.enabled():
        return "na", "DATA_ENCRYPTION_KEY 없음 — 암호화 꺼짐(개발 환경)"
    plain = {}
    for table, _pk, col, _aad, _extra in crypto.ENCRYPTED_COLUMNS:
        if table == "app_meta":                     # 키-값 표 — 외부 토큰 몇 개만 암호화 대상
            continue
        n = db.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} IS NOT NULL AND {col} <> '' "   # noqa: S608 — 상수
                     f"AND {col} NOT LIKE 'enc:%'")[0]["n"]
        if n:
            plain[f"{table}.{col}"] = int(n)
    return ("ok" if not plain else "fail"), "평문 0" if not plain else f"평문 발견 {plain}"


@probe("backup_recent", "백업 최신·암호화(RPO)")
def _p_backup():
    from app.services import monitoring
    s = monitoring.dr_status()
    if not s.get("backup"):
        return "na", "백업 기록 없음(개발 환경)"
    bad = [i for i in s["issues"] if i.startswith("backup_")]
    return ("ok" if not bad else "fail"), f"마지막 성공 {s.get('backup_age_hours')}시간 전" + (f" — {bad}" if bad else "")


@probe("dr_drill_recent", f"복구 훈련 성공 · {STALE_DAYS}일 이내")
def _p_dr_drill():
    from app.services import monitoring
    s = monitoring.dr_status()
    if not s.get("drill") and not s.get("backup"):
        return "na", "훈련·백업 기록 없음(개발 환경)"
    bad = [i for i in s["issues"] if i.startswith("dr_")]
    return ("ok" if not bad else "fail"), f"마지막 훈련 {s.get('drill_age_days')}일 전" + (f" — {bad}" if bad else "")


@probe("capacity", "부하 단계 경계 미만")
def _p_capacity():
    from app.services import capacity
    lv = capacity.level_now()
    return ("ok" if lv < 2 else "fail"), f"현재 {capacity.LEVELS[lv]}"


@probe("asset_drift", "운영 DB 의 표 = 분류 등록부(분류 안 된 자산 없음)")
def _p_asset_drift():
    from app.security import assets
    d = assets.drift()
    bad = d["unclassified"] + d["missing"]
    return ("ok" if not bad else "fail"), f"표 {d['tables']}개" + (
        f" — 분류 안 됨 {d['unclassified']} · 등록부에만 {d['missing']}" if bad else "")


@probe("egress_register", "허용 목록 ↔ 제3자 등록부 일치 · 선정 기준 칸")
def _p_egress_register():
    from app.security import assets
    bad = assets.supplier_problems()
    return ("ok" if not bad else "fail"), "일치" if not bad else "; ".join(bad[:5])


@probe("risk_register", "위험 등록부 — 점수·처리·검증 시험 · 재검토 기한")
def _p_risk_register():
    from app import clock
    from app.security import riskreg
    ev = riskreg.evaluate(today=clock.today())
    bad = ev["problems"] + [f"{t}: 재검토 기한 초과" for t in ev["overdue"]]
    return ("ok" if not bad else "fail"), (f"위협 {ev['summary']['threats']} · 자동 확인 {ev['summary']['tests']}건"
                                           if not bad else "; ".join(bad[:5]))


@probe("integrity", "배포 기준선 대비 파일 무결성")
def _p_integrity():
    from app.security import integrity
    st = integrity.check(emit=False)
    if st["state"] == "no_baseline":
        return "na", "기준선 없음(개발 환경 — 배포 때 run.py integrity-baseline)"
    if st["state"] == "error":
        return "error", f"검사 실패 {st.get('error')}"
    c = st.get("counts") or {}
    return ("ok" if st["state"] == "ok" else "fail"), (f"파일 {st.get('files')}개 일치" if st["state"] == "ok" else
                                                       f"변경 {c.get('changed')} · 삭제 {c.get('missing')} · 새 파일 "
                                                       f"{c.get('unexpected')}" + (" · 기준선 변조" if st.get("baseline_tampered") else ""))


@probe("dormant_roles", "쓰지 않는 관리 자격 없음")
def _p_dormant_roles():
    from app.security import rbac
    d = rbac.dormant()
    if not d["roles"]:
        return "na", "관리 자격 없음"
    late = [f"{r['username']}:{r['role']}" for r in d["dormant"] if not r["bootstrap"]]
    return ("ok" if not late else "fail"), (f"자격 {d['roles']}건 · 비상용(부트스트랩) {d['bootstrap']}건"
                                            if not late else f"{d['days']}일 넘게 안 쓴 자격 {late}")


@probe("drill_recent", f"사고 대응 훈련 성공 · {STALE_DAYS}일 이내")
def _p_drill_recent():
    from datetime import datetime
    from app import clock, db
    try:
        rec = json.loads(db.get_meta("incident_drill_last") or "null")
    except ValueError:
        rec = None
    if not rec:
        return "na", "훈련 기록 없음 — python run.py incident-drill"
    age = (clock.utc() - datetime.fromisoformat(rec["at"])).days
    ok = bool(rec.get("ok")) and age <= STALE_DAYS
    return ("ok" if ok else "fail"), f"{age}일 전 · 시나리오 {rec.get('passed')}/{rec.get('scenarios')} 통과"


@probe("uptime_slo", "가용성 목표(30일) 충족 · 합성 감시 동작")
def _p_uptime_slo():
    from app.services import incidents
    av = incidents.availability(30)
    if not av["checks"]:
        return "na", "가용성 확인 기록 없음(UPTIME_URL 꺼짐 또는 개발 환경)"
    ok = bool(av["met"]) and (av["coverage"] or 0) >= 90
    return ("ok" if ok else "fail"), (f"30일 가용률 {av['availability']}% / 목표 {av['target']}% · 감시 범위 {av['coverage']}%"
                                      f" · 확인 {av['checks']}회")


@probe("incident_reports", "보고 대상 사고의 최초 보고 기한 준수(시행세칙 제7조의4)")
def _p_incident_reports():
    from app import db
    late = [r["id"] for r in db.query("SELECT id FROM incident WHERE reportable AND first_reported_at IS NULL "
                                      "AND final_reported_at IS NULL AND report_due < now() ORDER BY id")]
    n = db.query("SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE reportable) AS r FROM incident")[0]
    return ("ok" if not late else "fail"), (f"사건 {n['n']}건(보고 대상 {n['r']}) · 기한 초과 0" if not late
                                            else f"최초 보고 기한 초과 사건 {late}")


@probe("alerts_acked", "오류 경보 확인(기한 지난 미확인 0 · MTTA ≤ 목표)")
def _p_alerts_acked():
    from app.services import alerts
    s = alerts.ack_stats(30)
    if not s["errors"]:
        return "ok", "30일 오류 경보 없음"
    bad = s["unacked_overdue"] > 0 or (s["mtta_min"] is not None and s["mtta_min"] > s["ack_target_min"])
    return ("fail" if bad else "ok"), (f"오류 경보 {s['errors']} · 확인 {s['acked']} · 기한 지난 미확인 {s['unacked_overdue']} · "
                                       f"MTTA {s['mtta_min']}분 / 목표 {s['ack_target_min']}분")


@probe("change_control", "변경 통제 — 승인 없는 배포·사후 검토 기한 초과 없음(90일)")
def _p_change_control():
    from app import db
    from app.services import changes
    rows = db.query("SELECT kind, COUNT(*) AS n FROM change_record WHERE status IN ('applied', 'failed', 'rolled_back') "
                    "AND COALESCE(applied_at, created_at) >= now() - interval '90 days' GROUP BY kind")
    if not rows:
        return "na", "배포 기록 없음(변경관리 도입 전·개발 환경)"
    by = {r["kind"]: int(r["n"]) for r in rows}
    late = changes.overdue_reviews()
    bad = by.get("unapproved", 0) > 0 or bool(late)
    return ("fail" if bad else "ok"), (f"정식 {by.get('planned', 0)} · 긴급 {by.get('emergency', 0)} · 미승인 "
                                       f"{by.get('unapproved', 0)} · 사후 검토 기한 초과 {len(late)}")


@probe("db_capacity", "DB 용량·건강 — 일일 크기 기록 2일 이내 · 디스크 소진 예측·XID·연결 등 오류 판정 0")
def _p_db_capacity():
    from datetime import date
    from app import clock, db
    from app.services import dbhealth
    last = db.query("SELECT max(day) AS d FROM db_size_snapshot")[0]["d"]
    if not last:
        return "na", "크기 기록 없음(개발 환경) — 스케줄러가 매일 04:50 · python run.py db-snapshot"
    age = (clock.today() - date.fromisoformat(str(last)[:10])).days
    rep = dbhealth.report()
    errs = [f["title"] for f in rep["findings"] if f["level"] == "error"]
    fc = rep["forecast"]
    ok = age <= 2 and not errs
    return ("ok" if ok else "fail"), (f"마지막 기록 {age}일 전 · {rep['db_bytes'] / 1048576:,.0f}MB · "
                                      f"증가 {((fc.get('bytes_per_day') or 0) / 1048576):.1f}MB/일"
                                      + (f" · 디스크 소진 {fc['days_to_full']}일" if fc.get("days_to_full") else "")
                                      + (f" — 오류: {', '.join(errs[:3])}" if errs else ""))


@probe("archive_integrity", "수명주기 보관 파일 무결(재해시 = 기록) · 기한 지난 행 정리")
def _p_archive_integrity():
    from app import db
    from app.services import ilm
    n = db.query("SELECT count(*) AS n FROM archive_log")[0]["n"]
    if not n:
        return "na", "보관한 달 없음(보관 기한 전) — 매월 2일 자동"
    from datetime import date, timedelta
    v = ilm.verify()
    late = [k for k, d in ilm.status()["datasets"].items()        # 가장 오래된 행이 기한보다 45일 넘게 오래됨 = 월간 정리가 밀림
            if d["retention_days"] > 0 and d.get("oldest")
            and d["oldest"] < (date.fromisoformat(d["cutoff"]) - timedelta(days=45)).isoformat()]
    ok = not v["bad"] and not late
    return ("ok" if ok else "fail"), (f"보관 파일 {v['files']}개 · 정상 {v['ok']}" + (f" · 문제 {len(v['bad'])}" if v["bad"] else "")
                                      + (f" · 정리 밀림 {late}" if late else ""))


@probe("pentest_recent", "취약점 자가 점검 183일 이내 · 열린 경로 0(감독규정 제37조의2)")
def _p_pentest_recent():
    from datetime import datetime
    from app import clock, db
    from app.security import pentest
    try:
        rec = json.loads(db.get_meta("pentest_last") or "null")
    except ValueError:
        rec = None
    if not rec:
        return "na", "점검 기록 없음 — python run.py pentest --base …"
    age = (clock.utc() - datetime.fromisoformat(rec["at"])).days
    c = rec.get("counts") or {}
    ok = bool(rec.get("ok")) and age <= pentest.STALE_DAYS
    return ("ok" if ok else "fail"), (f"{age}일 전 · {rec.get('target')} · 막힘 {c.get('pass')} · 열림 {c.get('fail')} · "
                                      f"주의 {c.get('warn')}")


def _run_probe(name: str) -> dict:
    title, fn = PROBES[name]
    try:
        status, detail = fn()
    except Exception as e:                              # noqa: BLE001 — 점검 실패가 진단 전체를 막지 않게
        log.warning("운영 점검 %s 예외: %s", name, type(e).__name__)
        status, detail = "error", f"점검 중 예외 {type(e).__name__}"
    return {"name": name, "title": title, "status": status, "detail": str(detail)[:300]}


# ── 판정 ─────────────────────────────────────────────────────────────────
def level_of(kinds: set[str]) -> int:
    lv = 1
    if "doc" in kinds and kinds & {"code", "test", "record"}:
        lv = 2
    if lv >= 2 and "test" in kinds and kinds & {"probe", "auto"}:
        lv = 3
    if lv >= 3 and "auto" in kinds and "record" in kinds:
        lv = 4
    return lv


def evaluate(mode: str = "static", probe_results: dict | None = None, catalog: dict | None = None,
             root: Path = ROOT) -> dict:
    """mode='static' — 근거의 존재로 판정(문서·CI 용, 결과가 환경과 무관). 'runtime' — 프로브까지 실행(운영).
    probe_results: {이름: {status, detail}} — 시험에서 프로브 결과를 정해 넣을 때."""
    if mode not in ("static", "runtime"):
        raise ValueError("mode 는 static|runtime")
    cat = catalog or load_catalog()
    weights = {d["id"]: float(d.get("weight", 1.0)) for d in cat["domains"]}
    cache: dict[str, dict] = {k: {"name": k, "title": PROBES.get(k, (k,))[0], **v}
                              for k, v in (probe_results or {}).items()}
    items = []
    for it in cat["items"]:
        dom = it["id"].split(".")[0]
        base = {"id": it["id"], "domain": dom, "name": it["name"], "csf": list(it["csf"]),
                "note": it.get("note", ""), "next": it.get("next", ""), "change": it.get("change", "")}
        if it.get("excluded"):
            items.append({**base, "excluded": it["excluded"], "level": None, "target": None, "gap": 0,
                          "priority": 0.0, "evidence": [], "kinds": [], "probes": []})
            continue
        ev = [resolve(r, root) for r in it.get("evidence") or ()]
        kinds = {e["kind"] for e in ev if e["ok"]}
        raw = level_of(kinds)
        level = min(raw, it["cap"]) if it.get("cap") else raw
        probes = []
        if mode == "runtime":
            from app.security import rls
            for e in ev:
                if e["kind"] == "probe" and e["ok"]:
                    name = e["ref"].split(":", 1)[1]
                    if name not in cache:
                        with rls.elevated("assessment"):          # 점검은 전체 행을 본다(읽기만)
                            cache[name] = _run_probe(name)
                    probes.append(cache[name])
        probe_failed = any(p["status"] == "fail" for p in probes)
        if probe_failed:
            level = min(level, 2)
        gap = max(0, it["target"] - level)
        items.append({**base, "level": level, "raw_level": raw, "target": it["target"], "gap": gap,
                      "priority": round(gap * weights.get(dom, 1.0), 2), "cap": it.get("cap"),
                      "cap_reason": it.get("cap_reason", ""), "probe_failed": probe_failed,
                      "evidence": ev, "kinds": sorted(kinds), "probes": probes})
    return _summarize(cat, items, mode)


def _avg(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _summarize(cat: dict, items: list[dict], mode: str) -> dict:
    scoped = [i for i in items if not i.get("excluded")]
    domains = []
    for d in cat["domains"]:
        mine = [i for i in scoped if i["domain"] == d["id"]]
        domains.append({"id": d["id"], "name": d["name"], "items": d["items"], "in_scope": len(mine),
                        "weight": d.get("weight", 1.0), "avg": _avg(i["level"] for i in mine),
                        "target_avg": _avg(i["target"] for i in mine)})
    dist = {str(lv): sum(1 for i in scoped if i["level"] == lv) for lv in LEVELS}
    covered: set[str] = set()
    for i in scoped:
        if i["level"] >= 2:
            covered.update(i["csf"])
    functions = []
    for fid, fname in CSF_FUNCTIONS.items():
        mine = [i for i in scoped if any(s.startswith(fid + ".") for s in i["csf"])]
        subs = [s for s in CSF_SUBCATEGORIES if s.startswith(fid + ".")]
        functions.append({"id": fid, "name": fname, "items": len(mine), "avg": _avg(i["level"] for i in mine),
                          "covered": sum(1 for s in subs if s in covered), "total": len(subs)})
    gaps = sorted((i for i in scoped if i["gap"] > 0), key=lambda i: (-i["priority"], i["id"]))
    broken = [{"item": i["id"], **e} for i in scoped for e in i["evidence"] if not e["ok"]]
    return {
        "framework": cat["framework"], "catalog_version": cat["version"], "catalog_digest": catalog_digest(),
        "scope": cat["scope"], "mode": mode,
        "overall": {"avg": _avg(i["level"] for i in scoped), "target_avg": _avg(i["target"] for i in scoped),
                    "in_scope": len(scoped), "excluded": len(items) - len(scoped), "distribution": dist,
                    "below_target": len(gaps), "probe_failures": sum(1 for i in scoped if i.get("probe_failed"))},
        "domains": domains,
        "csf": {"functions": functions, "covered": len(covered & set(CSF_SUBCATEGORIES)), "total": len(CSF_SUBCATEGORIES),
                "uncovered": [s for s in CSF_SUBCATEGORIES if s not in covered]},
        "gaps": [{"id": i["id"], "name": i["name"], "level": i["level"], "target": i["target"],
                  "priority": i["priority"], "next": i["next"]} for i in gaps],
        "broken": broken,
        "items": items,
    }


def compact(result: dict) -> dict:
    """이력·기준선 보관용 축약 — 항목별 등급·목표·근거 종류·깨진 근거만."""
    return {"framework": result["framework"], "catalog_version": result["catalog_version"],
            "catalog_digest": result["catalog_digest"], "mode": result["mode"], "overall": result["overall"],
            "domains": [{k: d[k] for k in ("id", "name", "avg", "target_avg")} for d in result["domains"]],
            "csf": {"covered": result["csf"]["covered"], "total": result["csf"]["total"],
                    "functions": [{k: f[k] for k in ("id", "avg", "covered", "total")} for f in result["csf"]["functions"]]},
            "items": [{"id": i["id"], "level": i["level"], "target": i["target"], "kinds": i["kinds"],
                       "probes": {p["name"]: p["status"] for p in i.get("probes") or ()},
                       "broken": [e["ref"] for e in i["evidence"] if not e["ok"]]} for i in result["items"]]}


def digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def load_baseline(path: Path | None = None) -> dict | None:
    p = path or BASELINE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def regressions(prev: dict | None, cur: dict) -> list[dict]:
    """직전 진단보다 등급이 내려간 항목 — 근거가 사라졌거나 운영 점검이 실패한 것."""
    if not prev:
        return []
    before = {i["id"]: i.get("level") for i in prev.get("items") or ()}
    return [{"id": i["id"], "name": i.get("name", ""), "from": before[i["id"]], "to": i["level"]}
            for i in cur["items"] if i.get("level") is not None and before.get(i["id"]) is not None
            and i["level"] < before[i["id"]]]


# ── 보고(경영진 요약 · 문서 · 레이더 그림) ──────────────────────────────────
def executive_summary(result: dict, baseline: dict | None = None) -> dict:
    """한 장 요약 — 분야별 현재·목표·개선 전, 오른 항목, 남은 갭, 범위 상한(결정이 필요한 것)."""
    before = {i["id"]: i.get("level") for i in (baseline or {}).get("items") or ()}
    raised = [{"id": i["id"], "name": i["name"], "from": before.get(i["id"]), "to": i["level"], "change": i.get("change", "")}
              for i in result["items"] if i.get("level") is not None and before.get(i["id"]) is not None
              and i["level"] > before[i["id"]]]
    bdom = {d["id"]: d.get("avg") for d in (baseline or {}).get("domains") or ()}
    return {"overall": result["overall"], "baseline_overall": (baseline or {}).get("overall"),
            "domains": [{**d, "baseline_avg": bdom.get(d["id"])} for d in result["domains"]],
            "raised": raised, "gaps": result["gaps"][:7],
            "caps": [{"id": i["id"], "name": i["name"], "cap": i["cap"], "reason": i["cap_reason"]}
                     for i in result["items"] if i.get("cap") and not i.get("excluded")],
            "excluded": [{"id": i["id"], "name": i["name"], "reason": i["excluded"]} for i in result["items"]
                         if i.get("excluded")],
            "csf": {"covered": result["csf"]["covered"], "total": result["csf"]["total"],
                    "baseline": ((baseline or {}).get("csf") or {}).get("covered")}}


def _lv(v) -> str:
    return "—" if v is None else f"{LEVELS[v][0]}({v})"


def _f(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def render_markdown(result: dict, baseline: dict | None = None) -> str:
    ex = executive_summary(result, baseline)
    fw, ov = result["framework"], result["overall"]
    L = ["# 금융보안 수준진단 자가진단 — OUTOFSTOCK", "",
         "> 생성: `python run.py assurance-docs` — 카탈로그(`app/security/ssf_catalog.json`)·코드·시험이 바뀌면 다시 만든다"
         "(`tests/test_assessment.py` 가 문서와 코드의 차이를 잡는다).",
         f"> 기준: {fw['publisher']} 「{fw['name']}」 {fw['version']} — {fw['shape']} · {fw['mapping']} 매핑.",
         "> 등급은 근거 '종류'로 자동 판정한 **자체 기준**이다(세부원칙·구현기준 비공개) — 금융회사 진단 결과와 직접 비교하지 않는다.",
         "> 이 문서의 등급은 정적 판정(설계·구현 근거)이고, 운영 서버의 점검(프로브) 결과까지 반영한 판정은 `/admin/assessment`.",
         "", "## 1. 경영진 보고 요약", "",
         "| 구분 | 내용 |", "|---|---|",
         f"| 범위 | {result['scope']['boundary']} |",
         f"| 조직 중요도 | {result['scope']['impact_tier']} |",
         f"| 전체 성숙도 | **현재 {_f(ov['avg'])}** / 목표 {_f(ov['target_avg'])}"
         + (f" (개선 전 {_f(ex['baseline_overall']['avg'])})" if ex["baseline_overall"] else "") + " — 4점 만점 |",
         "| 등급 분포 | " + " · ".join(f"{LEVELS[int(k)][0]} {v}" for k, v in ov["distribution"].items())
         + f" (범위 안 {ov['in_scope']}개 · 제외 {ov['excluded']}개) |",
         f"| 목표 미달 | {ov['below_target']}개 항목 |",
         f"| NIST CSF 2.0 | 근거가 있는 하위 항목 **{ex['csf']['covered']}/{ex['csf']['total']}**"
         + (f" (개선 전 {ex['csf']['baseline']})" if ex["csf"]["baseline"] is not None else "") + " |",
         "", "![분야별 성숙도 레이더](self_assessment_radar.svg)", "",
         "| 분야 | 범위 안 항목 | 개선 전 | 현재 | 목표 | 가중치 |", "|---|---:|---:|---:|---:|---:|"]
    for d in ex["domains"]:
        L.append(f"| {d['id']}. {d['name']} | {d['in_scope']}/{d['items']} | {_f(d['baseline_avg'])} | **{_f(d['avg'])}** | "
                 f"{_f(d['target_avg'])} | {d['weight']} |")
    if ex["raised"]:
        L += ["", "### 진단 → 개선 → 재평가: 이번에 오른 항목", "", "| 항목 | 개선 전 | 현재 | 무엇을 했나 |", "|---|---|---|---|"]
        for r in ex["raised"]:
            L.append(f"| {r['id']} {r['name']} | {_lv(r['from'])} | **{_lv(r['to'])}** | {r['change']} |")
    L += ["", "### 남은 갭(우선순위 = 갭 × 분야 가중치 — 금융은 보호·대응을 무겁게)", ""]
    if ex["gaps"]:
        L += ["| 항목 | 현재 → 목표 | 다음 단계 |", "|---|---|---|"]
        L += [f"| {g['id']} {g['name']} | {_lv(g['level'])} → {_lv(g['target'])} | {g['next']} |" for g in ex["gaps"]]
    else:
        nxt = [i for i in result["items"] if not i.get("excluded") and i.get("next")]
        L += ["이번 주기 목표 미달 없음. 다음 주기에 목표를 올릴 후보(목표 프로파일 갱신 — CSF 프로파일 5단계 '이행 및 갱신'):", "",
              "| 항목 | 현재 | 다음 단계 |", "|---|---|---|"]
        L += [f"| {i['id']} {i['name']} | {_lv(i['level'])} | {i['next']} |" for i in nxt]
    L += ["", "### 범위 상한·제외(결정이 필요한 것)", "", "| 항목 | 상한 | 이유 |", "|---|---|---|"]
    L += [f"| {c['id']} {c['name']} | {_lv(c['cap'])} | {c['reason']} |" for c in ex["caps"]]
    L += [f"| {e['id']} {e['name']} | 제외 | {e['reason']} |" for e in ex["excluded"]]
    L += ["", "## 2. 판정 규칙(자체 기준)", "",
          "| 등급 | 조건(앞 단계 포함) | 뜻 |", "|---|---|---|",
          "| 초기(1) Initial | 기준(문서)과 이행 근거가 함께 있지 않다 | 비체계적·임시 |",
          "| 기반(2) Defined | 문서 + 구현·시험·이행 기록 중 하나 | 정해져 있고 만들어져 있다 |",
          "| 발전(3) Managed | + 자동 시험 + 운영 점검(프로브) 또는 자동 대응 | 깨지면 안다 |",
          "| 고도화(4) Advanced | + 자동 대응 + 실측·훈련 기록 | 깨지면 스스로 막고, 효과를 숫자로 안다 |", "",
          "- 근거는 기계가 확인한다: 파일·함수·시험 이름이 실제로 있는지(파이썬은 구문 트리), 탐지 규칙·스케줄 작업이 등록돼 있는지. "
          "근거가 사라지면 등급이 내려가고, 시험이 실패한다(증빙 무결성).",
          "- 운영 모드에서는 프로브(감사 체인·원장·RLS·정책·설정 기준선·암호화·백업·훈련·부하·파일 무결성 등)를 실제로 돌려, "
          "실패한 항목을 기반(2)으로 내린다.",
          "- 사람이 정하는 것: 어떤 근거를 어느 항목에 거는가 · 범위 상한(cap) · 목표 — 모두 카탈로그에 이유와 함께.", "",
          "## 3. 45개 항목", "",
          "| ID | 항목 | CSF 2.0 | 개선 전 | 현재 | 목표 | 근거 종류 | 메모 |", "|---|---|---|---|---|---|---|---|"]
    before = {i["id"]: i.get("level") for i in (baseline or {}).get("items") or ()}
    for i in result["items"]:
        if i.get("excluded"):
            L.append(f"| {i['id']} | {i['name']} | {', '.join(i['csf'])} | 제외 | 제외 | — | — | {i['excluded']} |")
            continue
        kinds = " ".join(KINDS[k] for k in KINDS if k in i["kinds"])
        cap = f" (상한 {i['cap']})" if i.get("cap") else ""
        L.append(f"| {i['id']} | {i['name']} | {', '.join(i['csf'])} | {_lv(before.get(i['id']))} | **{_lv(i['level'])}**{cap} | "
                 f"{_lv(i['target'])} | {kinds} | {i['note']} |")
    L += ["", "## 4. NIST CSF 2.0 프로파일(현재)", "",
          "CSF 프로파일 작성 단계(범위 정의 → 정보 수집 → 프로파일 작성 → 갭 분석 → 이행·갱신)를 이 진단이 그대로 따른다 — "
          "범위는 §1, 현재 프로파일은 아래, 목표 프로파일은 항목별 목표, 갭은 §1 의 남은 갭.", "",
          "| 기능 | 연결 항목 | 평균 성숙도 | 근거 있는 하위 항목 |", "|---|---:|---:|---:|"]
    for f in result["csf"]["functions"]:
        L.append(f"| {f['id']} {f['name']} | {f['items']} | {_f(f['avg'])} | {f['covered']}/{f['total']} |")
    unc: dict[str, list[str]] = {}
    for s in result["csf"]["uncovered"]:
        unc.setdefault(s.split("-")[0], []).append(s.split("-")[1])
    L += ["", "근거가 아직 없는 하위 항목(범주별): " + " · ".join(
        f"{c}({CSF_CATEGORIES[c][0]}) {', '.join(ns)}" for c, ns in unc.items()) + "."]
    L += ["", "## 5. 항목별 근거", ""]
    for i in result["items"]:
        if i.get("excluded"):
            continue
        L.append(f"<details><summary>{i['id']} {i['name']} — {_lv(i['level'])}</summary>\n")
        for e in i["evidence"]:
            L.append(f"- {'✓' if e['ok'] else '✗'} {KINDS.get(e['kind'], e['kind'])} `{e['ref'].split(':', 1)[1]}`"
                     + (f" — {e['detail']}" if e["detail"] and not e["ok"] else ""))
        if i.get("cap"):
            L.append(f"- 범위 상한 {_lv(i['cap'])}: {i['cap_reason']}")
        if i.get("next"):
            L.append(f"- 다음 단계: {i['next']}")
        L.append("\n</details>")
    L += ["", "## 6. 한계", "",
          "- 자체 판정 규칙은 '근거가 있는가'를 본다 — 통제가 충분히 강한지는 시험·실측 내용과 독립 점검이 판단한다.",
          "- 범위가 서비스 1개다. 조직 거버넌스(경영진·예산·교육)는 상한을 두고 이유를 적었다.",
          "- 금융보안원 진단은 자가진단 → 현장 실사(인터뷰·증빙 확인) → 결과 보고로 간다. 이 문서는 첫 단계(자가진단)에 해당하고, "
          "현장 실사를 대신하는 것은 근거의 기계 확인과 감사 체인에 고정한 진단 이력이다."]
    return "\n".join(L) + "\n"


def render_radar_svg(result: dict, baseline: dict | None = None) -> str:
    """분야별 평균 성숙도 레이더(현재·목표·개선 전) — 정적 SVG(외부 글꼴·스크립트 없음)."""
    import math
    doms = result["domains"]
    n, cx, cy, R = len(doms), 300, 282, 170
    bdom = {d["id"]: d.get("avg") for d in (baseline or {}).get("domains") or ()}

    def pt(i, v):
        a = -math.pi / 2 + 2 * math.pi * i / n
        r = R * (v or 0) / 4
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def poly(vals):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, v) for i, v in enumerate(vals)))
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 570" width="600" height="570" role="img" '
           'aria-label="분야별 성숙도 레이더 — 현재·목표·개선 전" font-family="sans-serif">',
           '<rect width="600" height="570" fill="#ffffff"/>']
    for lv in (1, 2, 3, 4):
        out.append(f'<polygon points="{poly([lv] * n)}" fill="none" stroke="#d1d5db" stroke-width="1"/>')
        a = -math.pi / 2 + math.pi / n                    # 첫 두 축 사이 — 점·선과 겹치지 않게
        rx, ry = cx + R * lv / 4 * math.cos(a), cy + R * lv / 4 * math.sin(a)
        out.append(f'<text x="{rx + 3:.1f}" y="{ry - 3:.1f}" font-size="10" fill="#9ca3af">{LEVELS[lv][0]}</text>')
    for i in range(n):
        x, y = pt(i, 4)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
    if bdom:
        out.append(f'<polygon points="{poly([bdom.get(d["id"]) for d in doms])}" fill="none" stroke="#9ca3af" '
                   'stroke-width="2" stroke-dasharray="2 4"/>')
    out.append(f'<polygon points="{poly([d["target_avg"] for d in doms])}" fill="none" stroke="#ea580c" '
               'stroke-width="2" stroke-dasharray="7 5"/>')
    out.append(f'<polygon points="{poly([d["avg"] for d in doms])}" fill="#2563eb" fill-opacity="0.22" stroke="#2563eb" '
               'stroke-width="2.5"/>')
    for i, d in enumerate(doms):
        x, y = pt(i, d["avg"])
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#2563eb"/>')
        lx, ly = pt(i, 4.75)
        anchor = "middle" if abs(lx - cx) < 20 else ("start" if lx > cx else "end")
        out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="14" font-weight="600" fill="#111827" '
                   f'text-anchor="{anchor}">{d["name"]}</text>')
        sub = f"{_f(d['avg'])}" + (f" (전 {_f(bdom.get(d['id']))})" if bdom.get(d["id"]) is not None else "")
        out.append(f'<text x="{lx:.1f}" y="{ly + 16:.1f}" font-size="12" fill="#374151" text-anchor="{anchor}">{sub}</text>')
    ly = 545
    legend = [("#2563eb", "", "현재"), ("#ea580c", ' stroke-dasharray="7 5"', "목표")]
    if bdom:
        legend.append(("#9ca3af", ' stroke-dasharray="2 4"', "개선 전"))
    x0 = 300 - 85 * len(legend) // 2
    for k, (c, dash, label) in enumerate(legend):
        x = x0 + 85 * k
        out.append(f'<line x1="{x}" y1="{ly}" x2="{x + 26}" y2="{ly}" stroke="{c}" stroke-width="3"{dash}/>')
        out.append(f'<text x="{x + 32}" y="{ly + 4}" font-size="12" fill="#374151">{label}</text>')
    out.append(f'<text x="300" y="28" font-size="15" font-weight="700" fill="#111827" text-anchor="middle">'
               f'금융보안 수준진단 — 분야별 평균(4점 만점) · 전체 {_f(result["overall"]["avg"])}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_docs(root: Path = ROOT) -> list[str]:
    """정적 진단 → docs/security/self_assessment.md · 레이더 SVG · 위험 등록부(risk_register.md)."""
    from app.security import riskreg
    result = evaluate("static", root=root)
    base = load_baseline()
    files = {DOC_PATH: render_markdown(result, base), RADAR_PATH: render_radar_svg(result, base),
             riskreg.DOC_PATH: riskreg.render_markdown()}
    out = []
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out.append(str(path.relative_to(root)) if path.is_relative_to(root) else str(path))
    return out


# ── 실행·저장·이력(운영) ───────────────────────────────────────────────────
def _row(r: dict, with_result: bool = False) -> dict:
    out = {"id": r["id"], "at": r["at"].isoformat() if hasattr(r["at"], "isoformat") else r["at"], "actor": r["actor"],
           "mode": r["mode"], "overall": r["overall"], "below_target": r["below_target"],
           "probe_failures": r["probe_failures"], "digest": r["digest"]}
    if with_result:
        try:
            doc = json.loads(r["result"])
        except (TypeError, ValueError):
            doc = None
        out["result"] = doc
        out["intact"] = bool(doc is not None and digest(doc) == r["digest"])      # 저장 뒤 고친 흔적이 있나
    return out


def history(limit: int = 20) -> list[dict]:
    from app import db
    rows = db.query("SELECT id, at, actor, mode, overall, below_target, probe_failures, digest FROM security_assessment "
                    "ORDER BY id DESC LIMIT :n", {"n": max(1, min(int(limit), 200))})
    return [_row(r) for r in rows]


def get(assessment_id: int) -> dict | None:
    from app import db
    rows = db.query("SELECT * FROM security_assessment WHERE id=:i", {"i": int(assessment_id)})
    return _row(rows[0], with_result=True) if rows else None


def latest() -> dict | None:
    from app import db
    rows = db.query("SELECT * FROM security_assessment ORDER BY id DESC LIMIT 1")
    return _row(rows[0], with_result=True) if rows else None


def run(actor: str = "system", mode: str = "runtime") -> dict:
    """진단 1회 — 평가 → 저장(추가만) → 결과 다이제스트를 감사 체인에 → 직전 진단보다 내려간 항목·실패한 점검은 경보."""
    from app import clock, db
    from app.services import alerts, audit
    prev = latest()
    result = evaluate(mode)
    doc = {**compact(result), "at": clock.utc().isoformat(timespec="seconds")}
    dg = digest(doc)
    ov = result["overall"]
    rid = db.write_query(
        "INSERT INTO security_assessment(at, actor, mode, overall, below_target, probe_failures, result, digest) "
        "VALUES(:at, :a, :m, :o, :b, :p, :r, :d) RETURNING id",
        {"at": clock.utc(), "a": actor[:50], "m": mode, "o": ov["avg"], "b": ov["below_target"], "p": ov["probe_failures"],
         "r": json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "d": dg})[0]["id"]
    audit.record("assessment.run", actor, target=f"assessment:{rid}",
                 detail={"digest": dg[:16], "overall": ov["avg"], "below_target": ov["below_target"],
                         "probe_failures": ov["probe_failures"], "catalog": result["catalog_digest"][:16]})
    reg = regressions((prev or {}).get("result"), result)
    failing = [{"item": i["id"], "probe": p["name"], "detail": p["detail"]} for i in result["items"]
               for p in i.get("probes") or () if p["status"] == "fail"]
    if reg or failing:
        lines = [f"{r['id']} {r['name']}: {LEVELS[r['from']][0]} → {LEVELS[r['to']][0]}" for r in reg]
        lines += [f"{f['item']} 운영 점검 실패 — {f['probe']}: {f['detail']}" for f in failing[:8]]
        alerts.notify("assessment:regression", "[수준진단] 등급 하락·운영 점검 실패", "\n".join(lines)[:1500], level="warn")
    return {"id": rid, "at": doc["at"], "digest": dg, "mode": mode, "overall": ov, "domains": result["domains"],
            "csf": {k: result["csf"][k] for k in ("covered", "total")}, "gaps": result["gaps"], "regressions": reg,
            "probe_failures": failing, "broken": result["broken"],
            "items": [{"id": i["id"], "name": i["name"], "level": i["level"], "target": i["target"],
                       "probes": [{k: p[k] for k in ("name", "status", "detail")} for p in i.get("probes") or ()]}
                      for i in result["items"]]}


def overview() -> dict:
    """관리 화면용 — 최근 진단 요약 + 이력 + 재진단 기한."""
    from datetime import datetime, timezone
    hist = history(12)
    stale = True
    if hist:
        at = datetime.fromisoformat(hist[0]["at"])
        stale = (datetime.now(timezone.utc) - at).days > STALE_DAYS
    last = latest()
    return {"framework": load_catalog()["framework"], "stale": stale, "stale_days": STALE_DAYS, "history": hist,
            "latest": ({k: last[k] for k in ("id", "at", "actor", "mode", "overall", "below_target", "probe_failures",
                                             "intact")} | {"domains": (last.get("result") or {}).get("domains")}) if last else None}
