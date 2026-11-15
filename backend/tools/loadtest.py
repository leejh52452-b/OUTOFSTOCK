"""부하 시험 — 화면 사용 패턴을 흉내 낸 가상 사용자로 처리량·지연(p50/p95/p99)·오류율·전송량을 잰다.

    python tools/loadtest.py --base http://127.0.0.1:8000 --users 20 --seconds 30 [--etag] [--json out.json]
    python tools/loadtest.py --base … --compare-polling --users 20 --seconds 30   # 채팅 폴링 vs SSE 요청 수

시나리오(가중치): 비로그인 열람 60% · 로그인 사용자 화면 35% · 쓰기 5%. 로그인 사용자는 미리 만든 계정(lt-user-NN)으로
각자 한 번 로그인해 Bearer 토큰을 쓴다(기기 지문이 일정해야 세션이 유지된다 — 제로트러스트 기기 결속).
--etag: 브라우저처럼 URL 별 ETag 를 기억해 If-None-Match 를 보낸다(304 비율·절약 바이트 측정).
계정 준비: 같은 폴더의 DB 설정으로 직접 만든다(가입 API 의 IP 당 시도 제한을 피하려고).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PUBLIC = [("/indices", 3), ("/reco/top5", 2), ("/api/news/home", 2), ("/market/stocks?sort=value&limit=30", 2),
          ("/stocks/005930/detail", 2), ("/stocks/005930/ohlc?period=3m", 1), ("/learn/dictionary", 1),
          ("/overseas/list", 1), ("/search/suggest?q=삼성", 1)]
PRIVATE = [("/me", 2), ("/wallet", 2), ("/me/badges", 3), ("/watchlist", 1), ("/chat/rooms", 1), ("/invest", 1)]
PW = "Lt-pass-2026"


def _pick(items):
    total = sum(w for _p, w in items)
    x = random.uniform(0, total)
    for p, w in items:
        x -= w
        if x <= 0:
            return p
    return items[-1][0]


def prepare_users(n: int) -> list[str]:
    from app import db
    from app.services import auth
    db.init_db()
    names = [f"lt-user-{i:02d}" for i in range(n)]
    for u in names:
        try:
            auth.register(u, PW)
        except auth.AuthError:
            pass
    return names


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = defaultdict(list)
        self.status = defaultdict(lambda: defaultdict(int))
        self.bytes = defaultdict(int)

    def add(self, key, ms, status, nbytes):
        with self.lock:
            self.lat[key].append(ms)
            self.status[key][status] += 1
            self.bytes[key] += nbytes


def _q(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def run(base, users, seconds, etag, names):
    stats, stop = Stats(), time.time() + seconds
    tokens = {}
    with httpx.Client(base_url=base, timeout=20) as c:
        for u in names:
            r = c.post("/auth/login", json={"username": u, "password": PW})
            tokens[u] = r.json().get("token")

    def worker(i):
        u = names[i % len(names)]
        cache = {}
        with httpx.Client(base_url=base, timeout=20) as c:
            while time.time() < stop:
                roll = random.random()
                if roll < 0.60:
                    path, hdr, key = _pick(PUBLIC), {}, "public"
                elif roll < 0.95:
                    path, hdr, key = _pick(PRIVATE), {"Authorization": f"Bearer {tokens[u]}"}, "private"
                else:
                    path, hdr, key = "/search/log", {}, "write"
                if etag and path in cache:
                    hdr["If-None-Match"] = cache[path]
                t0 = time.perf_counter()
                try:
                    if key == "write":
                        r = c.post(path, json={"keyword": "삼성", "ticker": "005930"})
                    else:
                        r = c.get(path, headers=hdr)
                    ms = (time.perf_counter() - t0) * 1000
                    if etag and r.headers.get("etag"):
                        cache[path] = r.headers["etag"]
                    stats.add(path.split("?")[0], ms, r.status_code, len(r.content))
                    stats.add(f"<{key}>", ms, r.status_code, len(r.content))
                except httpx.HTTPError as e:
                    stats.add(path.split("?")[0], (time.perf_counter() - t0) * 1000, type(e).__name__, 0)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(users)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.time() - t0
    rows = []
    for k in sorted(stats.lat, key=lambda x: (not x.startswith("<"), x)):
        lat, st = stats.lat[k], stats.status[k]
        n = len(lat)
        errs = sum(v for s, v in st.items() if not (isinstance(s, int) and s < 500))
        rows.append({"endpoint": k, "count": n, "rps": round(n / elapsed, 1), "p50_ms": _q(lat, .5), "p95_ms": _q(lat, .95),
                     "p99_ms": _q(lat, .99), "mean_ms": round(statistics.mean(lat), 1) if lat else None,
                     "errors": errs, "status": {str(s): v for s, v in st.items()},
                     "kb": round(stats.bytes[k] / 1024, 1)})
    total = sum(r["count"] for r in rows if not r["endpoint"].startswith("<"))
    return {"users": users, "seconds": round(elapsed, 1), "etag": etag, "total": total,
            "rps": round(total / elapsed, 1), "rows": rows}


def compare_polling(base, users, seconds, names):
    """채팅방을 연 사용자 users 명 — 2초 폴링(예전 화면)과 SSE(열린 스트림 1개)의 요청 수를 실제로 센다."""
    tokens = {}
    with httpx.Client(base_url=base, timeout=20) as c:
        for u in names[:users]:
            tokens[u] = c.post("/auth/login", json={"username": u, "password": PW}).json().get("token")
    counts = {"polling": 0, "sse": 0}
    lock = threading.Lock()
    stop = time.time() + seconds

    def poller(u):
        with httpx.Client(base_url=base, timeout=20) as c:
            while time.time() < stop:
                c.get("/me/badges", headers={"Authorization": f"Bearer {tokens[u]}"})
                with lock:
                    counts["polling"] += 1
                time.sleep(2)

    def streamer(u):
        try:
            with httpx.stream("GET", base + "/events", headers={"Authorization": f"Bearer {tokens[u]}"},
                              timeout=seconds + 5) as r:
                with lock:
                    counts["sse"] += 1
                for _ in r.iter_lines():
                    if time.time() >= stop:
                        break
        except httpx.HTTPError:
            pass
    for target in (poller, streamer):
        ts = [threading.Thread(target=target, args=(u,), daemon=True) for u in names[:users]]
        for t in ts:
            t.start()
        for t in ts:
            t.join(seconds + 10)
        stop = time.time() + seconds
    per_min = {k: round(v / users / (seconds / 60), 1) for k, v in counts.items()}
    return {"users": users, "seconds": seconds, "requests": counts, "per_user_per_min": per_min}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--etag", action="store_true")
    ap.add_argument("--compare-polling", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    names = prepare_users(max(a.users, 5))
    res = compare_polling(a.base, a.users, a.seconds, names) if a.compare_polling else run(
        a.base, a.users, a.seconds, a.etag, names)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
