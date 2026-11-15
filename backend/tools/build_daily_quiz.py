"""Daily Stock Quiz 문제은행 → app/data/daily_quiz.json

    python tools/build_daily_quiz.py              # web/assets/daily-quiz.js(화면이 쓰는 바로 그 파일)에서 꺼낸다
    python tools/build_daily_quiz.py <경로>       # 다른 위치(팀 저장소 등)의 daily-quiz.js

node 로 평가해 QUIZ_BANK 배열을 그대로 꺼낸다(문자열 템플릿이 많아 정규식 파싱보다 안전 — 필요: node).
서버는 이 JSON 으로 그날 문제를 다시 만들어 채점한다(app/services/quiz.py — 점수를 화면이 정하지 않는다).
화면 파일을 고치면 이 도구를 다시 돌린다 — tests/test_quiz.py 가 원본 파일 해시(source_sha256)로 어긋남을 잡는다.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "assets" / "daily-quiz.js"
OUT = ROOT / "app" / "data" / "daily_quiz.json"


def extract(path: Path) -> list[dict]:
    code = path.read_text(encoding="utf-8") + "\nprocess.stdout.write(JSON.stringify(QUIZ_BANK));"
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, check=True, encoding="utf-8")
    items = json.loads(out.stdout)
    for q in items:
        if q.get("type") not in ("case", "deep") or len(q.get("options") or []) != 4:
            raise SystemExit(f"문제 형식 오류: {q.get('q', '')[:40]}")
    return items


def build(path: Path = SRC) -> dict:
    items = extract(path)
    return {"source": "web/assets/daily-quiz.js (QUIZ_BANK — options[0] 이 정답)",
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "count": len(items), "case": sum(q["type"] == "case" for q in items),
            "deep": sum(q["type"] == "deep" for q in items), "items": items}


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    data = build(src)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT} — {data['count']}문제(상황 {data['case']} · 심화 {data['deep']})")
