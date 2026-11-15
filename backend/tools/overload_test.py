"""과부하 시험 — 대량접속제어(우선순위 부하 차단)가 핵심 거래를 지키는지, 켠 서버와 끈 서버를 같은 부하로 비교한다.

    python tools/overload_test.py --base http://127.0.0.1:8101 --low 60 --critical 4 --seconds 20 [--json out.json]

부하: 공개 시세·뉴스 조회(low) 클라이언트 N개가 쉬지 않고 요청(503 이면 0.2초 뒤 다시 — 성급한 재시도),
      핵심 거래(critical) 클라이언트 M개가 '토큰 갱신(갱신 토큰 회전 + DB 잠금)'을 반복 — 로그인 유지의 핵심 경로.
보는 것: 핵심 거래의 성공률·p50/p95/p99, 공개 조회의 처리량·503 비율.
서버는 같은 설정에서 CAPACITY_ADMISSION=on / off 만 바꿔 두 번 띄운다(1 worker, CAPACITY_MAX_INFLIGHT 는 같게).
계정 준비는 DB 에 직접(가입 API 의 IP 당 시도 제한을 피하려고) — tools/loadtest.py 와 같은 방식.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOW = ["/market/stocks?sort=value&limit=50", "/api/news/home", "/indices", "/overseas/list", "/reco/top5"]
PW = "Ov-pass-2026"


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)


def _accounts(n):
    from app.services import auth
    names = [f"ov-user-{i:02d}" for i in range(n)]
    for u in names:
        try:
            auth.register(u, PW)
        except Exception:                                  # noqa: BLE001 — 이미 있음
            pass
    return names


def run(base: str, n_low: int, n_crit: int, seconds: int) -> dict:
    stop = time.monotonic() + seconds
    lock = threading.Lock()
    res = {"low": {"ok": 0, "shed": 0, "err": 0, "ms": []}, "critical": {"ok": 0, "fail": 0, "ms": [], "codes": {}}}

    def low_worker(i):
        with httpx.Client(base_url=base, timeout=30) as c:
            k = i
            while time.monotonic() < stop:
                t0 = time.perf_counter()
                try:
                    r = c.get(LOW[k % len(LOW)])
                    st = r.status_code
                except httpx.HTTPError:
                    st = 0
                ms = (time.perf_counter() - t0) * 1000
                k += 1
                with lock:
                    if st == 200 or st == 304:
                        res["low"]["ok"] += 1
                        res["low"]["ms"].append(ms)
                    elif st == 503:
                        res["low"]["shed"] += 1
                    else:
                        res["low"]["err"] += 1
                if st == 503:
                    time.sleep(0.2)

    def crit_worker(name):
        with httpx.Client(base_url=base, timeout=30) as c:
            j = c.post("/auth/login", json={"username": name, "password": PW}).json()
            rt = j.get("refresh_token")
            while time.monotonic() < stop and rt:
                t0 = time.perf_counter()
                try:
                    r = c.post("/auth/refresh", json={"refresh_token": rt})
                    st, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})
                except httpx.HTTPError:
                    st, body = 0, {}
                ms = (time.perf_counter() - t0) * 1000
                with lock:
                    if st == 200:
                        res["critical"]["ok"] += 1
                        res["critical"]["ms"].append(ms)
                        rt = body.get("refresh_token") or rt
                    else:
                        res["critical"]["fail"] += 1
                        res["critical"]["codes"][str(st)] = res["critical"]["codes"].get(str(st), 0) + 1
                time.sleep(0.05)

    names = _accounts(n_crit)
    ts = [threading.Thread(target=low_worker, args=(i,)) for i in range(n_low)]
    ts += [threading.Thread(target=crit_worker, args=(u,)) for u in names]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    took = time.monotonic() - t0
    lo, cr = res["low"], res["critical"]
    return {"seconds": round(took, 1), "low_clients": n_low, "critical_clients": n_crit,
            "critical": {"ok": cr["ok"], "fail": cr["fail"], "codes": cr["codes"],
                         "success_rate": round(cr["ok"] / max(1, cr["ok"] + cr["fail"]), 4),
                         "p50_ms": _pct(cr["ms"], .5), "p95_ms": _pct(cr["ms"], .95), "p99_ms": _pct(cr["ms"], .99),
                         "mean_ms": round(statistics.fmean(cr["ms"]), 1) if cr["ms"] else None},
            "low": {"ok": lo["ok"], "shed_503": lo["shed"], "err": lo["err"], "rps_ok": round(lo["ok"] / took, 1),
                    "p50_ms": _pct(lo["ms"], .5), "p95_ms": _pct(lo["ms"], .95)}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8101")
    ap.add_argument("--low", type=int, default=60)
    ap.add_argument("--critical", type=int, default=4)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--json")
    a = ap.parse_args()
    out = run(a.base, a.low, a.critical, a.seconds)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
