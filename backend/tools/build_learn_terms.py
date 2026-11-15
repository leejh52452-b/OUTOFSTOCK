"""학습하기 용어사전 빌더 — 팀 자료 2개 + 우리 보조지표·매매기법을 JSON 하나로 합친다.

입력
  1) 팀 사이트 assets/terms.js  (169개: 양봉이 비유·쉬운 풀이·난이도·주제·별칭·연관·실습 — 웹 화면 web/assets 와 같은 파일)
     → node 로 평가해 배열을 꺼낸다(문자열 템플릿이 많아 정규식 파싱보다 안전).
  2) 팀 자료조사/OUTOFSTOCK_주식기초용어.docx  (100개: 한 줄 정의·설명·읽는 법·오해 주의·함께 보기)
  3) 우리 쪽 추가분(아래 OUR_*): 기존 보조지표·매매기법 상세(계산·기간·보는 것·주의 / 신호·위험),
     기존 용어 중 팀 사전에 없는 것(체결강도), 실습 시나리오 연결, 다이버전스.

병합 규칙
  · terms.js 가 기준(카드 순서·문구 그대로). docx 용어가 같은 용어(이름·별칭·'·'로 나눈 부분)와
    맞으면 그 카드에 'notes'(한 줄 정의·읽는 법·오해 주의)로 붙인다 — 원문 문구는 고치지 않는다.
  · 어느 카드와도 맞지 않는 docx 용어는 새 카드로 만든다(비유 칸은 '한 줄 정의'로 표시).
  · 연관 용어는 실제로 있는 카드 이름으로만 정리(없는 이름은 버림).

사용
  python tools/build_learn_terms.py <팀 저장소 경로>        # → app/data/learn_terms.json
  (필요: node, python-docx)
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "data" / "learn_terms.json"

# 팀 learn.html 의 CAT_ORDER 와 같은 순서(2026-10 팀 사전 개편: 9개 → 11개 주제)
CAT_ORDER = ["주식 기본", "계좌·세금", "주문·거래", "차트 기초", "보조지표·기법", "기업 분석", "기업 이벤트",
             "시장·경제", "투자 상품", "투자 전략", "위험·주의"]

# docx 에만 있는 용어의 주제(팀 사전의 11개 주제 중 가장 가까운 곳 — 비슷한 기존 카드의 주제를 따름)
DOC_CAT = {
    "액면가": "주식 기본", "주가": "주식 기본", "현재가": "차트 기초", "돌파": "차트 기초", "급등·급락": "차트 기초",
    "자산·부채·자본": "기업 분석", "사업보고서": "기업 이벤트", "자산배분": "투자 전략", "리스크": "위험·주의",
    "대장주": "시장·경제",
}

# ── 우리 쪽 추가분 ────────────────────────────────────────────
# 실습 연결(팀 값이 비어 있을 때만 채움) — 키는 app/services/learn.PRACTICE 참고
OUR_PRACTICE = {"거래량": "volume", "돌파": "volume", "박스권": "sideways",
                "지지선 / 저항선": "ma", "다이버전스": "divergence"}
OUR_ALIAS = {"평균단가": ["평균매입가"], "돌파": ["거래량 돌파", "거래량 돌파 매매"],
             "과매수 / 과매도": ["RSI 과매도 반등"], "골든크로스": ["골든크로스 매매"],
             "데드크로스": ["데드크로스 매도"]}
# 기존 '보조지표' 탭 → 해당 카드의 상세
OUR_INDICATOR_OF = {"이동평균선": "SMA", "RSI": "RSI", "MACD": "MACD", "거래량": "VOL"}
# 기존 '매매기법' 탭 → 해당 카드의 상세
OUR_TECHNIQUE_OF = {"골든크로스": "golden-cross", "데드크로스": "dead-cross",
                    "돌파": "volume-breakout", "과매수 / 과매도": "rsi-rebound"}
OUR_TERMS = [
    {"name": "체결강도", "en": "Execution Strength", "level": "mid", "cat": "주문·거래",
     "analogy": "줄다리기에서 '사자' 팀이 '팔자' 팀보다 얼마나 세게 당기고 있는지 보여주는 힘 측정기예요!",
     "explain": "일정 시간 동안 매수 쪽에서 체결된 수량을 매도 쪽에서 체결된 수량으로 나눈 뒤 100을 곱한 값이에요. "
                "100보다 크면 사려는 힘이, 100보다 작으면 팔려는 힘이 더 강했다는 뜻이에요.\n"
                "다만 짧은 시간의 숫자는 쉽게 흔들리기 때문에 거래량·추세와 함께 봐야 해요.",
     "alias": ["체결 강도"], "related": ["거래량", "호가 (Bid & Ask)", "체결 / 미체결"], "practice": None},
    {"name": "다이버전스", "en": "Divergence", "level": "mid", "cat": "보조지표·기법",
     "analogy": "주가는 아래로 가는데 RSI는 슬쩍 고개를 드는 것처럼, 가격과 지표가 서로 다른 말을 하는 순간이에요!",
     "explain": "주가가 이전 저점보다 더 낮게 내려갔는데 RSI 같은 보조지표의 저점은 오히려 높아지는 현상을 "
                "'상승 다이버전스'라고 해요. 떨어지는 힘이 약해지고 있다는 신호로 봅니다.\n"
                "반대로 주가는 고점을 높이는데 지표의 고점이 낮아지면 '하락 다이버전스'예요. "
                "신호가 나와도 곧바로 방향이 바뀌는 건 아니어서 추세 전환을 함께 확인해야 해요.",
     "alias": ["다이버젼스", "괴리"], "related": ["RSI", "과매수 / 과매도", "추세"], "practice": "divergence"},
]


def norm(s):
    return re.sub(r"[\s/·()\-&+,]", "", str(s).lower())


def team_terms(repo):
    js = (Path(repo) / "assets" / "terms.js").read_text(encoding="utf-8")
    code = js + "\nprocess.stdout.write(JSON.stringify(TERMS));"
    # 코드를 명령줄 인자(-e)가 아니라 표준입력으로 넘긴다 — terms.js 가 커서 윈도 명령줄 한도(32,767자)를 넘었다
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


def doc_terms(repo):
    from docx import Document
    path = Path(repo) / "자료조사" / "OUTOFSTOCK_주식기초용어.docx"
    paras = [p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()]
    starts = [i for i, p in enumerate(paras) if p.startswith("1. 주식 기본 개념")]
    fields = {"한 줄 정의": "define", "설명": "desc", "읽는 법": "howto", "오해 주의": "caution", "함께 보기": "see"}
    items, cur = [], None
    for p in paras[starts[-1]:]:                       # 목차 다음의 본문부터
        if re.match(r"^[1-5]\.\s+", p) and len(p) < 30:
            continue
        fm = re.match(r"^(한 줄 정의|설명|읽는 법|오해 주의|함께 보기)\s*:\s*(.*)$", p)
        if fm and cur is not None:
            cur[fields[fm.group(1)]] = fm.group(2).strip()
            continue
        tm = re.match(r"^(.+?)\s{2,}(.+)$", p)          # "주식  Stock"
        if tm:
            cur = {"name": tm.group(1).strip(), "en": tm.group(2).strip()}
            items.append(cur)
    for it in items:
        it["see"] = [s.strip() for s in it.get("see", "").split(",") if s.strip()]
    return items


def _keys(t):
    base = re.sub(r"\(.*?\)", "", t["name"])
    ks = {norm(t["name"]), norm(base)}
    ks |= {norm(p) for p in re.split(r"[/·]", base) if p.strip()}
    ks |= {norm(i) for i in re.findall(r"\((.*?)\)", t["name"])}
    ks |= {norm(a) for a in t.get("alias", [])}
    return ks


def build(repo):
    from app.services.learn import INDICATORS, TECHNIQUES
    items = []
    for t in team_terms(repo):
        items.append({"name": t["name"], "en": t["en"], "level": t["level"], "cat": t["cat"],
                      "analogy": t["analogy"], "explain": t["explain"], "box": "비유",
                      "alias": list(t.get("alias") or []), "related": list(t.get("related") or []),
                      "practice": t.get("practice"), "notes": [], "detail": None, "src": "team"})
    keys = [(it, _keys(it)) for it in items]

    for d in doc_terms(repo):
        parts = [p for p in d["name"].split("·") if p.strip()]
        hits = [it for it, ks in keys if norm(d["name"]) in ks or any(norm(p) in ks for p in parts)]
        note = {"name": d["name"], "define": d["define"], "howto": d["howto"], "caution": d["caution"]}
        if hits:
            for it in hits:
                it["notes"].append(note)
                it["related"] += [s for s in d["see"] if s not in it["related"]]
            continue
        cat = DOC_CAT.get(d["name"])
        if not cat:
            raise SystemExit(f"새 docx 용어의 주제를 DOC_CAT 에 정해 주세요: {d['name']}")
        alias = [p for p in parts if p != d["name"]] + ([d["name"].replace(" ", "")] if " " in d["name"] else [])
        new = {"name": d["name"], "en": d["en"], "level": "basic", "cat": cat,
               "analogy": d["define"], "explain": d["desc"], "box": "정의", "alias": alias,
               "related": list(d["see"]), "practice": None,
               "notes": [{"name": d["name"], "define": "", "howto": d["howto"], "caution": d["caution"]}],
               "detail": None, "src": "doc"}
        items.append(new)
        keys.append((new, _keys(new)))

    for t in OUR_TERMS:
        new = dict(t, box="비유", notes=[], detail=None, src="ours")
        items.append(new)
        keys.append((new, _keys(new)))

    by_name = {it["name"]: it for it in items}
    ind = {i["key"]: i for i in INDICATORS}
    tech = {t["key"]: t for t in TECHNIQUES}
    for name, k in OUR_INDICATOR_OF.items():
        i = ind[k]
        by_name[name]["detail"] = {"kind": "indicator", "title": i["name"], "desc": i["desc"],
                                   "rows": [["계산", i["calc"]], ["기간", i["period"]],
                                            ["보는 것", i["watch"]], ["주의", i["caution"]]]}
    for name, k in OUR_TECHNIQUE_OF.items():
        t = tech[k]
        det = {"kind": "technique", "title": t["name"], "desc": t["desc"],
               "rows": [["신호", t["signal"]], ["위험", t["risk"]]]}
        cur = by_name[name]
        if cur["detail"]:                                  # 이미 지표 상세가 있으면 뒤에 이어 붙임
            cur["detail"]["rows"] += [[f"{t['name']} · 신호", t["signal"]], [f"{t['name']} · 위험", t["risk"]]]
        else:
            cur["detail"] = det
    for name, key in OUR_PRACTICE.items():
        if not by_name[name]["practice"]:
            by_name[name]["practice"] = key
    for name, al in OUR_ALIAS.items():
        by_name[name]["alias"] += [a for a in al if a not in by_name[name]["alias"]]

    # 연관 용어 → 실제 카드 이름으로 정리
    for it in items:
        seen, rel = set(), []
        for r in it["related"]:
            hit = by_name.get(r) or next((x for x, ks in keys if norm(r) in ks), None)
            if hit and hit is not it and hit["name"] not in seen:
                seen.add(hit["name"])
                rel.append(hit["name"])
        it["related"] = rel

    names = [it["name"] for it in items]
    assert len(names) == len(set(names)), "용어 이름 중복"
    assert all(it["cat"] in CAT_ORDER for it in items)
    return {"version": date.today().isoformat(), "count": len(items), "categories": CAT_ORDER,
            "sources": ["팀 사이트 assets/terms.js (「주식 용어 학습하기 페이지 기획」 기반)",
                        "팀 자료조사 OUTOFSTOCK_주식기초용어.docx (주식 기초 용어 사전)",
                        "OUTOFSTOCK 백엔드 보조지표·매매기법 설명"],
            "items": items}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.path.insert(0, str(ROOT))
    data = build(sys.argv[1])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    from collections import Counter
    print(f"{OUT} — {data['count']}개", dict(Counter(i["src"] for i in data["items"])))
