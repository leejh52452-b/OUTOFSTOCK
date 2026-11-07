"""
통계 5개 -> 가상 종목 일봉(OHLCV) 생성

  python virtual_stock_generator.py --season 1
  -> season_01.json 
  python virtual_stock_generator.py --season 1 --csv
  -> season_01.json + season_01.csv (엑셀 확인용)

시즌마다 가상 종목 5개. 시나리오(bull | bear | sideways | crash_rebound)는
종목마다 랜덤, 5개가 모두 같은 시나리오가 되지는 않게 한다.
"""
import argparse, csv, json, math
from pathlib import Path

import numpy as np

STOCKS = 5                     # 시즌마다 만들 가상 종목 수 (종목 A~E)
WARMUP, BARS = 120, 65         # 지표용 사전 봉 / 시즌 동안 재생할 봉
BASE_VOLUME = {"대형주": 1_500_000, "중형주": 300_000, "소형주": 120_000}
START_PRICE = (2_000, 300_000)                 # 시작가 공통 범위(원)
PRICE_LIMIT = {"대형주": (20_000, None),        # 체급별로 어색한 끝만 제한 (하한, 상한)
               "중형주": (5_000, None),
               "소형주": (None, 50_000)}
WARMUP_LIMIT = {"대형주": 0.40, "중형주": 0.50, "소형주": 0.60}   # 사전 봉 120개 동안 시작~끝 변동 한도
SECTORS = ["전기전자", "화학", "의약품", "운수장비", "금융업", "서비스업", "철강금속", "음식료품"]

# 시즌 장세: [(구간 비율, 하루 추세 추가분, 흔들림 배수), ...], 기본값
SCENARIOS = {
    "bull":          [(1.0, +0.003, 1.0)],
    "bear":          [(1.0, -0.003, 1.2)],
    "sideways":      [(1.0,  0.000, 0.8)],
    "crash_rebound": [(0.45, 0.000, 0.9), (0.15, -0.022, 2.0), (0.40, +0.006, 1.3)],
}


def tick(p: float) -> int:
    """KRX 호가단위로 맞추기"""
    for limit, unit in [(2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)]:
        if p < limit:
            return int(round(p / unit) * unit)
    return int(round(p / 1000) * 1000)


def season_plan(scenario: str):
    """봉마다의 (추세 추가분, 흔들림 배수). 사전 봉은 평소대로, 시나리오는 시즌 구간에만."""
    trend, shake = [0.0] * WARMUP, [1.0] * WARMUP
    for i, (part, t, s) in enumerate(SCENARIOS[scenario]):
        length = BARS - (len(trend) - WARMUP) if i == len(SCENARIOS[scenario]) - 1 else round(BARS * part)
        trend += [t] * length
        shake += [s] * length
    return trend, shake


def start_price(group: str, rng) -> float:
    """공통 범위에서 체급별 하한·상한만 적용하고, 자릿수 기준으로 고르게 뽑는다"""
    floor, ceil = PRICE_LIMIT.get(group, (None, None))
    low, high = floor or START_PRICE[0], ceil or START_PRICE[1]
    return math.exp(rng.uniform(math.log(low), math.log(high)))


def make_stock(st: dict, group: str, base_volume: int, scenario: str, rng) -> list:
    trend, shake = season_plan(scenario)
    price = start_price(group, rng)
    memory = 1.0                                   # 최근 흔들림 기억 (평균 1)
    candles = []
    # 변동성만으로도 생기는 -10% 급락 확률은 빼고, 모자란 만큼만 급락 이벤트로 추가
    natural = 0.5 * (1 + math.erf(math.log(0.9) / (st["volatility"] * math.sqrt(2))))
    crash_p = max(0.0, st["crash_rate"] - natural)

    for i in range(WARMUP + BARS):
        # ① 오늘 수익률 = 추세 + 변동성 × 최근 흔들림 × 무작위
        z = rng.normal()
        r = st["trend"] + trend[i] + st["volatility"] * memory * shake[i] * z
        if rng.random() < crash_p:                 # ② 가끔 급락: 차분한 종목은 덜, 출렁이는 종목은 더 크게
            r -= rng.uniform(0.12, 0.12 + 1.5 * st["volatility"])
        memory = 0.85 * memory + 0.15 * abs(z) * 1.25   # 어제 크게 흔들렸으면 오늘도 크게

        # ③ 캔들: 시가(작은 갭) -> 종가 -> 위아래 꼬리, ±30% 제한
        lo, hi = price * 0.7, price * 1.3
        o = np.clip(price * (1 + rng.normal(0, st["volatility"] * 0.3)), lo, hi)
        c = np.clip(price * np.exp(r), lo, hi)
        span = max(abs(c - o), c * st["day_range"] * memory * shake[i] * rng.uniform(0.6, 1.4))
        wick = max(span - abs(c - o), c * st["day_range"] * rng.uniform(0.1, 0.4))   # 꼬리 최소 길이
        up = wick * rng.uniform(0.2, 0.8)
        h = min(max(o, c) + up, hi)
        l = max(min(o, c) - (wick - up), lo)

        # ④ 거래량: 평소 수준 × 무작위 × 실제로 크게 움직인 날일수록 많이 (급락일 포함)
        move = abs(np.log(c / price)) / st["volatility"]          # 오늘 움직임이 평소의 몇 배인지
        s = np.sqrt(np.log(1 + (st["volume_swing"] * 0.7) ** 2))   # 움직임 효과가 출렁임을 더하므로 무작위는 70%만
        v = base_volume * rng.lognormal(-s * s / 2, s) * (0.5 + 0.6 * move)

        o, c = tick(o), tick(c)
        candles.append({"o": o, "h": max(tick(h), o, c), "l": min(tick(l), o, c), "c": c, "v": int(v)})
        price = c
    return candles


