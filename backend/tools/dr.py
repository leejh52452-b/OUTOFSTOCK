"""백업·복구 훈련(DR) — 백업은 '복구해 봐야' 백업이다.

    python tools/dr.py backup [--plaintext|--allow-plaintext] # 일관 스냅샷 덤프 → age 암호화 → manifest → 보관 주기
    python tools/dr.py list                                 # 백업 목록(시각·크기·암호화·행 수)
    python tools/dr.py verify [파일] [--identity 키]         # 암호문 무결성 + 복호화 + 목록(복원 없이, 빠름)
    python tools/dr.py drill  [파일] --identity 키 --target-admin-url URL [--keep] [--no-acl]

백업(backup)
  ① 일관 스냅샷 — REPEATABLE READ 트랜잭션에서 pg_export_snapshot() 을 내보내고, 그 '같은 순간'으로
     표별 정확한 행 수·감사 체인 머리·원장 마감 머리·분개 합·보안 통제(RLS 표·정책·트리거)·스키마 버전을 재고,
     pg_dump --snapshot 으로 덤프한다. manifest 의 숫자와 덤프 내용이 어긋날 수 없다(복구 검증의 기준값).
  ② RLS — 개인 데이터 표는 FORCE RLS 라 일반 계정의 pg_dump 는 실패한다(2026-10-20 훈련에서 발견: V010 적용 뒤
     기존 일일 백업이 매일 실패하고 있었다). 권장: BACKUP_DATABASE_URL = oos_backup(pg_read_all_data + BYPASSRLS,
     쓰기 없음). 없으면 지정 계정 + 시스템 문맥(app.rls=off, --enable-row-security)으로 덤프하고 manifest 에 남긴다.
     '조용히 빈 표'가 되는 경우(정책에 막혀 0행)는 manifest 행 수와 대조하는 복구 훈련 ④에서 걸린다.
  ③ 암호화 — 평문 덤프를 디스크에 쓰지 않는다: pg_dump 출력 → (SHA-256) → age(공개키) → 파일.
     서버에는 공개키(BACKUP_AGE_RECIPIENTS)만 있어 서버가 털려도 지난 백업을 열 수 없다. 수신자 2개(운영 키·금고 보관 키)
     를 두면 한쪽 키를 잃어도 복구된다. age = X25519 + ChaCha20-Poly1305(64KiB 조각마다 인증, 잘림·변조 탐지).
  ④ manifest(JSON, 개인정보 없음) — 스냅샷 시각(RPO 기준점)·행 수·지문·덤프/암호문 SHA-256·수신자 공개키.
  ⑤ 보관 — 최근 BACKUP_KEEP_DAILY 개 + 최근 BACKUP_KEEP_MONTHLY 개월 각 달의 첫 백업(GFS). 소산 명령(선택).
  ⑥ 기록 — app_meta.backup_last(관리자 헬스의 '백업 경과 시간' = 지금 사고 나면 잃을 최대 데이터) + 감사 로그.

복구 훈련(drill) — 격리된 대상(운영과 다른 인스턴스 권장)에 새 DB 를 만들어 복원하고 확인·실측한다.
  ① 암호문 SHA-256 = manifest   ② 대상 역할 확인(덤프의 GRANT 대상)   ③ 복호화 → pg_restore(단일 트랜잭션, 오류 시 중단)
     + 덤프 SHA-256 = manifest     ④ 표별 행 수 = manifest(빈 백업·누락 탐지)   ⑤ 감사 체인 재검증 + 머리 해시 일치
  ⑥ 원장: 마감 해시 사슬 재계산 · 시산표 합 0 · 마감 머리 일치   ⑦ 데이터 계약(critical) 통과
  ⑧ 암호화 열 전부 복호화(키 분실·교체 누락 탐지)   ⑨ 보안 통제 복원(FORCE RLS 표·정책 수·트리거) · 스키마 버전 일치
  ⑩ 서비스 기동: 복원 DB 로 앱을 띄워 마이그레이션 0건 · /health · /indices · 가입→로그인→지갑(원장 분개) 확인
  시간: 단계별 + 합계 = RTO 실측(검증까지). RPO = 훈련 시각 − 스냅샷 시각(논리 백업만이면 '백업 주기'가 최대 손실 —
  분 단위가 필요하면 WAL 보관 + PITR: deploy/backup/pitr_drill.sh). 보고서(MD·JSON)는 훈련 증적으로 남긴다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

FORMAT = "oos-backup/1"
CHUNK = 1 << 20
_NAME = re.compile(r"^oos-(\d{8}T\d{6}Z)\.dump(\.age)?$")
_TABLES_SQL = ("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
               "WHERE n.nspname IN ('public', 'analytics') AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
               "AND c.relpersistence = 'p' ORDER BY 1, 2")
_SKIP_COUNT = {"public.rate_event"}            # UNLOGGED(덤프 제외) — 위 조건으로 이미 빠지지만 이름으로도 명시


class DrError(RuntimeError):
    pass


# ── 공통 ──────────────────────────────────────────────────────────
def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _pg(url: str) -> dict:
    """SQLAlchemy URL → libpq 접속 값. 비밀번호는 명령줄이 아니라 환경변수(PGPASSWORD)로 넘긴다(ps 에 보이지 않게)."""
    from sqlalchemy.engine import make_url
    u = make_url(url)
    q = {k: (v if isinstance(v, str) else v[-1]) for k, v in u.query.items()}
    out = {"host": q.get("host") or u.host or "localhost", "port": str(u.port or 5432),
           "user": u.username or "", "password": u.password or "", "dbname": u.database or ""}
    for k in ("sslmode", "sslrootcert", "sslcert", "sslkey"):        # DB TLS(운영 compose 의 DB_TLS=on)도 그대로
        if q.get(k):
            out[k] = q[k]
    return out


_TLS = ("sslmode", "sslrootcert", "sslcert", "sslkey")


def _sa_url(pg: dict) -> str:
    from sqlalchemy.engine import URL
    socket = pg["host"].startswith("/")
    query = {k: pg[k] for k in _TLS if pg.get(k)}
    if socket:
        query["host"] = pg["host"]
    return URL.create("postgresql+psycopg", username=pg["user"] or None, password=pg["password"] or None,
                      host=None if socket else pg["host"], port=int(pg["port"]), database=pg["dbname"],
                      query=query).render_as_string(hide_password=False)


def _env(pg: dict, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST=pg["host"], PGPORT=pg["port"], PGUSER=pg["user"], PGDATABASE=pg["dbname"],
               PGCONNECT_TIMEOUT="10", **extra)
    if pg["password"]:
        env["PGPASSWORD"] = pg["password"]
    for k in _TLS:                                       # libpq 환경변수(PGSSLMODE …) — pg_dump·pg_restore 도 TLS 로
        if pg.get(k):
            env["PG" + k.upper()] = pg[k]
    return env


def _connect(pg: dict, **kw):
    import psycopg
    tls = {k: pg[k] for k in _TLS if pg.get(k)}
    return psycopg.connect(host=pg["host"], port=pg["port"], user=pg["user"], password=pg["password"] or None,
                           dbname=pg["dbname"], connect_timeout=10, **tls, **kw)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        hint = {"age": "sudo apt install age", "pg_dump": "sudo apt install postgresql-client",
                "pg_restore": "sudo apt install postgresql-client"}.get(tool, "")
        raise DrError(f"{tool} 없음 — {hint}")
    return p


def _backup_dir() -> Path:
    d = Path(settings.BACKUP_DIR)
    return d if d.is_absolute() else ROOT / d


def _manifest_path(dump: Path) -> Path:
    return dump.with_name(dump.name.split(".dump")[0] + ".manifest.json")


def _recipients() -> tuple[str | None, list[str]]:
    path = settings.BACKUP_AGE_RECIPIENTS
    if not path:
        return None, []
    p = Path(path)
    if not p.is_file():
        raise DrError(f"BACKUP_AGE_RECIPIENTS 파일이 없습니다: {path}")
    keys = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    if not keys or not all(k.startswith("age1") for k in keys):
        raise DrError("수신자 파일에는 age 공개키(age1…)만 한 줄에 하나씩")
    return str(p), keys


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ── 지문(백업 시점과 복원 뒤가 같아야 하는 값) ─────────────────────────────
def _fingerprint(cur) -> dict:
    """행 수 + 감사·원장 머리 + 보안 통제 + 스키마 버전. cur: psycopg 커서(같은 스냅샷 안)."""
    tables = cur.execute(_TABLES_SQL).fetchall()
    counts = {}
    for nsp, rel in tables:
        key = f"{nsp}.{rel}"
        if key not in _SKIP_COUNT:
            counts[key] = cur.execute(f"SELECT count(*) FROM {_q(nsp)}.{_q(rel)}").fetchone()[0]

    def one(sql, reg):
        return cur.execute(sql).fetchone() if key_exists(reg) else None

    def key_exists(reg):
        return cur.execute("SELECT to_regclass(%s) IS NOT NULL", (reg,)).fetchone()[0]

    a = one("SELECT audit_id, row_hash FROM public.audit_log ORDER BY audit_id DESC LIMIT 1", "public.audit_log")
    lc = one("SELECT close_date, close_hash FROM public.ledger_close ORDER BY close_date DESC LIMIT 1",
             "public.ledger_close")
    js = one("SELECT COALESCE(SUM(amount), 0) FROM public.journal_entry", "public.journal_entry")
    sv = one("SELECT max(version) FROM analytics.schema_migrations", "analytics.schema_migrations")
    forced = [r[0] for r in cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity ORDER BY 1").fetchall()]
    policies = cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'").fetchone()[0]
    triggers = [r[0] for r in cur.execute(
        "SELECT c.relname || '.' || t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal "
        "AND n.nspname IN ('public', 'analytics') ORDER BY 1").fetchall()]
    return {"tables": counts,
            "facts": {"audit_head": {"id": a[0], "hash": a[1]} if a else None,
                      "ledger_head": {"date": str(lc[0]), "hash": lc[1]} if lc else None,
                      "journal_sum": int(js[0]) if js else None,
                      "schema_version": sv[0] if sv else None,
                      "rls_forced": forced, "policies": int(policies), "triggers": triggers}}


def _dq_baseline(conn) -> list[str]:
    """백업 스냅샷 안에서 데이터 계약(critical)을 잰다 — 복원 뒤에는 '복원으로 새로 생긴 위반'만 실패로 본다.
    적시성(timeliness)은 시간이 지나면 저절로 바뀌므로 비교에서 뺀다(백업 뒤 일주일 지나 훈련하면 '오래된 시세'가 되는 게 정상)."""
    from app.services import contracts
    failed = []
    for c in contracts.CONTRACTS:
        if c.severity != "critical" or c.dimension == "timeliness":
            continue
        try:
            with conn.transaction():                     # 저장점 — 한 계약의 오류가 스냅샷 트랜잭션을 깨지 않게
                v = conn.execute(c.sql).fetchone()[0]
            ok = contracts._ok(float(v or 0), c.op, c.threshold)
        except Exception:                                # noqa: BLE001 — 측정 실패도 위반으로
            ok = False
        if not ok:
            failed.append(f"{c.dataset}.{c.name}")
    return failed


def _acl_roles(cur) -> list[str]:
    """덤프의 GRANT·기본 권한(ALTER DEFAULT PRIVILEGES)에 나오는 역할 — 복원 대상에 있어야 한다.
    (information_schema 는 '내가 속한 역할'의 권한만 보여 주므로 카탈로그의 ACL 을 직접 편다)"""
    rows = cur.execute(
        "SELECT pg_get_userbyid(a.grantee) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
        "  LATERAL aclexplode(c.relacl) a WHERE n.nspname IN ('public', 'analytics') AND a.grantee <> 0 "
        "UNION SELECT pg_get_userbyid(a.grantee) FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a "
        "  WHERE n.nspname IN ('public', 'analytics') AND a.grantee <> 0 "
        "UNION SELECT pg_get_userbyid(a.grantee) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "  LATERAL aclexplode(p.proacl) a WHERE n.nspname IN ('public', 'analytics') AND a.grantee <> 0 "
        "UNION SELECT pg_get_userbyid(d.defaclrole) FROM pg_default_acl d "
        "UNION SELECT pg_get_userbyid(a.grantee) FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a "
        "  WHERE a.grantee <> 0").fetchall()
    return sorted({r[0] for r in rows if r[0]})


# ── 백업 ─────────────────────────────────────────────────────────
_COMPRESSION: dict = {}


def dump_compression() -> str:
    """덤프 압축 방식 — BACKUP_COMPRESSION(기본 auto). auto = pg_dump 16+ 이고 zstd 를 지원하면 zstd:3, 아니면 gzip 6.
    실측(1년 규모 실험 DB 807MB, docs/perf/db_space_2026-11-03.md): gzip:6 124MB·35.6초 → zstd:3 111MB·4.6초(7.7배 빠르고 10% 작다).
    지원 여부는 연결 전에 판정되는 오류로 확인한다(없는 주소로 한 번 — 'not supported by this build' 면 gzip)."""
    want = (settings.BACKUP_COMPRESSION or "auto").strip()
    if want != "auto":
        return want
    if "auto" not in _COMPRESSION:
        mode = "6"
        try:
            ver = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=10).stdout
            major = int((ver.split()[-1] if ver.split() else "0").split(".")[0])
            if major >= 16:
                r = subprocess.run(["pg_dump", "--format=custom", "--compress=zstd:3", "--schema-only", "-f", os.devnull,
                                    "--dbname=postgresql://oos-probe@127.0.0.1:1/x?connect_timeout=1"],
                                   capture_output=True, text=True, timeout=15)
                if "support" not in (r.stderr or "").lower():
                    mode = "zstd:3"
        except (OSError, ValueError, subprocess.SubprocessError):
            mode = "6"
        _COMPRESSION["auto"] = mode
    return _COMPRESSION["auto"]


def backup(plaintext: bool = False, out_dir: Path | None = None) -> dict:
    _need("pg_dump")
    url = settings.BACKUP_DATABASE_URL or settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL
    pg = _pg(url)
    rfile, rkeys = _recipients()
    if not rfile and not plaintext:
        raise DrError("암호화 수신자(BACKUP_AGE_RECIPIENTS)가 없습니다 — age-keygen 으로 키를 만들고 공개키 파일을 지정하세요"
                      "(개발용 평문 백업은 --plaintext)")
    if rfile:
        _need("age")
    out = out_dir or _backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out, 0o700)                             # 백업 폴더는 소유자만
    except PermissionError:
        pass
    t0 = time.monotonic()
    import psycopg
    with _connect(pg) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        cur = conn.cursor()
        bypass = cur.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        if not bypass:
            cur.execute("SELECT set_config('app.rls', 'off', true)")   # 시스템 문맥 — 정책이 전 행을 보이게
        snap = cur.execute("SELECT pg_export_snapshot()").fetchone()[0]
        at = cur.execute("SELECT now()").fetchone()[0].astimezone(timezone.utc)
        server_version = cur.execute("SHOW server_version").fetchone()[0]
        fp = _fingerprint(cur)
        roles = _acl_roles(cur)
        dq_failed = _dq_baseline(conn)
        name = f"oos-{at:%Y%m%dT%H%M%SZ}.dump" + (".age" if rfile else "")
        final = out / name
        part = final.with_name(final.name + ".part")
        comp = dump_compression()
        args = ["pg_dump", "--format=custom", f"--compress={comp}", f"--snapshot={snap}", "--no-unlogged-table-data",
                "--no-password"]
        args += [f"--extension={e.strip()}" for e in settings.BACKUP_EXTENSIONS.split(",") if e.strip()]
        extra = {}
        if not bypass:
            args.append("--enable-row-security")
            extra["PGOPTIONS"] = "-c app.rls=off"
        h, nbytes = hashlib.sha256(), 0
        with tempfile.TemporaryFile() as dump_err, tempfile.TemporaryFile() as enc_err:
            dump = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=dump_err, env=_env(pg, **extra))
            enc = None
            if rfile:
                enc = subprocess.Popen(["age", "--encrypt", "-R", rfile, "-o", str(part)],
                                       stdin=subprocess.PIPE, stderr=enc_err)
                sink = enc.stdin
            else:
                sink = open(part, "wb")
                os.chmod(part, 0o600)
            broken = False
            try:
                for chunk in iter(lambda: dump.stdout.read(CHUNK), b""):
                    h.update(chunk)
                    nbytes += len(chunk)
                    try:
                        sink.write(chunk)
                    except BrokenPipeError:              # age 가 먼저 죽음(수신자 오류 등) — 덤프도 멈춘다
                        broken = True
                        break
            finally:
                try:
                    sink.close()
                except BrokenPipeError:
                    broken = True
            if broken:
                dump.kill()
            rc = dump.wait()
            erc = enc.wait() if enc else 0
            if broken or rc != 0 or erc != 0 or nbytes == 0:
                part.unlink(missing_ok=True)
                dump_err.seek(0)
                enc_err.seek(0)
                msg = (dump_err.read().decode("utf-8", "replace") + enc_err.read().decode("utf-8", "replace")).strip()
                raise DrError(f"덤프 실패(pg_dump={rc}, age={erc}): {msg[-800:]}")
        conn.rollback()
    os.chmod(part, 0o600)
    os.replace(part, final)
    pg_dump_version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout.strip()
    manifest = {
        "format": FORMAT, "file": name, "created_at": at.isoformat(timespec="seconds"),
        "database": pg["dbname"], "role": pg["user"], "server_version": server_version, "pg_dump": pg_dump_version,
        "rls_mode": "bypassrls" if bypass else "system_context(app.rls=off)", "compression": comp,
        "encryption": {"tool": "age", "recipients": rkeys} if rfile else None,
        "dump": {"bytes": nbytes, "sha256": h.hexdigest()},
        "cipher": {"bytes": final.stat().st_size, "sha256": _sha256_file(final)},
        "acl_roles": roles, "dq_critical_failed": dq_failed, "duration_s": round(time.monotonic() - t0, 2), **fp,
    }
    mpath = _manifest_path(final)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(mpath, 0o600)
    kept, removed = apply_retention(out)
    offsite = _offsite(final, mpath)
    return {**{k: manifest[k] for k in ("file", "created_at", "rls_mode", "duration_s")},
            "path": str(final), "bytes": manifest["cipher"]["bytes"], "encrypted": bool(rfile),
            "tables": len(manifest["tables"]), "rows": sum(manifest["tables"].values()),
            "kept": len(kept), "removed": removed, "offsite": offsite}


def plan_retention(names: list[str], keep_daily: int, keep_monthly: int) -> tuple[list[str], list[str]]:
    """보관할 것·지울 것 — 최근 keep_daily 개 + 최근 keep_monthly 개월 각 달의 '첫' 백업(월말 결산 시점 근처를 오래 남김)."""
    items = sorted(((datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ"), n) for n in names
                    if (m := _NAME.match(n))), reverse=True)
    keep = {n for _t, n in items[:max(0, keep_daily)]}
    first_of_month: dict = {}
    for t, n in sorted(items):
        first_of_month.setdefault((t.year, t.month), n)
    for ym in sorted(first_of_month, reverse=True)[:max(0, keep_monthly)]:
        keep.add(first_of_month[ym])
    return [n for _t, n in items if n in keep], [n for _t, n in items if n not in keep]


def apply_retention(out: Path) -> tuple[list[str], list[str]]:
    names = [p.name for p in out.iterdir() if _NAME.match(p.name)]
    kept, drop = plan_retention(names, settings.BACKUP_KEEP_DAILY, settings.BACKUP_KEEP_MONTHLY)
    for n in drop:
        (out / n).unlink(missing_ok=True)
        _manifest_path(out / n).unlink(missing_ok=True)
    return kept, drop


def _offsite(final: Path, mpath: Path) -> dict | None:
    cmd = settings.BACKUP_OFFSITE_CMD
    if not cmd:
        return None
    argv = [a.replace("{file}", str(final)).replace("{manifest}", str(mpath)) for a in shlex.split(cmd)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
        return {"ok": r.returncode == 0, "rc": r.returncode, "err": r.stderr[-300:] if r.returncode else ""}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "rc": None, "err": type(e).__name__}


def list_backups(out: Path | None = None) -> list[dict]:
    out = out or _backup_dir()
    rows = []
    for p in sorted(out.glob("oos-*.dump*"), reverse=True):
        if not _NAME.match(p.name):
            continue
        m = _load_manifest(p, required=False) or {}
        rows.append({"file": p.name, "created_at": m.get("created_at"), "bytes": p.stat().st_size,
                     "encrypted": p.name.endswith(".age"), "tables": len(m.get("tables") or {}),
                     "rows": sum((m.get("tables") or {}).values()), "manifest": bool(m)})
    return rows


def _load_manifest(dump: Path, required=True) -> dict | None:
    mp = _manifest_path(dump)
    if not mp.is_file():
        if required:
            raise DrError(f"manifest 없음: {mp.name} — 기준값 없이는 복구를 검증할 수 없습니다")
        return None
    m = json.loads(mp.read_text(encoding="utf-8"))
    if m.get("format") != FORMAT or m.get("file") != dump.name:
        raise DrError("manifest 형식·파일 이름이 백업과 맞지 않습니다")
    return m


def _latest() -> Path:
    files = [p for p in sorted(_backup_dir().glob("oos-*.dump*"), reverse=True) if _NAME.match(p.name)]
    if not files:
        raise DrError(f"백업이 없습니다: {_backup_dir()}")
    return files[0]


# ── 복호화 스트림(평문을 디스크에 쓰지 않는다) ──────────────────────────────
def _plain_stream(path: Path, identity: str | None):
    """(읽기 파일 객체, 종료 확인 함수) — .age 면 age -d 의 표준출력, 아니면 파일 그대로."""
    if path.name.endswith(".age"):
        _need("age")
        if not identity or not Path(identity).is_file():
            raise DrError("암호화된 백업 — 복호화 키(--identity 또는 AGE_IDENTITY) 파일이 필요합니다")
        err = tempfile.TemporaryFile()
        p = subprocess.Popen(["age", "--decrypt", "-i", identity, str(path)], stdout=subprocess.PIPE, stderr=err)

        def done():
            rc = p.wait()
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()
            err.close()
            if rc != 0:
                raise DrError(f"복호화 실패(키가 다르거나 파일이 변조·잘림): {msg[-300:]}")
        return p.stdout, done
    f = open(path, "rb")
    return f, lambda: None


def _pump(src, dst_proc, h) -> int:
    """src → (해시) → dst_proc.stdin. 대상이 일찍 끝나도(목록만 읽는 pg_restore -l) 해시는 끝까지 계산한다."""
    n, alive = 0, True
    for chunk in iter(lambda: src.read(CHUNK), b""):
        h.update(chunk)
        n += len(chunk)
        if alive:
            try:
                dst_proc.stdin.write(chunk)
            except BrokenPipeError:
                alive = False
    try:
        dst_proc.stdin.close()
    except BrokenPipeError:
        pass
    return n


def verify(path: Path, identity: str | None) -> dict:
    """복원 없이 — 암호문 해시 · 복호화 · 덤프 해시 · 덤프 목록(TOC) 읽기."""
    _need("pg_restore")
    m = _load_manifest(path)
    out = {"file": path.name, "cipher_sha256": _sha256_file(path) == m["cipher"]["sha256"]}
    if not out["cipher_sha256"]:
        out["ok"] = False
        return out
    src, done = _plain_stream(path, identity)
    h = hashlib.sha256()
    with tempfile.TemporaryFile() as toc:
        pr = subprocess.Popen(["pg_restore", "--list"], stdin=subprocess.PIPE, stdout=toc, stderr=subprocess.DEVNULL)
        n = _pump(src, pr, h)
        src.close()
        done()
        rc = pr.wait()
        toc.seek(0)
        entries = sum(1 for ln in toc.read().decode("utf-8", "replace").splitlines() if ln and not ln.startswith(";"))
    out.update(dump_sha256=h.hexdigest() == m["dump"]["sha256"], dump_bytes=n == m["dump"]["bytes"],
               toc_entries=entries, toc_ok=rc == 0 and entries > 0)
    out["ok"] = all(out[k] for k in ("cipher_sha256", "dump_sha256", "dump_bytes", "toc_ok"))
    return out


# ── 복구 훈련 ──────────────────────────────────────────────────────
class _Steps:
    def __init__(self):
        self.rows, self.t0 = [], time.monotonic()

    def run(self, name, fn):
        t = time.monotonic()
        try:
            ok, note = fn()
        except DrError as e:
            ok, note = False, str(e)
        self.rows.append({"step": name, "seconds": round(time.monotonic() - t, 2), "ok": bool(ok), "note": note})
        return ok

    @property
    def ok(self):
        return all(r["ok"] for r in self.rows)


def drill(path: Path, identity: str | None, admin_url: str, keep=False, no_acl=False,
          report_dir: Path | None = None, record=True) -> dict:
    _need("pg_restore")
    started = _utc()
    m = _load_manifest(path)
    admin = _pg(admin_url)
    dbname = f"oos_drill_{started:%Y%m%d%H%M%S}"
    target = {**admin, "dbname": dbname}
    s = _Steps()
    created = False
    verify_out: dict = {}

    def cipher():
        ok = _sha256_file(path) == m["cipher"]["sha256"]
        return ok, "일치" if ok else "불일치 — 파일이 바뀌었거나 다른 백업"

    def roles():
        if no_acl:
            return True, "권한(GRANT) 복원 생략(--no-acl) — 복원 뒤 deploy/sql/roles.sql 로 다시 부여"
        with _connect(admin, autocommit=True) as c:
            have = {r[0] for r in c.execute("SELECT rolname FROM pg_roles").fetchall()}
        missing = [r for r in m.get("acl_roles", []) if r not in have]
        return not missing, ("역할 " + ", ".join(m.get("acl_roles", [])) + " 있음") if not missing else \
            f"대상에 역할 없음: {', '.join(missing)} — deploy/sql/roles.sql 먼저, 또는 --no-acl"

    def create():
        nonlocal created
        with _connect(admin, autocommit=True) as c:
            c.execute(f"CREATE DATABASE {_q(dbname)}")
        created = True
        return True, dbname

    def restore():
        args = ["pg_restore", "--no-owner", "--exit-on-error", "--single-transaction", "--no-password",
                f"--dbname={dbname}"]
        if no_acl:
            args.append("--no-acl")
        src, done = _plain_stream(path, identity)
        h = hashlib.sha256()
        with tempfile.TemporaryFile() as err:
            pr = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=err, env=_env(target))
            n = _pump(src, pr, h)
            src.close()
            done()
            rc = pr.wait()
            err.seek(0)
            emsg = err.read().decode("utf-8", "replace").strip()
        if rc != 0:
            return False, f"pg_restore 실패: {emsg[-500:]}"
        if h.hexdigest() != m["dump"]["sha256"] or n != m["dump"]["bytes"]:
            return False, "덤프 SHA-256/크기 불일치"
        return True, f"덤프 {n / 1e6:.1f}MB · SHA-256 일치"

    def checks():
        nonlocal verify_out
        env = _isolated_env(target)
        mp = _manifest_path(path)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_verify", str(mp)],
                           capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=900)
        try:
            verify_out = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1])
        except (ValueError, IndexError):
            return False, f"검증 프로세스 오류: {(r.stderr or r.stdout)[-600:]}"
        return verify_out.get("ok", False), f"{sum(c['ok'] for c in verify_out['checks'])}/{len(verify_out['checks'])} 통과"

    try:
        if s.run("암호문 무결성(SHA-256)", cipher) and s.run("복원 대상 역할 확인", roles) and \
                s.run("복원 DB 생성", create) and s.run("복호화 + 복원(pg_restore, 단일 트랜잭션)", restore):
            s.run("검증 · 서비스 기동", checks)
    finally:
        if created and not keep:
            try:
                with _connect(admin, autocommit=True) as c:
                    c.execute(f"DROP DATABASE IF EXISTS {_q(dbname)} WITH (FORCE)")
            except Exception as e:                        # noqa: BLE001 — 정리 실패는 보고서에 남긴다
                s.rows.append({"step": "훈련 DB 삭제", "seconds": 0, "ok": False, "note": type(e).__name__})
    finished = _utc()
    snap = datetime.fromisoformat(m["created_at"])
    report = {"ok": s.ok and bool(verify_out.get("ok")), "file": path.name, "snapshot_at": m["created_at"],
              "started_at": started.isoformat(timespec="seconds"), "finished_at": finished.isoformat(timespec="seconds"),
              "rto_seconds": round(time.monotonic() - s.t0, 2),
              "rpo_hours": round((started - snap).total_seconds() / 3600, 2),
              "target": {"host": admin["host"], "port": admin["port"], "database": dbname, "kept": keep and created},
              "encrypted": path.name.endswith(".age"), "dump_bytes": m["dump"]["bytes"],
              "tables": len(m["tables"]), "rows": sum(m["tables"].values()), "steps": s.rows,
              "checks": verify_out.get("checks", []), "tool": FORMAT}
    paths = _write_report(report, report_dir)
    report["report"] = paths
    if record:
        _record_drill(report)
    return report


def _write_report(rep: dict, report_dir: Path | None) -> dict:
    d = report_dir or (ROOT / "var" / "dr")
    d.mkdir(parents=True, exist_ok=True)
    stem = "drill-" + rep["started_at"][:19].replace(":", "").replace("-", "")
    (d / f"{stem}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / f"{stem}.md").write_text(render_md(rep), encoding="utf-8")
    return {"json": str(d / f"{stem}.json"), "md": str(d / f"{stem}.md")}


def render_md(r: dict) -> str:
    ok = "통과" if r["ok"] else "실패"
    lines = [f"# 복구 훈련 보고서 — {r['started_at'][:16].replace('T', ' ')} UTC", "",
             "| 항목 | 값 |", "|---|---|",
             f"| 결과 | **{ok}** |",
             f"| 백업 | `{r['file']}` ({'age 암호화' if r['encrypted'] else '평문'}, 덤프 {r['dump_bytes'] / 1e6:.1f}MB, "
             f"표 {r['tables']}개 · {r['rows']:,}행) |",
             f"| 스냅샷 시각(RPO 기준점) | {r['snapshot_at']} |",
             f"| RPO 실측(훈련 시작 − 스냅샷) | {r['rpo_hours']}시간 |",
             f"| RTO 실측(검증 완료까지) | {r['rto_seconds']}초 |",
             f"| 복원 대상 | {r['target']['host']}:{r['target']['port']} / {r['target']['database']}"
             f"{' (보존)' if r['target']['kept'] else ' (훈련 뒤 삭제)'} |", "",
             "## 단계", "", "| 단계 | 소요(초) | 결과 | 비고 |", "|---|---:|:---:|---|"]
    for s in r["steps"]:
        lines.append(f"| {s['step']} | {s['seconds']} | {'✓' if s['ok'] else '✗'} | {s['note']} |")
    if r.get("checks"):
        lines += ["", "## 검증 항목", "", "| 검증 | 결과 | 내용 |", "|---|:---:|---|"]
        for c in r["checks"]:
            lines.append(f"| {c['name']} | {'✓' if c['ok'] else '✗'} | {c['detail']} |")
    return "\n".join(lines) + "\n"


def _record_drill(rep: dict) -> None:
    """운영 DB 에 마지막 훈련 결과를 남긴다(관리자 헬스 · 감사 로그). 실패해도 훈련 결과는 보고서로 남아 있다."""
    try:
        from app import db
        from app.services import audit
        db.set_meta("dr_drill_last", json.dumps({
            "ok": rep["ok"], "at": rep["finished_at"], "file": rep["file"], "rto_seconds": rep["rto_seconds"],
            "rpo_hours": rep["rpo_hours"], "failed": [s["step"] for s in rep["steps"] if not s["ok"]] +
            [c["name"] for c in rep.get("checks", []) if not c["ok"]]}, ensure_ascii=False))
        audit.record("dr.drill", "system", target=rep["file"],
                     detail={"ok": rep["ok"], "rto_s": rep["rto_seconds"], "rpo_h": rep["rpo_hours"]},
                     result="ok" if rep["ok"] else "fail")
        if not rep["ok"]:
            from app.services import alerts
            alerts.notify("dr:drill", "[DR] 복구 훈련 실패", json.dumps(rep["steps"], ensure_ascii=False)[:1500],
                          level="error")
    except Exception as e:                                  # noqa: BLE001
        print(f"(훈련 기록을 운영 DB 에 남기지 못함: {type(e).__name__})", file=sys.stderr)


def _record_backup(result: dict | None, error: str | None) -> None:
    try:
        from app import db
        from app.services import audit
        prev = json.loads(db.get_meta("backup_last") or "{}")
        now = _utc().isoformat(timespec="seconds")
        if result:
            rec = {"status": "ok", "at": now, "last_ok_at": result["created_at"], "file": result["file"],
                   "bytes": result["bytes"], "encrypted": result["encrypted"], "rls_mode": result["rls_mode"],
                   "offsite": (result.get("offsite") or {}).get("ok")}
            audit.record("backup.create", "system", target=result["file"],
                         detail={"bytes": result["bytes"], "encrypted": result["encrypted"]})
        else:
            rec = {**prev, "status": "fail", "at": now, "error": (error or "")[:300]}
            audit.record("backup.create", "system", detail={"error": (error or "")[:200]}, result="fail")
            from app.services import alerts
            alerts.notify("backup:fail", "[백업] 실패", (error or "")[:1500], level="error", dedup_hours=6)
        db.set_meta("backup_last", json.dumps(rec, ensure_ascii=False))
    except Exception as e:                                  # noqa: BLE001
        print(f"(백업 기록을 운영 DB 에 남기지 못함: {type(e).__name__})", file=sys.stderr)


# ── 복원된 DB 검증(별도 프로세스 — DATABASE_URL 이 훈련 DB 를 가리킨다) ─────────────────
_OTHER_DB_URLS = ("MIGRATION_DATABASE_URL", "ANALYTICS_DATABASE_URL", "BACKUP_DATABASE_URL")


def _isolated_env(target: str) -> dict:
    """검증 프로세스의 환경 — 모든 DB 주소가 복원 대상만 가리키게.
    다른 DB 주소는 '빈 값'으로 둔다(지우면 안 된다): 앱 설정이 .env 를 읽을 때(load_dotenv) 없는 키는 .env 값으로 채우므로,
    지우기만 하면 운영 .env 의 MIGRATION_DATABASE_URL 이 되살아나 검증 프로세스의 init_db(마이그레이션·시작 잔액 분개)가
    운영 DB 에서 돌고, '스키마 최신' 판정도 운영 DB 를 보고 내렸다(결함 31). 빈 값은 '있는 키'라 덮어쓰지 않는다."""
    env = dict(os.environ)
    env.update({k: "" for k in _OTHER_DB_URLS})
    env.update(DATABASE_URL=_sa_url(target), WARMUP_ON_START="false", PYTHONDONTWRITEBYTECODE="1",
               LOG_LEVEL="WARNING", EGRESS_MODE="enforce", REALTIME_ENABLED="false")
    return env


def _verify_restored(manifest_path: str, write: bool = True) -> dict:
    """write=False: 읽기만(운영 복원 직전 검증용 — 시험 가입 같은 흔적을 남기지 않는다). 서비스 기동 확인은 전환 뒤 헬스체크로."""
    from app import crypto, db
    from app.services import audit, contracts, ledger
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    pg = _pg(db.get_engine().url.render_as_string(hide_password=False))
    with _connect(pg) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.rls', 'off', true)")
        fp = _fingerprint(cur)
        conn.rollback()
    want, got = m["tables"], fp["tables"]
    diff = {t: (want.get(t), got.get(t)) for t in set(want) | set(got) if want.get(t) != got.get(t)}
    add("표별 행 수 = manifest", not diff,
        f"표 {len(got)}개 · {sum(got.values()):,}행 일치" if not diff else
        "불일치 " + ", ".join(f"{t}: {a}→{b}" for t, (a, b) in sorted(diff.items())[:8]))

    f0, f1 = m["facts"], fp["facts"]
    av = audit.verify()
    add("감사 로그 해시 체인", av["ok"] and f0["audit_head"] == f1["audit_head"],
        f"{av['rows']}행 재계산 · 머리 {'일치' if f0['audit_head'] == f1['audit_head'] else '불일치'}"
        + ("" if av["ok"] else f" · audit_id {av['broken_at']} 부터 깨짐"))
    w = audit.verify_witnesses()
    add("감사 외부 증인 대조", w["ok"], f"{w['checked']}건 · 불일치 {len(w['mismatches'])}" + (
        f" ({w['note']})" if w.get("note") else ""))
    lv, tb = ledger.verify_closes(), ledger.trial_balance()
    # 빠진 날(gaps)은 원본에도 있던 운영 사실이라 복원 실패로 보지 않는다(표시만) — 복원이 만든 차이는 깨진 날·머리 해시로 본다
    add("원장 마감 사슬 · 시산표", not lv["broken"] and tb["total"] == 0 and f0["ledger_head"] == f1["ledger_head"]
        and f0["journal_sum"] == f1["journal_sum"],
        f"마감 {lv['days']}일 재계산 · 깨진 날 {lv['broken'] or '없음'} · 빠진 날 {len(lv['gaps'])} · 시산표 합 {tb['total']} · "
        f"머리 {'일치' if f0['ledger_head'] == f1['ledger_head'] else '불일치'}")
    dq = contracts.run(record=False)
    crit = {f"{f['dataset']}.{f['check']}" for f in dq["failed"]
            if f["severity"] == "critical" and f["dimension"] != "timeliness"}
    base = set(m.get("dq_critical_failed") or [])
    new = sorted(crit - base)
    add("데이터 계약(critical) — 복원으로 생긴 새 위반", not new,
        f"점수 {dq['score']:.0%} · 백업 시점 위반 {sorted(base) or '없음'} · 새 위반 {new or '없음'}"
        " (적시성은 시간 의존이라 제외)")
    kv = crypto.verify_all()
    n = sum(c["scanned"] for c in kv["columns"].values())
    bad = {k: v for k, v in kv["columns"].items() if v["failed"]}
    add("암호화 열 복호화(키 링)", kv["ok"],
        ("암호화된 값 없음" if n == 0 else f"{n}건 전부 복호화") if kv["ok"] else
        "실패 " + ", ".join(f"{k} {v['failed']}건(키 {v['missing_kids'] or '?'} 없음)" for k, v in bad.items()))
    sec_ok = (f0["rls_forced"] == f1["rls_forced"] and f0["policies"] == f1["policies"]
              and f0["triggers"] == f1["triggers"])
    add("보안 통제 복원(RLS·정책·트리거)", sec_ok,
        f"FORCE RLS 표 {len(f1['rls_forced'])} · 정책 {f1['policies']} · 트리거 {len(f1['triggers'])}"
        + ("" if sec_ok else " — manifest 와 다름"))
    add("스키마 버전", f0["schema_version"] == f1["schema_version"], f"V{f1['schema_version']}")

    # 서비스 기동 — 복원 DB 로 앱을 띄워(마이그레이션 확인 포함) 공개·개인 흐름을 한 번씩
    if not write:
        return {"ok": all(c["ok"] for c in checks), "checks": checks}
    # 앱을 띄우기(init_db) 전에 격리 확인 — 다른 DB 주소가 하나라도 복원 대상 밖을 가리키면 멈춘다(결함 31의 2중 방어)
    from sqlalchemy.engine import make_url

    from app.config import settings

    def where(u):
        x = make_url(u)
        return x.host, x.port, x.database
    stray = [k for k in _OTHER_DB_URLS if getattr(settings, k, "") and where(getattr(settings, k)) != where(settings.DATABASE_URL)]
    add("검증 격리(모든 DB 주소 = 복원 대상)", not stray,
        "다른 DB 를 가리키는 설정 없음" if not stray else f"{stray} 가 복원 대상이 아닌 DB 를 가리킴 — 앱 기동 검증을 건너뜀")
    if stray:
        return {"ok": False, "checks": checks}
    try:
        applied = db.init_db()
        # 스키마 변경(열 추가·형 변경·V0xx SQL)이 필요하면 백업과 앱 버전이 어긋난 것 — 실패.
        # 멱등 데이터 보정(_ONCE 표식이 없는 경우 등)은 정보로만 남긴다.
        schema = [a for a in applied if "." in a.split("→")[0] or re.match(r"^V\d+__", a)]
        data = [a for a in applied if a not in schema]
        add("앱 기동 시 스키마 변경", not schema,
            ("0건(스키마 최신)" + (f" · 멱등 데이터 보정 {len(data)}건" if data else "")) if not schema
            else f"필요한 변경 {schema} — 백업과 앱 버전이 다름(복원 뒤 같은 버전의 앱으로 기동해야 한다)")
        from fastapi.testclient import TestClient

        from app.api import app
        with TestClient(app) as c:
            h = c.get("/health")
            i = c.get("/indices")
            add("공개 API(/health · /indices)", h.status_code == 200 and h.json().get("db_ok") and i.status_code == 200,
                f"/health {h.status_code} · /indices {i.status_code}")
            import secrets
            u, pw = f"drill-{secrets.token_hex(3)}", "Dr1ll-" + secrets.token_urlsafe(9)
            r1 = c.post("/auth/register", json={"username": u, "password": pw})
            r2 = c.post("/auth/login", json={"username": u, "password": pw})
            tok = (r2.json() or {}).get("token") if r2.status_code == 200 else None
            r3 = c.get("/wallet", headers={"Authorization": f"Bearer {tok}"}) if tok else None
            ok = r1.status_code in (200, 201) and tok and r3 is not None and r3.status_code == 200
            add("개인 흐름(가입 → 로그인 → 지갑)", ok,
                f"가입 {r1.status_code} · 로그인 {r2.status_code} · 지갑 {r3.status_code if r3 is not None else '-'}"
                + (f" · 잔액 {r3.json().get('balance'):,}" if ok else ""))
    except Exception as e:                                      # noqa: BLE001
        add("서비스 기동", False, f"{type(e).__name__}: {str(e)[:200]}")
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ── CLI ─────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="백업·복구 훈련")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--plaintext", action="store_true", help="개발용 — 암호화 없이(권장하지 않음)")
    b.add_argument("--allow-plaintext", action="store_true",
                   help="수신자 키가 없으면 평문으로(경고 + manifest·헬스에 표시) — 키 설정 전 기존 VM 호환용")
    b.add_argument("--out")
    sub.add_parser("list")
    v = sub.add_parser("verify")
    v.add_argument("file", nargs="?")
    v.add_argument("--identity", default=os.getenv("AGE_IDENTITY"))
    d = sub.add_parser("drill")
    d.add_argument("file", nargs="?")
    d.add_argument("--identity", default=os.getenv("AGE_IDENTITY"))
    d.add_argument("--target-admin-url", default=os.getenv("DRILL_ADMIN_URL"),
                   help="복원 대상 인스턴스의 관리자 접속(데이터베이스 생성 권한) — 운영과 다른 인스턴스 권장")
    d.add_argument("--keep", action="store_true", help="훈련 DB 를 지우지 않고 남긴다(조사용)")
    d.add_argument("--no-acl", action="store_true", help="GRANT 복원 생략(대상에 역할이 아직 없을 때)")
    d.add_argument("--report-dir")
    d.add_argument("--no-record", action="store_true", help="운영 DB 에 훈련 결과를 남기지 않음")
    x = sub.add_parser("_verify")
    x.add_argument("manifest")
    x.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "backup":
            try:
                plain = a.plaintext or (a.allow_plaintext and not settings.BACKUP_AGE_RECIPIENTS)
                r = backup(plaintext=plain, out_dir=Path(a.out) if a.out else None)
            except DrError as e:
                _record_backup(None, str(e))
                raise
            _record_backup(r, None)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            if not r["encrypted"]:
                print("⚠ 평문 백업 — 운영에서는 BACKUP_AGE_RECIPIENTS 를 설정하세요", file=sys.stderr)
        elif a.cmd == "list":
            for r in list_backups():
                print(f"{r['file']:40s} {r['created_at'] or '-':26s} {r['bytes'] / 1e6:8.2f}MB "
                      f"{'암호화' if r['encrypted'] else '평문':4s} 표 {r['tables']:3d} · {r['rows']:,}행"
                      f"{'' if r['manifest'] else ' (manifest 없음)'}")
        elif a.cmd == "verify":
            r = verify(Path(a.file) if a.file else _latest(), a.identity)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            sys.exit(0 if r["ok"] else 1)
        elif a.cmd == "drill":
            if not a.target_admin_url:
                raise DrError("--target-admin-url(또는 DRILL_ADMIN_URL) 필요 — 데이터베이스를 만들 수 있는 계정")
            r = drill(Path(a.file) if a.file else _latest(), a.identity, a.target_admin_url, keep=a.keep,
                      no_acl=a.no_acl, report_dir=Path(a.report_dir) if a.report_dir else None,
                      record=not a.no_record)
            print(render_md(r))
            print("보고서:", r["report"]["md"])
            sys.exit(0 if r["ok"] else 1)
        elif a.cmd == "_verify":
            r = _verify_restored(a.manifest, write=not a.no_write)
            for c in r["checks"]:
                print(f"  {'✓' if c['ok'] else '✗'} {c['name']} — {c['detail']}", file=sys.stderr)
            print(json.dumps(r, ensure_ascii=False, default=str))
            sys.exit(0 if r["ok"] else 1)
    except DrError as e:
        print("실패:", e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
