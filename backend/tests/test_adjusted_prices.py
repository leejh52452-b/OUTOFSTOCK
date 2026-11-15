"""수정주가(액면분할·무상증자) — 기간 수익률·52주 신고가·거래량 배수·낙폭·변동성이 분할 날 '−80% 폭락'으로 틀리지 않는지.

점검에서 찾은 결함: 원 종가 비율로 계산하던 곳(분석 계층 스냅샷·월말 피처·1년 낙폭·거래량 급증·업종 순환,
화면용 리스크·차트·추천 모델)이 액면분할 날을 폭락으로 봤다. KRX 등락률(기준가 대비)로 권리 이벤트를 찾아
그 이전 가격을 맞추는지 숫자로 확인한다."""
import math
from datetime import date

from app import db
from app.collectors import krx_history as kh
from app.services import adjust, platform, risk

T = "777770"
SPLIT = 5          # 5:1 액면분할


def _series(n_extra=0, split_back=3, rate=0.2, bad_rates=False, ticker=T, shares=10_000_000):
    """매일 +rate% 오르는 종목, 끝에서 split_back 거래일 전에 5:1 분할(그날 공식 등락률 +1%).
    반환 [(날짜, 행)] 시간순."""
    days = sorted(kh.trading_days(date(2024, 9, 2), date(2025, 12, 30)))[-(300 + n_extra):]
    out, c, sh = [], 50_000.0, shares
    for i, d in enumerate(days):
        if i == 0:
            r = 0.0
        elif i == len(days) - split_back:
            r = 1.0
            c = c * (1 + r / 100) / SPLIT
            sh *= SPLIT
        else:
            r = rate
            c = c * (1 + r / 100)
        close = int(round(c))
        prev = out[-1][1]["close"] if out else close
        official = 0.0 if bad_rates else (r if i == len(days) - split_back else round((close / prev - 1) * 100, 2))
        vol = 100_000 * (SPLIT if sh > shares else 1)
        out.append((d, {"ticker": ticker, "name": "분할테스트", "market": "KOSPI", "sect_tp": None,
                        "open": close, "high": close, "low": close, "close": close, "volume": vol,
                        "value": close * vol, "mktcap": close * sh, "shares": sh, "change_rate": official,
                        "halted": False}))
    return out


def _load(series):
    for d, row in series:
        kh.store_day(d, "K", [row])


# ── 순수 함수 ─────────────────────────────────────────────
def test_event_factor_found_only_on_the_split_day():
    s = _series()
    dates = [d.isoformat() for d, _ in s]
    closes = [r["close"] for _, r in s]
    rates = {d.isoformat(): r["change_rate"] for d, r in s}
    ev = adjust.event_factors(dates, closes, rates)
    assert list(ev) == [dates[-3]]
    assert abs(ev[dates[-3]] - 1 / SPLIT) < 0.002                       # 약 0.2
    bars = adjust.adjust_bars([{"trade_date": d, "close": c, "high": c, "volume": 100} for d, c in zip(dates, closes)], rates)
    assert abs(bars[0]["close"] - closes[0] / SPLIT) / (closes[0] / SPLIT) < 0.003     # 분할 전 가격 ÷ 5
    assert bars[-1]["close"] == closes[-1] and bars[-4]["volume"] == 500                # 거래량 × 5(같은 주식 수)
    rets = [bars[i]["close"] / bars[i - 1]["close"] - 1 for i in range(1, len(bars))]
    assert min(rets) > -0.01                                                          # 가짜 −80% 없음


def test_consistent_rates_mean_no_adjustment_and_bad_rates_are_ignored():
    dates = ["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06"]
    closes = [100, 101, 102, 103]
    good = {"2025-03-04": 1.0, "2025-03-05": 0.99, "2025-03-06": 0.98}
    assert adjust.event_factors(dates, closes, good) == {}
    bars = [{"trade_date": d, "close": c} for d, c in zip(dates, closes)]
    assert adjust.adjust_bars(bars, good) is bars                                     # 이벤트 없으면 그대로
    closes2 = [100, 110, 99, 121]                                                     # 등락률이 늘 0 인 잘못된 자료
    bad = {d: 0.0 for d in dates}
    assert adjust.event_factors(dates, closes2, bad) == {}                            # 어긋난 날이 너무 많음 → 믿지 않음


