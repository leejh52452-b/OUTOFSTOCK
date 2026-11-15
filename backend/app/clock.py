"""시각의 단일 기준 — 한국시간(KST).

서버(VM) 시간대가 UTC 여도 저장되는 시각·'오늘' 판정·데이터 신선도가 한국시간이 되게 한다.
예전: 모듈마다 datetime.now()(서버 로컬 시각)를 써서, 시간대가 UTC 인 VM 에서는 지갑·실습·채팅 기록 시각이
9시간 느리게 저장·표시되고 새벽(00~09시 KST)엔 날짜까지 하루 전으로 찍혔다.

저장 형식은 예전과 같은 'YYYY-MM-DDTHH:MM:SS'(시간대 표기 없음 = 한국시간)로 유지한다 — 이미 쌓인 행과
문자열 정렬·비교·화면 표시(앞 16자)가 그대로 맞물리게. (시세 쪽 quotes.iso_now() 는 +09:00 을 붙인다.)
"""
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now() -> datetime:
    """현재 한국시간(naive) — 서버 시간대와 무관."""
    return datetime.now(KST).replace(tzinfo=None)


def utc() -> datetime:
    """현재 UTC(시간대 포함) — 보안 표(세션·이벤트)는 timestamptz 로 저장한다(시간대 혼동 없는 비교·감사)."""
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """저장용 타임스탬프 'YYYY-MM-DDTHH:MM:SS'(한국시간)."""
    return now().isoformat(timespec="seconds")


def parse_utc(v) -> datetime:
    """timestamptz 를 isoformat 한 문자열(또는 datetime) → 시간대 포함 datetime. 시간대 표기가 없으면 UTC 로 본다
    (보안·운영 표는 timestamptz 로 저장 — 화면용 KST 문자열(iso_now)에는 쓰지 않는다)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def today() -> date:
    """오늘(한국 날짜)."""
    return now().date()


def today_str() -> str:
    """오늘 'YYYY-MM-DD'(한국 날짜)."""
    return today().isoformat()


# 다른 작업 라인(outofstock-backend-full)에서 쓰던 이름 — 같은 뜻이라 별칭으로 둔다(어느 쪽 코드든 그대로 동작)
now_naive = now          # 현재 한국시간(naive)
stamp = iso_now          # 저장용 'YYYY-MM-DDTHH:MM:SS'(한국시간)
