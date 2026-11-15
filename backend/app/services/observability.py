"""관측(가시성) — 요청 1건마다 구조화 접근 로그(JSON 한 줄) + 경로별 지연·오류 지표(메모리).

  접근 로그 필드  rid(요청 ID) · m · route(경로 템플릿 — 쿼리 문자열·경로 값은 남기지 않는다) · status · ms ·
                  principal(anonymous|guest|user) · user(가명 HMAC) · action(정책 동작) · decision(allow|deny:코드) ·
                  ip(가명 HMAC) · device(브라우저·OS 계열)
  개인정보 최소화  아이디·IP 원문, 검색어 같은 쿼리 값은 로그에 남기지 않는다. 같은 사람·같은 IP 인지는 가명으로
                  비교할 수 있어 이상 징후 분석에는 충분하다. 원문이 필요한 조사는 감사 로그·보안 이벤트(권한 필요)로.
  지표            경로별 요청 수·오류 수·지연 히스토그램(p50/p95/p99) — /admin/metrics(권한 필요)·부하 시험 보고서용.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from bisect import bisect_left

from app.security import keys

log = logging.getLogger("access")

BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_lock = threading.Lock()
_routes: dict[str, dict] = {}
_started = time.time()


def request_finished(ctx) -> None:
    ms = (time.perf_counter() - ctx.t0) * 1000.0
    route = ctx.route or ("<static>" if ctx.path.startswith("/static/") else "<unmatched>")
    p = ctx.principal
    rec = {"rid": ctx.request_id, "m": ctx.method, "route": route, "status": ctx.status, "ms": round(ms, 1),
           "principal": p.kind, "user": keys.hmac_hex("user", p.name) if p.kind == "user" and p.name else None,
           "action": ctx.action, "decision": ctx.decision,
           "ip": keys.hmac_hex("ip", ctx.ip) if ctx.ip else None, "device": ctx.device_label or None}
    level = logging.WARNING if (ctx.status or 500) >= 500 else logging.INFO
    if log.isEnabledFor(level):
        log.log(level, json.dumps(rec, ensure_ascii=False))
    key = f"{ctx.method} {route}"
    with _lock:
        r = _routes.setdefault(key, {"n": 0, "err4": 0, "err5": 0, "sum_ms": 0.0,
                                     "hist": [0] * (len(BUCKETS_MS) + 1), "max_ms": 0.0})
        r["n"] += 1
        r["sum_ms"] += ms
        r["max_ms"] = max(r["max_ms"], ms)
        st = ctx.status or 500
        if 400 <= st < 500:
            r["err4"] += 1
        elif st >= 500:
            r["err5"] += 1
        r["hist"][bisect_left(BUCKETS_MS, ms)] += 1


def _quantile(hist, n, q):
    if not n:
        return None
    target, acc = q * n, 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= target:
            return BUCKETS_MS[i] if i < len(BUCKETS_MS) else float("inf")
    return None


def snapshot(top: int = 50) -> dict:
    """경로별 요약 — 요청 수 많은 순. 분위수는 히스토그램 상한(근사치, 단위 ms)."""
    with _lock:
        items = [(k, dict(v, hist=list(v["hist"]))) for k, v in _routes.items()]
    out = []
    for k, v in sorted(items, key=lambda kv: -kv[1]["n"])[:top]:
        n = v["n"]
        out.append({"route": k, "count": n, "err4": v["err4"], "err5": v["err5"],
                    "avg_ms": round(v["sum_ms"] / n, 1) if n else None, "max_ms": round(v["max_ms"], 1),
                    "p50_ms": _quantile(v["hist"], n, 0.5), "p95_ms": _quantile(v["hist"], n, 0.95),
                    "p99_ms": _quantile(v["hist"], n, 0.99)})
    return {"since_epoch": int(_started), "routes": out}


def prometheus() -> str:
    """Prometheus 텍스트 형식(수집기 연동용)."""
    lines = ["# TYPE oos_http_requests_total counter", "# TYPE oos_http_request_duration_ms histogram"]
    with _lock:
        items = list(_routes.items())
    for k, v in items:
        m, route = k.split(" ", 1)
        lab = f'method="{m}",route="{route}"'
        lines.append(f"oos_http_requests_total{{{lab}}} {v['n']}")
        lines.append(f"oos_http_errors_total{{{lab},class=\"4xx\"}} {v['err4']}")
        lines.append(f"oos_http_errors_total{{{lab},class=\"5xx\"}} {v['err5']}")
        acc = 0
        for i, b in enumerate(BUCKETS_MS):
            acc += v["hist"][i]
            lines.append(f"oos_http_request_duration_ms_bucket{{{lab},le=\"{b}\"}} {acc}")
        lines.append(f"oos_http_request_duration_ms_bucket{{{lab},le=\"+Inf\"}} {v['n']}")
        lines.append(f"oos_http_request_duration_ms_sum{{{lab}}} {round(v['sum_ms'], 1)}")
        lines.append(f"oos_http_request_duration_ms_count{{{lab}}} {v['n']}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    with _lock:
        _routes.clear()
