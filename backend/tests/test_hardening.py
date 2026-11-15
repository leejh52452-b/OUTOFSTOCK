"""운영 안정성·정합성 점검에서 찾은 결함의 회귀 테스트.
  · 잘못된 입력(길이 초과·NUL·정수 범위)이 500 이 아니라 422
  · 파라미터 범위(VaR 신뢰수준·what-if 기간/금액)
  · 장기 모의투자 매도 — 동시 요청 이중 입금 방지(행 잠금) · 부분 매도 실현손익 누계
  · 레거시 /sim — 주문가 가격제한폭 검증 · 데모 모드에서 가입 계정 사칭 차단
  · 백테스트 캐시 키 정규화 · 무거운 계산은 허용 값으로 맞추고 캐시 미스만 IP 당 횟수 제한"""
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.services import invest, reco_engine, wallet

client = TestClient(app)


def _token(username, password="pw123456"):
    client.post("/auth/register", json={"username": username, "password": password})
    return client.post("/auth/login", json={"username": username, "password": password}).json()["token"]


def _price(ticker="005930", close=70000, d="2026-09-30"):
    db.execute("INSERT INTO daily_price(ticker,trade_date,close,volume,change_rate,market_cap) "
               "VALUES(:t,:d,:c,1000,0,1) ON CONFLICT(ticker,trade_date) DO UPDATE SET close=:c",
               {"t": ticker, "d": d, "c": close})


# ── 잘못된 입력 → 422 (500 아님) ──
def test_db_rejected_values_return_422_not_500():
    h = {"Authorization": f"Bearer {_token('qa_user')}"}
    assert client.post("/auth/login", json={"username": "a\x00b", "password": "x"}).status_code == 422
    assert client.post("/search/log", json={"keyword": "\x00", "ticker": "005930"}).status_code in (200, 422)
    assert client.get("/stocks/005930/news", params={"offset": 2 ** 63}).status_code == 422
    r = client.post("/transfer", headers=h, json={"to": "qa_peer", "amount": 1, "memo": "x" * 5000})
    assert r.status_code in (400, 422)
    assert "SQL" not in r.text and "psycopg" not in r.text                     # 내부 오류문 노출 금지


def test_learning_log_long_or_dirty_input_is_clipped():
    h = {"Authorization": f"Bearer {_token('qa_learn')}"}
    r = client.post("/learn/log", headers=h, json={"kind": "term\x00" + "k" * 50, "item_key": "x" * 5000,
                                                   "item_name": "y" * 5000})
    assert r.status_code == 422                                                # 길이 초과는 요청 단계에서 거절
    r = client.post("/learn/log", headers=h, json={"kind": "term\x00", "item_key": "x" * 60, "item_name": "y" * 100})
    assert r.status_code == 200                                                # 한도 안의 NUL 은 지우고 저장
    row = db.query("SELECT kind, item_key, item_name FROM learning_log WHERE user_id='qa_learn'")[0]
    assert len(row["kind"]) <= 20 and "\x00" not in row["kind"] and len(row["item_key"]) == 60


def test_param_ranges_validated():
    for conf in (0, -1, 1.5, 0.5):
        assert client.get("/stocks/005930/var-backtest", params={"conf": conf}).status_code == 422


# ── 장기 모의투자 매도: 동시성·부분 매도 ──
def test_concurrent_sell_of_same_position_credits_once(monkeypatch):
    _price()
    monkeypatch.setattr(invest, "_current_price", lambda t, live=None: (time.sleep(0.3), 70000)[1])
    monkeypatch.setattr(invest, "_latest_date", lambda t: "2026-09-30")
    from app.services import auth
    auth.register("alice", "Hd-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    pid = invest.buy("alice", "005930", 10)["position_id"]
    before = wallet.balance("alice")
    ok, errs = [], []

    def go():
        try:
            ok.append(invest.sell("alice", pid))
        except invest.InvestError as e:
            errs.append(str(e))
    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ok) == 1 and len(errs) == 2 and all("청산" in e for e in errs)
    assert wallet.balance("alice") - before == 700_000                         # 한 번만 입금


