"""CI 실패를 GitHub 주석(annotation)으로 — 로그를 열지 않아도 커밋 화면·API(check-runs annotations)에서 원인이 보이게.

    python tools/ci_annotate.py junit pytest-report.xml        실패·오류 난 시험마다 주석 하나(최대 30개)
    python tools/ci_annotate.py tail step.log "이미지 기동"      로그 끝부분(최대 60줄)을 주석 하나로

GitHub Actions 의 작업 명령(::error ...::) 형식만 출력한다. 주석을 만들 것이 없으면 아무것도 출력하지 않는다.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # nosec B405 — CI 가 방금 만든 자기 시험 보고서만 읽는다
from pathlib import Path

MAX_TESTS = 30
MAX_LINES = 60
MAX_CHARS = 4000


def _esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    return _esc(text).replace(":", "%3A").replace(",", "%2C")


def junit(path: str) -> int:
    p = Path(path)
    if not p.is_file():
        print(f"::warning title=ci_annotate::{_esc(path + ' 없음 — pytest 가 보고서를 만들기 전에 멈췄다')}")
        return 0
    root = ET.parse(p).getroot()  # nosec B314 — 위와 같음
    n = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            el = case.find(kind)
            if el is None:
                continue
            n += 1
            if n > MAX_TESTS:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            msg = (el.get("message") or "").strip()
            body = (el.text or "").strip()
            tail = "\n".join(body.splitlines()[-25:])
            text = (msg + "\n\n" + tail).strip()[-MAX_CHARS:]
            file = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
            line = case.get("line")
            loc = f"file={_prop(file)}" + (f",line={int(line) + 1}" if line and line.isdigit() else "")
            print(f"::error {loc},title={_prop(kind + ' ' + name)}::{_esc(text)}")
    if n > MAX_TESTS:
        print(f"::error title=ci_annotate::{_esc(f'실패 {n}개 중 {MAX_TESTS}개만 주석으로 남김')}")
    return 0


def tail(path: str, title: str) -> int:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else f"{path} 없음"
    lines = text.splitlines()[-MAX_LINES:]
    print(f"::error title={_prop(title)}::{_esc(chr(10).join(lines)[-MAX_CHARS:])}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "junit":
        return junit(argv[1])
    if len(argv) >= 2 and argv[0] == "tail":
        return tail(argv[1], argv[2] if len(argv) > 2 else Path(argv[1]).name)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
