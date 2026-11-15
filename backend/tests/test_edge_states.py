"""8차 점검 — 데이터가 다시 받아지거나 비어 있는 '경계 상태'에서의 오동작.
각 테스트는 고치기 전 코드에서 실패한다(재현 → 수정 → 고정)."""
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.collectors import marketgen
from app.services import practice

client = TestClient(app)


# ── 실습: 커서 날짜의 봉이 사라진 세션(실데이터로 다시 받으며 생성 데이터의 휴장일 봉이 지워짐) ──
def _practice_with_removed_cursor_bar(user):
    sc = practice._BY_ID["hyundai-volume"]
    marketgen.seed_scenario_ohlc(sc, real=False)
    st = practice.start(user, "hyundai-volume")
    sid = st["session_id"]
    practice.trade(sid, "BUY", 10)
    st = practice.step(sid, 5)
    db.execute("DELETE FROM ohlc WHERE ticker=:t AND trade_date=:d", {"t": sc["ticker"], "d": st["cursor_date"]})
    return sid


def test_practice_order_uses_last_price_when_cursor_bar_was_removed():
    sid = _practice_with_removed_cursor_bar("edge-p1")
    shown = practice.get_state(sid)
    st = practice.trade(sid, "BUY", 1)                      # 예전: '현재 시점 가격이 없습니다'
    assert st["account"]["holding_qty"] == 11
    assert st["account"]["cash"] == shown["account"]["cash"] - int(shown["price"])


def test_practice_settlement_values_holdings_at_shown_price():
    sid = _practice_with_removed_cursor_bar("edge-p2")
    shown = practice.get_state(sid)["account"]["return_rate"]
    done = practice.end(sid)
    assert done["status"] == "done"
    assert done["final_return"] == shown                    # 예전: 보유 주식을 0원으로 평가해 손실로 확정


def test_practice_list_progress_matches_run_screen_when_cursor_bar_was_removed():
    _practice_with_removed_cursor_bar("edge-p3")
    run = practice.home_active("edge-p3")["progress"]
    item = next(i for i in practice.list_scenarios("edge-p3")["items"] if i["status"] == "active")
    assert run["current"] > 0
    assert item["progress"] == run                          # 예전: 목록만 0/N(0%) — 실행 화면은 실제 위치


# ── 학습 시뮬레이터(레거시 /backtest) ──
def _seed_bt(ticker="005930", prices=(100, 110, 120, 130, 140)):
    for i, p in enumerate(prices):
        db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) VALUES(:t, :d, :c, 0, 0)",
                   {"t": ticker, "d": f"2026-09-{i + 1:02d}", "c": p})


# ── 종목 상세: 아는 종목인데 시세가 아직 없음(새 VM 첫 수집 전) ──
def test_detail_404_explains_missing_data_for_known_stock():
    r = client.get("/stocks/005930/detail")
    assert r.status_code == 404 and "아직 시세 데이터" in r.json()["detail"]
    r = client.get("/stocks/999999/detail")
    assert r.status_code == 404 and "종목 없음" in r.json()["detail"]


# ── 지갑·송금·선물: 동시 재전송과 탈퇴 경합 ──
def _user(name):
    from app.services import auth
    auth.register(name, "Passw0rd!x")
    return name


def test_same_idempotency_key_sent_concurrently_is_applied_once_without_conflict():
    from app.services import wallet
    _user("edge-w1")
    wallet.credit_once("edge-w1", 1000, "attendance", idem_key="warm")       # 지갑 생성(환영금)
    before = wallet.balance("edge-w1")
    barrier, results, errors = threading.Barrier(6), [], []

    def go():
        barrier.wait()
        try:
            results.append(wallet.credit_once("edge-w1", 5000, "iap", idem_key="same-key"))
        except Exception as e:                              # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors                                       # 예전: 늦은 요청은 제약 위반(409)으로 실패
    assert sum(1 for r in results if r["applied"]) == 1
    assert wallet.balance("edge-w1") == before + 5000


