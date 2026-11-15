"""팀 저장소(GitHub OUTOFSTOCK)의 화면을 서버의 web/ 로 가져오기 — 팀이 화면을 고친 뒤 배포 전에 한 번.

    python tools/sync_web.py <팀 저장소 경로>            # 미리 보기(무엇이 바뀌는지만)
    python tools/sync_web.py <팀 저장소 경로> --apply    # 실제로 복사
    python tools/sync_web.py <팀 저장소 경로> --apply --prune   # 팀 저장소에서 지운 그림·스크립트도 web/ 에서 지움

가져오는 것: 맨 위 *.html · assets/** · images/** (숨김 파일 제외)
가져오지 않는 것: 자료조사/(서버 데이터를 만드는 원본 — 아래 빌더가 읽는다) · README.md · .git 등
지키는 것: web/README.md(팀 저장소에는 '백엔드연동_안내.md')는 그대로 둔다.

팀 저장소에 서버 연동판(assets/oos/runtime.js)이 없으면 멈춘다 — 예전 시안을 덮어쓰면 로그인·저장이 모두 끊긴다.
복사한 뒤에는 꼭:
    python tools/check_frontend.py
    pytest -q tests/test_web_security.py tests/test_web_front.py tests/test_accessibility.py
(인라인 onclick·외부 스크립트·등록 안 된 data-on 함수·서버와 어긋난 용어/퀴즈/설문 문항을 시험이 잡는다)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.webui import PAGES  # noqa: E402

RUNTIME = "assets/oos/runtime.js"
KEEP = {"README.md"}                                            # web/ 에만 있는 문서(지우지도 덮지도 않음)
_INLINE_HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_EXTERNAL = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']?\s*(?:https?:)?//", re.I)


def wanted(team: Path) -> list[str]:
    out = sorted(p.name for p in team.glob("*.html") if p.is_file())
    for sub in ("assets", "images"):
        for p in sorted((team / sub).rglob("*")):
            rel = p.relative_to(team).as_posix()
            if p.is_file() and not any(part.startswith(".") for part in rel.split("/")):
                out.append(rel)
    return out


def lint(name: str, html: str) -> list[str]:
    """복사 전에 바로 보이는 규칙 위반(정밀 검사는 시험이 한다)."""
    body = _SCRIPT.sub("", html)
    warn = []
    if _INLINE_HANDLER.search(body):
        warn.append(f"{name}: 인라인 이벤트 속성(onclick=… 등) — 서버 화면에서는 실행되지 않는다(data-on 으로)")
    if _EXTERNAL.search(html):
        warn.append(f"{name}: 외부 스크립트(<script src=\"https://…\">) — 서버 CSP 가 막는다(assets/ 에 두고 상대 경로로)")
    if 'src="assets/oos/runtime.js"' not in html:
        warn.append(f"{name}: assets/oos/runtime.js 를 불러오지 않는다 — 로그인·저장·헤더가 동작하지 않는다")
    return warn


def plan(team: Path, web: Path) -> dict:
    files = wanted(team)
    add, change, same = [], [], []
    for rel in files:
        dst = web / rel
        if not dst.exists():
            add.append(rel)
        elif dst.read_bytes() != (team / rel).read_bytes():
            change.append(rel)
        else:
            same.append(rel)
    have = [p.relative_to(web).as_posix() for sub in ("assets", "images") for p in sorted((web / sub).rglob("*"))
            if p.is_file()] if web.exists() else []
    gone = sorted(set(have) - set(files))
    new_pages = [f for f in files if f.endswith(".html") and f[:-5] not in PAGES]
    return {"files": files, "add": add, "change": change, "same": same, "gone": gone, "new_pages": new_pages}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="팀 저장소 화면 → web/")
    ap.add_argument("team", help="팀 저장소(OUTOFSTOCK) 폴더")
    ap.add_argument("--apply", action="store_true", help="실제로 복사(없으면 미리 보기)")
    ap.add_argument("--prune", action="store_true", help="팀 저장소에 없는 assets·images 파일을 web/ 에서 지움")
    ap.add_argument("--web", default=str(ROOT / "web"), help=argparse.SUPPRESS)      # 시험용
    a = ap.parse_args(argv)
    team, web = Path(a.team).resolve(), Path(a.web).resolve()

    if not (team / "index.html").is_file():
        print(f"팀 저장소가 아니다(index.html 없음): {team}")
        return 2
    if not (team / RUNTIME).is_file():
        print(f"팀 저장소에 서버 연동판이 없다({RUNTIME} 없음) — 'OUTOFSTOCK-화면연동' 묶음을 먼저 올린 뒤 다시.")
        return 2

    p = plan(team, web)
    warn = [w for f in p["files"] if f.endswith(".html") for w in lint(f, (team / f).read_text(encoding="utf-8"))]
    print(f"팀 저장소: {team}")
    print(f"새 파일 {len(p['add'])} · 바뀐 파일 {len(p['change'])} · 같음 {len(p['same'])} · 팀 저장소에 없는 파일 {len(p['gone'])}")
    for tag, key in (("+", "add"), ("~", "change"), ("-", "gone")):
        for f in p[key]:
            print(f"  {tag} {f}")
    for f in p["new_pages"]:
        print(f"  ! 새 페이지 {f} — app/webui.py 의 PAGES 에 넣어야 서버가 내보낸다")
    for w in warn:
        print(f"  ! {w}")
    data_changed = {f for f in p["add"] + p["change"]} & {"assets/terms.js", "assets/daily-quiz.js"}

    if not a.apply:
        print("미리 보기만 했다 — 복사하려면 --apply")
        return 0
    for rel in p["add"] + p["change"]:
        dst = web / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(team / rel, dst)
    removed = 0
    if a.prune:
        for rel in p["gone"]:
            if rel == RUNTIME or Path(rel).name in KEEP:
                continue
            (web / rel).unlink()
            removed += 1
    print(f"복사 {len(p['add']) + len(p['change'])}개" + (f" · 지움 {removed}개" if a.prune else ""))
    if "assets/terms.js" in data_changed:
        print("  → 용어가 바뀌었다: python tools/build_learn_terms.py " + str(team))
    if "assets/daily-quiz.js" in data_changed:
        print("  → 퀴즈가 바뀌었다: python tools/build_daily_quiz.py")
    print("다음: python tools/check_frontend.py && pytest -q tests/test_web_security.py tests/test_web_front.py tests/test_accessibility.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
