"""양봉이 꾸미기 아이템 → app/data/avatar_items.json (서버가 이 가격으로 돈을 뺀다)

    python tools/build_avatar_items.py              # web/assets/avatar-items.js(화면이 쓰는 바로 그 파일)에서 꺼낸다
    python tools/build_avatar_items.py <경로>       # 다른 위치(팀 저장소 등)의 avatar-items.js

node 로 평가해 아이템 목록(id·부위·등급·이름·가격·설명)을 꺼낸다 — 그림(SVG)은 화면에만 둔다(필요: node).
가격·이름을 화면 파일에서 바꾸면 이 도구를 다시 돌린다 — tests/test_avatar.py 가 원본 파일 해시(source_sha256)로 어긋남을 잡는다
(화면에 보이는 값과 서버가 빼는 돈이 다르면 안 된다).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "assets" / "avatar-items.js"
OUT = ROOT / "app" / "data" / "avatar_items.json"
SLOTS = ("hat", "top", "bottom", "shoes", "glasses", "pet")


def extract(path: Path) -> dict:
    code = ("globalThis.window = globalThis;\n" + path.read_text(encoding="utf-8")
            + "\nprocess.stdout.write(JSON.stringify({slots: AVATAR_SLOTS, tiers: AVATAR_TIERS, classes: AVATAR_CLASSES,"
              " items: AVATAR_ITEMS.map(({id, slot, tier, name, price, desc}) => ({id, slot, tier, name, price, desc}))}));")
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, check=True, encoding="utf-8")
    data = json.loads(out.stdout)
    seen = set()
    for it in data["items"]:
        ok = (isinstance(it.get("id"), str) and 0 < len(it["id"]) <= 20 and it["id"] not in seen
              and it.get("slot") in SLOTS and it.get("tier") in (1, 2, 3, 4, 5)
              and isinstance(it.get("price"), int) and 0 < it["price"] <= 10_000_000_000
              and isinstance(it.get("name"), str) and 0 < len(it["name"]) <= 40)
        if not ok:
            raise SystemExit(f"아이템 형식 오류: {it}")
        seen.add(it["id"])
    if [s["id"] for s in data["slots"]] != list(SLOTS):
        raise SystemExit(f"부위 목록이 서버와 다르다: {[s['id'] for s in data['slots']]}")
    return data


def build(path: Path = SRC) -> dict:
    data = extract(path)
    return {"source": "web/assets/avatar-items.js (AVATAR_ITEMS — 그림은 화면에만)",
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "count": len(data["items"]), **data}


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    data = build(src)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT} — 아이템 {data['count']}개(부위 {len(data['slots'])} · 등급 {len(data['tiers'])})")