def test_transfer_to_account_deleted_meanwhile_creates_no_ghost_wallet(monkeypatch):
    from app.services import transfer, wallet
    _user("edge-t1")
    wallet.credit_once("edge-t1", 1000, "attendance", idem_key="warm")
    real_query = transfer.db.query
    # 받는 계정 확인(트랜잭션 밖)은 통과했는데 그 직후 상대가 탈퇴한 상황
    monkeypatch.setattr(transfer.db, "query",
                        lambda sql, p=None: [{"?column?": 1}] if "FROM app_user" in sql else real_query(sql, p))
    with pytest.raises(transfer.TransferError):
        transfer.send("edge-t1", "edge-gone", 10_000)
    monkeypatch.setattr(transfer.db, "query", real_query)
    assert not db.query("SELECT 1 FROM wallet WHERE username='edge-gone'")       # 예전: 유령 지갑에 입금
    assert not db.query("SELECT 1 FROM wallet_ledger WHERE username='edge-t1' AND reason='transfer_out'")


def test_concurrent_check_in_pays_once_and_reports_already_checked():
    from app.services import attendance, wallet
    _user("edge-a1")
    wallet.balance("edge-a1")
    barrier, results, errors = threading.Barrier(5), [], []

    def go():
        barrier.wait()
        try:
            results.append(attendance.check_in("edge-a1", today="2026-10-05"))
        except Exception as e:                              # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors                                       # 예전: 늦은 요청은 제약 위반(409)
    assert sum(1 for r in results if r["claimed"]) == 1
    paid = db.query("SELECT COUNT(*) AS n FROM wallet_ledger WHERE username='edge-a1' AND reason='attendance'")
    assert paid[0]["n"] == 1


def test_chat_to_account_deleted_meanwhile_leaves_no_message(monkeypatch):
    from app.services import chat, friends
    _user("edge-c1")
    _user("edge-c2")
    friends.request("edge-c1", "edge-c2")
    friends.accept("edge-c2", "edge-c1")
    # 확인(트랜잭션 밖)은 통과했는데 그 직후 상대가 탈퇴한 상황
    monkeypatch.setattr(chat, "_exists", lambda u: True)
    monkeypatch.setattr(chat, "can_chat", lambda a, b: True)
    from app.services import account
    account.delete_account("edge-c2")
    with pytest.raises(chat.ChatError):
        chat.send("edge-c1", "edge-c2", "안녕")
    assert not db.query("SELECT 1 FROM chat_message WHERE receiver='edge-c2'")   # 예전: 없는 계정 앞으로 남음


# ── 데모 → 실데이터 전환: 실데이터 구간보다 오래된 데모(생성) 봉이 남아 위험·수익률 계산에 섞이던 문제 ──
def _fake_krx_two(today, close=90000):
    def f(bas_dd):
        if bas_dd >= today:
            return {}
        row = {"open": close, "high": close + 500, "low": close - 500, "close": close, "volume": 1_000_000,
               "value": 1, "shares": 1, "change_rate": 0.5}
        return {"005930": {"name": "삼성전자", "market": "KOSPI", "mktcap": 500_000_000_000_000, **row},
                "950010": {"name": "실데이터전용", "market": "KOSDAQ", "mktcap": 1_000_000_000_000, **row}}
    return f


def test_switch_from_demo_to_real_removes_older_generated_bars(monkeypatch):
    from app import config
    from app.collectors import krx
    marketgen.seed_market(real=False)                                   # 데모: 약 280일치 생성 봉
    today = marketgen._today().isoformat()
    monkeypatch.setattr(krx, "enabled", lambda: True)
    monkeypatch.setattr(krx, "index_day", lambda _b: [])
    monkeypatch.setattr(krx, "daily_stocks", _fake_krx_two(today))
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    assert marketgen.seed_market(days=5, real=True)["source"] == "krx"
    first = db.query("SELECT MIN(trade_date) AS d FROM daily_price WHERE ticker='950010'")[0]["d"]
    left = db.query("SELECT COUNT(*) AS n FROM daily_price WHERE ticker='005930' AND trade_date < :f", {"f": first})
    assert left[0]["n"] == 0                                            # 예전: 실데이터 앞에 생성 봉 수백 개
    assert not db.query("SELECT 1 FROM ohlc WHERE ticker='005930' AND trade_date < :f", {"f": first})


