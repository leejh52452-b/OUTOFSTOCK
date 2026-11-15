#!/usr/bin/env bash
# 보안 점검(3번 정보보호) — 의존성 취약점(pip-audit) + 코드 정적 분석(bandit) + 숨은 문자(Trojan Source).
#   bash tools/security_scan.sh          → 결과 요약 출력(실패 항목이 있으면 종료 코드 1)
# CI(.github/workflows/ci.yml)의 security 작업도 같은 스크립트를 쓴다.
set -u
cd "$(dirname "$0")/.." || exit 1
# 파이썬 실행 파일은 SCAN_PY 로 바꾼다 — CI 의 env PY 는 파이썬 '버전'("3.12")이라 그 이름을 쓰면 명령이 아니어서 전부 실패한다
PY=${SCAN_PY:-python3}
fail=0
$PY -m pip_audit --version >/dev/null 2>&1 && $PY -m bandit --version >/dev/null 2>&1 \
  || $PY -m pip install -q "pip-audit>=2.7,<3" "bandit>=1.7,<2" >/dev/null 2>&1 || true

REQ=requirements.lock; [ -f "$REQ" ] || REQ=requirements.txt   # 운영 이미지가 실제로 설치하는 목록(해시 고정)을 본다
echo "== 1. 의존성 취약점(pip-audit, $REQ)"
if $PY -m pip_audit -r "$REQ" --progress-spinner off; then echo "  알려진 취약점 없음"; else fail=1; fi

echo "== 2. 정적 분석(bandit, 중간 이상 · app·tools) — B608(문자열 SQL)은 검토 완료 항목이라 제외(docs/security/scan_report.md)"
if $PY -m bandit -q -r app tools -ll --skip B608; then echo "  지적 없음"; else fail=1; fi

echo "== 3. 숨은 방향 제어·폭 없는 문자(Trojan Source) — 코드·설정·문서 전부"
if $PY - <<'PY'
import pathlib, sys, unicodedata
# 코드뿐 아니라 설정(nginx·systemd·compose)·문서(검토자가 읽는 것)도 — 문서에 실제 문자가 섞여 있던 일이 있었다(2026-10-22)
SUFFIX = {".py", ".html", ".js", ".ts", ".sql", ".sh", ".md", ".yml", ".yaml", ".conf", ".service", ".timer",
          ".http", ".example", ".txt", ".json", ".toml", ".cfg", ".ini", ".lock"}
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "var"}
INVISIBLE = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x3164, 0xFFA0, 0x115F, 0x1160}   # 폭 없음·한글 채움(사칭)
bad = []
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and (p.suffix in SUFFIX or p.name in ("Dockerfile",)) and not SKIP & set(p.parts):
        text = p.read_text(encoding="utf-8", errors="ignore").removeprefix("\ufeff")       # 파일 맨 앞 BOM 만 허용
        for n, line in enumerate(text.splitlines(), 1):
            if any(unicodedata.bidirectional(c) in ("LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI")
                   or ord(c) in INVISIBLE or ord(c) == 0xFEFF for c in line):
                bad.append(f"{p}:{n}")
print("\n".join(bad) or "  없음")
sys.exit(1 if bad else 0)
PY
then :; else fail=1; fi
exit $fail
