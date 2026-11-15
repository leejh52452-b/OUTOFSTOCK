"""데이터 정합성(4차 점검) — 실습용 과거 구간이 52주·차트에 섞이지 않음 · 실데이터 모드에서 데모 상수 노출 금지 ·
DART 계정 파싱(연결/별도 혼합 금지·'(손실)' 표기) · 프론트 타입 정의 중복 선언 금지."""
import datetime as dt
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.collectors import dart
from app.services import detail, quotes

client = TestClient(app)


def _bars(ticker, start, n, base, step=0.0):
    d = dt.date.fromisoformat(start)
    rows = []
    while len(rows) < n:
        if d.weekday() < 5:
            c = base * (1 + step) ** len(rows)
            rows.append({"t": ticker, "d": d.isoformat(), "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000})
        d += dt.timedelta(days=1)
    db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES(:t,:d,:o,:h,:l,:c,:v) "
               "ON CONFLICT DO NOTHING", rows)
    db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
               "VALUES(:t,:d,:c,:v,0,1000000000000) ON CONFLICT DO NOTHING", rows)
    return rows


def test_recent_segment_cuts_long_gaps_but_keeps_holidays():
    rows = [{"trade_date": d} for d in ("2023-05-02", "2023-05-03", "2026-01-02", "2026-01-05",
                                         "2026-01-14", "2026-01-15")]           # 9일 연휴는 유지
    assert [r["trade_date"] for r in quotes.recent_segment(rows)] == ["2026-01-02", "2026-01-05",
                                                                        "2026-01-14", "2026-01-15"]


def test_practice_segment_does_not_leak_into_52w_or_1y_chart():
    db.set_meta("market_source", "gen")
    _bars("005380", "2023-05-02", 90, 380000)                                   # 실습용 과거 구간(높은 가격)
    _bars("005380", "2026-01-05", 180, 240000)                                  # 실데이터 최근 180거래일
    f = detail.stock_detail("005380")["fundamentals"]
    assert f["high_52w"] < 250000 and f["low_52w"] > 230000                    # 2023년 38만원대가 섞이면 안 됨
    s = client.get("/stocks/005380/ohlc", params={"period": "1Y"}).json()["series"]
    assert s[0]["trade_date"] >= "2026-01-05" and len(s) <= 180                # 수년 점프 없음


def test_real_mode_never_shows_demo_constants():
    _bars("005930", "2026-01-05", 120, 70000)
    db.set_meta("market_source", "krx")
    db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,net_income,total_equity) "
               "VALUES('005930',2025,'11011',-5000000000,300000000000000)")      # 적자
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] is None and f["pbr"] is not None                           # 적자 PER 은 '없음'(데모 14.2 아님)
    assert f["dividend_yield"] is None and f["fundamentals_source"] == "dart"
    db.execute("DELETE FROM financial_statement WHERE ticker='005930'")
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] is None and f["fundamentals_source"] is None               # 실데이터 모드: 재무 없으면 비움
    db.set_meta("market_source", "gen")
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] == 14.2 and f["fundamentals_source"] == "sample"           # 표본 모드에서만 데모 상수


def _row(fs, acc, amt):
    return {"fs_div": fs, "account_nm": acc, "thstrm_amount": amt, "rcept_no": "20260310000123"}


def test_dart_parse_uses_one_statement_basis_and_handles_loss_labels():
    data = {"status": "000", "list": [
        _row("OFS", "자산총계", "100"), _row("OFS", "유동부채", "10"), _row("OFS", "이익잉여금", "30"),
        _row("CFS", "자산총계", "1,000"), _row("CFS", "부채총계", "600"), _row("CFS", "자본총계", "400"),
        _row("CFS", "영업이익(손실)", "-50"), _row("CFS", "당기순이익(손실)", "-70"),
        _row("CFS", "이익잉여금(결손금)", "-120"), _row("CFS", "수익(매출액)", "2,000")]}
    p = dart.parse_financials(data)
    assert p["fs_basis"] == "CFS" and p["total_assets"] == 1000                 # 별도(OFS) 100 이 아니라 연결
    assert p["current_liabilities"] is None                                     # 연결에 없으면 별도로 채우지 않음
    assert p["operating_profit"] == -50 and p["net_income"] == -70 and p["revenue"] == 2000
    assert p["retained_earnings"] == -120 and p["debt_ratio"] == 150.0 and p["disclosed_at"] == "2026-03-10"
    only_ofs = dart.parse_financials({"status": "000", "list": [_row("OFS", "결손금", "40"),
                                                                 _row("OFS", "자산총계", "500")]})
    assert only_ofs["fs_basis"] == "OFS" and only_ofs["retained_earnings"] == -40