def test_existing_real_db_cleans_leftover_generated_bars_but_keeps_practice_ranges():
    sc = practice._BY_ID["samsung-golden"]
    marketgen.seed_scenario_ohlc(sc, real=False)                       # 과거 사례 구간(보존 대상)
    for t, d in (("950010", "2026-05-04"), ("950010", "2026-05-06"), ("005930", "2026-03-02"),
                 ("005930", "2026-05-04"), ("005930", "2026-05-06")):
        db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate) VALUES(:t,:d,1000,1,0)",
                   {"t": t, "d": d})
        db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES(:t,:d,1000,1000,1000,1000,1)",
                   {"t": t, "d": d})
    db.set_meta("market_source", "krx")
    before = db.query("SELECT COUNT(*) AS n FROM ohlc WHERE ticker='005930' AND trade_date BETWEEN :s AND :e",
                      {"s": sc["start_date"], "e": sc["end_date"]})[0]["n"]
    marketgen.purge_demo_artifacts()
    assert not db.query("SELECT 1 FROM daily_price WHERE ticker='005930' AND trade_date='2026-03-02'")
    assert not db.query("SELECT 1 FROM ohlc WHERE ticker='005930' AND trade_date='2026-03-02'")
    assert db.query("SELECT 1 FROM daily_price WHERE ticker='005930' AND trade_date='2026-05-04'")
    after = db.query("SELECT COUNT(*) AS n FROM ohlc WHERE ticker='005930' AND trade_date BETWEEN :s AND :e",
                     {"s": sc["start_date"], "e": sc["end_date"]})[0]["n"]
    assert after == before > 0                                          # 과거 사례 구간은 그대로


# ── 추천 감사 스냅샷: 확정 종가 전(15:35~15:40) 가격으로 영구 기록되던 문제 ──
def test_reco_snapshot_waits_until_close_is_confirmed(monkeypatch):
    from app.services import quotes, signals
    from app.services import reco_engine as R
    today = quotes.kr_session_date()
    db.execute("INSERT INTO ohlc(ticker,trade_date,open,high,low,close,volume) VALUES('005930',:d,1,1,1,1,1)",
               {"d": today})
    db.set_meta("market_source", "krx")
    monkeypatch.setattr(quotes, "kr_phase", lambda now=None: "closed")      # 장 마감 판정 직후, KIS 종가 갱신 전
    fake = {"as_of": today, "model": {"regime": "x", "weights": {}},
            "items": [{"ticker": "005930", "entry_price": 1, "rank": 1}]}
    monkeypatch.setattr(R, "recommend", lambda *a, **k: fake)
    saved = []
    monkeypatch.setattr(R, "save_snapshot", lambda res, prof=None: saved.append(res["as_of"]))
    signals.clear_cache()
    signals.today(5)
    assert saved == []                                                      # 예전: 이때 저장 → 틀린 매수가 영구 기록
    quotes.mark_close_final(today)                                          # 장 마감 뒤 KIS 갱신(또는 KRX 적재) 완료
    signals.clear_cache()
    signals.today(5)
    assert saved == [today]
    quotes.mark_close_final("2000-01-01")                                   # 뒤로 가지 않음
    assert quotes.close_is_final(today)


# ── 미국 시세: 장이 닫혀 있어도 폴링할 때마다 'LIVE', 애프터마켓 체결가가 '종가'로 남던 문제 ──
def test_us_poller_saves_the_trade_time_not_the_save_time(monkeypatch):
    from app.collectors import overseas_realtime as orl
    saved = []
    monkeypatch.setattr(orl, "save_quote", lambda s, q, source="finnhub", session_date=None, **k: saved.append(k))
    b = orl._Book()
    t = 1758758340                                                  # 2026-09-24 19:59 EDT(애프터마켓 마지막 체결)
    b.write("AAPL", b.base("AAPL", {"price": 264.5, "prev_close": 250.0, "t": t}, "2026-09-24"))
    assert saved and saved[-1].get("trade_ts") == t                 # 예전: 저장 시각 → 주말에도 폴링마다 'LIVE'


def test_us_official_close_wins_over_stale_after_hours_tick():
    from app.services import overseas
    overseas.ensure_data()
    sym = next(iter(overseas.US_STOCKS))
    db.execute("DELETE FROM overseas_ohlc WHERE symbol=:s", {"s": sym})
    for d, c in (("2026-09-23", 250.0), ("2026-09-24", 256.8)):
        db.execute("INSERT INTO overseas_ohlc(symbol,trade_date,open,high,low,close,volume) VALUES(:s,:d,:c,:c,:c,:c,1)",
                   {"s": sym, "d": d, "c": c})
    db.execute("DELETE FROM overseas_quote WHERE symbol=:s", {"s": sym})
    db.execute("INSERT INTO overseas_quote(symbol,price,change,change_rate,prev_close,session_date,source,ts) "
               "VALUES(:s,264.5,14.5,5.8,250.0,'2026-09-24','finnhub','2026-09-24T23:59:00+00:00')", {"s": sym})
    q = overseas.quotes_map([sym])[sym]
    assert q["status"] != "live"
    assert q["price"] == 256.8                                      # 예전: 애프터마켓 체결가 264.5 가 '종가'


# ── 시점(PIT) 정합: 장 마감 뒤 기사·당일 공시를 그날 종가 추천에 쓰던 미래참조, as_of 문자열 비교 ──
def test_after_close_news_counts_from_next_day():
    from sqlalchemy import text
    from app.services import reco_engine as R
    with db.transaction() as conn:
        for nid, ts in ((901, "2026-09-24T10:05:00+09:00"), (902, "2026-09-24T18:30:00+09:00")):
            conn.execute(text("INSERT INTO news(news_id,title,url,source,published_at,published_ts) "
                              "VALUES(:i,'t',:u,'x','2026-09-24',:ts)"), {"i": nid, "u": f"u{nid}", "ts": ts})
            conn.execute(text("INSERT INTO news_stock(news_id,ticker) VALUES(:i,'005930')"), {"i": nid})
            conn.execute(text("INSERT INTO news_sentiment(news_id,label,score,model) VALUES(:i,'POS',0.9,'t')"),
                         {"i": nid})
    panel = R.Panel(["005930"], end="2026-09-30", start="2026-09-20")
    same_day = sum(x[1] for x in panel.news_at("005930", "2026-09-24"))
    next_day = sum(x[1] for x in panel.news_at("005930", "2026-09-25"))
    assert same_day == 1 and next_day == 2                 # 예전: 9/24 종가 추천에 18:30 기사까지(2건)


def test_as_of_must_be_an_iso_date():
    assert client.get("/stocks/005930/soundness?as_of=2025-1-5").status_code == 422    # 예전: 문자열 비교로 미래 재무
    assert client.get("/stocks/005930/soundness?as_of=20250105").status_code == 422    # 문서 형식(YYYY-MM-DD)만
    assert client.get("/stocks/005930/soundness?as_of=2025-01-05").status_code == 200
    assert client.get("/stocks/005930/soundness?as_of=yesterday").status_code == 422


# ── NaN 입력·정의되지 않는 상관계수 → 500 ──


def test_rsi_of_a_flat_price_series_is_neutral():
    from app.services import indicators
    from app.services import reco_engine as R
    flat = [1000.0] * 30
    assert indicators.rsi(flat, 14)[-1] == 50.0                          # 예전: 100(극단적 과매수)
    assert R._rsi(R.np.array(flat), 14)[-1] == 50.0
    assert indicators.rsi([1000.0 + i for i in range(30)], 14)[-1] == 100.0


def test_search_accepts_decomposed_hangul_input():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "삼성")                         # macOS 에서 붙여 넣은 형태
    assert nfd != "삼성"
    r = client.get("/search/suggest", params={"q": nfd}).json()
    names = [x.get("name") for x in (r.get("items") or r.get("results") or [])]
    assert any(n and n.startswith("삼성") for n in names)               # 예전: 0건


def test_snapshot_export_reads_one_consistent_point_in_time(tmp_path, monkeypatch):
    from app.services import snapshot
    real_open = snapshot.gzip.open
    seen = {"headers": 0}

    class Spy:
        def __init__(self, f):
            self.f = f

        def write(self, s):
            if s.startswith('{"table": '):
                seen["headers"] += 1
                if seen["headers"] == 2:                   # 첫 표를 읽은 뒤(스냅샷 시점 이후) 다른 곳에서 뉴스 수집이 커밋
                    db.execute("INSERT INTO news(title,url,source,published_at) VALUES('late','u-late','x','2026-09-30')")
            return self.f.write(s)

        def __enter__(self):
            self.f.__enter__()
            return self

        def __exit__(self, *a):
            return self.f.__exit__(*a)
    monkeypatch.setattr(snapshot.gzip, "open", lambda *a, **k: Spy(real_open(*a, **k)))
    r = snapshot.export_snapshot(str(tmp_path / "s.jsonl.gz"))
    assert r["tables"]["news"] == 0                        # 예전: 내보내는 도중 커밋된 기사까지 섞임


def test_edgar_annual_value_ignores_q4_fact_with_same_end_date():
    from app.collectors import edgar
    usg = {"Revenues": {"units": {"USD": [
        {"form": "10-K", "fp": "FY", "fy": 2025, "start": "2025-07-01", "end": "2025-09-30", "val": 100, "filed": "2025-11-01"},
        {"form": "10-K", "fp": "FY", "fy": 2025, "start": "2024-10-01", "end": "2025-09-30", "val": 400, "filed": "2025-11-01"},
    ]}}}
    assert edgar._latest_annual(usg, ["Revenues"])["value"] == 400
    usg["Revenues"]["units"]["USD"].reverse()
    assert edgar._latest_annual(usg, ["Revenues"])["value"] == 400          # 예전: 순서에 따라 4분기 값(100)


def test_invest_sell_quantity_zero_is_rejected_not_full_sale():
    from app.services import invest, wallet
    from app.collectors import marketgen as mg
    mg.seed_market(real=False)
    _user("edge-i1")
    wallet.balance("edge-i1")
    pos = invest.buy("edge-i1", "005930", 3)
    with pytest.raises(invest.InvestError):
        invest.sell("edge-i1", pos["position_id"], 0)                     # 예전: 3주 전량 매도
    assert invest.sell("edge-i1", pos["position_id"])["sold"] == 3       # 생략하면 전량


