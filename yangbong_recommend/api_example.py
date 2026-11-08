"""FastAPI 연결 예시 — 홈 화면 두 카드가 부르는 API: GET /api/yangbong/weekly

백엔드에서 바꿀 곳은 load_market_data(), load_baskets(), save_basket() 세 함수뿐이다.
(DB에서 꺼내는 방법은 서버마다 다르므로 비워 둠 — 반환 형식만 맞추면 됨)

주간 배치 (예: 매주 금요일 18:00 이후 1번)
    run_weekly_batch()  → 이번 주 5종목 저장 + 30일 지난 바구니 채점
화면
    GET /api/yangbong/weekly → 아래 JSON (index.html 이 그대로 그림)
"""
import pandas as pd
from fastapi import APIRouter

import yangbong_picks as Y

router = APIRouter()


# ───────── 백엔드가 채울 부분 ─────────

def load_market_data():
    """최근 최소 300거래일치 '날짜 × 종목코드' 표들을 돌려준다.
    return dict(
        open=DataFrame,      # 수정시가
        close=DataFrame,     # 수정종가 (KRX 등락률로 만들 때는 Y.adjusted_close 사용)
        volume=DataFrame,    # 거래량
        mktcap=DataFrame,    # 시가총액
        common=set,          # 코스피 보통주 코드 (우선주·리츠·스팩·ETF 제외)
        industry=dict,       # {코드: 업종명}
        names=dict,          # {코드: 종목명}
        kospi_open=Series, kospi_close=Series,   # 코스피 지수 (비교용)
    )"""
    raise NotImplementedError


def load_baskets() -> list[dict]:
    """저장된 바구니 전체 (오래된 순). 처음에는 history_baskets_2022_2026.csv 를 넣어 두면 된다.
    각 dict: signal_date, buy_date, sell_date, codes(list), names(list), stock_returns(list|None),
             basket_return(float|None), kospi_return(float|None)"""
    raise NotImplementedError


def save_basket(row: dict) -> None:
    """바구니 1개 저장 또는 갱신 (signal_date 기준)."""
    raise NotImplementedError


def seed_rows_from_csv(path="history_baskets_2022_2026.csv") -> list[dict]:
    """초기 데이터 CSV(값은 '|' 로 구분) → save_basket() 에 넣을 dict 목록. DB를 처음 만들 때 1번만."""
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    num = lambda x: float(x) if isinstance(x, str) and x != "" else None
    return [{"signal_date": r["signal_date"], "buy_date": r["buy_date"], "sell_date": r["sell_date"],
             "codes": r["codes"].split("|"), "names": r["names"].split("|"),
             "stock_returns": [num(x) for x in r["stock_returns"].split("|")],
             "basket_return": num(r["basket_return"]), "kospi_return": num(r["kospi_return"])}
            for r in df.to_dict("records")]


# ───────── 주간 배치 ─────────

def run_weekly_batch(as_of=None):
    d = load_market_data()
    day, picks = Y.select_weekly(d["close"], d["volume"], d["mktcap"], d["common"], d["industry"], d["names"], as_of)
    save_basket({"signal_date": day.date().isoformat(), "codes": [p.code for p in picks],
                 "names": [p.name for p in picks], "buy_date": None, "sell_date": None,
                 "stock_returns": None, "basket_return": None, "kospi_return": None})
    # 30일이 지난 바구니 채점
    for b in load_baskets():
        if b["basket_return"] is not None:
            continue
        s = Y.score_basket(d["open"], d["close"], b["signal_date"], b["codes"])
        if s is None:
            continue
        k_buy = d["kospi_open"].get(pd.Timestamp(s["buy_date"]))
        k_sell = d["kospi_close"].get(pd.Timestamp(s["sell_date"]))
        save_basket({**b, "buy_date": s["buy_date"], "sell_date": s["sell_date"],
                     "stock_returns": [s["stock_returns"][c] for c in b["codes"]],
                     "basket_return": s["basket_return"],
                     "kospi_return": (k_sell / k_buy - 1) if k_buy and k_sell else None})


# ───────── 화면용 API ─────────

@router.get("/api/yangbong/weekly")
def weekly():
    baskets = load_baskets()
    latest = baskets[-1]                                            # 이번 주 추천 (아직 채점 전)
    scored = [b for b in baskets if b["basket_return"] is not None]
    last = scored[-1] if scored else None                           # 가장 최근에 30일이 지난 바구니
    st = Y.stats(scored)
    return {
        "as_of": latest["signal_date"],
        "picks": [{"rank": i + 1, "code": c, "name": n} for i, (c, n) in enumerate(zip(latest["codes"], latest["names"]))],
        "last_basket": None if last is None else {
            "buy_date": last["buy_date"], "sell_date": last["sell_date"],
            "basket_return": last["basket_return"], "kospi_return": last["kospi_return"],
            "stocks": [{"code": c, "name": n, "return": r}
                       for c, n, r in zip(last["codes"], last["names"], last["stock_returns"])],
        },
        "stats": {**st, "since": scored[0]["signal_date"] if scored else None},
    }
