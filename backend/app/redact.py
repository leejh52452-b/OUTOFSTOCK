"""비밀값 가리기(redaction) — 로그·DB 메모·API 응답에 API 키가 새지 않게.

왜 필요한가
  DART(crtfc_key)·Finnhub(token)·수출입은행(authkey)은 키를 'URL 쿼리'로 보낸다. 요청이 실패하면
  requests 예외 메시지에 URL 전체가 들어가고(…?crtfc_key=실제키&…), 그 문자열이
    · 로그(재시도 경고·수집 실패) → 서버 로그·터미널 캡처·팀 채팅 공유
    · pipeline_run.note → 공개 API /health 의 last_run.note
  로 그대로 남았다. 출구마다 막는 대신 '문자열이 밖으로 나가는 지점'에서 한 번에 가린다.
가리는 대상
  1) 알려진 쿼리 파라미터 이름(crtfc_key·token·authkey·appkey·appsecret …)의 값
  2) 설정(.env)에 들어 있는 실제 키 값 자체(헤더 등 어디에 섞여 있어도)
  3) 주민등록번호·카드번호(2026-11 — 오류 메시지·알림에 사용자 입력이 섞여 나가도 가린다, app/security/dlp.py)
"""
import logging
import re
import traceback

_PARAM = re.compile(r"(?i)\b(crtfc_key|token|authkey|auth_key|apikey|api_key|appkey|appsecret|secretkey|"
                    r"servicekey|access_token|client_secret|approval_key)=([^&\s'\"<>]+)")
_SECRET_NAMES = ("DART_API_KEY", "FINNHUB_API_KEY", "EXIM_API_KEY", "KRX_API_KEY", "KIS_APP_KEY",
                 "KIS_APP_SECRET", "TOSS_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "ALERT_WEBHOOK_URL",
                 "DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_KEYS_OLD", "ECOS_API_KEY", "APP_SECRET_KEY",
                 "APP_SECRET_KEYS_OLD", "TOSS_CLIENT_KEY", "BACKUP_DATABASE_URL")
MASK = "***"
# URL 에 박힌 자격 증명 — DB 주소(postgresql://아이디:비밀번호@호스트)·텔레그램 봇 토큰(/bot123:ABC…/)
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s/]+)(@)")
_TG_TOKEN = re.compile(r"\bbot(\d{5,}):([A-Za-z0-9_\-]{20,})")
# 한국은행 ECOS 는 키를 경로에 넣는다(/api/StatisticSearch/{키}/json/…) — 쿼리 파라미터 규칙으로는 안 잡힌다
_ECOS_PATH = re.compile(r"(?i)(ecos\.bok\.or\.kr/api/[A-Za-z]+/)([^/\s?]+)")


def _secret_values():
    from app.config import settings
    vals = {str(getattr(settings, n, "") or "") for n in _SECRET_NAMES}
    return sorted((v for v in vals if len(v) >= 6), key=len, reverse=True)


def redact(text):
    """문자열 속 비밀값을 *** 로. None·비문자열도 안전(str 변환)."""
    if text is None:
        return None
    s = str(text)
    s = _PARAM.sub(lambda m: f"{m.group(1)}={MASK}", s)
    s = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", s)
    s = _TG_TOKEN.sub(lambda m: f"bot{m.group(1)}:{MASK}", s)
    s = _ECOS_PATH.sub(lambda m: f"{m.group(1)}{MASK}", s)
    for v in _secret_values():
        if v in s:
            s = s.replace(v, MASK)
    if any(ch.isdigit() for ch in s):                    # 주민등록번호·카드번호(형식+날짜/룬 검사) — app/security/dlp.py
        from app.security import dlp
        s = dlp.scan(s)[0]
    return s


def redact_obj(obj):
    """dict·list 안의 문자열까지 재귀적으로 가린다(API 응답용)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


_installed = False


def install_log_redaction():
    """모든 로그 레코드를 만들 때 메시지·예외 트레이스백을 가린다(핸들러 구성과 무관하게 전역 적용)."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        rec = base(*args, **kwargs)
        try:
            msg = rec.getMessage()
            red = redact(msg)
            if red != msg:
                rec.msg, rec.args = red, ()
            if rec.exc_info and not rec.exc_text:
                rec.exc_text = redact("".join(traceback.format_exception(*rec.exc_info)).rstrip("\n"))
        except Exception:                                  # noqa: BLE001 — 로깅이 앱을 죽이면 안 된다
            pass
        return rec
    logging.setLogRecordFactory(factory)
    _installed = True
