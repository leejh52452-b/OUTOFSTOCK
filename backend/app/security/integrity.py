"""파일 무결성 감시(FIM) — 금융보안 수준진단 4.4 '소프트웨어 및 시스템 파일 모니터링' · 웹셸 방지 · 변경 통제.

기준선(baseline)
  배포가 끝난 직후(vm.sh install·update 가 `python run.py integrity-baseline`) 감시 대상 파일마다 SHA-256 을 계산해
  integrity_baseline 표에 한 행(파일 목록 JSON + 다이제스트)으로 남긴다. 이 표는 소유자(마이그레이션) 계정만 쓸 수 있고
  서비스 계정은 읽기만(V016) — 앱 권한을 얻은 공격자가 기준선을 고쳐 자기 파일을 '정상'으로 만들 수 없다.
  기준선 기록은 감사 체인(외부 증인)에도 남는다 — 언제·누가·몇 개 파일로 기준을 바꿨는지.
검사
  API 기동 시 + 스케줄러 10분마다 — 지금 파일 ↔ 최신 기준선:
    changed(내용이 바뀜) · missing(사라짐) · unexpected(기준선에 없는 새 파일 — 웹셸·몰래 심은 스크립트 의심)
  위반이 처음 보이거나 바뀌면 보안 이벤트 file_integrity(심각) → 탐지 규칙이 운영 경보. 같은 위반은 다시 울리지 않는다.
  기준선 행 자체를 DB 에서 고친 흔적(다이제스트 불일치)도 위반으로 본다.
감시 범위 — 실행되거나 내보내는 것만: app/ · web/ · sql/ · deploy/ · tools/ · run.py · scheduler.py · requirements*.
  제외: 캐시(__pycache__·*.pyc) · 서버마다 다른 운영 값(.env 류·인증서·키·생성 설정) — 설치본(make_installer)과 같은 규칙.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger("security.integrity")
ROOT = Path(__file__).resolve().parents[2]
WATCH_DIRS = ("app", "web", "sql", "deploy", "tools")
WATCH_FILES = ("run.py", "scheduler.py", "requirements.txt", "requirements.lock")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "generated"}
SKIP_PATHS = ("deploy/generated", "deploy/.seeded", "deploy/backup/recipients.txt")
SKIP_GLOBS = ("*.pyc", "*.pyo", "*.age", "*.bak", "*.db", "*.log", "*.key", "*.p12", "*.pem", "*.crt", "*.csr",
              "*.srl", ".env", ".env.*", ".release-files", "*.swp", ".DS_Store")
KEEP_IN_TLS = {".gitkeep", ".gitignore", "README.md"}
MAX_LIST = 50                                       # 응답·이벤트에 담는 경로 수 상한

_lock = threading.Lock()
_last: dict = {}


def _skipped(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    if any(rel == s or rel.startswith(s + "/") for s in SKIP_PATHS):
        return True
    if any(fnmatch.fnmatch(parts[-1], g) for g in SKIP_GLOBS):
        return True
    return rel.startswith("deploy/tls/") and parts[-1] not in KEEP_IN_TLS


def scan(root: Path = ROOT) -> dict[str, str]:
    """감시 대상 파일 → SHA-256(16진). 심볼릭 링크는 따라가지 않는다(대상 밖을 가리키게 바꿔치기하는 경로)."""
    out: dict[str, str] = {}
    paths = [root / f for f in WATCH_FILES]
    for d in WATCH_DIRS:
        base = root / d
        if base.is_dir():
            paths += sorted(base.rglob("*"))
    for p in paths:
        try:
            if p.is_symlink() or not p.is_file():
                if p.is_symlink():
                    out[p.relative_to(root).as_posix()] = "symlink"
                continue
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        if _skipped(rel):
            continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return dict(sorted(out.items()))


def manifest_digest(manifest: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{k}\t{v}" for k, v in sorted(manifest.items())).encode()).hexdigest()


def _writer():
    """기준선은 소유자(마이그레이션) 계정으로만 쓴다 — 서비스 계정에는 INSERT 권한이 없다(V016)."""
    if settings.MIGRATION_DATABASE_URL:
        from sqlalchemy import create_engine
        return create_engine(settings.MIGRATION_DATABASE_URL, future=True), True
    return db.get_engine(), False


def record_baseline(root: Path = ROOT, actor: str = "deploy", release: str = "", only: set[str] | None = None) -> dict:
    """only — 설치본이 가져온 파일 목록(vm.sh 의 .release-files·되돌린 스냅샷 목록). 주면 그 목록에 있는 파일만 기준선에 넣는다:
    배포 전부터 몰래 있던 파일(웹셸)이 '새 기준선'에 섞여 정상으로 바뀌지 않고 계속 '새 파일'로 경보된다."""
    found = scan(root)
    manifest = found if only is None else {k: v for k, v in found.items() if k in only}
    excluded = sorted(set(found) - set(manifest))
    dg = manifest_digest(manifest)
    eng, own = _writer()
    try:
        with eng.begin() as conn:
            bid = conn.execute(text(
                "INSERT INTO integrity_baseline(created_at, created_by, release, files, digest, manifest) "
                "VALUES(:t, :by, :rel, :n, :d, :m) RETURNING id"),
                {"t": clock.utc(), "by": actor[:50], "rel": (release or "")[:120], "n": len(manifest), "d": dg,
                 "m": json.dumps(manifest, separators=(",", ":"))}).scalar_one()
    finally:
        if own:
            eng.dispose()
    from app.services import audit
    audit.record("integrity.baseline", actor, target=f"baseline:{bid}",
                 detail={"files": len(manifest), "digest": dg[:16], "release": (release or "")[:120],
                         "excluded": excluded[:MAX_LIST]})
    with _lock:
        _last.clear()
    return {"id": bid, "files": len(manifest), "digest": dg, "excluded": excluded}


def latest_baseline() -> dict | None:
    rows = db.query("SELECT id, created_at, created_by, release, files, digest, manifest FROM integrity_baseline "
                    "ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    try:
        r["manifest"] = json.loads(r["manifest"])
    except (TypeError, ValueError):
        r["manifest"] = {}
    return r


def verify(root: Path = ROOT, baseline: dict | None = None) -> dict:
    base = baseline if baseline is not None else latest_baseline()
    if not base:
        return {"state": "no_baseline", "checked_at": clock.iso_now()}
    want: dict[str, str] = base["manifest"]
    tampered = manifest_digest(want) != base["digest"]
    now = scan(root)
    changed = sorted(k for k in want.keys() & now.keys() if want[k] != now[k])
    missing = sorted(want.keys() - now.keys())
    unexpected = sorted(now.keys() - want.keys())
    bad = bool(changed or missing or unexpected or tampered)
    return {"state": "violation" if bad else "ok", "baseline_id": base["id"],
            "baseline_at": base["created_at"].isoformat() if hasattr(base["created_at"], "isoformat") else base["created_at"],
            "baseline_by": base["created_by"], "release": base.get("release"), "files": len(now),
            "baseline_tampered": tampered, "changed": changed[:MAX_LIST], "missing": missing[:MAX_LIST],
            "unexpected": unexpected[:MAX_LIST],
            "counts": {"changed": len(changed), "missing": len(missing), "unexpected": len(unexpected)},
            "checked_at": clock.iso_now()}


def _signature(r: dict) -> str:
    return hashlib.sha256(json.dumps([r.get("changed"), r.get("missing"), r.get("unexpected"),
                                      r.get("baseline_tampered"), r.get("baseline_id")]).encode()).hexdigest()[:32]


def check(emit: bool = True, root: Path = ROOT) -> dict:
    """검사 1회 — 새 위반이면 보안 이벤트(규칙 → 경보). 결과는 /admin/integrity 가 보여 준다."""
    try:
        r = verify(root)
    except Exception as e:                              # noqa: BLE001 — 표가 아직 없는 구버전 DB 등
        log.warning("무결성 검사 실패: %s", type(e).__name__)
        r = {"state": "error", "error": type(e).__name__, "checked_at": clock.iso_now()}
    if emit and r["state"] in ("ok", "violation"):
        sig = _signature(r) if r["state"] == "violation" else ""
        prev = db.get_meta("integrity_last_sig") or ""
        if sig != prev:
            if sig:
                from app.security import events
                events.emit("file_integrity", "critical", changed=r["counts"]["changed"], missing=r["counts"]["missing"],
                            unexpected=r["counts"]["unexpected"], sample=(r["unexpected"] + r["changed"] + r["missing"])[:5],
                            baseline_id=r["baseline_id"], tampered=r["baseline_tampered"] or None)
            db.set_meta("integrity_last_sig", sig)
    with _lock:
        _last.clear()
        _last.update(r)
    return r


def status(refresh: bool = False) -> dict:
    with _lock:
        cached = dict(_last)
    return check(emit=True) if (refresh or not cached) else cached


def status_reset() -> None:
    """보관한 마지막 결과를 비운다(훈련이 임시 폴더로 돌린 결과가 /admin/integrity 에 남지 않게)."""
    with _lock:
        _last.clear()