def test_frontend_types_have_no_duplicate_declarations():
    src = Path(__file__).resolve().parents[1].joinpath("frontend", "types.ts").read_text(encoding="utf-8")
    names = re.findall(r"export (?:interface|type) (\w+)", src)
    dups = sorted({n for n in names if names.count(n) > 1})
    assert not dups, f"중복 선언(타입 충돌로 tsc 실패): {dups}"


def _fin_rows(ticker):
    return db.query("SELECT fiscal_year, net_income FROM financial_statement WHERE ticker=:t ORDER BY fiscal_year",
                    {"t": ticker})


def test_demo_statements_are_purged_when_real_market_loads(monkeypatch):
    # 데모를 한 번이라도 돌린 DB 는 표본 재무(PER 역산 값)가 남는다 → 실데이터 전환 시 DART 가 못 채운 종목에서
    # 표본 재무로 PER·PBR·Z''·Merton PD 가 '실데이터'처럼 계산되던 문제
    import run
    from app import config
    from app.collectors import krx, marketgen
    from app.services import themes
    run._seed_demo_financials()
    themes.seed_demo_news()
    run._seed_market_news()
    db.execute("INSERT INTO news(title,url,source,published_at,published_ts) VALUES('실제 기사','https://n.example/1',"
               "'연합뉴스','2026-09-30','2026-09-30T09:00:00+09:00')")
    assert _fin_rows("005930") and _fin_rows("000660")
    assert db.query("SELECT 1 FROM news WHERE url LIKE 'demo://%'") and db.query("SELECT 1 FROM news_stock")
    db.execute("INSERT INTO financial_statement(ticker,fiscal_year,report_code,net_income,total_equity) "
               "VALUES('000660',2024,'11011',19800000000000,77000000000000)")       # 실제 DART 행(다른 연도)
    today = marketgen._today().isoformat()

    def day(bas_dd):
        if bas_dd >= today:
            return {}
        return {t: {"name": t, "market": "KOSPI", "open": 1000, "high": 1010, "low": 990, "close": 1000,
                    "volume": 1000, "value": 1, "mktcap": 10 ** 12, "shares": 1, "change_rate": 0.0}
                for t in ("005930", "000660")}
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "daily_stocks", day)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    assert marketgen.seed_market(days=3, real=True)["source"] == "krx"
    assert _fin_rows("005930") == []                                             # 표본만 있던 종목 → 비움
    assert _fin_rows("000660") == [{"fiscal_year": 2024, "net_income": 19800000000000}]   # 실제 행은 보존
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] is None and f["fundamentals_source"] is None                # 표본 재무로 PER 를 만들지 않음
    assert [r["url"] for r in db.query("SELECT url FROM news")] == ["https://n.example/1"]   # 가상 언론사 기사 제거
    assert not db.query("SELECT 1 FROM news_stock")                              # 매핑도 함께(고아 행 없음)


def test_startup_purges_leftover_demo_data_in_real_mode():
    import run
    from app.services import themes
    run._seed_demo_financials()
    themes.seed_demo_news()
    db.set_meta("market_source", "krx")
    with TestClient(app):                                                       # lifespan(서버 시작) 실행
        pass
    assert not db.query("SELECT 1 FROM financial_statement") and not db.query("SELECT 1 FROM news")
    run._seed_demo_financials()
    themes.seed_demo_news()
    db.set_meta("market_source", "gen")
    with TestClient(app):
        pass
    assert db.query("SELECT 1 FROM financial_statement") and db.query("SELECT 1 FROM news")   # 데모 모드에선 유지


