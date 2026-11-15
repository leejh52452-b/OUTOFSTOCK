"""멀티팩터 추천 모델(mf-2) — 적격성 필터 · 시점(PIT) 정합성 · 적합성 · 분산 · 설명 가능성 · 검증/감사."""
import random
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.api import app
from app.services import reco_engine as R
from app.services import signals

client = TestClient(app)
START = date(2026, 3, 2)


def _days(n, start=START):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _days(130)


def _walk(seed, n=130, vol=0.015, drift=0.0005, base=50000.0):
    rng = random.Random(seed)
    p, out = base, []
    for _ in range(n):
        p *= 1 + drift + rng.gauss(0, vol)
        out.append(round(p, 0))
    return out


def _put(ticker, name, closes, vols=None, shares=100_000_000, sector=None):
    days = DAYS[-len(closes):]
    with db.transaction() as conn:
        conn.execute(text("INSERT INTO stock(ticker, name, market, sector) VALUES(:t,:n,'KOSPI',:s) "
                          "ON CONFLICT(ticker) DO UPDATE SET name=:n, sector=:s"), {"t": ticker, "n": name, "s": sector})
        conn.execute(text("DELETE FROM ohlc WHERE ticker=:t"), {"t": ticker})
        conn.execute(text("DELETE FROM daily_price WHERE ticker=:t"), {"t": ticker})
        rows, prev = [], closes[0]
        for i, (d, c) in enumerate(zip(days, closes)):
            v = (vols[i] if vols else 1_000_000)
            rows.append({"t": ticker, "d": d, "o": prev, "h": max(prev, c) * 1.004, "l": min(prev, c) * 0.996,
                         "c": c, "v": v, "m": int(c * shares)})
            prev = c
        conn.execute(text("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) "
                          "VALUES(:t,:d,:o,:h,:l,:c,:v)"), rows)
        conn.execute(text("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
                          "VALUES(:t,:d,:c,:v,0,:m)"), rows)


def _fin(ticker, ni, eq, rev=None, op=None, debt=80.0, disclosed="2026-03-20"):
    db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,revenue,operating_profit,net_income,"
               "total_equity,debt_ratio,disclosed_at) VALUES(:t,2025,'11011',:r,:o,:n,:e,:d,:dis)",
               {"t": ticker, "r": rev or abs(ni) * 8, "o": op if op is not None else abs(ni) * 1.2, "n": ni,
                "e": eq, "d": debt, "dis": disclosed})


def _universe(n=12):
    for k in range(n):
        _put(f"9{k:05d}", f"테스트{k:02d}", _walk(k, drift=0.0004 * (k % 5)))


def _clear_curated():
    db.execute("DELETE FROM stock WHERE ticker NOT LIKE '9%' AND ticker NOT IN ('005930','005935')")
    R.clear_cache()
    signals.clear_cache()


def test_eligibility_filters_exclude_with_reasons():
    _clear_curated()
    _universe(10)
    halted = _walk(101)[:-6] + [_walk(101)[-7]] * 6                       # 마지막 6일 가격 고정
    _put("800001", "거래정지주", halted)
    spike = _walk(102)
    spike[-1] = spike[-2] * 1.29                                             # 상한가
    _put("800002", "급등주", spike)
    _put("800003", "자본잠식주", _walk(103))
    _fin("800003", ni=-5e9, eq=-1e9)
    res = R.recommend(5, as_of=DAYS[-1])
    ex = res["model"]["excluded"]
    assert any("거래정지" in k for k in ex) and any("과열" in k for k in ex) and any("자본잠식" in k for k in ex)
    assert not {"800001", "800002", "800003"} & {i["ticker"] for i in res["items"]}
    assert len(res["items"]) == 5


def test_items_are_explainable():
    _clear_curated()
    _universe(12)
    it = R.recommend(5, as_of=DAYS[-1])["items"][0]
    assert 0 <= it["score"] <= 100 and it["grade"] in "SABCD" and it["tag"] and it["brief"] is not None
    assert set(it["contrib"]) == set(R.FACTORS) and it["reasons"]
    assert abs(sum(it["contrib"].values()) - it["composite"]) < 0.01      # 기여도 합 = 종합점수
    assert it["risk"]["level"] in ("낮음", "보통", "높음", "매우 높음")


