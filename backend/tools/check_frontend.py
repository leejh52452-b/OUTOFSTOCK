"""웹 화면 스크립트 문법 점검 — web/*.html 의 인라인 <script> 와 web/assets/**/*.js 를 node --check 로 확인.

    python tools/check_frontend.py        # CI(.github/workflows/ci.yml lint 작업)와 같은 점검

문법 오류 하나가 그 화면 전체를 멈춘다 — 브라우저를 띄우기 전에 잡는다. node 가 없으면 건너뛴다
(CI 에서는 실패로 본다: 환경 변수 CI 가 있으면 종료 코드 1).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def main():
    node = shutil.which("node")
    if not node:
        print("node 없음 — 건너뜀")
        return 1 if os.getenv("CI") else 0
    targets = []
    with tempfile.TemporaryDirectory() as tmp:
        for html in sorted(WEB.glob("*.html")):
            body = html.read_text(encoding="utf-8")
            for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)):
                p = Path(tmp) / f"{html.stem}.inline{i}.js"
                p.write_text(m.group(1), encoding="utf-8")
                targets.append((f"{html.name} 인라인 #{i}", p))
        targets += [(p.relative_to(WEB).as_posix(), p) for p in sorted(WEB.glob("assets/**/*.js"))]
        bad = 0
        for name, p in targets:
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
            print(("OK   " if r.returncode == 0 else "FAIL ") + name)
            if r.returncode:
                bad += 1
                print(r.stderr.strip()[:2000])
    print(f"{len(targets)}개 중 실패 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
