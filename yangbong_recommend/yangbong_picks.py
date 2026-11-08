"""양봉이 주간 추천 알고리즘 (5종목 × 20% 분산, 30일 보유)

외부 API 호출 없음 — 백엔드가 DB에서 꺼낸 '날짜 × 종목코드' 표(pandas DataFrame)만 넣으면 된다.
필요 패키지: pandas, numpy

규칙 (매주 마지막 거래일 = 보통 금요일 장 마감 후)
  1. 후보: 그날 시가총액 상위 200 코스피 보통주 (우선주·리츠·스팩·ETF 제외)
           + 거래정지 아님 + 상장 후 200거래일 이상
  2. 최근 60거래일 하루 수익률 표준편차(흔들림)가 후보 중 하위 30%
  3. 종가가 200일 이동평균 위 (장기 상승 흐름)
  4. 시가총액 큰 순서로 고르되, 5종목 모두 다른 업종
채점: 다음 거래일(보통 월요일) 시가 매수 → 매수일 + 30일(휴장이면 직전 거래일) 종가 매도
      종목 수익률의 단순 평균 = 바구니 수익률 (20%씩 나눠 샀다고 가정). 보유 중 상장폐지면 마지막 종가.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

N_PICKS = 5
UNIVERSE_SIZE = 200
VOL_WINDOW = 60
VOL_PCT = 0.30
MA_WINDOW = 200
MIN_HISTORY = 200
HOLD_DAYS = 30


@dataclass
class Pick:
    rank: int
    code: str
    name: str
    industry: str


def eligible(close: pd.DataFrame, volume: pd.DataFrame, mktcap: pd.DataFrame, common: set) -> pd.DataFrame:
    """날짜×종목 True/False: 그날 추천 후보인가.
    close  : 수정종가 (액면분할 등 반영된 가격 — 아래 adjusted_close() 참고)
    volume : 거래량 (0이면 거래정지로 봄)
    mktcap : 그날 시가총액
    common : 추천 가능한 종목코드 집합 (코스피 보통주, 우선주·리츠·스팩·ETF 제외)"""
    cols = [c for c in close.columns if c in common]
    close, volume, mktcap = close[cols], volume.reindex(columns=cols), mktcap.reindex(columns=cols)
    top = mktcap.rank(axis=1, ascending=False) <= UNIVERSE_SIZE
    history = close.notna().cumsum()
    return close.notna() & (volume > 0) & (history >= MIN_HISTORY) & top


def select_weekly(close: pd.DataFrame, volume: pd.DataFrame, mktcap: pd.DataFrame, common: set,
                  industry: dict, names: dict, as_of=None) -> tuple[pd.Timestamp, list[Pick]]:
    """as_of(포함) 이전 마지막 거래일 기준 이번 주 5종목.
    industry: {종목코드: 업종명} — 모르는 종목은 각자 다른 업종으로 취급
    names   : {종목코드: 종목명}"""
    day = close.index[-1] if as_of is None else close.index[close.index <= pd.Timestamp(as_of)][-1]
    hist = close.loc[:day]
    mask = eligible(hist, volume.loc[:day], mktcap.loc[:day], common).loc[day]
    cand = mask[mask].index
    px = hist[cand]
    ret = px.pct_change(fill_method=None).iloc[-VOL_WINDOW:]
    vol = ret.std().where(ret.notna().sum() == VOL_WINDOW)                  # 최근 60거래일 흔들림 (빈 날 있으면 제외)
    vol_rank = vol.rank(pct=True)
    last200 = px.iloc[-MA_WINDOW:]
    ma200 = last200.mean().where(last200.notna().sum() == MA_WINDOW)
    ok = vol_rank[(vol_rank <= VOL_PCT) & (px.iloc[-1] > ma200)].index
    order = mktcap.loc[day, ok].sort_values(ascending=False).index
    picks, seen = [], set()
    for code in order:
        ind = industry.get(code) or f"?{code}"
        if ind in seen:
            continue
        seen.add(ind)
        picks.append(Pick(len(picks) + 1, code, names.get(code, code), industry.get(code, "")))
        if len(picks) == N_PICKS:
            break
    return day, picks


def score_basket(open_: pd.DataFrame, close: pd.DataFrame, signal_date, codes: list[str]) -> dict | None:
    """signal_date(금요일)에 고른 바구니의 30일 성적. 아직 30일이 안 지났으면 None.
    open_/close: 수정시가/수정종가 (날짜×종목)"""
    dates = close.index
    pos = dates.searchsorted(pd.Timestamp(signal_date), side="right")
    if pos >= len(dates):
        return None
    buy = dates[pos]
    target = buy + pd.Timedelta(days=HOLD_DAYS)
    if target > dates[-1]:
        return None
    sell = dates[dates <= target][-1]
    last = close.ffill()
    rets = {}
    for c in codes:
        b = open_.at[buy, c] if c in open_.columns else np.nan
        s = last.at[sell, c] if c in last.columns else np.nan
        rets[c] = float(s / b - 1) if pd.notna(b) and b > 0 and pd.notna(s) else None
    vals = [v for v in rets.values() if v is not None]
    return {"buy_date": buy.date().isoformat(), "sell_date": sell.date().isoformat(),
            "stock_returns": rets, "basket_return": float(np.mean(vals)) if vals else None}


def adjusted_close(raw_close: pd.DataFrame, fluc_rt: pd.DataFrame) -> pd.DataFrame:
    """KRX 일별매매정보로 수정종가 만들기: 등락률(FLUC_RT, %)은 액면분할 등을 반영한 기준가 대비라서,
    등락률을 이어 붙이고 마지막 날 실제 종가에 맞추면 분할 영향이 없는 가격이 된다.
    수정시가 = 실제 시가 × (수정종가 ÷ 실제 종가)."""
    growth = (1 + fluc_rt / 100).where(raw_close.notna())
    adj = growth.cumprod()
    scale = raw_close.ffill().iloc[-1] / adj.ffill().iloc[-1]
    return adj * scale


def stats(baskets: list[dict], won_per_week: int = 1_000_000) -> dict:
    """누적 성적: 승률(바구니 수익 > 0 인 주 비율), 평균 수익, 매주 100만 원씩 담았다면 총 손익."""
    r = [b["basket_return"] for b in baskets if b.get("basket_return") is not None]
    if not r:
        return {"weeks": 0, "wins": 0, "win_rate": None, "avg_return": None, "total_won": 0}
    return {"weeks": len(r), "wins": int(sum(x > 0 for x in r)), "win_rate": float(np.mean([x > 0 for x in r])),
            "avg_return": float(np.mean(r)), "total_won": int(round(sum(r) * won_per_week))}
