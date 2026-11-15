"""행 수준 보안(RLS) 문맥 — 애플리케이션 인가(PEP/PDP) 뒤에 DB 가 한 번 더 막는 다층 방어.

정책(sql/migrations/V010__row_level_security.sql)
  개인 데이터 표(관심종목·지갑·원장·출석·투자·결제·성향·학습·접속 기록·실습·친구·채팅)는 FORCE ROW LEVEL SECURITY.
  행이 보이는 조건: app.rls = 'off'(시스템 문맥) 이거나, 행의 주인 열 = app.user(지금 요청의 주체).
  아무것도 설정되지 않은 연결(사람이 앱 계정으로 직접 접속 등)은 개인 데이터가 하나도 보이지 않는다(기본 거부).

문맥 결정(커넥션을 풀에서 꺼낼 때마다 — db.py 의 Pool checkout 훅)
  · 웹 요청 안: app.rls='on', app.user=주체(로그인 아이디·게스트 ID, 익명이면 빈 값)
  · 요청 밖(스케줄러·배치·CLI·백그라운드 스레드): app.rls='off' — 시스템 작업
  · 요청 안이지만 여러 사람의 행을 정당하게 다루는 코드(송금 입금·탈퇴·친구 공개 프로필·관리자 열람):
    with elevated("사유"): — 상승 구간이 코드에 이름으로 드러나고, 횟수가 지표로 남는다(검토 대상이 한눈에).
위협 모델: 인가 로직 실수(IDOR·조건 누락)로 남의 행을 읽고 쓰는 것을 DB 가 막는다. SQL 주입은 별도(전부 바인드 변수)로 막는다 —
주입이 성공하면 같은 세션 변수를 바꿀 수 있으므로 RLS 가 그 대책은 아니다(문서에 한계로 명시).
"""
from __future__ import annotations

import contextvars
import logging
import threading
from collections import Counter
from contextlib import contextmanager

from app.security import context

log = logging.getLogger("security.rls")
_elevated: contextvars.ContextVar[str | None] = contextvars.ContextVar("oos_rls_elevated", default=None)
_acting: contextvars.ContextVar[str | None] = contextvars.ContextVar("oos_rls_acting", default=None)
_lock = threading.Lock()
_counts: Counter = Counter()


def current() -> tuple[str, str]:
    """(app.rls, app.user) — 커넥션을 꺼낼 때 그 값으로 세션 변수를 맞춘다."""
    acting = _acting.get()
    if acting is not None:                          # 테스트·점검 도구: 특정 주체로 RLS 를 켠 채 실행
        return "on", acting
    ctx = context._ctx.get()
    if ctx is None or _elevated.get():
        return "off", ""
    return "on", ctx.principal.name or ""


@contextmanager
def elevated(reason: str):
    """요청 안에서 여러 주체의 행을 다루는 구간 — 사유를 남기고 RLS 를 잠시 연다(이 스레드·이 문맥만)."""
    token = _elevated.set(reason)
    with _lock:
        _counts[reason] += 1
    log.debug("rls elevated: %s (rid=%s)", reason, context.request_id())
    try:
        yield
    finally:
        _elevated.reset(token)


@contextmanager
def acting_as(name: str):
    """RLS 를 켠 채 특정 주체로 실행(검증·점검용)."""
    token = _acting.set(name)
    try:
        yield
    finally:
        _acting.reset(token)


def stats() -> dict:
    with _lock:
        return dict(_counts)
