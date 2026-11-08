"""연결이 맞는지 확인하는 검증 스크립트 — `python test_yangbong.py` (pytest 없이 실행 가능)

1~3번은 데이터 없이도 통과해야 한다. 4번은 서버 DB에 2026-10-23 전후 시세가 있을 때만 실행된다.
"""
import sys

import numpy as np
import pandas as pd

import api_example as A
import yangbong_picks as Y

ok = lambda msg: print("  통과:", msg)


def test_1_seed_stats():
    """초기 데이터 247주로 계산한 누적 성적이 백테스트 결과와 같아야 한다."""
    rows = A.seed_rows_from_csv()
    s = Y.stats(rows)
    assert s["weeks"] == 247 and s["wins"] == 136, s
    assert abs(s["avg_return"] - 0.015147) < 1e-4, s
    assert abs(s["total_won"] - 3_741_202) <= 2, s
    ok(f"seed 247주 · 136주 수익 · 평균 {s['avg_return']:+.2%} · 누적 {s['total_won']:,}원")


def test_2_api_shape():
    """GET /api/yangbong/weekly 응답 모양이 index.html 이 기대하는 형식이어야 한다."""
    store = {r["signal_date"]: r for r in A.seed_rows_from_csv()}
    store["2026-10-23"] = {"signal_date": "2026-10-23", "codes": ["068270", "138040", "011200", "033780", "021240"],
                           "names": ["셀트리온", "메리츠금융지주", "HMM", "KT&G", "코웨이"], "buy_date": None,
                           "sell_date": None, "stock_returns": None, "basket_return": None, "kospi_return": None}
    A.load_baskets = lambda: [store[k] for k in sorted(store)]
    d = A.weekly()
    assert d["as_of"] == "2026-10-23"
    assert [p["name"] for p in d["picks"]] == ["셀트리온", "메리츠금융지주", "HMM", "KT&G", "코웨이"]
    lb = d["last_basket"]
    assert lb["buy_date"] == "2026-09-28" and lb["sell_date"] == "2026-10-28"
    assert abs(lb["basket_return"] - (-0.021677)) < 1e-5 and len(lb["stocks"]) == 5
    assert {"code", "name", "return"} <= set(lb["stocks"][0])
    assert {"weeks", "wins", "win_rate", "avg_return", "total_won", "since"} <= set(d["stats"])
    ok("API 응답 형식 · 이번 주 5종목 · 최근 바구니 −2.17%")


def test_3_algorithm_toy():
    """작은 가짜 데이터로 규칙 확인: 흔들림 하위 30% · 200일선 위 · 큰 회사 순 · 업종 중복 없음 · 30일 채점."""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2024-01-01", periods=320)
    codes = [f"{i:06d}" for i in range(20)]
    drift = np.linspace(0.002, -0.002, 20)                 # 앞 종목일수록 상승 추세
    vol = np.linspace(0.005, 0.04, 20)                     # 앞 종목일수록 덜 흔들림
    ret = rng.normal(drift, vol, size=(len(dates), 20))
    close = pd.DataFrame(100 * np.exp(np.cumsum(ret, 0)), index=dates, columns=codes)
    volume = pd.DataFrame(1000, index=dates, columns=codes)
    mktcap = pd.DataFrame(np.tile(np.arange(20, 0, -1) * 1e12, (len(dates), 1)), index=dates, columns=codes)   # 앞 종목일수록 큼
    industry = {c: ("금융" if i < 3 else f"업종{i}") for i, c in enumerate(codes)}  # 0,1,2 같은 업종
    day, picks = Y.select_weekly(close.iloc[:300], volume, mktcap, set(codes), industry, {})
    got = [p.code for p in picks]
    assert len(set(industry[c] for c in got)) == len(got), "업종 중복"
    assert got[0] == "000000" and "000001" not in got, got    # 금융은 1개만
    s = Y.score_basket(close, close, dates[250], got)
    assert s["sell_date"] <= (pd.Timestamp(s["buy_date"]) + pd.Timedelta(days=30)).date().isoformat()
    ok(f"가짜 데이터 선정 {got} · 30일 채점 {s['buy_date']}→{s['sell_date']}")


def test_4_real_data():
    """(선택) 서버 데이터로 2026-10-23 금요일 5종목이 백테스트와 같아야 한다."""
    try:
        d = A.load_market_data()
    except NotImplementedError:
        print("  건너뜀: load_market_data() 아직 구현 전")
        return
    day, picks = Y.select_weekly(d["close"], d["volume"], d["mktcap"], d["common"], d["industry"], d["names"], "2026-10-23")
    names = [p.name for p in picks]
    expected = ["셀트리온", "메리츠금융지주", "HMM", "KT&G", "코웨이"]
    assert str(day.date()) == "2026-10-23", day
    assert names == expected, f"{names} ≠ {expected} — 수정주가·보통주 목록·업종 데이터를 확인"
    ok(f"실제 데이터 2026-10-23 → {names}")


if __name__ == "__main__":
    fails = 0
    for t in (test_1_seed_stats, test_2_api_shape, test_3_algorithm_toy, test_4_real_data):
        print(t.__name__)
        try:
            t()
        except Exception as e:
            fails += 1
            print("  실패:", e)
    print("\n전부 통과" if not fails else f"\n실패 {fails}개")
    sys.exit(1 if fails else 0)