def test_missing_bar_is_not_mistaken_for_an_event():
    # 3/4(화) 봉이 빠졌다 — 3/3→3/5 원 비율(+3%)이 3/5 공식 등락률(+1%)과 달라도 이벤트가 아니다
    dates, closes = ["2025-03-03", "2025-03-05"], [100, 103]
    assert adjust.event_factors(dates, closes, {"2025-03-05": 1.0}) == {}
    # 주말을 사이에 둔 금→월은 빠진 날이 아니다(진짜 이벤트면 잡힘)
    assert adjust.event_factors(["2025-03-07", "2025-03-10"], [100, 21], {"2025-03-10": 5.0})


# ── 분석 계층 ─────────────────────────────────────────────
def test_snapshot_returns_new_high_and_volume_ratio_use_adjusted_prices():
    s = _series()
    _load(s)
    _load(_series(ticker="777771", bad_rates=True, rate=1.0))        # 매일 +1% 인데 등락률이 늘 0 — 자료를 믿지 않는다
    snap = kh.refresh_snapshot(full_adj=True)
    assert snap["ok"] and snap["adj_events"] == 1
    ev = db.query("SELECT ticker, trade_date, factor FROM analytics.price_adj_event")
    assert len(ev) == 1 and ev[0]["ticker"] == T and abs(ev[0]["factor"] - 1 / SPLIT) < 0.002
    row = db.query("SELECT ret_1d, ret_1w, ret_1m, ret_1y, new_high_52w, vol_ratio20 FROM analytics.mv_stock_snapshot "
                   "WHERE ticker = :t", {"t": T})[0]
    g = 1 + 0.2 / 100
    assert abs(float(row["ret_1w"]) - ((g ** 4) * 1.01 - 1) * 100) < 0.1                  # 분할 날 +1%(공식) 포함
    assert abs(float(row["ret_1m"]) - ((g ** 20) * 1.01 - 1) * 100) < 0.15
    assert abs(float(row["ret_1y"]) - ((g ** 249) * 1.01 - 1) * 100) < 1.0                # 예전: 약 −67%
    assert row["new_high_52w"] is True                                                  # 예전: 분할 전 고가에 막혀 False
    assert abs(float(row["vol_ratio20"]) - 1) < 0.05                                    # 예전: 5배(거래량 급증 오탐)


def test_named_queries_drawdown_and_volume_spike_are_not_fooled_by_split():
    _load(_series())
    kh.refresh_snapshot(full_adj=True)
    dd = {r["ticker"]: r for r in platform.run_query("drawdown_1y", {"min_mktcap_eok": 0})["rows"]}
    assert dd[T]["mdd_pct"] > -1                                                        # 예전: −80% 가 1위
    spikes = platform.run_query("volume_spike", {"min_ratio": 3})["rows"]
    assert T not in {r["ticker"] for r in spikes}
    rot = platform.run_query("sector_rotation", {"months": 3})["rows"]
    assert all(r["ret"] is None or float(r["ret"]) > -20 for r in rot)


def test_market_monthly_features_use_adjusted_log_returns():
    _load(_series())
    kh.refresh_snapshot(full_adj=True)
    db.execute("SELECT analytics.refresh_market_monthly(1200)")
    last = db.query("SELECT ret_1y, vol_1y, dd_1y FROM analytics.market_monthly WHERE ticker = :t "
                    "ORDER BY month_end DESC LIMIT 1", {"t": T})[0]
    assert last["dd_1y"] > -0.01                                                        # 예전: −0.8
    assert last["vol_1y"] < 0.05                                                        # 예전: 분할 날 하나로 0.1 넘게
    assert 0.4 < last["ret_1y"] < 0.8                                                   # 매일 +0.2% × 약 250일