def test_sample_market_labels_fundamentals_as_sample_even_with_statements():
    import run
    _bars("005930", "2026-01-05", 120, 70000)
    run._seed_demo_financials()
    db.set_meta("market_source", "gen")
    f = detail.stock_detail("005930")["fundamentals"]
    assert f["per"] is not None and f["fundamentals_source"] == "sample"       # 예전: 데모 재무인데 'dart'


def test_dart_statement_basis_is_stored_and_reported(monkeypatch):
    # 연결/별도 중 어느 재무제표로 계산했는지(데이터 계보) — 수집 시 저장하고 건전성 응답에 노출
    from app import config
    from app.services import soundness
    rows = [_row("OFS", "자산총계", "900"), _row("OFS", "부채총계", "300"), _row("OFS", "자본총계", "600"),
            _row("OFS", "당기순이익(손실)", "45"), _row("OFS", "영업이익(손실)", "60"), _row("OFS", "매출액", "700")]
    monkeypatch.setattr(config.settings, "DART_API_KEY", "test-key")
    monkeypatch.setattr(dart, "get_corp_code_map", lambda _k: {"005930": "00126380"})
    monkeypatch.setattr(dart, "fetch_financials", lambda _k, _cc: {"status": "000", "list": rows})
    assert dart.collect_financials(["005930"]) == {"ok": 1, "skipped": 0}
    assert db.query("SELECT fs_basis, net_income FROM financial_statement WHERE ticker='005930'") == \
        [{"fs_basis": "OFS", "net_income": 45}]
    sd = soundness.soundness("005930")
    assert sd["ok"] and sd["fs_basis"] == "OFS"


def test_cli_never_overwrites_real_db_with_demo(monkeypatch, capsys):
    # 예전: 인자 없이 'python run.py' 만 쳐도 demo 가 돌아 실데이터 시세를 표본으로 덮고 공개 비밀번호 계정을 만들었다
    import sys

    import pytest

    import run
    db.set_meta("market_source", "krx")
    monkeypatch.setattr(sys, "argv", ["run.py"])
    run.main()
    assert "사용법" in capsys.readouterr().out and db.get_meta("market_source") == "krx"
    monkeypatch.setattr(sys, "argv", ["run.py", "demo"])
    with pytest.raises(SystemExit) as e:
        run.main()
    assert e.value.code == 2 and "--force" in capsys.readouterr().out       # 2 = 잘못된 사용(--force 없이)
    assert db.get_meta("market_source") == "krx" and not db.query("SELECT 1 FROM app_user WHERE username='demo@oos.io'")


def test_zero_open_high_low_bars_are_filled_with_close():
    # 거래정지일처럼 KRX 가 시가·고가·저가를 0 으로 준 봉 — 예전엔 그대로 저장돼 52주 최저가 0원, 캔들이 0까지 늘어남
    from app.collectors import marketgen
    db.set_meta("market_source", "krx")
    _bars("005930", "2026-01-05", 60, 70000)
    with db.transaction() as conn:
        marketgen.store_ohlc(conn, "005930", [{"date": "2026-03-31", "open": 0, "high": 0, "low": 0, "close": 70500,
                                              "volume": 0},
                                             {"date": "2026-04-01", "open": 70100, "high": 0, "low": 0,
                                              "close": 70300, "volume": 5},
                                             {"date": "2026-04-02", "open": 0, "high": 0, "low": 0, "close": 0,
                                              "volume": 0}])                           # 종가도 없으면 저장 안 함
    rows = db.query("SELECT trade_date, open, high, low, close FROM ohlc WHERE ticker='005930' "
                    "AND trade_date >= '2026-03-31' ORDER BY trade_date")
    assert [(r["open"], r["high"], r["low"], r["close"]) for r in rows] == [
        (70500, 70500, 70500, 70500), (70100, 70300, 70100, 70300)]
    db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) "
               "VALUES('005930','2026-04-03',0,0,0,70200,0)")                   # 예전 버전이 저장한 0원 봉
    db.init_db()                                                                # 마이그레이션 정리(반복 무해)
    assert db.query("SELECT open, high, low FROM ohlc WHERE ticker='005930' AND trade_date='2026-04-03'") == \
        [{"open": 70200, "high": 70200, "low": 70200}]
    assert detail.stock_detail("005930")["fundamentals"]["low_52w"] > 0
