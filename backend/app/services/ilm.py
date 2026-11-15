"""데이터 수명주기(ILM) — 오래된 대량 데이터를 압축 파일로 보관(검증)하고 DB 에서 정리, 필요하면 다시 들인다(ADR-0027).

정책(DATASETS): 같이 움직이는 표 묶음 + 기준 날짜 열 + 보관 일수.
  news   : news(published_at) + news_stock·news_sentiment(news_id 로 묶임) — NEWS_RETENTION_DAYS(기본 400일 ≈ 13개월)
  search : search_log(searched_at) — 키워드·종목만(개인정보 없음) — SEARCH_LOG_RETENTION_DAYS(기본 400일)
개인정보가 담긴 표(접속 기록·보안 이벤트 등)는 여기서 다루지 않는다 — 보관 기간이 지나면 '파기'(privacy.retention_purge)이지
따로 보관하면 안 된다(개인정보 최소 보관). 돈(원장·분개)은 5년 보관 의무라 대상이 아니다.

한 달씩(가장 오래된 달부터) — 그 달 전체가 보관 기한보다 오래됐을 때만:
  ① 내보내기  그 달 행을 JSON Lines + gzip 으로(서버 쪽 커서로 흘려 써서 메모리는 일정), 쓰면서 SHA-256
  ② 검증      파일을 처음부터 다시 읽어 해시·표별 행 수가 같은지 → manifest(JSON) → archive_log(DB, 수정·삭제 불가 — V018)
  ③ 정리      그 달 행을 5,000건씩 지운다(짧은 트랜잭션 — 큰 잠금·WAL 폭증 없이). 파일 검증 전에는 한 행도 지우지 않는다.
지우다 멈추면 남은 행은 다음 실행 때 다시 보관된다(같은 행이 두 파일에 있을 수 있다 — 복원은 키가 같으면 건너뛴다).
복원: restore(manifest) — 해시를 먼저 확인하고 ON CONFLICT DO NOTHING 으로 넣는다(지금 있는 행은 건드리지 않는다).
파일은 var/archive(ILM_ARCHIVE_DIR) — 서비스 샌드박스가 쓸 수 있는 곳. 백업의 외부 보관 명령(BACKUP_OFFSITE_CMD)이 있으면
같은 명령으로 보관 파일도 바로 내보내고, 그 복사가 성공한 달만 DB 에서 지운다(파일이 서버 한 곳에만 있는 채로 지우지 않는다).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shlex
import subprocess
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

log = logging.getLogger(__name__)
BATCH = 5000
FORMAT = "oos-ilm-jsonl-gzip/1"

DATASETS = {
    "news": {"title": "뉴스(기사·종목 태깅·감성)", "root": "news", "key": "news_id", "date_col": "published_at",
             "tz": False, "children": ("news_stock", "news_sentiment"), "setting": "NEWS_RETENTION_DAYS"},
    "search": {"title": "검색 기록(키워드·고른 종목)", "root": "search_log", "key": "log_id", "date_col": "searched_at",
               "tz": True, "children": (), "setting": "SEARCH_LOG_RETENTION_DAYS"},
}


def archive_dir() -> Path:
    p = Path(settings.ILM_ARCHIVE_DIR)
    return p if p.is_absolute() else Path(__file__).resolve().parents[2] / p


def retention_days(name: str) -> int:
    return int(getattr(settings, DATASETS[name]["setting"]))


def _month_add(d: date, n: int = 1) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def cutoff(name: str, today: date | None = None) -> date:
    """이 날짜보다 오래된 행이 보관 대상(보관 일수만큼은 DB 에 남는다)."""
    from datetime import timedelta
    return (today or clock.today()) - timedelta(days=retention_days(name))


def _bound(spec, d: date) -> str:
    """월 경계 값 — 날짜 열은 'YYYY-MM-DD', 시각(timestamptz) 열은 그날 00:00 한국시간."""
    return f"{d.isoformat()}T00:00:00+09:00" if spec["tz"] else d.isoformat()


def _where(spec, alias=""):
    c = f"{alias}{spec['date_col']}"
    return f"{c} >= :a AND {c} < :b"


def plan(name: str | None = None, today: date | None = None) -> list[dict]:
    """보관할 달 목록 — [{dataset, start, end, rows}] (그 달 전체가 기한보다 오래된 달만, 오래된 순)."""
    out = []
    for n, spec in DATASETS.items():
        if (name and n != name) or retention_days(n) <= 0:     # 0 = 이 데이터셋은 정리하지 않는다
            continue
        cut = cutoff(n, today)
        first = db.query(f"SELECT MIN({spec['date_col']}) AS d FROM {spec['root']}")[0]["d"]
        if not first:
            continue
        m = date.fromisoformat(str(first)[:10]).replace(day=1)
        while _month_add(m) <= cut:
            nxt = _month_add(m)
            rows = db.query(f"SELECT count(*) AS n FROM {spec['root']} WHERE {_where(spec)}",
                            {"a": _bound(spec, m), "b": _bound(spec, nxt)})[0]["n"]
            if rows:
                out.append({"dataset": n, "start": m.isoformat(), "end": nxt.isoformat(), "rows": int(rows)})
            m = nxt
    return out


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


class _HashWriter(io.RawIOBase):
    """gzip 이 쓰는 압축 바이트를 파일에 쓰면서 SHA-256·크기를 함께 센다(파일을 다시 읽지 않고 해시)."""

    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def writable(self):
        return True

    def write(self, b):
        self.h.update(b)
        self.n += len(b)
        return self.f.write(b)


def _columns(table: str) -> list[str]:
    return [c.name for c in db.metadata.tables[table].columns]


def _types(conn, tables) -> dict:
    rows = conn.execute(text(
        "SELECT a.attrelid::regclass::text AS t, a.attname AS c, format_type(a.atttypid, a.atttypmod) AS ty "
        "FROM pg_attribute a WHERE a.attrelid = ANY(CAST(:ts AS regclass[])) AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attrelid, a.attnum"), {"ts": list(tables)}).mappings().all()
    out = {}
    for r in rows:
        out.setdefault(r["t"], {})[r["c"]] = r["ty"]
    return out


def _export(name: str, start: date, end: date, path: Path) -> dict:
    """한 달 내보내기 — 반환 {rows: {표: 수}, sha256, bytes, raw_bytes}. 쓰는 동안 이름 끝에 .part(검증 전 파일과 구분)."""
    spec = DATASETS[name]
    params = {"a": _bound(spec, start), "b": _bound(spec, end)}
    tables = (spec["root"],) + tuple(spec["children"])
    counts, raw = {t: 0 for t in tables}, 0
    part = path.with_name(path.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    with open(part, "wb") as f:
        hw = _HashWriter(f)
        with gzip.GzipFile(fileobj=hw, mode="wb", compresslevel=6, mtime=0) as gz:   # mtime=0 — 같은 내용이면 같은 파일
            with db.get_engine().connect().execution_options(stream_results=True, yield_per=2000) as conn:
                for t in tables:
                    cols = _columns(t)
                    if t == spec["root"]:
                        sql = (f"SELECT {', '.join(cols)} FROM {t} WHERE {_where(spec)} ORDER BY {spec['key']}")
                    else:
                        sel = ", ".join(f"c.{c}" for c in cols)
                        sql = (f"SELECT {sel} FROM {t} c JOIN {spec['root']} r ON r.{spec['key']} = c.{spec['key']} "
                               f"WHERE {_where(spec, 'r.')} ORDER BY c.{spec['key']}")
                    for row in conn.execute(text(sql), params):
                        line = json.dumps({"t": t, "r": {c: _jsonable(v) for c, v in zip(cols, row)}},
                                          ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                        gz.write(line)
                        raw += len(line)
                        counts[t] += 1
        f.flush()
        os.fsync(f.fileno())
    return {"rows": counts, "sha256": hw.h.hexdigest(), "bytes": hw.n, "raw_bytes": raw, "part": part}


def _sha256(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _scan(path: Path) -> dict:
    """파일 다시 읽기 — 해시·표별 줄 수(보관 직후 검증 — 압축을 끝까지 풀어 본다)."""
    digest, n = _sha256(path)
    counts = {}
    with gzip.open(path, "rb") as gz:
        for line in gz:
            t = json.loads(line)["t"]
            counts[t] = counts.get(t, 0) + 1
    return {"sha256": digest, "bytes": n, "rows": counts}


def _delete_month(name: str, start: date, end: date) -> dict:
    """그 달 행을 BATCH 건씩 지운다(자식 표 먼저). 반환 {표: 지운 수}."""
    spec = DATASETS[name]
    params = {"a": _bound(spec, start), "b": _bound(spec, end)}
    out = {t: 0 for t in (spec["root"],) + tuple(spec["children"])}
    while True:
        with db.transaction() as conn:
            ids = [r[0] for r in conn.execute(text(
                f"SELECT {spec['key']} FROM {spec['root']} WHERE {_where(spec)} ORDER BY {spec['key']} LIMIT :n"),
                {**params, "n": BATCH})]
            if not ids:
                break
            for t in spec["children"]:
                out[t] += conn.execute(text(f"DELETE FROM {t} WHERE {spec['key']} = ANY(:ids)"), {"ids": ids}).rowcount
            out[spec["root"]] += conn.execute(text(f"DELETE FROM {spec['root']} WHERE {spec['key']} = ANY(:ids)"),
                                              {"ids": ids}).rowcount
    return out


def _offsite(gz: Path, man: Path) -> dict | None:
    """외부 보관 — 백업과 같은 명령({file}·{manifest} 자리표시). 없으면 None(서버 디스크에만)."""
    cmd = settings.BACKUP_OFFSITE_CMD
    if not cmd:
        return None
    argv = [a.replace("{file}", str(gz)).replace("{manifest}", str(man)) for a in shlex.split(cmd)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
        return {"ok": r.returncode == 0, "rc": r.returncode, "err": r.stderr[-300:] if r.returncode else ""}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "rc": None, "err": type(e).__name__}


def _vacuum(tables) -> str:
    """정리 뒤 VACUUM(ANALYZE) — 빈자리를 재사용 가능하게·통계 갱신. 앱 계정은 표 주인이 아니라 못 할 수 있다(그럼 자동 VACUUM 몫 —
    V018/_STORAGE 가 이 표들의 자동 VACUUM 기준을 2% 로 낮춰 둠)."""
    try:
        with db.get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            for t in tables:
                c.execute(text(f"VACUUM (ANALYZE) {t}"))
        return "done"
    except Exception as e:                               # noqa: BLE001 — 권한 없음 등: 자동 VACUUM 에 맡긴다
        return f"skipped({type(e).__name__})"


def archive(name: str | None = None, dry_run: bool = False, max_months: int | None = None,
            today: date | None = None) -> dict:
    """보관 기한이 지난 달을 보관 → 검증 → 정리. 반환 {months: [...], files, bytes, rows_deleted, vacuum}."""
    with db.advisory_lock("ilm") as got:              # 스케줄러와 수동 실행이 겹치면 늦게 온 쪽은 건너뛴다
        if not got:
            return {"skipped": "다른 보관 작업이 실행 중"}
        return _archive(name, dry_run, max_months, today)


def _archive(name, dry_run, max_months, today):
    from app.services import audit
    months = plan(name, today)
    if max_months:
        months = months[:max_months]
    if dry_run:
        return {"dry_run": True, "months": months}
    done, touched = [], set()
    for m in months:
        spec = DATASETS[m["dataset"]]
        start, end = date.fromisoformat(m["start"]), date.fromisoformat(m["end"])
        stamp = clock.now().strftime("%Y%m%dT%H%M%S")
        rel = Path(m["dataset"]) / m["start"][:4] / f"{m['dataset']}-{m['start'][:7]}-{stamp}.jsonl.gz"
        path = archive_dir() / rel
        ex = _export(m["dataset"], start, end, path)
        chk = _scan(ex["part"])
        if chk["sha256"] != ex["sha256"] or chk["rows"] != {k: v for k, v in ex["rows"].items() if v}:
            ex["part"].unlink(missing_ok=True)
            raise RuntimeError(f"보관 파일 검증 실패 {rel} — 쓴 내용과 다시 읽은 내용이 다르다(행을 지우지 않았다)")
        os.replace(ex["part"], path)
        with db.transaction() as conn:
            types = _types(conn, (spec["root"],) + tuple(spec["children"]))
        manifest = {"format": FORMAT, "dataset": m["dataset"], "title": spec["title"], "period": [m["start"], m["end"]],
                    "date_column": f"{spec['root']}.{spec['date_col']}", "file": path.name, "sha256": ex["sha256"],
                    "bytes": ex["bytes"], "raw_bytes": ex["raw_bytes"], "rows": ex["rows"], "columns": types,
                    "retention_days": retention_days(m["dataset"]), "created_at": clock.utc().isoformat(timespec="seconds"),
                    "restore": f"python run.py ilm-restore {rel.with_suffix('').with_suffix('')}.manifest.json"}
        mpath = path.with_name(path.name.replace(".jsonl.gz", ".manifest.json"))
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        with db.transaction() as conn:
            aid = conn.execute(text(
                "INSERT INTO archive_log(dataset, period_start, period_end, path, sha256, bytes, raw_bytes, rows, created_at) "
                "VALUES(:d, :a, :b, :p, :h, :n, :r, :rows, now()) RETURNING id"),
                {"d": m["dataset"], "a": m["start"], "b": m["end"], "p": str(rel), "h": ex["sha256"], "n": ex["bytes"],
                 "r": ex["raw_bytes"], "rows": json.dumps(ex["rows"])}).scalar()
        off = _offsite(path, mpath)
        if off is not None and not off["ok"]:            # 외부 사본이 없으면 DB 행을 남긴다(다음 달 작업이 다시 보관·복사)
            log.warning("보관 파일 외부 복사 실패 %s — DB 정리는 건너뜀: %s", rel, off["err"])
            done.append({**m, "file": str(rel), "bytes": ex["bytes"], "raw_bytes": ex["raw_bytes"], "rows": ex["rows"],
                         "deleted": {}, "offsite": off, "ratio": None})
            continue
        deleted = _delete_month(m["dataset"], start, end)
        with db.transaction() as conn:
            conn.execute(text("UPDATE archive_log SET deleted_at = now(), deleted_rows = :n WHERE id = :i"),
                         {"n": sum(deleted.values()), "i": aid})
        touched.update((spec["root"],) + tuple(spec["children"]))
        done.append({**m, "file": str(rel), "bytes": ex["bytes"], "raw_bytes": ex["raw_bytes"], "rows": ex["rows"],
                     "deleted": deleted, "offsite": off,
                     "ratio": round(ex["raw_bytes"] / ex["bytes"], 1) if ex["bytes"] else None})
        log.info("보관 %s %s~%s — %s행 → %s(%.1f MB), DB 에서 %s행 정리", m["dataset"], m["start"], m["end"],
                 sum(ex["rows"].values()), rel, ex["bytes"] / 1048576, sum(deleted.values()))
    out = {"months": done, "files": len(done), "bytes": sum(d["bytes"] for d in done),
           "rows_deleted": sum(sum(d["deleted"].values()) for d in done),
           "offsite_failed": [f"{d['dataset']}:{d['start'][:7]}" for d in done if d.get("offsite") and not d["offsite"]["ok"]]}
    if done:
        out["vacuum"] = _vacuum(sorted(touched))
        audit.record("ilm.archive", "system", detail={k: v for k, v in out.items() if k != "months"} |
                     {"periods": [f"{d['dataset']}:{d['start'][:7]}" for d in done]})
    return out


def _resolve(manifest: str | Path) -> Path:
    p = Path(manifest)
    if not p.is_absolute() and not p.exists():
        p = archive_dir() / p
    base = archive_dir().resolve()
    rp = p.resolve()
    if base not in rp.parents:                           # 보관 폴더 밖 파일은 들이지 않는다(임의 파일을 DB 에 넣는 경로 차단)
        raise ValueError(f"보관 폴더({base}) 안의 manifest 만 복원할 수 있다")
    return rp


def restore(manifest: str | Path, actor: str = "system") -> dict:
    """보관 파일을 DB 로 다시 들인다 — 해시·행 수가 manifest·archive_log 와 같을 때만. 이미 있는 행(키 같음)은 건너뛴다."""
    from app.services import audit
    mp = _resolve(manifest)
    man = json.loads(mp.read_text(encoding="utf-8"))
    if man.get("format") != FORMAT or man.get("dataset") not in DATASETS:
        raise ValueError("알 수 없는 보관 형식")
    gz = mp.with_name(man["file"])
    digest, _ = _sha256(gz)                              # 압축을 풀기 전에 해시부터 — 변조·손상 파일은 열어 보지도 않는다
    if digest != man["sha256"]:
        raise ValueError(f"해시 불일치 — 파일이 바뀌었거나 깨졌다({gz.name})")
    rec = db.query("SELECT id, sha256 FROM archive_log WHERE sha256 = :h ORDER BY id DESC LIMIT 1", {"h": man["sha256"]})
    if not rec:
        raise ValueError("archive_log 에 없는 파일 — 이 서비스가 만든 보관 파일만 복원한다")
    spec = DATASETS[man["dataset"]]
    tables = (spec["root"],) + tuple(spec["children"])
    inserted = {t: 0 for t in tables}
    in_file = {t: 0 for t in tables}
    buf = {t: [] for t in tables}

    def flush(conn, t):
        if not buf[t]:
            return
        cols = list(buf[t][0].keys())
        res = conn.execute(text(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                                "ON CONFLICT DO NOTHING"), buf[t])
        inserted[t] += max(res.rowcount, 0)
        buf[t].clear()

    with db.transaction() as conn:
        with gzip.open(gz, "rb") as f:
            for line in f:
                o = json.loads(line)
                t = o["t"]
                if t not in buf:
                    raise ValueError(f"보관 파일에 모르는 표 {t}")
                buf[t].append(o["r"])
                in_file[t] += 1
                if len(buf[t]) >= 1000:
                    flush(conn, t)
        for t in tables:                                 # 부모 먼저(자식이 고아가 되지 않게 — 외래키는 없지만 같은 순서)
            flush(conn, t)
        conn.execute(text("UPDATE archive_log SET restored_at = now() WHERE id = :i"), {"i": rec[0]["id"]})
    out = {"dataset": man["dataset"], "period": man["period"], "file": gz.name, "rows_in_file": in_file,
           "inserted": inserted, "skipped_existing": {t: in_file[t] - inserted[t] for t in tables}}
    audit.record("ilm.restore", actor, target=gz.name, detail={"period": man["period"], "inserted": inserted})
    return out


def verify() -> dict:
    """archive_log 의 모든 파일을 다시 해시 — 없어진·바뀐 파일을 찾는다(정기 점검·진단 프로브)."""
    rows = db.query("SELECT id, dataset, path, sha256, bytes, rows FROM archive_log ORDER BY id")
    bad, ok = [], 0
    for r in rows:
        p = archive_dir() / r["path"]
        if not p.exists():
            bad.append({"id": r["id"], "path": r["path"], "problem": "missing"})
            continue
        if _sha256(p)[0] != r["sha256"]:
            bad.append({"id": r["id"], "path": r["path"], "problem": "sha256_mismatch"})
        else:
            ok += 1
    if rows:
        db.execute("UPDATE archive_log SET verified_at = now() WHERE id = ANY(:ids)",
                   {"ids": [r["id"] for r in rows if r["id"] not in {b["id"] for b in bad}]})
    return {"files": len(rows), "ok": ok, "bad": bad}


def status(today: date | None = None) -> dict:
    """정책·보관 현황 — 데이터셋별 보관 일수·기한·DB 의 기한 지난 행·보관 파일 수·크기."""
    out = {"archive_dir": str(archive_dir()), "datasets": {}}
    agg = {r["dataset"]: r for r in db.query(
        "SELECT dataset, count(*) AS files, COALESCE(sum(bytes), 0) AS bytes, COALESCE(sum(raw_bytes), 0) AS raw_bytes, "
        "max(period_end) AS through, max(created_at) AS last_at FROM archive_log GROUP BY dataset")}
    for n, spec in DATASETS.items():
        cut = cutoff(n, today)
        r = db.query(f"SELECT count(*) FILTER (WHERE {spec['date_col']} < :c) AS n, MIN({spec['date_col']}) AS oldest "
                     f"FROM {spec['root']}", {"c": _bound(spec, cut)})[0]
        due, oldest = r["n"], r["oldest"]
        a = agg.get(n) or {}
        out["datasets"][n] = {"title": spec["title"], "retention_days": retention_days(n), "cutoff": cut.isoformat(),
                              "rows_past_cutoff": int(due), "oldest": str(oldest)[:10] if oldest else None,
                              "files": int(a.get("files") or 0),
                              "bytes": int(a.get("bytes") or 0), "raw_bytes": int(a.get("raw_bytes") or 0),
                              "archived_through": str(a["through"])[:10] if a.get("through") else None,
                              "last_at": a["last_at"].isoformat() if a.get("last_at") else None}
    return out