def test_incremental_refresh_keeps_earlier_events_and_finds_new_ones():
    s = _series()
    _load(s[:-5])
    kh.refresh_snapshot(full_adj=True)
    assert db.query("SELECT count(*) AS n FROM analytics.price_adj_event")[0]["n"] == 0
    _load(s[-5:])
    snap = kh.refresh_snapshot(adj_since=s[-5][0])                    # 새로 받은 가장 이른 날부터만 다시 찾기
    assert snap["adj_events"] == 1


# ── 화면용 표 ────────────────────────────────────────────
def test_risk_metrics_and_chart_bars_are_adjusted():
    s = _series()
    for d, r in s:
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) VALUES(:t, :d, :c, :v, :r)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"], "r": r["change_rate"]})
        db.execute("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) VALUES(:t, :d, :c, :c, :c, :c, :v)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"]})
    p = risk.risk_profile(T)
    assert p["ok"] and p["max_drawdown"] > -0.01 and p["cumulative_return"] > 0         # 예전: −0.8 · −0.67
    from app.services import detail
    bars = detail._last_bars(T, 300, 500)
    assert max(b["high"] for b in bars[:-3]) < bars[-1]["close"]                        # 52주 최고가가 분할 전 5배 가격이 아님
    F = adjust.factors_since([T], s[-10][0].isoformat())
    assert abs(F[T] - 1 / SPLIT) < 0.002                                               # 추천 사후 검증용 계수


def test_adjusted_return_math_matches_official_rates():
    s = _series()
    dates = [d.isoformat() for d, _ in s]
    closes = [r["close"] for _, r in s]
    rates = {d.isoformat(): r["change_rate"] for d, r in s}
    adj = adjust.adjust_closes(dates, closes, rates)
    lr = sum(math.log1p(rates[d] / 100) for d in dates[1:])
    assert abs(math.log(adj[-1] / adj[0]) - lr) < 0.01                                 # 수정 경로 = 공식 등락률의 누적