# ── 로그인: 시도 제한 동시성 · 탈퇴/비밀번호 변경과의 경합 · 응답 시간 ──
def test_concurrent_wrong_passwords_are_capped_by_the_limit(monkeypatch):
    from app import config
    from app.services import auth
    _user("edge-l1")
    calls = []
    real_login = auth.login

    def slow_login(u, p, **kw):                            # 비밀번호 검사가 시간이 걸리는 동안 요청이 몰림
        calls.append(1)
        import time
        time.sleep(0.2)
        return real_login(u, p, **kw)
    monkeypatch.setattr(auth, "login", slow_login)
    monkeypatch.setattr(config.settings, "LOGIN_MAX_FAILS", 3)
    codes, barrier = [], threading.Barrier(12)

    def go():
        barrier.wait()
        codes.append(client.post("/auth/login", json={"username": "edge-l1", "password": "wrong-pass-1"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert len(calls) <= 3                                   # 예전: 12건 모두 비밀번호 검사(한도 무력화)
    assert codes.count(429) >= 9


def test_login_does_not_mint_token_after_password_changed_meanwhile(monkeypatch):
    from app.services import auth
    from app.security import passwords
    _user("edge-l2")
    real_verify = passwords.verify
    fired = []

    def verify_then_change(password, salt, stored):          # 확인 직후 다른 기기에서 비밀번호 변경이 끝남
        ok = real_verify(password, salt, stored)
        if not fired:
            fired.append(1)
            auth.change_password("edge-l2", "Passw0rd!x", "NewPassw0rd!")
        return ok
    monkeypatch.setattr(passwords, "verify", verify_then_change)
    with pytest.raises(auth.AuthError):
        auth.login("edge-l2", "Passw0rd!x")                  # 예전: 옛 비밀번호로 새 토큰 발급(24시간 유효)
    monkeypatch.setattr(passwords, "verify", real_verify)
    assert not db.query("SELECT 1 FROM auth_token WHERE username='edge-l2'")


def test_legacy_hash_login_costs_as_much_as_unknown_user(monkeypatch):
    from app.services import auth
    from app import config
    _user("edge-l3")
    db.execute("UPDATE app_user SET password_hash=:h WHERE username='edge-l3'",
               {"h": auth._hash("x" * 8, "00" * 16, auth.LEGACY_ITERATIONS)})   # 예전 형식(10만 회)
    monkeypatch.setattr(config.settings, "PASSWORD_HASH_ITERATIONS", 600_000)
    from app.security import passwords
    work = []
    real_pbkdf2 = passwords._pbkdf2
    monkeypatch.setattr(passwords, "_pbkdf2", lambda sec, s, it: work.append(it) or real_pbkdf2(sec, s, 1))
    with pytest.raises(auth.AuthError):
        auth.login("edge-l3", "wrong-password")
    existing = sum(work)
    work.clear()
    with pytest.raises(auth.AuthError):
        auth.login("edge-nobody", "wrong-password")
    assert existing == sum(work) == 600_000                  # 예전: 있는 아이디 10만 회 vs 없는 아이디 60만 회


# ── 인앱결제: 긴 영수증 저장 실패 · 탈퇴 후 같은 영수증 재사용 ──
def test_long_receipt_is_accepted_and_reuse_after_account_deletion_is_blocked():
    from app.services import account, iap, wallet
    receipt = "MII" + "A" * 6000                                  # 애플 영수증처럼 수 KB
    _user("edge-iap")
    r = iap.purchase("edge-iap", "apple", "coins_small", receipt)    # 예전: 512자 컬럼 초과로 실패
    assert r["coins"] == 1_000_000 and not r["duplicate"]
    account.delete_account("edge-iap")
    _user("edge-iap")                                             # 같은 아이디로 다시 가입
    again = iap.purchase("edge-iap", "apple", "coins_small", receipt)
    assert again["duplicate"] and again["coins"] == 0             # 예전: 탈퇴 때 기록이 지워져 다시 지급
    assert wallet.balance("edge-iap") < 1_000_000 + 1_000_001


def test_index_self_repair_runs_right_after_boot(monkeypatch):
    """VM 이 막 켜져 monotonic 시계가 작을 때(부팅 10초 뒤)도 첫 자가 복구는 바로 — 예전: '마지막 실행 0.0' 으로 보고 5분 대기."""
    import importlib.util
    from app import config
    from app.collectors import indexes as live_mod
    spec = importlib.util.spec_from_file_location("indexes_fresh", live_mod.__file__)
    indexes = importlib.util.module_from_spec(spec)                  # 갓 켜진 서버의 모듈 상태(다른 테스트와 분리)
    spec.loader.exec_module(indexes)
    ran = []
    monkeypatch.setattr(indexes, "refresh", lambda **k: ran.append(k))
    monkeypatch.setattr(indexes.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(config.settings, "INDEX_LIVE_REFRESH", True)
    monkeypatch.setattr(config.settings, "MARKET_SOURCE", "krx")
    db.set_meta("market_source", "krx")
    assert indexes.maybe_refresh_async(bootstrap=True) is True
