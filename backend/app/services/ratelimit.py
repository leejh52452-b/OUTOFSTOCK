"""시도 횟수 제한(메모리, 슬라이딩 윈도) — 로그인 실패·회원가입 시도처럼 '짧은 시간에 몰리는' 요청을 막는다.

  take([(key, limit), …], window) → 하나라도 limit 에 찼으면 풀릴 때까지 남은 초(기록 안 함),
                                    아니면 모든 키에 1번씩 '먼저' 기록하고 0 — 확인과 기록이 한 잠금 안에서
                                    일어나므로, 동시에 40개가 몰려도 정확히 limit 개만 통과한다.
  undo(key)                       → 방금 기록 1개 취소(로그인 성공 시 IP 실패 수에서 빼기)
  retry_after(key, limit, window) / hit(key, window) / reset(key) / clear()

키에는 아이디 원문 대신 해시를 쓴다(아주 긴 아이디로 메모리를 채우는 공격 방지). 오래된 키는 주기적으로 정리.
외부 저장소 없이 프로세스 메모리에 둔다(uvicorn 워커 1개 기준 — 워커가 여럿이면 워커마다 따로 센다).
서버를 재시작하면 기록은 사라진다(잠금이 영구적으로 남지 않는 쪽이 이 규모에선 안전하다)."""
import hashlib
import threading
import time
from collections import deque

from app.config import settings

_lock = threading.Lock()
_hits = {}                      # key → deque[시각(monotonic)]
_clock = time.monotonic         # 테스트에서 바꿔 끼운다
_MAX_KEYS = 10_000              # 메모리 상한 — 넘으면 오래된 키부터 정리
_SWEEP_AGE = 3600               # 정리 기준(가장 긴 윈도 = 가입 1시간)
_SWEEP_EVERY = 60               # 초 — 이 간격마다 만료된 키를 훑어 지운다(키 개수와 무관)
_last_sweep = [float("-inf")]   # '정리한 적 없음'(0.0 이면 부팅 직후 첫 정리가 늦어진다)


def key_of(*parts):
    """제한 키 — 각 부분을 소문자로 합쳐 해시(고정 길이)."""
    raw = "\x1f".join(str(p or "").lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _prune(q, now, window):
    while q and q[0] <= now - window:
        q.popleft()


def _sweep(now):
    if len(_hits) > _MAX_KEYS or now - _last_sweep[0] >= _SWEEP_EVERY:
        for k in [k for k, v in _hits.items() if not v or v[-1] <= now - _SWEEP_AGE]:
            _hits.pop(k, None)
        _last_sweep[0] = now


def _wait(q, limit, window, now):
    """limit 개 미만으로 떨어질 때까지 남은 초 — 그 시점에 만료되는 기록 = 뒤에서 limit 번째."""
    return max(1, int(q[len(q) - limit] + window - now) + 1)


def _shared():
    return settings.RATE_LIMIT_BACKEND == "postgres"


def take(rules, window):
    """rules=[(key, limit), …] — 모두 여유가 있으면 전부에 1번 기록하고 0, 아니면 남은 대기 초(기록 없음).
    RATE_LIMIT_BACKEND=postgres 면 공유 저장소(ratelimit_pg) — 워커·서버가 여럿이어도 한도가 하나."""
    if _shared():
        from app.services import ratelimit_pg
        return ratelimit_pg.take(rules, window)
    now = _clock()
    with _lock:
        _sweep(now)
        wait = 0
        for key, limit in rules:
            q = _hits.get(key)
            if q:
                _prune(q, now, window)
                if len(q) >= limit:
                    wait = max(wait, _wait(q, limit, window, now))
        if wait:
            return wait
        for key, _limit in rules:
            _hits.setdefault(key, deque()).append(now)
        return 0


def undo(key):
    """가장 최근 기록 1개 취소(없으면 무시)."""
    if _shared():
        from app.services import ratelimit_pg
        return ratelimit_pg.undo(key)
    with _lock:
        q = _hits.get(key)
        if q:
            q.pop()
            if not q:
                _hits.pop(key, None)


def retry_after(key, limit, window):
    now = _clock()
    with _lock:
        q = _hits.get(key)
        if not q:
            return 0
        _prune(q, now, window)
        if len(q) < limit:
            if not q:
                _hits.pop(key, None)
            return 0
        return _wait(q, limit, window, now)


def hit(key, window):
    now = _clock()
    with _lock:
        _sweep(now)
        q = _hits.setdefault(key, deque())
        _prune(q, now, window)
        q.append(now)


def acquire(key, limit, window):
    """확인과 기록을 한 번에(원자적) — 이미 limit 번 찼으면 풀릴 때까지 남은 초, 아니면 1회 기록하고 0.
    retry_after → (오래 걸리는 처리) → hit 로 나누면, 동시에 들어온 요청이 모두 '아직 0회'를 보고 통과해
    로그인 비밀번호를 한도(5회)의 수십 배까지 동시에 시도할 수 있었다."""
    now = _clock()
    with _lock:
        q = _hits.setdefault(key, deque())
        _prune(q, now, window)
        if len(q) >= limit:
            return max(1, int(q[0] + window - now) + 1)
        q.append(now)
        if len(_hits) > _MAX_KEYS:
            for k in [k for k, v in _hits.items() if not v or v[-1] <= now - _SWEEP_AGE]:
                _hits.pop(k, None)
        return 0


def release(key):
    """acquire 로 기록한 1회를 되돌린다(성공한 로그인처럼 '실패'로 세지 않을 시도)."""
    with _lock:
        q = _hits.get(key)
        if q:
            q.pop()
            if not q:
                _hits.pop(key, None)


def reset(key):
    if _shared():
        from app.services import ratelimit_pg
        return ratelimit_pg.reset(key)
    with _lock:
        _hits.pop(key, None)


def clear():
    if _shared():
        from app.services import ratelimit_pg
        ratelimit_pg.clear()
    with _lock:
        _hits.clear()
        _last_sweep[0] = float("-inf")
