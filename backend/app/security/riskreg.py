"""위험 등록부 — 자율보안 수행 4단계(자산 식별·중요도 평가 → 위협 시나리오 정의 → 리스크 측정 → 리스크 해결 및 검증)를 코드로.

  위협 시나리오  위협 모델(docs/security/threat_model.md §3 STRIDE 표의 T-xx)을 그대로 읽는다 — 목록을 두 군데 적지 않는다
  리스크 측정    risk_register.json — 가능성(1~5) × 영향(1~5), 고유(통제 전)·잔여(통제 후)
                등급: 1~4 낮음 · 5~9 보통 · 10~14 높음 · 15~25 매우 높음
  리스크 처리    감소·수용·전가·회피 + 담당 + 재검토일. 잔여 10 이상(위험 성향 초과)은 계획 또는 책임자 승인 수용 필수
  검증          위협 모델 '확인' 열에 적힌 시험(파일::함수, 함수 이름…)이 실제로 있는지 구문 트리로 확인 —
                문서가 '시험으로 막았다'고 적었는데 그 시험이 지워졌으면 시험이 실패한다(문서가 거짓말하지 않게)
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER_PATH = Path(__file__).with_name("risk_register.json")
THREAT_MODEL_PATH = ROOT / "docs" / "security" / "threat_model.md"
DOC_PATH = ROOT / "docs" / "security" / "risk_register.md"
TREATMENTS = ("감소", "수용", "전가", "회피")


def band(score: int) -> str:
    return "매우 높음" if score >= 15 else "높음" if score >= 10 else "보통" if score >= 5 else "낮음"


def load(path: Path | None = None) -> dict:
    return json.loads((path or REGISTER_PATH).read_text(encoding="utf-8"))


def threats(text: str | None = None) -> list[dict]:
    """위협 모델 §3 표의 행 → {id, flow, threat, controls, checks, residual_text}."""
    text = text if text is not None else THREAT_MODEL_PATH.read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if not re.match(r"^\| T-\d{2} \|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        out.append({"id": cells[0], "flow": cells[1], "threat": cells[2], "controls": cells[3], "checks": cells[4],
                    "residual_text": cells[5]})
    return out


# ── '확인' 열 해석 ─────────────────────────────────────────────────────────
_TOKEN = re.compile(r"`([^`]+)`")
_FILE_FN = re.compile(r"^(?:tests/)?(test_\w+\.py)(?:::(test_\w+?)(…)?)?$")
_BARE_FN = re.compile(r"^(test_\w+?)(…)?$")


def _test_index(root: Path) -> dict[str, set[str]]:
    from app.security.assessment import _py_symbols
    return {p.name: {n for n in _py_symbols(p) if n.startswith("test_")} for p in sorted((root / "tests").glob("test_*.py"))}


def resolve_checks(cell: str, extra: list[str] | None = None, root: Path = ROOT, index: dict | None = None) -> dict:
    """확인 칸 → {tests: [확인된 시험], missing: [없는 시험 참조], other: [기타 근거(실측 ID·도구)]}."""
    idx = index if index is not None else _test_index(root)
    ok, missing, other = [], [], []
    for tok in _TOKEN.findall(cell) + list(extra or ()):
        tok = tok.strip()
        m = _FILE_FN.match(tok)
        if m:
            fname, fn, ell = m.groups()
            names = idx.get(fname)
            if names is None:
                missing.append(tok)
            elif not fn:
                ok.append(fname)
            elif (fn in names) or (ell and any(n.startswith(fn) for n in names)):
                ok.append(f"{fname}::{fn}{ell or ''}")
            else:
                missing.append(tok)
            continue
        m = _BARE_FN.match(tok)
        if m:
            fn, ell = m.groups()
            hit = next((f for f, names in idx.items() if fn in names or (ell and any(n.startswith(fn) for n in names))), None)
            (ok if hit else missing).append(f"{hit}::{fn}{ell or ''}" if hit else tok)
            continue
        if re.match(r"^[\w./-]+\.(md|json)$", tok):
            cands = [root / tok, root / "docs" / "security" / tok, root / "docs" / tok]
            (other if any(c.is_file() for c in cands) else missing).append(tok)
            continue
        other.append(tok)
    return {"tests": ok, "missing": missing, "other": other}


# ── 평가 ─────────────────────────────────────────────────────────────────
def evaluate(today: date | None = None, root: Path = ROOT) -> dict:
    """등록부 + 위협 모델 → 행·문제 목록. today 를 주면 재검토 기한 초과도 본다(운영 점검)."""
    reg = load()
    model = {t["id"]: t for t in threats(None if root == ROOT else (root / "docs/security/threat_model.md").read_text(
        encoding="utf-8"))}
    idx = _test_index(root)
    risks, problems = reg["risks"], []
    for tid in sorted(set(model) - set(risks)):
        problems.append(f"{tid}: 위협 모델에 있으나 점수·처리가 없다")
    for tid in sorted(set(risks) - set(model)):
        problems.append(f"{tid}: 등록부에만 있다(위협 모델에 없음)")
    rows = []
    for tid in sorted(set(model) & set(risks)):
        t, r = model[tid], risks[tid]
        (li, ii), (lr, ir) = r["inherent"], r["residual"]
        for v in (li, ii, lr, ir):
            if v not in (1, 2, 3, 4, 5):
                problems.append(f"{tid}: 점수는 1~5")
        si, sr = li * ii, lr * ir
        if sr > si:
            problems.append(f"{tid}: 잔여 위험({sr})이 고유 위험({si})보다 크다")
        if r.get("treatment") not in TREATMENTS:
            problems.append(f"{tid}: 처리 방법은 {'/'.join(TREATMENTS)}")
        if r.get("owner") not in reg["owners"]:
            problems.append(f"{tid}: 담당이 owners 에 없다")
        if r.get("treatment") == "수용" and not r.get("accepted_by"):
            problems.append(f"{tid}: 수용은 승인자(accepted_by) 필요")
        if sr > reg["appetite"] and not (r.get("plan") and r.get("due")) and not r.get("accepted_by"):
            problems.append(f"{tid}: 잔여 {sr}(위험 성향 {reg['appetite']} 초과) — 계획+기한(due) 또는 승인 수용 필요")
        chk = resolve_checks(t["checks"], r.get("tests"), root, idx)
        problems += [f"{tid}: '확인'의 시험이 없다 — {m}" for m in chk["missing"]]
        if not chk["tests"] and not r.get("manual"):
            problems.append(f"{tid}: 자동 확인(시험)이 하나도 없다 — 시험을 붙이거나 manual 사유")
        try:
            reviewed = date.fromisoformat(r["reviewed"])
        except (KeyError, ValueError):
            problems.append(f"{tid}: 재검토일(reviewed) 형식 YYYY-MM-DD")
            reviewed = None
        nxt = reviewed + timedelta(days=int(reg["review_days"])) if reviewed else None
        rows.append({"id": tid, "flow": t["flow"], "threat": t["threat"], "asset": r["asset"],
                     "inherent": [li, ii], "inherent_score": si, "inherent_band": band(si),
                     "residual": [lr, ir], "residual_score": sr, "residual_band": band(sr),
                     "treatment": r.get("treatment"), "owner": r.get("owner"),
                     "owner_name": reg["owners"].get(r.get("owner"), "?"), "plan": r.get("plan", ""),
                     "accepted_by": r.get("accepted_by", ""), "manual": r.get("manual", ""),
                     "reviewed": r.get("reviewed"), "next_review": nxt.isoformat() if nxt else None,
                     "overdue": bool(today and nxt and today > nxt), "checks": chk})
    over = [x["id"] for x in rows if x["overdue"]]
    return {"rows": rows, "problems": problems, "overdue": over, "appetite": reg["appetite"],
            "review_days": reg["review_days"],
            "summary": {"threats": len(rows), "tests": sum(len(x["checks"]["tests"]) for x in rows),
                        "manual": sum(1 for x in rows if x["manual"]),
                        "inherent": {b: sum(1 for x in rows if x["inherent_band"] == b) for b in
                                     ("매우 높음", "높음", "보통", "낮음")},
                        "residual": {b: sum(1 for x in rows if x["residual_band"] == b) for b in
                                     ("매우 높음", "높음", "보통", "낮음")},
                        "above_appetite": [x["id"] for x in rows if x["residual_score"] > reg["appetite"]]}}


def heatmap(rows: list[dict], which: str) -> list[list[list[str]]]:
    """[영향 5..1][가능성 1..5] → 위협 ID 목록."""
    grid = [[[] for _ in range(5)] for _ in range(5)]
    for r in rows:
        lk, im = r[which]
        grid[5 - im][lk - 1].append(r["id"])
    return grid


def _heat_md(rows: list[dict], which: str) -> list[str]:
    g = heatmap(rows, which)
    out = ["| 영향 ＼ 가능성 | 1 드묾 | 2 낮음 | 3 보통 | 4 높음 | 5 매우 높음 |", "|---|---|---|---|---|---|"]
    names = {5: "5 심각", 4: "4 큼", 3: "3 보통", 2: "2 낮음", 1: "1 미미"}
    for k, line in enumerate(g):
        im = 5 - k
        cells = []
        for lk, ids in enumerate(line, start=1):
            s = lk * im
            mark = "🟥" if s >= 15 else "🟧" if s >= 10 else "🟨" if s >= 5 else "🟩"
            cells.append(f"{mark} {' '.join(ids)}" if ids else mark)
        out.append(f"| {names[im]} | " + " | ".join(cells) + " |")
    return out


def render_markdown() -> str:
    from app.security import assets
    ev, inv = evaluate(), assets.inventory()
    s = ev["summary"]
    L = ["# 자산·위험 등록부 — OUTOFSTOCK", "",
         "> 생성: `python run.py assurance-docs` · 원천: 자산은 코드(분류 등록부·접근 정책·허용 목록·잠금 파일·배포 구성), "
         "위협은 [`threat_model.md`](threat_model.md) §3, 점수·처리는 `app/security/risk_register.json`.",
         "> 자율보안 수행 4단계 — **자산 식별·중요도 평가 → 위협 시나리오 정의 → 리스크 측정 → 리스크 해결 및 검증**을 한 문서로.",
         "", "## 1. 자산 식별·중요도", "",
         "중요도(데이터·구성요소) = 기밀성 + 무결성 + 가용성(각 1~3) → 8~9 **1등급(핵심)** · 6~7 2등급(중요) · 3~5 3등급(일반). "
         "업무 서비스는 접근 정책의 중요도(critical → 1등급), 외부 연동은 등록부의 중요도(높음 → 1등급).", "",
         "| 자산 | 개수 | 1등급 | 2등급 | 3등급 |", "|---|---:|---:|---:|---:|"]
    for key, label in (("data", "데이터(표)"), ("services", "업무 서비스(정책 동작)"), ("external", "외부 연동(호스트)"),
                       ("components", "구성요소")):
        d = inv["summary"][key]
        L.append(f"| {label} | {d['total']} | {d[assets.GRADES[1]]} | {d[assets.GRADES[2]]} | {d[assets.GRADES[3]]} |")
    sw = inv["software"]
    L.append(f"| 소프트웨어(해시 고정 패키지) | {sw['packages']} | — | — | — |")
    L += ["", "### 구성요소와 신뢰 구역", "", "| 구성요소 | 구역 | 기밀성·무결성·가용성 | 중요도 |", "|---|---|---|---|"]
    for c in inv["components"]:
        L.append(f"| {c['desc']} | {c['zone']} | {c['c']}·{c['i']}·{c['a']} | {assets.GRADES[c['grade']]} |")
    L += ["", "```mermaid", "flowchart LR",
          "  subgraph EXT[외부]", "    U[이용자 브라우저]", "    SRC[외부 원천·결제·알림]", "  end",
          "  subgraph DMZ[DMZ — 경계]", "    EDGE[nginx 경계]", "    EGR[egress 프록시]", "  end",
          "  subgraph IN[내부 — 앱]", "    APP[API·화면]", "    SCH[스케줄러]", "    RT[실시간 수집기]", "  end",
          "  subgraph RST[제한 — 데이터]", "    DB[(PostgreSQL)]", "    BK[(암호화 백업)]", "  end",
          "  subgraph MGT[관리]", "    OPS[관리 단말]", "  end",
          "  U -- DF1 로그인·DF2 돈·DF3 채팅/SSE (TLS) --> EDGE --> APP",
          "  OPS -- DF5 기기 인증서·TOTP·JIT --> EDGE",
          "  APP -- SQL·RLS --> DB", "  SCH --> DB", "  RT --> DB",
          "  SCH -- DF4 수집 --> EGR --> SRC", "  RT --> EGR",
          "  DB -- DF6 age 암호화 --> BK", "  OPS -- DF7 배포(설치본 SHA-256·기준선) --> APP", "```", "",
          "### 데이터 자산(분류 등록부의 모든 표)", "",
          "| 표 | 분류 | 기·무·가 | 중요도 | 보호 | 설명 |", "|---|---|---|---|---|---|"]
    for d in inv["data"]:
        L.append(f"| `{d['name']}` | {d['class']} | {d['c']}·{d['i']}·{d['a']} | {assets.GRADES[d['grade']]} | "
                 f"{', '.join(d['protection']) or '—'} | {d['desc']} |")
    L += ["", "### 외부 연동(허용 목록 ↔ 제3자 등록부)", "", "| 호스트 | 용도 | 제3자 | 중요도 |", "|---|---|---|---|"]
    for e in inv["external"]:
        L.append(f"| {e['host']} | {e['purpose']} | {e['supplier'] or '❗등록부 누락'} | {e['importance'] or '—'} |")
    L += ["", f"업무 서비스 {inv['summary']['services']['total']}개 동작의 중요도·기기 결속은 `app/security/policy.json`"
          " — 1등급(critical)은 과부하 때도 끝까지 받고(대량접속제어), 돈·관리 동작은 기기 키 증명이 필요하다.", "",
          "## 2. 리스크 측정 — 히트맵", "",
          f"위협 {s['threats']}개 · 등급 기준 1~4 🟩 낮음 · 5~9 🟨 보통 · 10~14 🟧 높음 · 15~25 🟥 매우 높음. "
          f"**위험 성향(수용 기준) {ev['appetite']}** — 잔여가 이보다 크면 계획+기한 또는 책임자 승인 수용이 있어야 한다(시험이 강제).", "",
          "### 고유 위험(통제 전)", ""] + _heat_md(ev["rows"], "inherent") + ["", "### 잔여 위험(통제 후)", ""] + \
        _heat_md(ev["rows"], "residual") + [
          "", f"고유: 매우 높음 {s['inherent']['매우 높음']} · 높음 {s['inherent']['높음']} · 보통 {s['inherent']['보통']} · "
          f"낮음 {s['inherent']['낮음']} → 잔여: 매우 높음 {s['residual']['매우 높음']} · 높음 {s['residual']['높음']} · "
          f"보통 {s['residual']['보통']} · 낮음 {s['residual']['낮음']}. 위험 성향 초과 잔여: "
          f"{', '.join(s['above_appetite']) or '없음'}.", "",
          "## 3. 위험 등록부(처리·검증)", "",
          "| ID | 위협(흐름) | 자산 | 고유 L×I | 잔여 L×I | 처리 | 담당 | 자동 확인 | 다음 재검토 | 계획·수용 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ev["rows"]:
        threat = re.sub(r"\*\*", "", r["threat"])
        chk = f"시험 {len(r['checks']['tests'])}" + (f" · 수동({r['manual']})" if r["manual"] else "")
        note = r["plan"] or (f"수용: {r['accepted_by']}" if r["accepted_by"] else "")
        L.append(f"| {r['id']} | {threat} ({r['flow']}) | {r['asset']} | {r['inherent'][0]}×{r['inherent'][1]}="
                 f"{r['inherent_score']} {r['inherent_band']} | {r['residual'][0]}×{r['residual'][1]}={r['residual_score']} "
                 f"{r['residual_band']} | {r['treatment']} | {r['owner_name']} | {chk} | {r['next_review']} | {note} |")
    L += ["", "## 4. 검증 — 문서가 거짓말하지 않게", "",
          f"- 위협 모델 '확인' 열 + 등록부의 추가 시험 = 자동 확인 **{s['tests']}건**, 모두 실제로 있는 시험(구문 트리로 확인). "
          f"수동 근거만 있는 위협 {s['manual']}개(사유 기록).",
          "- 시험(`tests/test_risk_register.py`)이 강제하는 것: 위협 모델의 모든 T-xx 에 점수·처리·담당이 있다 · 잔여 ≤ 고유 · "
          "'확인'에 적힌 시험이 모두 있다 · 수용은 승인자 · 위험 성향 초과는 계획+기한 또는 승인.",
          f"- 운영 점검(프로브 `risk_register`)은 여기에 **재검토 기한({ev['review_days']}일) 초과**를 더한다 — 수준진단 2.4·2.5 항목의 근거.",
          "", "## 5. 처리 원칙", "",
          "| 처리 | 뜻 | 이 등록부에서 |", "|---|---|---|",
          "| 감소 | 통제를 더해 가능성·영향을 낮춘다 | 대부분 — 남은 부분은 '계획'에 |",
          "| 수용 | 위험 성향 안에서 받아들인다(승인자 기록) | T-29(계정 잠금 악용 — 규정상 잠금 필수)·T-30(VM 같은 서버) |",
          "| 전가 | 보험·위탁·상위 서비스로 넘긴다 | 계획에 적은 WAF·DDoS 방어·KMS(클라우드 배포 시) |",
          "| 회피 | 기능 자체를 없앤다 | 기각 기능 제거(경로 175 → 120, ADR) · 파일 업로드 없음 |"]
    return "\n".join(L) + "\n"
