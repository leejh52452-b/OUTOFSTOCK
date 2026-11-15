"""네트워크 호출용 재시도 데코레이터 (지수 백오프). 외부 API는 종종 실패하므로 필수."""
import time
import functools
import logging

log = logging.getLogger(__name__)


def call_with_timeout(fn, *args, timeout=60, **kwargs):
    """fn 을 별도 스레드에서 실행하고 timeout 초 안에 끝나지 않으면 TimeoutError.
    FinanceDataReader 처럼 내부에서 requests.get 을 '시간 제한 없이' 부르는 라이브러리용 —
    연결이 응답 없이 멈추면 23:00 배치 전체가(그리고 그 DB 트랜잭션·잠금이) 무기한 멈췄다.
    (멈춘 스레드는 데몬이라 프로세스 종료를 막지 않는다)"""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:                     # noqa: BLE001 — 호출한 쪽에서 다시 올린다
            box["error"] = e
    t = threading.Thread(target=run, name=f"timeout-{getattr(fn, '__name__', 'call')}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', 'call')} {timeout}초 안에 응답 없음")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def retry(times=3, delay=1.0, backoff=2.0, exceptions=(Exception,)):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    if attempt == times:
                        raise
                    log.warning("재시도 %d/%d [%s]: %s", attempt, times, fn.__name__, e)
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator
