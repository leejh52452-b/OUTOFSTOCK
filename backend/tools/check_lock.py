"""requirements.lock(해시 고정) 점검 — 운영 이미지·CI 가 설치하는 목록이 requirements.txt 와 어긋나지 않는가.

    python tools/check_lock.py            # 문제 없으면 0, 있으면 목록을 보여 주고 1

보는 것
  · requirements.txt 의 모든 패키지가 잠금 파일에 있고, 고정 버전이 그 범위(예: fastapi>=0.110,<1)를 만족하는가
  · 잠금 파일의 모든 줄이 '이름==버전' + --hash(sha256) 를 갖는가(해시 없는 줄이 하나라도 있으면 pip 가 해시 검사를 끈다)
다시 만들기(같은 파이썬 3.12 기준):
  uv pip compile requirements.txt --generate-hashes --python-version 3.12 -o requirements.lock
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:                                             # pip 에 들어 있는 사본
    from pip._vendor.packaging.requirements import Requirement  # type: ignore
    from pip._vendor.packaging.utils import canonicalize_name   # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def _logical_lines(text: str) -> list[str]:
    """백슬래시 줄 이음을 합치고 주석·빈 줄을 뺀다."""
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        if cur.strip():
            out.append(cur.strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def problems(req_path: Path = ROOT / "requirements.txt", lock_path: Path = ROOT / "requirements.lock") -> list[str]:
    errs: list[str] = []
    pins: dict[str, str] = {}
    for line in _logical_lines(lock_path.read_text(encoding="utf-8")):
        m = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?==([^\s;]+)", line)
        if not m:
            errs.append(f"잠금 파일 형식: {line[:60]}")
            continue
        name = canonicalize_name(m.group(1))
        pins[name] = m.group(3)
        if not re.search(r"--hash=sha256:[0-9a-f]{64}", line):
            errs.append(f"해시 없음: {name}=={m.group(3)}")
    for line in _logical_lines(req_path.read_text(encoding="utf-8")):
        if line.startswith("-"):
            continue
        req = Requirement(line)
        name = canonicalize_name(req.name)
        if name not in pins:
            errs.append(f"잠금 파일에 없음: {req.name}")
        elif req.specifier and not req.specifier.contains(pins[name], prereleases=True):
            errs.append(f"범위 밖: {req.name} {req.specifier} ↔ 잠금 {pins[name]}")
    return errs


def main() -> int:
    errs = problems()
    if errs:
        print("requirements.lock 점검 실패:")
        for e in errs:
            print("  -", e)
        print("다시 만들기: uv pip compile requirements.txt --generate-hashes --python-version 3.12 -o requirements.lock")
        return 1
    print("requirements.lock 정상 — requirements.txt 의 범위를 모두 만족하고 모든 줄에 해시가 있다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