def test_point_in_time_financials_and_news():
    """기준일 이후 공시된 재무·발행된 뉴스는 그날 점수에 영향을 주지 않는다."""
    _clear_curated()
    _universe(8)
    asof = DAYS[-30]
    _fin("900001", ni=9e10, eq=5e11, disclosed=DAYS[-10])                   # 기준일 뒤 공시
    panel = R.Panel(R.catalog.universe())
    before = R.score_universe(panel, asof)["rows"]["900001"]
    after = R.score_universe(panel, DAYS[-1])["rows"]["900001"]
    assert before["e"]["per"] is None and after["e"]["per"] is not None
    with db.transaction() as conn:
        conn.execute(text("INSERT INTO news(news_id,title,url,source,published_at) VALUES(1,'호재','u1','x',:d)"),
                     {"d": DAYS[-5]})
        conn.execute(text("INSERT INTO news_stock(news_id,ticker) VALUES(1,'900002')"))
        conn.execute(text("INSERT INTO news_sentiment(news_id,label,score,model) VALUES(1,'POS',0.9,'t')"))
    panel = R.Panel(R.catalog.universe())
    assert R.score_universe(panel, asof)["rows"]["900002"]["e"]["news_n"] == 0
    assert R.score_universe(panel, DAYS[-1])["rows"]["900002"]["e"]["news_n"] == 1


def test_future_bars_do_not_change_past_picks():
    _clear_curated()
    _universe(12)
    asof = DAYS[-25]
    a = [i["ticker"] for i in R.recommend(5, as_of=asof)["items"]]
    _put("900003", "테스트03", _walk(3)[:-20] + [x * 3 for x in _walk(3)[-20:]])   # 미래 구간만 폭등
    R.clear_cache()
    b = [i["ticker"] for i in R.recommend(5, as_of=asof)["items"]]
    assert a == b


def test_suitability_excludes_high_volatility_for_stable_profile():
    _clear_curated()
    _universe(10)
    _put("700001", "고변동주", _walk(77, vol=0.06, drift=0.004))
    allp = R.recommend(10, as_of=DAYS[-1])
    stable = R.recommend(10, profile="안정형", as_of=DAYS[-1])
    assert all(i["risk"]["volatility"] <= 0.30 for i in stable["items"])
    assert "700001" not in {i["ticker"] for i in stable["items"]}
    assert stable["model"]["profile_rule"] and allp["model"]["profile"] is None


def test_top5_api_tells_profile_filter_apart_from_missing_data():
    """실데이터처럼 변동성이 큰 시장(연 50%대) — 안정형 기준(연변동성 30% 이하)을 통과한 종목이 0개면 items 는 비지만,
    model 에 적격 수와 '투자성향 대비 위험 높음' 탈락 수가 남아 화면이 데이터 부족과 구분해 알리고
    성향 무관 Top 5 를 보여줄 수 있다. (10-10: 화면이 무조건 '추천할 종목이 없어요(시세 데이터 부족)'으로 안내했다.
    표본(gen) 데이터는 연변동성이 5~7% 라 기존 테스트에서는 이 경우가 생기지 않았다.)"""
    _clear_curated()
    for k in range(8):
        _put(f"8{k:05d}", f"고변동{k:02d}", _walk(100 + k, vol=0.034, drift=0.001))
    stable = client.get("/reco/top5", params={"profile": "안정형"}).json()
    allp = client.get("/reco/top5").json()
    m = stable["model"]
    assert stable["as_of"] and stable["items"] == [] and m["eligible"] >= 5
    assert m["diversified_out"].get("투자성향 대비 위험 높음") == m["eligible"]      # 전부 성향 기준에서 빠짐
    assert not m["excluded"] or "데이터 부족(25거래일 미만)" not in m["excluded"]
    assert len(allp["items"]) == 5 and allp["profile"] is None                        # 성향 무관이면 5개
    # 데이터가 모자란 경우는 다르게 보인다: 적격 0 + 제외 사유 '데이터 부족'
    _clear_curated()
    db.execute("DELETE FROM ohlc")
    for k in range(6):
        _put(f"7{k:05d}", f"신규{k:02d}", _walk(200 + k, n=15))
    short = client.get("/reco/top5").json()
    assert short["items"] == [] and short["model"]["eligible"] == 0
    assert short["model"]["excluded"].get("데이터 부족(25거래일 미만)") == 6


