"""추천 Top 5 진단 — 홈에 '추천할 종목이 없어요'가 뜰 때 원인을 숫자로 확인한다(읽기 전용, DB 를 바꾸지 않음).

VM:  cd ~/stock-backend && .venv/bin/python tools/reco_check.py
     → 화면에 출력하고 같은 내용을 stock-backend/reco_check_result.txt 로 저장(WinSCP 로 내려받아 공유)
보는 것: 시세 출처·기준일·마지막 전체 수집 결과(실패 단계의 오류 요약 포함), 유니버스·일봉 수, 적격/제외 사유,
성향별로 남는 종목 수, 외부 연결(KRX·DART 각 1회 조회 — DB 에는 아무것도 쓰지 않음).
비밀값(API 키·DB 주소)은 출력하지 않는다 — 오류 요약은 키를 가린 것(safe_error)만."""
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)                                   # .env(DB 주소)를 앱과 같은 규칙으로 찾게

from app import db                               # noqa: E402
from app.logging_conf import redact, safe_error  # noqa: E402
from app.services import catalog, quotes, reco_engine as R   # noqa: E402

OUT = ROOT / "reco_check_result.txt"
lines = []


def say(*parts):
    s = " ".join(str(x) for x in parts)
    print(s)
    lines.append(s)


def meta(k):
    try:
        return db.get_meta(k)
    except Exception:                            # noqa: BLE001
        return None


def main():
    say("추천 Top 5 진단 ·", quotes.iso_now(), "· 모델", R.MODEL_VERSION)
    try:
        db.query("SELECT 1")
    except Exception as e:                       # noqa: BLE001 — 원문(주소·비밀번호)은 출력하지 않는다
        say("→ DB 에 연결하지 못했다(", type(e).__name__, "). sudo service postgresql status 로 DB 를 확인.")
        return
    say("== 1. 수집 상태")
    say("시세 출처:", meta("market_source"), "| 시세 기준일:", meta("market_asof"), "| 지금 장 상태:", quotes.kr_phase())
    st = meta("pipeline_real_status")
    if st:
        try:
            s = json.loads(st)
            say("마지막 전체 수집:", s.get("at"), "| 성공:", s.get("ok"), "| 일부 실패:", s.get("partial"))
            for f in (s.get("failed") or []):           # 기록할 때 이미 키를 가렸다(safe_error) — 한 번 더 가림
                say("  실패:", redact(str(f))[:300])
        except ValueError:
            say("마지막 전체 수집: 기록을 읽지 못함")
    else:
        say("마지막 전체 수집: 기록 없음(run.py real 이 한 번도 끝나지 않음)")
    uni = catalog.universe()
    rows = db.query("SELECT ticker, MAX(trade_date) AS hi FROM ohlc WHERE ticker = ANY(:ts) GROUP BY ticker",
                    {"ts": uni})
    say("유니버스:", len(uni), "종목 | 일봉 있는 종목:", len(rows))
    if not rows:
        say("→ 결론: 국내 일봉이 하나도 없다. 국내 시세 수집(KRX)이 아직 안 됐다.")
        return
    latest = max(str(r["hi"])[:10] for r in rows)
    say("일봉 최신일:", latest, "| 그날 봉이 있는 종목:", sum(1 for r in rows if str(r["hi"])[:10] == latest))

    say("== 2. 추천 계산(최신일 기준)")
    p = R.Panel(uni, end=latest)
    lens = sorted(len(v["dates"]) for v in p.series.values())
    if lens:
        say("최근 연속 일봉 수: 최소", lens[0], "/ 중앙", statistics.median(lens), "/ 최대", lens[-1],
            f"| {R.MIN_BARS}거래일 이상인 종목:", sum(1 for x in lens if x >= R.MIN_BARS))
    sc = R.score_universe(p, latest)
    say("적격:", len(sc["rows"]), "| 제외 사유:", dict(Counter(sc["excluded"].values())))
    rows_ok = list(sc["rows"].values())
    vols = sorted(r["f"]["vol"] for r in rows_ok if r["f"]["vol"] is not None)
    mdds = sorted(r["f"]["mdd"] for r in rows_ok if r["f"]["mdd"] is not None)
    if vols:
        say("적격 종목 연변동성: 최소 {:.0%} / 중앙 {:.0%} / 최대 {:.0%}".format(vols[0], statistics.median(vols), vols[-1]))
    if mdds:
        say("적격 종목 6개월 최대낙폭: 가장 작음 {:.0%} / 중앙 {:.0%} / 가장 큼 {:.0%}".format(
            mdds[-1], statistics.median(mdds), mdds[0]))
    for prof, rule in R.PROFILES.items():
        if rule["max_vol"] is None:
            continue
        v_ok = sum(1 for r in rows_ok if r["f"]["vol"] is not None and r["f"]["vol"] <= rule["max_vol"])
        m_ok = sum(1 for r in rows_ok if r["f"]["mdd"] is not None and r["f"]["mdd"] >= rule["min_mdd"])
        say(f"{prof} 기준별 통과: 연변동성 {rule['max_vol']:.0%} 이하 {v_ok}종목 · "
            f"최대낙폭 {-rule['min_mdd']:.0%} 이내 {m_ok}종목 · 둘 다(+부실 제외) "
            f"{sum(1 for r in rows_ok if R.suitable(r, prof))}종목")
    say("== 3. 성향별 Top 5")
    for prof in (None, *R.PROFILES):
        picked, skipped = R.select(sc, p, 5, prof)
        names = [p.meta[x["ticker"]]["name"] for x in picked]
        out = sum(1 for v in skipped.values() if v.startswith("투자성향"))
        say(f"{prof or '성향 없음'}: {len(picked)}개 {names} | 성향 기준 탈락 {out}")
    if not rows_ok:
        say("→ 결론: 적격 종목이 0 — 위 '제외 사유'가 원인(대개 데이터 부족: 최근 연속 일봉이 25거래일 미만).")
    elif not R.select(sc, p, 5, "안정형")[0]:
        say("→ 결론: 데이터는 충분하다. 안정형 기준에서 모두 빠져 '안정형'으로 보면 비어 보인다.")


def probes():
    """외부 연결 확인 — KRX(코스피 하루치)·DART(회사 코드 목록) 각 1번. DB 에는 쓰지 않는다."""
    from datetime import timedelta
    from app.config import settings
    say("== 4. 외부 연결 확인(각 1회 조회)")
    d = quotes.now_kst().date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    if settings.KRX_API_KEY:
        try:
            from app.collectors import krx
            n = len(krx.daily_stocks(d.isoformat(), markets=("KOSPI",)))
            say(f"KRX({d}): 정상 — 코스피 {n}종목" + ("(빈 응답 = 휴장일이거나 아직 미제공)" if not n else ""))
        except Exception as e:                       # noqa: BLE001
            say("KRX: 실패 —", safe_error(e, 300))
    else:
        say("KRX: KRX_API_KEY 없음")
    if settings.DART_API_KEY:
        try:
            from app.collectors import dart
            say(f"DART: 정상 — 회사 코드 {len(dart.get_corp_code_map(settings.DART_API_KEY))}개")
        except Exception as e:                       # noqa: BLE001
            say("DART: 실패 —", safe_error(e, 300))
    else:
        say("DART: DART_API_KEY 없음")


if __name__ == "__main__":
    try:
        main()
        probes()
    finally:
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n저장: {OUT}")
