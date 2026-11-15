"""양봉이 주간 추천 — 팀 알고리즘(yangbong_picks.py)을 바꾸지 않고, 우리 데이터(분석 계층)·DB·배치·API 에 이었는지.

팀 검증 스크립트(yangbong_recommend/test_yangbong.py)의 1~3번을 그대로 옮기고(초기 247주 성적 · API 응답 모양 · 가짜 데이터 규칙),
우리 쪽 연결(코스피 행만·보통주 규칙·업종·수정주가·끝난 주만·후보 수 안전장치·채점·멱등)을 합성 시장으로 확인한다.
4번(실제 2026-10-23 5종목)은 KRX 전 종목이 적재된 서버에서 run.py yangbong 으로 확인한다(운영 문서)."""
import hashlib
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.collectors import krx_history as kh
from app.config import settings
from app.services import yangbong
from app.services import yangbong_picks as Y

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
TEAM_SHA256 = "62313d8581d743c78e01e7a099bdfaa9669823e21a0f946ba26c9eaee0d3560d"   # 팀 원본 yangbong_picks.py(d4fd73f)


# ── 팀 원본 그대로 ─────────────────────────────────────────
def test_algorithm_file_is_the_team_original():
    body = (ROOT / "app" / "services" / "yangbong_picks.py").read_bytes()
    assert hashlib.sha256(body).hexdigest() == TEAM_SHA256, "팀 알고리즘을 고치지 않는다 — 바뀌면 팀과 먼저 합의"
    assert (Y.N_PICKS, Y.UNIVERSE_SIZE, Y.VOL_WINDOW, Y.VOL_PCT, Y.MA_WINDOW, Y.MIN_HISTORY, Y.HOLD_DAYS) == \
        (5, 200, 60, 0.30, 200, 200, 30)


def test_1_seed_stats_match_the_team_backtest():
    assert yangbong.seed() == 247 and yangbong.seed() == 0                     # 두 번째는 할 일 없음(멱등)
    s = Y.stats(yangbong.load_baskets())
    assert s["weeks"] == 247 and s["wins"] == 136, s
    assert abs(s["avg_return"] - 0.015147) < 1e-4, s
    assert abs(s["total_won"] - 3_741_202) <= 2, s
    st = yangbong.weekly()["stats"]
    assert st["since"] == "2022-01-07" and st["weeks"] == 247