def test_diversification_skips_preferred_share_duplicate():
    _clear_curated()
    _universe(8)
    strong = _walk(55, drift=0.006, vol=0.01)
    _put("005930", "삼성전자", strong)
    _put("005935", "삼성전자우", [round(x * 0.8) for x in strong])
    res = R.recommend(5, as_of=DAYS[-1])
    picks = [i["ticker"] for i in res["items"]]
    assert len({"005930", "005935"} & set(picks)) == 1                     # 둘 중 하나만
    assert res["model"]["diversified_out"].get("보통주·우선주 중복") == 1


def test_backtest_reports_validation_metrics():
    _clear_curated()
    _universe(15)
    bt = R.backtest(days=100, horizon=10)
    assert bt["available"] and bt["summary"]["periods"] >= 5
    s = bt["summary"]
    assert {"avg_excess", "win_rate", "hit_rate", "ic_mean", "cum_top", "cum_bench"} <= set(s)
    assert bt["curve"][-1]["date"] == bt["periods"][-1]["exit"] and bt["caveats"]
    for p in bt["periods"]:
        assert p["date"] < p["exit"]                                          # 진입 후 성과만


def test_snapshot_is_immutable_and_used_by_review():
    _clear_curated()
    _universe(10)
    res = R.recommend(5, as_of=DAYS[-40])
    assert R.save_snapshot(res) == 5
    _put("900004", "테스트04", _walk(4, drift=0.02))                       # 데이터가 바뀌어도
    R.clear_cache()
    assert R.save_snapshot(R.recommend(5, as_of=DAYS[-40])) == 5            # (다시 저장 시도)
    snap = R.snapshot(DAYS[-40])
    assert [s["ticker"] for s in snap] == [i["ticker"] for i in res["items"]]   # 처음 공개한 그대로


def test_api_top5_profile_model_and_backtest():
    _clear_curated()
    _universe(12)
    d = client.get("/reco/top5", params={"profile": "중립형"}).json()
    assert d["profile"] == "중립형" and d["model"]["version"] == R.MODEL_VERSION
    assert d["items"] and {"score", "grade", "reasons", "contrib", "risk", "price"} <= set(d["items"][0])
    m = client.get("/reco/model").json()
    assert len(m["factors"]) == 7 and m["eligibility"] and m["profiles"]["안정형"]
    bt = client.get("/reco/top5/backtest", params={"days": 90, "horizon": 10}).json()
    assert bt["available"] and bt["params"]["horizon"] == 10
    rv = client.get("/reco/top5/review", params={"days": 30}).json()
    assert rv["available"] and "bench_return" in rv and "excess_return" in rv


def test_stable_profile_excludes_distress_zone_and_warns():
    _clear_curated()
    _universe(10)
    _put("600001", "부실징후주", _walk(66, drift=0.004, vol=0.008))          # 저변동·강한 추세(원래면 상위권)
    db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,revenue,operating_profit,net_income,"
               "total_equity,debt_ratio,disclosed_at,total_assets,current_assets,current_liabilities,"
               "total_liabilities,retained_earnings) VALUES('600001',2025,'11011',1000,-50,-100,200,900,"
               "'2026-03-20',2000,300,500,1800,-600)")
    allp = R.recommend(11, as_of=DAYS[-1])
    stable = R.recommend(11, profile="안정형", as_of=DAYS[-1])
    item = next(i for i in allp["items"] if i["ticker"] == "600001")
    assert item["metrics"]["altman_zone"] == "부실 위험" and any("부실" in c for c in item["cautions"])
    assert "600001" not in {i["ticker"] for i in stable["items"]}


# ── 모형 모니터링(PSI·IC) ─────────────────────────────────
def test_psi_basic_properties():
    rng = random.Random(1)
    a = [rng.gauss(0, 1) for _ in range(500)]
    b = [rng.gauss(0, 1) for _ in range(500)]
    shifted = [rng.gauss(1.5, 2) for _ in range(500)]
    assert R.psi(a, b) < R.PSI_WARN                                           # 같은 분포 → 안정
    assert R.psi(a, shifted) > R.PSI_ALERT                                    # 이동·확산 → 재검토
    assert R.psi(a[:10], b[:10]) is None and R.psi([1.0] * 50, b) is None      # 표본 부족·한 점 몰림


