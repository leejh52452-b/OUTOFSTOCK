"""추천 엔진 캐시 — 만료 뒤엔 가진 값 먼저(뒤에서 1번만 다시 계산), 같은 키 동시 계산은 1번(single-flight).
예전: 5분마다 첫 요청이 추천 계산(1~2초, 작은 VM 에선 더)을 기다렸고, 그때 몰린 요청도 각자 같은 계산을 반복했다."""
import threading
import time

from app.services import reco_engine as R


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _wait_refresh(key, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        with R._GUARD:
            if key not in R._REFRESHING:
                return
        time.sleep(0.01)
    raise AssertionError("백그라운드 갱신이 끝나지 않음")


def test_expired_entry_is_served_immediately_and_refreshed_once(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(R.time, "monotonic", clk)
    R.clear_cache()
    calls = []
    gate = threading.Event()

    def compute():
        calls.append(1)
        if len(calls) > 1:
            gate.wait(2)                                  # 다시 계산이 오래 걸려도
        return len(calls)
    key = ("t-swr",)
    assert R._cached(key, compute, ttl=10) == 1
    clk.t += 11                                           # 만료
    t0 = time.perf_counter()
    assert R._cached(key, compute, ttl=10) == 1           # 기다리지 않고 가진 값
    assert R._cached(key, compute, ttl=10) == 1           # 갱신 중 또 와도 다시 계산을 또 걸지 않음
    assert time.perf_counter() - t0 < 0.5
    gate.set()
    _wait_refresh(key)
    assert len(calls) == 2 and R._cached(key, compute, ttl=10) == 2


def test_concurrent_misses_compute_once(monkeypatch):
    R.clear_cache()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return "v"
    out = []
    ts = [threading.Thread(target=lambda: out.append(R._cached(("t-sf",), slow))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == ["v"] * 8 and len(calls) == 1


def test_too_stale_entry_is_recomputed_synchronously(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(R.time, "monotonic", clk)
    R.clear_cache()
    n = iter(range(1, 10))
    key = ("t-old",)
    assert R._cached(key, lambda: next(n), ttl=10) == 1
    clk.t += 10 * (R._STALE_FACTOR + 2)                   # 아주 오래 지나면 옛 값을 주지 않음
    assert R._cached(key, lambda: next(n), ttl=10) == 2


def test_clear_cache_discards_in_flight_refresh(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(R.time, "monotonic", clk)
    R.clear_cache()
    gate = threading.Event()
    key = ("t-gen",)
    R._cached(key, lambda: "old", ttl=10)
    clk.t += 11

    def late():
        gate.wait(2)
        return "from-before-clear"
    assert R._cached(key, late, ttl=10) == "old"          # 뒤에서 계산 시작
    R.clear_cache()                                       # 데이터가 바뀌어 캐시를 비움
    gate.set()
    _wait_refresh(key)
    assert key not in R._CACHE                            # 옛 계산 결과가 새 캐시에 들어오지 않음