def test_2_api_shape_is_what_index_html_expects():
    yangbong.seed()
    with db.transaction() as conn:
        yangbong._save(conn, {"signal_date": "2026-10-23", "codes": ["068270", "138040", "011200", "033780", "021240"],
                              "names": ["셀트리온", "메리츠금융지주", "HMM", "KT&G", "코웨이"], "buy_date": None,
                              "sell_date": None, "stock_returns": None, "basket_return": None, "kospi_return": None,
                              "source": "batch"})
    yangbong.invalidate()
    r = client.get("/yangbong/weekly")
    assert r.status_code == 200 and r.headers.get("etag")
    d = r.json()
    assert d["as_of"] == "2026-10-23" and d["source"] == "batch"
    assert [p["name"] for p in d["picks"]] == ["셀트리온", "메리츠금융지주", "HMM", "KT&G", "코웨이"]
    assert [p["rank"] for p in d["picks"]] == [1, 2, 3, 4, 5]
    lb = d["last_basket"]
    assert lb["buy_date"] == "2026-09-28" and lb["sell_date"] == "2026-10-28"
    assert abs(lb["basket_return"] - (-0.021677)) < 1e-5 and len(lb["stocks"]) == 5
    assert {"code", "name", "return"} <= set(lb["stocks"][0])
    assert {"weeks", "wins", "win_rate", "avg_return", "total_won", "since"} <= set(d["stats"])
    assert client.get("/yangbong/weekly", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_empty_table_answers_without_error():
    d = client.get("/yangbong/weekly").json()
    assert d["picks"] == [] and d["last_basket"] is None and d["stats"]["weeks"] == 0


def test_3_algorithm_toy_rules():
    """팀 3번 그대로 — 흔들림 하위 30% · 200일선 위 · 큰 회사 순 · 업종 중복 없음 · 30일 채점."""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2024-01-01", periods=320)
    codes = [f"{i:06d}" for i in range(20)]
    drift = np.linspace(0.002, -0.002, 20)
    vol = np.linspace(0.005, 0.04, 20)
    ret = rng.normal(drift, vol, size=(len(dates), 20))
    close = pd.DataFrame(100 * np.exp(np.cumsum(ret, 0)), index=dates, columns=codes)
    volume = pd.DataFrame(1000, index=dates, columns=codes)
    mktcap = pd.DataFrame(np.tile(np.arange(20, 0, -1) * 1e12, (len(dates), 1)), index=dates, columns=codes)
    industry = {c: ("금융" if i < 3 else f"업종{i}") for i, c in enumerate(codes)}
    day, picks = Y.select_weekly(close.iloc[:300], volume, mktcap, set(codes), industry, {})
    got = [p.code for p in picks]
    assert len(set(industry[c] for c in got)) == len(got), "업종 중복"
    assert got[0] == "000000" and "000001" not in got, got
    s = Y.score_basket(close, close, dates[250], got)
    assert s["sell_date"] <= (pd.Timestamp(s["buy_date"]) + pd.Timedelta(days=30)).date().isoformat()


# ── 우리 쪽 연결 ───────────────────────────────────────────
def test_common_stock_and_industry_rules():
    ok = [("005930", "삼성전자"), ("105560", "KB금융"), ("0009K0", "에임드바이오")]
    no = [("005935", "삼성전자우"), ("00104K", "CJ4우(전환)"), ("088980", "맥쿼리인프라"), ("395400", "SK리츠"),
          ("094800", "맵스리얼티1"), ("442310", "하나머스트7호스팩"), ("155900", "바다로19호"), ("900140", "엘브이엠씨홀딩스"),
          ("12345", "짧은코드")]
    assert all(yangbong.is_common(t, n) for t, n in ok)
    assert not any(yangbong.is_common(t, n) for t, n in no)
    assert yangbong.industry_key("64992", "금융") == yangbong.industry_key("649", None) == "ksic:649"   # KSIC 소분류
    assert yangbong.industry_key(None, "반도체") == "sector:반도체" and yangbong.industry_key("", "") is None


def test_week_signal_is_the_last_trading_day_and_only_finished_weeks():
    assert yangbong._calendar_last_day(date(2026, 9, 21)) == date(2026, 9, 23)        # 추석(24~26) — 수요일
    assert yangbong._calendar_last_day(date(2026, 10, 5)) == date(2026, 10, 8)        # 한글날(금) — 목요일
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    assert yangbong.completed_weeks(days, "2026-11-03") == ["2026-10-30"]             # 이번 주(화요일까지)는 아직
    assert yangbong.completed_weeks(days[:5], "2026-10-30") == ["2026-10-30"]         # 금요일까지 들어오면 끝난 주
    assert yangbong.completed_weeks(days[:4], "2026-10-29") == []                     # 금요일 시세 전 — 기다린다
    # 달력에 없는 임시 휴장(금요일): 다음 주 데이터가 들어오면 그 주 실제 마지막 거래일(목)로 확정
    assert yangbong.completed_weeks(days[:4] + ["2026-11-02"], "2026-11-02") == ["2026-10-29"]


# 합성 시장 — 코스피 보통주 24개(앞 종목일수록 큼·덜 흔들림·오름세) + 고르면 안 되는 종목(우선주·리츠·스팩·외국주권·코스닥)
N = 24
EXCLUDED = {"100015": "가나다우", "200000": "테스트리츠", "200010": "테스트스팩1호", "900110": "외국기업홀딩스"}
SPLIT_T, SPLIT_K = "100030", 5          # 셋째 종목 — 5:1 액면분할(원 가격이 1/5, 등락률은 실제 수익률)


def _code(i):
    return f"{100000 + i * 10:06d}"


def _days(n=290, end=date(2026, 10, 30)):
    return sorted(kh.trading_days(date(2025, 1, 2), end))[-n:]


def _market(monkeypatch, n_days=290):
    """합성 시장 적재 → (거래일, 실제 수정 종가 표, 실제 수정 시가 표, 코스피 시가·종가, 업종)."""
    monkeypatch.setattr(settings, "YANGBONG_MIN_UNIVERSE", 8)
    monkeypatch.setattr(settings, "YANGBONG_CATCHUP_WEEKS", 8)
    days = _days(n_days)
    rng = np.random.default_rng(7)
    codes = [_code(i) for i in range(N)]
    allc = codes + list(EXCLUDED)
    drift = dict(zip(allc, list(np.linspace(0.0025, -0.002, N)) + [0.003] * len(EXCLUDED)))
    vol = dict(zip(allc, list(np.linspace(0.006, 0.035, N)) + [0.004] * len(EXCLUDED)))
    cap = dict(zip(allc, [(N - i) * 10 ** 12 for i in range(N)] + [50 * 10 ** 12] * len(EXCLUDED)))   # 제외 종목이 가장 큼
    true_c = {c: [] for c in allc}
    true_o = {c: [] for c in allc}
    split_at = len(days) - 40
    k_open, k_close, k = [], [], 2500.0
    for di, d in enumerate(days):
        rows = []
        for c in allc:
            prev = true_c[c][-1] if true_c[c] else 20_000.0
            o = prev * (1 + rng.normal(0, 0.002))
            cl = prev * float(np.exp(rng.normal(drift[c], vol[c])))
            true_o[c].append(o)
            true_c[c].append(cl)
            f = SPLIT_K if (c == SPLIT_T and di >= split_at) else 1          # 분할 뒤 원 가격은 1/5
            raw_o, raw_c = int(round(o / f)), int(round(cl / f))
            raw_prev = int(round(prev / (SPLIT_K if (c == SPLIT_T and di - 1 >= split_at) else 1)))
            rate = round((cl / prev - 1) * 100, 2) if di else 0.0                # KRX 등락률 = 기준가 대비 실제 수익률
            halted = c == _code(1) and d == days[-1]                             # 마지막 날 둘째 종목 거래정지
            row = {"ticker": c, "name": EXCLUDED.get(c, f"보통{c}"), "market": "KOSPI", "sect_tp": None,
                   "open": 0 if halted else raw_o, "high": 0 if halted else max(raw_o, raw_c),
                   "low": 0 if halted else min(raw_o, raw_c), "close": raw_c if not halted else raw_prev,
                   "volume": 0 if halted else 1000, "value": raw_c * 1000, "mktcap": cap[c],
                   "shares": cap[c] // max(raw_c, 1), "change_rate": 0.0 if halted else rate, "halted": halted}
            rows.append(row)
        kh.store_day(d, "K", rows)
        kh.store_day(d, "Q", [{**rows[0], "ticker": "300000", "name": "코스닥대형", "market": "KOSDAQ", "mktcap": 90 * 10 ** 12}])
        kp = k
        k = k * float(np.exp(rng.normal(0.0002, 0.01)))
        k_open.append(kp)
        k_close.append(k)
    industry = {c: ("64992" if i in (0, 2, 4) else f"{300 + i:03d}10") for i, c in enumerate(codes)}   # 0·2·4 = 같은 업종(금융)
    with db.transaction() as conn:
        from sqlalchemy import text
        for c, ks in industry.items():
            conn.execute(text("UPDATE analytics.security_master SET ksic_code = :k WHERE ticker = :t"), {"k": ks, "t": c})
        for d, o, cl in zip(days, k_open, k_close):
            conn.execute(text("INSERT INTO index_daily(code, trade_date, open, high, low, close, change, change_rate, source) "
                              "VALUES('KOSPI', :d, :o, :h, :l, :c, 0, 0, 'krx')"),
                         {"d": d, "o": o, "h": max(o, cl), "l": min(o, cl), "c": cl})
    idx = pd.to_datetime(days)
    tc = pd.DataFrame({c: true_c[c] for c in codes}, index=idx)
    to = pd.DataFrame({c: true_o[c] for c in codes}, index=idx)
    return days, tc, to, pd.Series(k_open, index=idx), pd.Series(k_close, index=idx), \
        {c: "ksic:" + ks[:3] for c, ks in industry.items()}


def test_batch_picks_finished_weeks_with_team_rules_on_our_data(monkeypatch):
    days, tc, to, ko, kc, ind = _market(monkeypatch)
    r = yangbong.run()
    assert r["data_as_of"] == "2026-10-30" and r["picked"], r
    got = {b["signal_date"]: b for b in yangbong.load_baskets()}
    assert max(got) == "2026-10-30" and "2026-10-09" not in got and "2026-10-08" in got     # 한글날(금) 주 = 목요일
    vol = pd.DataFrame(1000.0, index=tc.index, columns=tc.columns)
    vol.loc[tc.index[-1], _code(1)] = 0                                         # 마지막 날 거래정지 — 그 주엔 후보 아님
    cap = pd.DataFrame({c: [(N - i) * 10 ** 12] * len(tc) for i, c in enumerate(tc.columns)}, index=tc.index, dtype=float)
    for s, b in got.items():
        _, want = Y.select_weekly(tc, vol, cap, set(tc.columns), ind, {}, s)       # 실제(분할 없는) 가격으로 고른 답
        assert list(b["codes"]) == [p.code for p in want], s
        assert not set(b["codes"]) & (set(EXCLUDED) | {"300000"})               # 우선주·리츠·스팩·외국주권·코스닥 제외
        assert len({ind[c] for c in b["codes"]}) == len(b["codes"])             # 업종 중복 없음(KSIC 앞 3자리)
        assert b["source"] == "batch" and b["names"][0].startswith("보통")
    assert _code(1) not in got["2026-10-30"]["codes"]
    assert SPLIT_T in {c for b in got.values() for c in b["codes"]}            # 분할 종목도 수정주가로 정상 평가


def test_batch_scores_after_30_days_with_adjusted_prices_and_kospi(monkeypatch):
    days, tc, to, ko, kc, ind = _market(monkeypatch)
    yangbong.run()
    scored = [b for b in yangbong.load_baskets() if b["basket_return"] is not None]
    pending = [b for b in yangbong.load_baskets() if b["basket_return"] is None]
    assert scored and pending
    for b in scored:
        buy, sell = pd.Timestamp(b["buy_date"]), pd.Timestamp(b["sell_date"])
        assert buy == tc.index[tc.index > pd.Timestamp(b["signal_date"])][0]                         # 다음 거래일 시가 매수
        assert sell == tc.index[tc.index <= buy + pd.Timedelta(days=30)][-1]                         # 30일 뒤(직전 거래일)
        want = [tc.at[sell, c] / to.at[buy, c] - 1 for c in b["codes"]]
        assert np.allclose(b["stock_returns"], want, atol=2e-3), (b["signal_date"], b["stock_returns"], want)
        assert abs(b["basket_return"] - float(np.mean(b["stock_returns"]))) < 1e-9
        assert abs(b["kospi_return"] - (kc[sell] / ko[buy] - 1)) < 1e-6
    for b in pending:                                                           # 30일이 아직 안 지난 주
        assert pd.Timestamp(b["signal_date"]) + pd.Timedelta(days=31) > tc.index[-1] - pd.Timedelta(days=3)
    assert yangbong.run()["picked"] == []                                       # 다시 돌려도 같은 주를 또 고르지 않는다
    d = client.get("/yangbong/weekly").json()
    assert d["as_of"] == "2026-10-30" and d["last_basket"]["sell_date"] == scored[-1]["sell_date"]


def test_guards_small_universe_and_missing_signal_day(monkeypatch):
    _market(monkeypatch)
    monkeypatch.setattr(settings, "YANGBONG_MIN_UNIVERSE", 150)                  # 표본 DB(24종목) — 추천을 만들지 않는다
    r = yangbong.run()
    assert r["picked"] == [] and any("후보" in x for x in r["skipped"])
    assert yangbong.load_baskets() == []
    assert yangbong.status()["last_run"]["skipped"]


def test_concurrent_run_is_skipped():
    with db.advisory_lock("yangbong") as got:
        assert got
        assert yangbong.run() == {"skipped": "다른 실행이 진행 중"}


def test_no_market_data_keeps_seed_and_reports():
    yangbong.seed()
    r = yangbong.run()
    assert r["picked"] == [] and r["skipped"] and yangbong.weekly()["as_of"] == "2026-09-23"


@pytest.mark.parametrize("path", ["/yangbong/weekly"])
def test_public_endpoint_needs_no_login_and_is_read_only(path):
    assert client.get(path).status_code == 200
    assert client.post(path).status_code in (403, 405)