def test_model_monitor_flags_volatility_regime_shift():
    _clear_curated()
    for k in range(30):                                                        # 잠잠한 70일 → 변동성 5배 60일
        calm = _walk(500 + k, n=70, vol=0.006)
        storm = _walk(900 + k, n=60, vol=0.03, drift=0.0, base=calm[-1])
        _put(f"9{k:05d}", f"테스트{k:02d}", calm + storm)
    m = R.monitor(ref_days=60)
    assert m["available"] and m["cur_date"] == DAYS[-1] and m["ref_date"] == DAYS[-61]
    vol = next(r for r in m["psi"] if r["var"] == "vol")
    assert vol["psi"] >= R.PSI_ALERT and vol["cur_median"] > vol["ref_median"] * 2
    assert vol["kind"] == "market" and vol["status"] == "주의"                 # 시장 국면 변수는 '주의'까지
    assert any("국면" in a and "연변동성" in a for a in m["actions"])
    assert m["status"] in ("주의", "재검토") and m["ic"]["status"] in ("양호", "저하", "데이터 부족")
    api = client.get("/reco/model/monitor").json()
    assert api["available"] and api["thresholds"]["alert"] == 0.25


def test_model_monitor_escalates_structural_shift_in_financials():
    """재무 입력(ROE)이 3개월 새 통째로 바뀌면(데이터 오류·구조 변화 의심) '재검토'."""
    _clear_curated()
    rng = random.Random(3)
    for k in range(30):
        t = f"9{k:05d}"
        _put(t, f"테스트{k:02d}", _walk(300 + k))
        eq = 1e11
        db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,revenue,operating_profit,net_income,"
                   "total_equity,debt_ratio,disclosed_at) VALUES(:t,2024,'11011',8e11,9e10,:n,:e,80,:d)",
                   {"t": t, "n": eq * rng.gauss(0.10, 0.02), "e": eq, "d": DAYS[0]})
        db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,revenue,operating_profit,net_income,"
                   "total_equity,debt_ratio,disclosed_at) VALUES(:t,2025,'11011',8e11,9e10,:n,:e,80,:d)",
                   {"t": t, "n": eq * rng.gauss(-0.15, 0.02), "e": eq, "d": DAYS[-20]})    # 기준일 뒤 공시
    m = R.monitor(ref_days=60)
    roe = next(r for r in m["psi"] if r["var"] == "roe")
    assert roe["kind"] == "financial" and roe["status"] == "재검토" and m["status"] == "재검토"
    assert any("재무" in a and "ROE" in a for a in m["actions"])


def test_high_market_implied_pd_adds_caution(monkeypatch):
    from app.services import credit
    _clear_curated()
    _universe(10)
    monkeypatch.setattr(credit, "merton_for", lambda t, fin, name="", as_of=None:
                        {"ok": True, "dd": 0.8, "pd": 0.21, "pd_band": "매우 높음"})
    res = R.recommend(3, as_of=DAYS[-1])
    it = res["items"][0]
    assert it["metrics"]["merton_pd"] == 0.21 and any("부도확률" in c for c in it["cautions"])


# ── Champion–Challenger ──────────────────────────────────
def test_champion_challenger_comparison():
    _clear_curated()
    _universe(15)
    c = R.compare_models(days=100, horizon=10)
    assert c["available"] and [m["key"] for m in c["models"]] == ["champion", "equal", "momentum", "lowrisk"]
    champ = c["models"][0]
    assert champ["periods"] >= 5 and champ["avg_excess"] is not None and "vs_champion" not in champ
    for m in c["models"][1:]:
        v = m["vs_champion"]
        assert v["n"] == m["periods"] and v["verdict"]
        if v["n"] >= 6:
            assert "판단 보류" not in v["verdict"]
    api = client.get("/reco/model/challengers", params={"days": 100, "horizon": 10}).json()
    assert api["available"] and len(api["models"]) == 4


def test_rescore_reweights_without_recomputing_factors():
    _clear_curated()
    _universe(12)
    panel = R.Panel(R.catalog.universe())
    base = R.score_universe(panel, DAYS[-1])
    mom = R._rescore(base, {"momentum": 1.0})
    for t, r in mom["rows"].items():
        assert abs(r["composite"] - base["rows"][t]["z"]["momentum"]) < 1e-12   # 모멘텀만 반영
    assert base["rows"][t]["composite"] != mom["rows"][t]["composite"] or len(base["used"]) == 1
