"""용량·성능 관리 — 금융감독원 「금융IT 안정성 강화를 위한 가이드라인」(2023.11)의 '성능 관리'를 서버 안에서 돌린다.

  임계치 4단계    정상 → 주의 → 경계 → 심각. 지표는 ① 동시에 처리 중인 요청 수(워커별, SSE 같은 긴 연결 제외)
                  ② DB 연결 사용률(풀) ③ 최근 1분 p95 지연 ④ 최근 1분 5xx 비율. 단계 = 지표별 단계 중 가장 나쁜 것.
  대량접속제어    요청마다 '중요도'(policy.json 동작의 tier — critical · normal · low)를 보고
                  경계면 low(공개 시세·뉴스 조회)를, 심각이면 normal 까지 즉시 503(Retry-After)으로 돌려보낸다 —
                  로그인·송금·결제·관리 같은 핵심 거래(critical)는 끝까지 받는다. 돌려보낸 요청은 DB·스레드를 쓰지 않는다.
                  입장 판단은 '즉시 값'(①②)만 쓴다. 지연·오류율(③④)로 막으면 막을수록 지연이 줄어 다시 열리는 진동이 생긴다
                  — 그래서 ③④는 단계 보고·경보에만 쓴다.
  성능관리보고서  보고 단계가 경계·심각으로 올라가면 그 순간의 지표·느린 경로·돌려보낸 건수·권고를 담아 capacity_report 에 남기고
                  운영 알림(같은 단계는 10분 안에 다시 보내지 않는다). 가이드라인: 경계·심각 → 원인 분석·대응방안 보고(CIO 지체 없이).
워커(프로세스)마다 따로 센다 — 한도(CAPACITY_MAX_INFLIGHT)는 워커 하나의 스레드풀(기본 40)에 맞춘다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque

from app.config import settings

log = logging.getLogger("capacity")

LEVELS = ("정상", "주의", "경계", "심각")
TIERS = ("critical", "normal", "low")
# 단계별로 받지 않는 중요도 — 경계(2): low, 심각(3): low·normal
SHED_AT = {"low": 2, "normal": 3}
EXCLUDE_PATHS = ("/events",)            # SSE — 오래 열려 있지만 스레드·DB 연결을 붙잡지 않는다(동시 요청 수에서 뺀다)
WINDOW_S = 60.0

_lock = threading.Lock()
_inflight = 0
_peak = 0
_recent: deque = deque(maxlen=8192)     # (끝난 시각, ms, status) — 받아들인 요청만(돌려보낸 503 은 따로 센다)
_shed = {t: 0 for t in TIERS}
_pool_timeouts = [0]
_state = {"level": 0, "since": time.time(), "checked": 0.0, "reported": {}}   # reported: 단계 → 마지막 보고 시각
_mono = time.monotonic                  # 시험에서 바꿔 끼운다


class Overloaded(Exception):
    """대량접속제어로 돌려보냄 — 503 + Retry-After."""
    def __init__(self, level: int, tier: str):
        super().__init__(LEVELS[level])
        self.level, self.tier = level, tier
        self.retry_after = 2 if level < 3 else 5


def _cfg_ratio() -> tuple[float, float, float]:
    try:
        a, b, c = (float(x) for x in settings.CAPACITY_RATIOS.split(","))
        return a, b, c
    except ValueError:
        return 0.6, 0.8, 0.95


def _cfg_ms() -> tuple[float, float, float]:
    try:
        a, b, c = (float(x) for x in settings.CAPACITY_P95_MS.split(","))
        return a, b, c
    except ValueError:
        return 500.0, 1000.0, 3000.0


def _bucket(v: float, cuts: tuple[float, float, float]) -> int:
    return 3 if v >= cuts[2] else 2 if v >= cuts[1] else 1 if v >= cuts[0] else 0


# ── 요청 들고 나기(미들웨어) ─────────────────────────────────
def counted(path: str) -> bool:
    return not path.startswith(EXCLUDE_PATHS)


def enter() -> None:
    global _inflight, _peak
    with _lock:
        _inflight += 1
        _peak = max(_peak, _inflight)


def leave(ms: float | None, status: int | None, shed: bool = False) -> None:
    global _inflight
    now = time.time()
    with _lock:
        _inflight = max(0, _inflight - 1)
        if not shed and ms is not None:
            _recent.append((now, ms, status or 500))
    if now - _state["checked"] >= 1.0:                 # 단계 점검은 1초에 한 번(요청마다 정렬하지 않게)
        _state["checked"] = now
        try:
            _transition(level_now())
        except Exception as e:                         # noqa: BLE001 — 관측이 응답을 막지 않는다
            log.error("용량 단계 점검 실패: %s", type(e).__name__)


def note_pool_timeout() -> None:
    with _lock:
        _pool_timeouts[0] += 1


# ── 지표 ───────────────────────────────────────────────────
def _pool() -> tuple[int, int]:
    """(사용 중 연결, 풀 최대) — 풀을 못 읽으면 (0, 1)."""
    try:
        from app import db
        p = db.get_engine().pool
        cap = int(p.size()) + max(0, int(getattr(p, "_max_overflow", 0)))
        return int(p.checkedout()), max(1, cap)
    except Exception:                                  # noqa: BLE001
        return 0, 1


def _window():
    cut = time.time() - WINDOW_S
    with _lock:
        rows = [r for r in _recent if r[0] >= cut]
    return rows


def metrics() -> dict:
    used, cap = _pool()
    rows = _window()
    lat = sorted(r[1] for r in rows)
    p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0.0
    err5 = sum(1 for r in rows if r[2] >= 500)
    with _lock:
        inflight, peak, shed, pto = _inflight, _peak, dict(_shed), _pool_timeouts[0]
    return {"inflight": inflight, "max_inflight": settings.CAPACITY_MAX_INFLIGHT, "peak_inflight": peak,
            "pool_in_use": used, "pool_capacity": cap, "p95_ms": round(p95, 1), "requests_1m": len(rows),
            "error5_ratio": round(err5 / len(rows), 4) if len(rows) >= 20 else 0.0,
            "shed": shed, "pool_timeouts": pto}


def capacity_level(m: dict | None = None) -> int:
    """입장 판단용 — 즉시 값(동시 요청·연결 사용률)만."""
    if m is None:
        with _lock:
            inflight = _inflight
        used, cap = _pool()
    else:
        inflight, used, cap = m["inflight"], m["pool_in_use"], m["pool_capacity"]
    r = max(inflight / max(1, settings.CAPACITY_MAX_INFLIGHT), used / max(1, cap))
    return _bucket(r, _cfg_ratio())


def level_now(m: dict | None = None) -> int:
    """보고용 — 즉시 값 + 최근 1분 p95 지연·5xx 비율 중 가장 나쁜 단계."""
    m = m or metrics()
    lv = capacity_level(m)
    if m["requests_1m"] >= 20:
        lv = max(lv, _bucket(m["p95_ms"], _cfg_ms()), _bucket(m["error5_ratio"], (0.01, 0.05, 0.2)))
    return lv


# ── 대량접속제어(입장) ─────────────────────────────────────
def admit(tier: str) -> None:
    """중요도에 따라 받을지 — 못 받으면 Overloaded. 끄면(CAPACITY_ADMISSION=off) 항상 받는다."""
    if not settings.CAPACITY_ADMISSION or tier not in SHED_AT:
        return
    lv = capacity_level()
    if lv >= SHED_AT[tier]:
        with _lock:
            _shed[tier] += 1
        raise Overloaded(lv, tier)


# ── 단계 변화 → 성능관리보고서 ─────────────────────────────
def _transition(lv: int) -> None:
    prev = _state["level"]
    if lv == prev:
        return
    _state["level"], _state["since"] = lv, time.time()
    log.log(logging.WARNING if lv >= 2 else logging.INFO, json.dumps(
        {"event": "capacity_level", "from": LEVELS[prev], "to": LEVELS[lv], "pid": os.getpid()}, ensure_ascii=False))
    if lv >= 2 and lv > prev:
        last = _state["reported"].get(lv, 0.0)
        if time.time() - last >= settings.CAPACITY_REPORT_COOLDOWN_S:
            _state["reported"][lv] = time.time()
            threading.Thread(target=report, args=(lv, prev), daemon=True, name="capacity-report").start()


def _advice(m: dict, lv: int) -> list[str]:
    out = []
    if m["pool_in_use"] / max(1, m["pool_capacity"]) >= _cfg_ratio()[1]:
        out.append("DB 연결 사용률이 높다 — 느린 쿼리(pg_top_queries)·풀 크기·DB max_connections 예산 확인")
    if m["inflight"] / max(1, m["max_inflight"]) >= _cfg_ratio()[1]:
        out.append("동시 요청이 워커 한도에 가깝다 — 워커 수(WORKERS)·서버 증설 검토(가이드라인: 경계·심각은 즉각 증설)")
    if m["p95_ms"] >= _cfg_ms()[1]:
        out.append("p95 지연이 경계 이상 — 아래 느린 경로부터 실행 계획 확인")
    if m["error5_ratio"] >= 0.05:
        out.append("5xx 비율 5% 이상 — 오류 로그(요청 ID)·DB 상태 확인")
    if sum(m["shed"].values()):
        out.append(f"대량접속제어로 돌려보낸 요청 {sum(m['shed'].values())}건(low·normal) — 핵심 거래 보호 중")
    return out or ["지표가 회복 중 — 추이 관찰"]


def report(lv: int, prev: int = 0) -> dict | None:
    """성능관리보고서 1건 — 표에 남기고 운영 알림. 실패해도 예외를 올리지 않는다."""
    from app import clock, db
    from app.services import alerts, observability
    try:
        m = metrics()
        slow = sorted(observability.snapshot(top=200)["routes"], key=lambda r: -(r["p95_ms"] or 0))[:5]
        body = {"level": LEVELS[lv], "from": LEVELS[prev], "pid": os.getpid(), "metrics": m,
                "slow_routes": [{"route": r["route"], "p95_ms": r["p95_ms"], "count": r["count"]} for r in slow],
                "advice": _advice(m, lv)}
        rid = db.write_query("INSERT INTO capacity_report(at, level, detail) VALUES(:t, :l, :d) RETURNING id",
                             {"t": clock.utc(), "l": lv, "d": json.dumps(body, ensure_ascii=False)})[0]["id"]
        alerts.notify(f"capacity:{lv}", f"[성능] 부하 단계 {LEVELS[lv]}(보고서 #{rid})",
                      "\n".join(f"· {a}" for a in body["advice"])
                      + f"\n동시 요청 {m['inflight']}/{m['max_inflight']} · DB 연결 {m['pool_in_use']}/{m['pool_capacity']}"
                      f" · p95 {m['p95_ms']}ms", level="error" if lv >= 3 else "warn",
                      dedup_hours=settings.CAPACITY_REPORT_COOLDOWN_S / 3600)
        return {"id": rid, **body}
    except Exception as e:                             # noqa: BLE001
        log.error("성능관리보고서 기록 실패: %s", type(e).__name__)
        return None


def status(reports: int = 20) -> dict:
    """관리 화면용 — 지금 단계·지표·기준·최근 보고서."""
    from app import db
    m = metrics()
    lv = level_now(m)
    rows = db.query("SELECT id, at, level, detail FROM capacity_report ORDER BY id DESC LIMIT :n",
                    {"n": max(1, min(int(reports), 100))})
    return {"level": lv, "level_name": LEVELS[lv], "admission_level": LEVELS[capacity_level(m)],
            "admission": bool(settings.CAPACITY_ADMISSION), "metrics": m, "pid": os.getpid(),
            "thresholds": {"ratios": _cfg_ratio(), "p95_ms": _cfg_ms(), "error5": (0.01, 0.05, 0.2),
                           "shed": {"경계": ["low"], "심각": ["low", "normal"]}},
            "reports": [{"id": r["id"], "at": r["at"].isoformat(), "level": LEVELS[r["level"]],
                         **json.loads(r["detail"])} for r in rows]}


def prometheus() -> str:
    m = metrics()
    lines = ["# TYPE oos_capacity_level gauge", f"oos_capacity_level {level_now(m)}",
             "# TYPE oos_inflight_requests gauge", f"oos_inflight_requests {m['inflight']}",
             "# TYPE oos_db_pool_in_use gauge", f"oos_db_pool_in_use {m['pool_in_use']}",
             f"oos_db_pool_capacity {m['pool_capacity']}", "# TYPE oos_shed_total counter"]
    lines += [f'oos_shed_total{{tier="{t}"}} {n}' for t, n in m["shed"].items()]
    lines.append(f"oos_db_pool_timeouts_total {m['pool_timeouts']}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    """시험용."""
    global _inflight, _peak
    with _lock:
        _inflight, _peak = 0, 0
        _recent.clear()
        for t in _shed:
            _shed[t] = 0
        _pool_timeouts[0] = 0
    _state.update(level=0, since=time.time(), checked=0.0, reported={})


def set_inflight(n: int) -> None:
    """시험용 — 부하 상태를 흉내."""
    global _inflight
    with _lock:
        _inflight = max(0, int(n))