def test_partial_sells_accumulate_realized_pnl(monkeypatch):
    px = {"v": 100}
    monkeypatch.setattr(invest, "_current_price", lambda t, live=None: px["v"])
    monkeypatch.setattr(invest, "_latest_date", lambda t: "2026-09-30")
    from app.services import auth
    auth.register("bob", "Hd-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    pid = invest.buy("bob", "005930", 10)["position_id"]                      # 원가 1,000
    px["v"] = 150
    assert invest.sell("bob", pid, 4)["realized_pnl"] == 200                   # 4주 × (150-100)
    mid = invest.positions("bob")
    assert mid["summary"]["realized_pnl"] == 200 and mid["open"][0]["quantity"] == 6
    px["v"] = 120
    invest.sell("bob", pid)                                                    # 남은 6주 × (120-100) = 120
    p = db.query("SELECT * FROM invest_position WHERE position_id=:p", {"p": pid})[0]
    assert p["realized_pnl"] == 320 and p["proceeds"] == 600 + 720 and p["sold_qty"] == 10
    closed = invest.positions("bob")["closed"][0]
    assert closed["quantity"] == 10 and closed["realized_pct"] == 32.0         # 320 / 1,000


# ── 레거시 /sim ──


# ── 백테스트 캐시 키 정규화 ──
def test_backtest_params_normalized_for_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(reco_engine, "backtest", lambda *a, **k: calls.append(a) or {"available": False})
    reco_engine.clear_cache()
    for days in (400, 401, 100_000):                                           # 모두 400 으로 정규화 → 1회 계산
        client.get("/reco/top5/backtest", params={"days": days, "horizon": 20})
    assert len(calls) == 1
    reco_engine.clear_cache()


def test_backtest_params_snap_to_small_set_and_bound_work():
    # 예전: 범위 안 아무 값이나 받음 → days·horizon·step 만 바꾼 요청마다 새 계산(수백만 가지), step=1·400일이면 기준일 140개
    seen = {reco_engine.backtest_params(d, h, s, n)
            for d in range(1, 450, 7) for h in range(1, 70, 3) for s in (None, 1, 2, 3, 7, 13, 33, 61) for n in range(0, 12)}
    R = reco_engine
    assert len(seen) <= len(R.BT_DAYS) * len(R.BT_HORIZONS) ** 2 * len(R.BT_LIMITS)   # 450 가지 이하
    for days, horizon, step, limit in seen:
        assert horizon in reco_engine.BT_HORIZONS and step in reco_engine.BT_HORIZONS and limit in reco_engine.BT_LIMITS
        assert days >= horizon + step and days // step <= 80                   # 한 번 계산의 기준일 수 상한
    assert reco_engine.backtest_params(180, 20, None, 5) == (180, 20, 20, 5)   # 화면 기본값은 그대로
    assert reco_engine.backtest_params(100, 10) == (90, 10, 10, 5)
    assert {reco_engine.monitor_ref_days(d) for d in range(20, 251)} == set(reco_engine.MONITOR_REF_DAYS)


def test_heavy_endpoints_limit_cache_misses_per_ip_not_cache_hits(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "HEAVY_MISS_PER_MIN", 3)
    calls = []
    monkeypatch.setattr(reco_engine, "backtest", lambda *a, **k: calls.append(a) or {"available": False})
    reco_engine.clear_cache()
    for h in (5, 10, 20):                                                      # 서로 다른 계산 3번까지는 허용
        assert client.get("/reco/top5/backtest", params={"horizon": h}).status_code == 200
    r = client.get("/reco/top5/backtest", params={"horizon": 40})              # 4번째 새 계산 → 429
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert len(calls) == 3
    for h in (5, 10, 20):                                                      # 캐시에 있는 값은 계속 바로
        assert client.get("/reco/top5/backtest", params={"horizon": h}).status_code == 200
    assert client.get("/reco/model/monitor").status_code == 429                # 같은 IP 의 다른 무거운 계산도 같이 셈
    assert len(calls) == 3
    reco_engine.clear_cache()


def test_deploy_launchers_keep_alive_longer_than_client_pools():
    # uvicorn 기본 keep-alive 5초 = 브라우저·Node·파이썬 연결 풀의 유휴 시간과 같아, 서버가 닫는 순간 클라이언트가
    # 그 연결을 재사용하면 '응답 없이 끊김'(부하 시험 80명·60초 5,824건 중 2건) → 운영 기동 명령은 65초
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for f in ("Dockerfile", "deploy/outofstock-api.service", "deploy/vm.sh"):
        src = root.joinpath(f).read_text(encoding="utf-8")
        launches = [ln for ln in src.splitlines() if "uvicorn" in ln and "app.api:app" in ln and "--host" in ln]
        assert launches, f
        for ln in launches:
            m = re.search(r'--timeout-keep-alive"?,?\s*"?(\d+)', ln)
            assert m and int(m.group(1)) > 5, (f, ln)


def test_images_are_cacheable_but_page_html_and_scripts_are_not():
    # VM·VPN 너머에선 파일마다 304 왕복도 비용 — 그림은 하루 캐시, 화면 HTML 은 매번 새로(nonce), 스크립트는 매번 확인(304)
    r = client.get("/assets/logo.png")
    assert r.status_code == 200 and "max-age=86400" in r.headers.get("cache-control", "")
    r = client.get("/assets/terms/" + next(p.name for p in (Path(__file__).resolve().parents[1] / "web/assets/terms").iterdir()))
    assert r.status_code == 200 and "max-age=86400" in r.headers.get("cache-control", "")
    assert client.get("/assets/terms.js").headers["cache-control"] == "no-cache"
    assert client.get("/learn.html").headers["cache-control"] == "no-store"


def test_request_body_size_is_capped(monkeypatch):
    # VM 배포는 nginx 없이 uvicorn 이 바로 받는다 — 예전엔 본문 상한이 없어 로그인 1건(200MB)에 API 메모리가 1.3GB 까지
    from app.config import settings
    limit = settings.MAX_BODY_BYTES
    big = b'{"username":"a","password":"' + b"x" * (limit + 10) + b'"}'
    r = client.post("/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "너무 커요" in r.json()["detail"]
    assert r.headers.get("x-content-type-options") == "nosniff"                 # 413 에도 보안 헤더

    def chunks():                                                              # 길이 표시 없이(chunked) 보내도
        yield b'{"username":"a","password":"'
        for _ in range(limit // 65536 + 2):
            yield b"y" * 65536
        yield b'"}'
    r = client.post("/auth/login", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    ok = b'{"username":"nobody@oos.io","password":"' + b"z" * 1000 + b'"}'     # 보통 크기는 그대로 처리
    assert client.post("/auth/login", content=ok, headers={"content-type": "application/json"}).status_code == 401


def test_concurrent_requests_for_same_heavy_result_are_not_counted_as_new_computations(monkeypatch):
    # 학교 VPN 처럼 여러 명이 한 IP 로 보일 때 — 같은 화면을 동시에 열면 계산은 1번, 나머지는 그 결과를 기다릴 뿐(429 아님)
    from app.config import settings
    monkeypatch.setattr(settings, "HEAVY_MISS_PER_MIN", 1)
    gate, calls = threading.Event(), []

    def slow(*a, **k):
        calls.append(1)
        gate.wait(5)
        return {"available": False}
    monkeypatch.setattr(reco_engine, "backtest", slow)
    reco_engine.clear_cache()
    codes = []
    first = threading.Thread(target=lambda: codes.append(client.get("/reco/top5/backtest").status_code))
    first.start()
    for _ in range(100):                                                      # 첫 요청이 계산을 시작할 때까지
        if calls:
            break
        time.sleep(0.02)
    others = [threading.Thread(target=lambda: codes.append(client.get("/reco/top5/backtest").status_code))
              for _ in range(5)]
    for t in others:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in [first, *others]:
        t.join(10)
    assert codes == [200] * 6 and len(calls) == 1
    reco_engine.clear_cache()