def test_mock_investment_position_gets_split_shares_once():
    from app.services import invest, wallet
    s = _series()
    for d, r in s:
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) VALUES(:t, :d, :c, :v, :r)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"], "r": r["change_rate"]})
        db.execute("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) VALUES(:t, :d, :c, :c, :c, :c, :v)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"]})
    buy_day, buy_close = s[-10][0].isoformat(), s[-10][1]["close"]
    from app.services import auth
    auth.register("ca_user", "Ca-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    with db.transaction() as conn:
        wallet.ensure(conn, "ca_user")
    db.execute("INSERT INTO invest_position(username, ticker, quantity, buy_price, buy_date, cost, status, created_at) "
               "VALUES('ca_user', :t, 10, :p, :d, :c, 'open', '2025-12-01T00:00:00')",
               {"t": T, "p": buy_close, "d": buy_day, "c": buy_close * 10})
    r1 = invest.apply_corporate_actions()
    r2 = invest.apply_corporate_actions()                                              # 다시 돌려도 한 번만
    assert r1["adjusted"] == 1 and r2["adjusted"] == 0
    p = db.query("SELECT quantity, buy_price, cost FROM invest_position WHERE username = 'ca_user'")[0]
    assert p["quantity"] == 50 and abs(p["buy_price"] - buy_close / SPLIT) / (buy_close / SPLIT) < 0.003
    assert p["cost"] == buy_close * 10                                                 # 원가(지갑에서 나간 돈)는 그대로


# ── 점검에서 찾은 오탐·누락(2차) ─────────────────────────────────
def test_missing_bar_in_analytics_layer_is_not_a_split():
    """그날 적재가 빠진 종목(결측) — 이틀 치 원 비율과 하루 치 등락률을 비교해 가짜 이벤트를 만들면 빠진 날 +5% 가 지워진다."""
    days = sorted(kh.trading_days(date(2025, 9, 1), date(2025, 12, 30)))[-40:]
    gap = days[20]
    c = {"700010": 10_000.0, "700020": 20_000.0}
    for d in days:
        rows = []
        for t in c:
            r = 5.0 if d == gap else 0.3
            c[t] *= 1 + r / 100
            if t == "700020" and d == gap:
                continue                                              # 이 종목만 그날 봉이 없음
            rows.append({"ticker": t, "name": t, "market": "KOSPI", "sect_tp": None, "open": round(c[t]),
                         "high": round(c[t]), "low": round(c[t]), "close": round(c[t]), "volume": 100,
                         "value": round(c[t]) * 100, "mktcap": round(c[t]) * 10**6, "shares": 10**6,
                         "change_rate": r, "halted": False})
        kh.store_day(d, "K", rows)
    kh.refresh_snapshot(full_adj=True)
    assert not db.query("SELECT 1 FROM analytics.price_adj_event")
    lr = db.query("SELECT sum(lr) AS s FROM analytics.adj_prices(:a, :b, ARRAY['700020'])", {"a": days[0], "b": days[-1]})[0]["s"]
    want = math.log(1.003) * 38 + math.log(1.05)                      # 첫날 제외 39일 중 빠진 날은 다음 날 비율에 포함
    assert abs(lr - want) < 0.002


def test_untrusted_rates_stay_untrusted_in_incremental_refresh():
    s = _series(ticker="777772", bad_rates=True, rate=1.0)
    _load(s)
    assert kh.refresh_snapshot(full_adj=True)["adj_events"] == 0
    snap = kh.refresh_snapshot(adj_since=s[-10][0])                  # 좁은 창으로 다시 찾아도 같은 판단
    assert snap["adj_events"] == 0
    assert not db.query("SELECT 1 FROM analytics.price_adj_event")


def test_split_day_position_is_valued_and_sold_with_new_share_count_before_close():
    """분할 첫날 장중(일봉 반영 전) — 예전엔 보유 10주를 분할 뒤 가격으로 평가(−80%)하고 그 값으로 팔렸다."""
    from app.services import invest, wallet
    s = _series()
    ev_day, ev_row = s[-3]
    for d, r in s[:-3]:                                              # 분할 전날까지의 일봉만
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) VALUES(:t, :d, :c, :v, :r)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"], "r": r["change_rate"]})
        db.execute("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) VALUES(:t, :d, :c, :c, :c, :c, :v)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"]})
    price = ev_row["close"]
    db.execute("INSERT INTO realtime_price(ticker, price, change_rate, change, volume, ts, session_date, source) "
               "VALUES(:t, :p, 1.0, :chg, 1000, :ts, :d, 'kis')",
               {"t": T, "p": price, "chg": round(price - price / 1.01, 2), "ts": f"{ev_day.isoformat()}T10:00:00+09:00",
                "d": ev_day.isoformat()})
    buy_close = s[-10][1]["close"]
    from app.services import auth
    auth.register("sp_user", "Sp-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    with db.transaction() as conn:
        wallet.ensure(conn, "sp_user")
    db.execute("INSERT INTO invest_position(username, ticker, quantity, buy_price, buy_date, cost, status, created_at) "
               "VALUES('sp_user', :t, 10, :p, :d, :c, 'open', '2025-12-01T00:00:00')",
               {"t": T, "p": buy_close, "d": s[-10][0].isoformat(), "c": buy_close * 10})
    pos = invest.positions("sp_user")["open"][0]
    assert pos["quantity"] == 50 and pos["pending_adjustment"] and -1 < pos["pnl_pct"] < 5     # 예전: 10주 · −80%
    pid = pos["position_id"]
    out = invest.sell("sp_user", pid, 20)                            # 일부 매도 — 분할 뒤 수량 기준
    assert out["sold"] == 20 and out["proceeds"] == 20 * price
    row = db.query("SELECT quantity, adj_through FROM invest_position WHERE position_id = :p", {"p": pid})[0]
    assert row["quantity"] == 30 and row["adj_through"] == ev_day.isoformat()
    # 장 마감 뒤 그날 일봉이 들어와도 같은 분할을 또 반영하지 않는다
    for d, r in s[-3:]:
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) VALUES(:t, :d, :c, :v, :r)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"], "r": r["change_rate"]})
        db.execute("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) VALUES(:t, :d, :c, :c, :c, :c, :v)",
                   {"t": T, "d": d.isoformat(), "c": r["close"], "v": r["volume"]})
    assert invest.apply_corporate_actions()["adjusted"] == 0
    assert db.query("SELECT quantity FROM invest_position WHERE position_id = :p", {"p": pid})[0]["quantity"] == 30
