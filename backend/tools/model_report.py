"""모형 결과 문서 — 최신 실행 기록(analytics.model_run)으로 docs/models/model_results.md 를 만든다(자소서·면접용 숫자).

    .venv/bin/python tools/model_report.py            # 실데이터 실행이 있으면 그것, 없으면 합성(표에 '합성' 표시)
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.models import common  # noqa: E402

OUT = ROOT / "docs" / "models" / "model_results.md"


def _row(name, m):
    f = lambda k, pct=False: "–" if m.get(k) is None else (f"{m[k] * 100:.1f}%" if pct else f"{m[k]:.3f}")  # noqa: E731
    return f"| {name} | {f('auc')} | {f('ks')} | {f('pr_auc')} | {f('top10_capture', True)} | {f('brier')} |"


def credit_section(r):
    m = r["metrics"]
    sp = m["split"]
    lines = [f"## 기업 신용위험 — run {r['run_id']} ({'합성 데이터' if r['data_source'] == 'synthetic' else '실데이터'}, "
             f"{r['finished_at'][:10] if r['finished_at'] else ''})",
             f"- 학습 {sp['train_years']} · 검증 {sp['valid_years']} · 시험 {sp['test_years']}",
             f"- 표본(학습/검증/시험) {sp['n']['train']}/{sp['n']['valid']}/{sp['n']['test']}, "
             f"부실 징후 {sp['events']['train']}/{sp['events']['valid']}/{sp['events']['test']}건",
             "", "| 시험 연도 | AUC | KS | PR-AUC | 상위10% 포착 | Brier |", "|---|---|---|---|---|---|",
             _row("로지스틱(보정)", m["logit"]), _row("부스팅(보정)", m["gbm"]),
             _row(f"Altman Z'' (적용 {m['altman_z'].get('coverage', 0):.0%})", m["altman_z"]),
             _row(f"Merton PD (적용 {m['merton'].get('coverage', 0):.0%})", m["merton"]),
             "", "보정표(로지스틱, 시험 연도 10분위: 예측 평균 → 실제 비율)", ""]
    lines += [f"- {c['pred'] * 100:.2f}% → {c['actual'] * 100:.2f}% (n={c['n']})" for c in m["logit"]["calibration"]]
    lines += ["", "위험을 가장 크게 움직인 변수(로지스틱 표준화 계수 상위 5)", ""]
    lines += [f"- {c['label']}: {c['coef']:+.3f}" for c in m["coefficients"][:5]]
    if m.get("sign_dropped"):
        lines += ["", "부호 검사로 뺀 변수(계수 부호가 업무상 기대 방향과 반대 — 공선성 신호)", ""]
        lines += [f"- {d['label']}: {d['coef']:+.3f} (기대 {d['expected']})" for d in m["sign_dropped"]]
    return lines


def return_section(r):
    m = r["metrics"]
    a, b = m["random_kfold"], m["walk_forward"]
    return [f"## 5거래일 수익률 예측 검증 — run {r['run_id']} ({'합성 데이터' if r['data_source'] == 'synthetic' else '실데이터'})",
            f"- {m['tickers']}종목, {m['period'][0]}~{m['period'][1]}, {m['rows']:,}행",
            "", "| 방식 | AUC | IC 평균 | IC t값 | 상·하위 20% 5일 차(비용 후) |", "|---|---|---|---|---|",
            f"| 무작위 K-겹(누수) | {a['auc']} | {a['ic_mean']} | {a['ic_t']} | {a['spread_5d_bps']}bp ({a['spread_after_cost_bps']}bp) |",
            f"| 시간순 + 퍼지 | {b['auc']} | {b['ic_mean']} | {b['ic_t']} | {b['spread_5d_bps']}bp ({b['spread_after_cost_bps']}bp) |",
            *[f"| 기준선: {k} | {v['auc']} | {v['ic_mean']} | {v['ic_t']} | {v['spread_5d_bps']}bp |"
              for k, v in m["baselines"].items()],
            "", f"- 누수 크기(AUC 차): {m['leakage_auc_gap']}", f"- 판정: {m['verdict']}"]


def theme_section(r):
    m = r["metrics"]
    pct = lambda v: "–" if v is None else f"{v * 100:.0f}%"                            # noqa: E731
    num = lambda v: "–" if v is None else v                                            # noqa: E731
    po, cs = m.get("precision_overall") or {}, m.get("cohesion_summary") or {}
    ci = po.get("precision_ci")
    lines = [f"## 테마 분류 검증 — run {r['run_id']} ({'합성·표본' if r['data_source'] == 'synthetic' else '실데이터'}, "
             f"사전 {m.get('dict_version') or r.get('version')})",
             f"- 기간 {' ~ '.join(m.get('period') or [])} · 시장 초과수익 {m.get('universe')}종목",
             f"- 정밀도(사람 라벨): {pct(po.get('precision'))}"
             + (f" (95% 구간 {pct(ci[0])}~{pct(ci[1])}" if ci else " (라벨 없음")
             + f", 맞음 {po.get('tp', 0)} · 틀림 {po.get('fp', 0)} · 놓침 {po.get('fn', 0)})",
             f"- 응집도: 중앙값 z {num(cs.get('median_z'))} · 강함(z≥3) {cs.get('strong')} · 약함(z<1.5) {cs.get('weak')} / {cs.get('themes')}개",
             "", "| 테마 | 종목 수 | 평균 상관 | 응집도 z | 판정 | 정밀도 | ETF∩문서 |", "|---|---|---|---|---|---|---|"]
    for k, t in sorted((m.get("themes") or {}).items(), key=lambda kv: -(kv[1].get("cohesion_z") or -99)):
        c, a = t.get("cohesion") or {}, t.get("agreement") or {}
        lines.append(f"| {t.get('name') or k} | {t.get('members')} | {num(c.get('avg_corr'))} | {num(c.get('z'))} | "
                     f"{c.get('verdict') or '–'} | {pct(t.get('precision'))} | {pct(a.get('etf_confirmed'))} |")
    bs = m.get("precision_by_source") or {}
    if bs:
        lines += ["", "근거별 정밀도: " + " · ".join(f"{k} {pct(v.get('precision'))}(n={v['tp'] + v['fp']})" for k, v in bs.items())]
    cand = (m.get("clusters") or {}).get("candidates") or []
    if cand:
        lines += ["", "새 테마 후보(가격 군집, 기존 테마와 안 겹침)", ""]
        lines += [f"- 상관 {c['avg_corr']} · {c['size']}종목 · {', '.join(c['names'][:8])}" for c in cand[:5]]
    return lines


def _theme_run():
    from app import db
    rows = db.query("SELECT run_id, version, data_source, finished_at, metrics FROM analytics.model_run "
                    "WHERE model = 'theme_classifier' AND COALESCE(metrics->>'kind', '') <> 'planted' "
                    "ORDER BY (data_source = 'real') DESC, run_id DESC LIMIT 1")
    return rows[0] if rows else None


def main():
    lines = ["# 모형 결과(자동 생성 — tools/model_report.py)", ""]
    for model, fn in (("credit_risk", credit_section), ("return_5d", return_section)):
        r = common.latest_run(model, "real") or common.latest_run(model)
        lines += (fn(r) if r else [f"## {model}: 실행 기록 없음"]) + [""]
    tr = _theme_run()
    lines += (theme_section(tr) if tr else ["## 테마 분류 검증: 실행 기록 없음(python run.py theme-eval)"]) + [""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"저장: {OUT}")
    print(json.dumps({"lines": len(lines)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
