"""뉴스 감성 사전 평가 — 라벨 달린 제목(tests/test_sentiment.py 의 CASES 120 + HELDOUT 40)으로 v1·v2 비교.

  python tools/eval_sentiment.py          # 정확도 · 클래스별 정밀도/재현율 · v2 가 틀린 제목
  python tools/eval_sentiment.py "제목"    # 한 제목의 판정과 점수(규칙 만들 때 확인용)

CASES 는 규칙을 만들면서 본 예시라 점수가 높게 나온다. HELDOUT 은 규칙을 확정한 뒤 따로 적은 제목이라
처음 보는 문장에 가까운 참고치다. 실제 수집 기사로 재려면 DB 에서 제목을 뽑아 라벨을 붙여 같은 방식으로 돌리면 된다."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.services import sentiment as S          # noqa: E402


def _report(name, fn, cases):
    got = [fn(t).label for t, _ in cases]
    acc = sum(g == y for g, (_, y) in zip(got, cases)) / len(cases)
    print(f"\n[{name}] 정확도 {acc:.1%} ({sum(g == y for g, (_, y) in zip(got, cases))}/{len(cases)})")
    for lab in ("POS", "NEG", "NEU"):
        tp = sum(g == lab and y == lab for g, (_, y) in zip(got, cases))
        pred = sum(g == lab for g in got)
        real = sum(y == lab for _, y in cases)
        p = tp / pred if pred else 0.0
        r = tp / real if real else 0.0
        print(f"  {lab}: 정밀도 {p:.2f} · 재현율 {r:.2f} (정답 {real}건)")
    return got


def main():
    if len(sys.argv) > 1:
        for t in sys.argv[1:]:
            v1, v2 = S._lexicon_v1(t), S._lexicon_v2(t)
            print(f"{t}\n  v1 {v1.label} {v1.score:+.2f}  |  v2 {v2.label} {v2.score:+.2f}")
        return
    from tests.test_sentiment import CASES, HELDOUT
    for title, cases in (("규칙을 만들며 본 제목 120개", CASES), ("규칙 확정 뒤 새로 적은 제목 40개", HELDOUT)):
        print(f"\n══ {title} ══")
        _report("사전 v1(단어 포함 여부)", S._lexicon_v1, cases)
        got = _report("사전 v2(구절 규칙·반전)", S._lexicon_v2, cases)
        wrong = [(t, y, g) for g, (t, y) in zip(got, cases) if g != y]
        if wrong:
            print("\n  v2 가 틀린 제목:")
            for t, y, g in wrong:
                print(f"    정답 {y} · 판정 {g} — {t}")


if __name__ == "__main__":
    main()