def fits(scenario: str, candles: list) -> bool:
    """만든 차트가 시나리오 이름과 맞는지 (시즌 구간 기준)"""
    closes = [x["c"] for x in candles[WARMUP - 1:]]
    change = closes[-1] / closes[0] - 1
    if scenario == "bull":                           # +10% ~ +45%
        return 0.10 <= change <= 0.45
    if scenario == "bear":                           # -35% ~ -10%
        return -0.35 <= change <= -0.10
    if scenario == "sideways":                       # 끝이 제자리 + 중간에도 크게 안 벌어짐
        return abs(change) <= 0.08 and max(closes) / min(closes) <= 1.25
    # crash_rebound: 바닥 이전 고점에서 15~50% 빠졌다가, 바닥에서 10~60% 회복
    bottom = closes.index(min(closes))
    peak = max(closes[:bottom + 1])
    return 0.50 <= closes[bottom] / peak <= 0.85 and 1.10 <= closes[-1] / closes[bottom] <= 1.60


def make_matching_stock(st: dict, group: str, base_volume: int, scenario: str, rng) -> list:
    """시나리오와 맞고, 사전 봉도 너무 극단적이지 않은 차트가 나올 때까지 다시 만든다"""
    limit = WARMUP_LIMIT.get(group, 0.5)
    for _ in range(500):
        candles = make_stock(st, group, base_volume, scenario, rng)
        warmup_change = candles[WARMUP - 1]["c"] / candles[0]["c"] - 1
        if abs(warmup_change) <= limit and fits(scenario, candles):
            return candles
    print(f"  ! {scenario} 조건에 맞는 차트를 500번 안에 못 만들었습니다. --seed를 바꿔 다시 실행.")
    return candles


def to_json(season: int, stocks: list) -> str:
    """보기 좋은 JSON: 종목 정보는 한 줄에 하나씩, 캔들은 봉 하나당 한 줄"""
    dump = lambda v: json.dumps(v, ensure_ascii=False)
    lines = ["{", f'  "season": {season},',
             f'  "note": {dump("학습용 가상 주가 (실제 시세 아님)")},', '  "stocks": [']
    for i, s in enumerate(stocks):
        lines.append("    {")
        for key, value in s.items():
            if key != "candles":
                lines.append(f"      {dump(key)}: {dump(value)},")
        candles = [f"        {dump(c)}" for c in s["candles"]]
        lines += ['      "candles": [', ",\n".join(candles), "      ]"]
        lines.append("    }" + ("," if i < len(stocks) - 1 else ""))
    lines += ["  ]", "}"]
    return "\n".join(lines) + "\n"


def save_csv(path: Path, season: int, stocks: list):
    """한 줄 = 봉 하나. 엑셀에서 한글이 안 깨지게 utf-8-sig로 저장"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["시즌", "종목ID", "종목명", "업종", "시총구간", "시나리오",
                    "봉번호", "구간", "시가", "고가", "저가", "종가", "거래량"])
        for s in stocks:
            for i, c in enumerate(s["candles"]):
                part = "사전봉" if i < s["warmup"] else "시즌"
                w.writerow([season, s["id"], s["name"], s["sector"], s["size"], s["scenario"],
                            i + 1, part, c["o"], c["h"], c["l"], c["c"], c["v"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default="stats_summary.json")
    ap.add_argument("--season", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--csv", action="store_true", help="CSV 파일도 함께 저장")
    args = ap.parse_args()

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    groups = summary.get("groups", summary)          # 그룹 통계 (예전 형식 파일도 읽기)
    # KRX 실제 구간에서 잰 시나리오 값이 있으면 기본값 대신 쓴다 (급락 후 반등은 기본값 유지)
    for name, v in summary.get("scenarios", {}).items():
        if name in ("bull", "bear", "sideways"):
            SCENARIOS[name] = [(1.0, v["trend"], v["shake"])]
            print(f"{name}: 실제 구간 {v['windows']}개에서 잰 값 사용 (추세 {v['trend']:+.4f}, 흔들림 {v['shake']:.2f}배)")
    rng = np.random.default_rng(args.seed if args.seed is not None else args.season)

    # 종목 5개의 시나리오를 먼저 정한다. 5개가 전부 같으면 다시 뽑는다.
    while True:
        scenarios = [str(rng.choice(list(SCENARIOS))) for _ in range(STOCKS)]
        if len(set(scenarios)) > 1:
            break

    stocks = []
    for k, scenario in enumerate(scenarios):
        group = str(rng.choice(list(groups)))
        # 그룹 안 25%~75% 범위에서 값을 뽑아 종목마다 성격이 조금씩 다르게
        st = {name: rng.uniform(q[0], q[2]) for name, q in groups[group].items()}
        st["trend"] = groups[group]["trend"][1]    # 추세는 중앙값만 (방향은 시나리오가 정함)
        stocks.append({"id": f"S{args.season:02d}-{k + 1}", "name": f"종목 {chr(65 + k)}",
                       "sector": str(rng.choice(SECTORS)), "size": group,
                       "scenario": scenario, "warmup": WARMUP,
                       "candles": make_matching_stock(st, group, BASE_VOLUME.get(group, 300_000), scenario, rng)})

    out = Path(f"season_{args.season:02d}.json")
    out.write_text(to_json(args.season, stocks), encoding="utf-8")
    for s in stocks:
        print(f'{s["name"]}  {s["size"]}  {s["scenario"]}')
    print(f"-> {out}")
    if args.csv:
        csv_out = out.with_suffix(".csv")
        save_csv(csv_out, args.season, stocks)
        print(f"-> {csv_out}")
