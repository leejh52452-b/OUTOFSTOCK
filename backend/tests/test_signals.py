"""오늘의 추천 종목 Top 5 — 기술적 신호 판정 + 한 달 전 추천 사후 검증(미래참조 없음)."""
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.api import app
from app.services import signals

client = TestClient(app)


def _bars(closes, vols=None, opens=None, lows=None, highs=None, start=date(2026, 1, 1)):
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        out.append({"trade_date": (start + timedelta(days=i)).isoformat(), "open": o,
                    "high": highs[i] if highs else max(o, c) * 1.002, "low": lows[i] if lows else min(o, c) * 0.998,
                    "close": c, "volume": (vols[i] if vols else 1000)})
    return out


def _tags(bars):
    return [t for t, _ in signals.evaluate(bars)]


def test_golden_cross_detected():
    closes = [100 - i * 0.5 for i in range(40)] + [80 + i * 1.5 for i in range(8)]
    assert "골든크로스 발생" in _tags(_bars(closes))


def test_rsi_oversold_rebound_detected():
    closes = [100.0] * 20 + [100 - i * 2 for i in range(1, 16)] + [72, 74.5, 77]
    tags = _tags(_bars(closes))
    assert "RSI 과매도 반등" in tags


def test_volume_breakout_detected():
    closes = [100 + (i % 3) * 0.3 for i in range(40)] + [104]
    vols = [1000] * 40 + [5000]
    assert "거래량 돌파 신호" in _tags(_bars(closes, vols=vols))


def test_box_bottom_support_detected():
    closes = [110 + (8 if (i // 5) % 2 else -8) for i in range(70)] + [102.5]
    opens = closes[:-1] + [101.0]
    lows = [c * 0.995 for c in closes[:-1]] + [100.5]
    assert "박스권 하단 지지" in _tags(_bars(closes, opens=opens, lows=lows))


def test_ma_support_detected():
    closes = [100 + i * 1.0 for i in range(40)] + [139, 137, 136.5]
    b = _bars(closes)
    s20 = sum(closes[-20:]) / 20
    b[-1]["low"] = s20 * 1.005                                  # 저가가 20일선에 닿고
    b[-1]["close"] = max(b[-1]["close"], s20 * 1.01)            # 종가는 그 위
    assert "주요 이평선 지지" in _tags(b)


def test_short_history_has_no_signal():
    assert signals.evaluate(_bars([100] * 10)) == []


def test_rank_prefers_distinct_tags_then_fills():
    g = _bars([100 - i * 0.5 for i in range(40)] + [80 + i * 1.5 for i in range(8)])
    v = _bars([100 + (i % 3) * 0.3 for i in range(40)] + [104], vols=[1000] * 40 + [5000])
    picked = signals.rank({"A": g, "B": g, "C": v}, limit=2)
    assert {p["tag"] for p in picked} == {"골든크로스 발생", "거래량 돌파 신호"}   # 같은 신호 2개보다 다양성
    assert len(signals.rank({"A": g, "B": g}, limit=2)) == 2                     # 모자라면 보충


def _insert(ticker, bars):
    with db.transaction() as conn:
        conn.execute(text("DELETE FROM ohlc WHERE ticker=:t"), {"t": ticker})
        conn.execute(text("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) "
                          "VALUES(:t,:d,:o,:h,:l,:c,:v)"),
                     [{"t": ticker, "d": b["trade_date"], "o": b["open"], "h": b["high"], "l": b["low"],
                       "c": b["close"], "v": b["volume"]} for b in bars])


def test_review_is_point_in_time_and_scores_from_entry_close():
    signals.clear_cache()
    start = date(2026, 6, 1)
    # 005930: 기준일(6/1+51)에 골든크로스 → 이후 20% 상승,  000660: 신호 없음(평탄)
    # 원화 가격은 정수 원으로 저장된다(ADR-0027) — 원 단위로 떨어지는 값으로(소수 가격은 반올림돼 비교가 어긋난다)
    up = [100_000 - i * 500 for i in range(40)] + [80_000 + i * 1500 for i in range(8)]
    after = [round(up[-1] * (1 + 0.01 * k)) for k in range(1, 21)]
    _insert("005930", _bars(up + after, start=start))
    _insert("000660", _bars([50_000] * 72, start=start))
    as_of = (start + timedelta(days=len(up) - 1)).isoformat()
    today = start + timedelta(days=len(up) - 1 + 30)
    r = signals.review(days=30, today_date=today)
    assert r["available"] and r["as_of"] == as_of
    top = r["top"]
    assert top["ticker"] == "005930" and top["entry_date"] == as_of and top["entry_price"] == up[-1]
    assert top["return_pct"] > 0                                  # 지금 가격(마지막 봉) 기준 수익
    # 미래 봉을 바꿔도 '그날의 추천'은 그대로(미래참조 없음)
    signals.clear_cache()
    _insert("005930", _bars(up + [x * 0.5 for x in after], start=start))
    r2 = signals.review(days=30, today_date=today)
    assert r2["top"]["ticker"] == "005930" and r2["top"]["return_pct"] < 0


def test_api_top5_shape():
    signals.clear_cache()
    _insert("005930", _bars([100 - i * 0.5 for i in range(40)] + [80 + i * 1.5 for i in range(8)]))
    d = client.get("/reco/top5").json()
    assert d["items"] and d["items"][0]["ticker"] == "005930" and d["items"][0]["tag"]
    assert "투자 권유" in d["note"]
    assert client.get("/reco/top5/review").status_code == 200
