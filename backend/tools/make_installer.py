"""VM 설치·업데이트용 한 파일 설치본 — install_outofstock_full.sh (WinSCP 로 이것 하나만 올리면 된다)

    python tools/make_installer.py [출력 경로]          # 기본: dist/install_outofstock_full.sh

VM 에서
    처음   : mkdir -p ~/stock-backend && cd ~/stock-backend && bash ~/install_outofstock_full.sh && bash deploy/vm.sh install
    새 버전: cd ~/stock-backend && bash deploy/vm.sh update ~/install_outofstock_full.sh
             (vm.sh 가 임시 폴더에 먼저 풀어 파일 수·필수 파일을 확인 → 백업 → 교체 → 마이그레이션 → 재시작 → 헬스체크)

담는 것: 저장소 파일 전부. 넣지 않는 것: .env·DB 비밀번호 파일·인증서/키·백업 공개키 파일(운영 값 — 업데이트가 덮어쓰면 안 됨)·
         가상환경·캐시·로컬 백업(var/)·생성물. 같은 내용이면 같은 설치본이 나온다(파일 순서·시각·소유자 고정).
무결성: 압축본의 SHA-256 을 머리말에 적고, 풀기 전에 확인한다 — 전송 중 잘리거나 바뀌면 아무것도 풀지 않고 멈춘다.
"""
from __future__ import annotations

import base64
import fnmatch
import gzip
import hashlib
import io
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
             "var", "snapshots", "dist", "Claude outputs"}
SKIP_PATHS = {"deploy/generated", "deploy/.seeded", "deploy/backup/recipients.txt"}
SKIP_GLOBS = ("*.pyc", "*.pyo", "*.age", "*.bak", "*.db", "*.log", "*.key", "*.p12", "*.pem", "*.crt", "*.csr",
              "*.srl", ".env", ".env.db", ".env.local", ".release-files")
KEEP_IN_TLS = {".gitkeep", ".gitignore", "README.md"}          # deploy/tls 아래는 자리표시·설명만(인증서·키는 서버에서 만든다)
REQUIRED = ("app/api.py", "run.py", "requirements.txt", "requirements.lock", "deploy/vm.sh", ".env.example")
MARK = "OUTOFSTOCK 전체 프로젝트 원클릭 설치"                        # vm.sh update 가 확인하는 표식(바꾸지 말 것)


def files() -> list[str]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        parts = rel.split("/")
        if any(d in SKIP_DIRS for d in parts[:-1]) or (p.is_dir() and parts[-1] in SKIP_DIRS):
            continue
        if not p.is_file() or p.is_symlink():
            continue
        if any(rel == s or rel.startswith(s + "/") for s in SKIP_PATHS):
            continue
        if any(fnmatch.fnmatch(parts[-1], g) for g in SKIP_GLOBS):
            continue
        if rel.startswith("deploy/tls/") and parts[-1] not in KEEP_IN_TLS:
            continue
        out.append(rel)
    missing = [r for r in REQUIRED if r not in out]
    if missing:
        raise SystemExit(f"필수 파일 없음: {missing}")
    return out


def payload(names: list[str], root: Path = ROOT) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in names:
            src = root / rel
            ti = tarfile.TarInfo(rel)
            ti.size = src.stat().st_size
            ti.mode = 0o755 if src.suffix == ".sh" or rel in ("run.py", "scheduler.py") else 0o644
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""   # 재현 가능(같은 내용 = 같은 해시)
            with src.open("rb") as f:
                tar.addfile(ti, f)
    return gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)


def build(out: Path, root: Path = ROOT, names: list[str] | None = None) -> dict:
    """root·names 를 주면 그 폴더의 그 파일들로(변경관리 훈련·시험용 작은 설치본) — 형식은 같다."""
    names = files() if names is None else names
    blob = payload(names, root)
    digest = hashlib.sha256(blob).hexdigest()
    b64 = base64.encodebytes(blob).decode()
    built = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")
    script = f"""#!/usr/bin/env bash
# {MARK} — 빈 폴더(또는 기존 stock-backend)에서 실행하면 그 자리에 파일을 푼다.
#   처음   : mkdir -p ~/stock-backend && cd ~/stock-backend && bash ~/install_outofstock_full.sh && bash deploy/vm.sh install
#   새 버전: cd ~/stock-backend && bash deploy/vm.sh update ~/install_outofstock_full.sh   ← 이쪽 권장(백업·검증·되돌리기 포함)
# OUTOFSTOCK 전체 {len(names)}개 파일 · 만든 시각 {built} · 압축본 SHA-256 {digest}
# 들어 있지 않은 것: .env(API 키)·DB 비밀번호·인증서/키·백업 공개키·가상환경 — 서버에 있는 것을 그대로 쓴다.
set -euo pipefail
command -v base64 >/dev/null && command -v tar >/dev/null && command -v sha256sum >/dev/null \\
  || {{ echo "base64·tar·sha256sum 이 필요합니다" >&2; exit 1; }}
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
grep -q '^__OOS_PAYLOAD_END__$' "$0" && sed -n '/^__OOS_PAYLOAD_BEGIN__$/,/^__OOS_PAYLOAD_END__$/{{/^__OOS_PAYLOAD_/d;p}}' "$0" \
  | base64 -d > "$tmp" 2>/dev/null \
  || {{ echo "설치본이 잘렸습니다(끝 표식 없음·내용 깨짐) — 전송이 끝까지 됐는지 확인하고 다시 올리세요. 아무것도 풀지 않았습니다." >&2; exit 1; }}
echo "{digest}  $tmp" | sha256sum -c --quiet - \\
  || {{ echo "설치본이 손상됐습니다(SHA-256 불일치) — 전송이 끝까지 됐는지 확인하고 다시 올리세요. 아무것도 풀지 않았습니다." >&2; exit 1; }}
tar -xzf "$tmp" --no-same-owner
echo "OUTOFSTOCK {len(names)}개 파일을 풀었습니다 → $(pwd)"
exit 0
__OOS_PAYLOAD_BEGIN__
{b64}__OOS_PAYLOAD_END__
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")
    out.chmod(0o755)
    return {"path": str(out), "files": len(names), "sha256": digest, "bytes": out.stat().st_size}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "install_outofstock_full.sh"
    r = build(target)
    print(f"{r['path']} — 파일 {r['files']}개 · {r['bytes'] / 1e6:.1f}MB · 압축본 SHA-256 {r['sha256'][:16]}…")
