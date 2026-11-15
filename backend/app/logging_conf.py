"""구조적 로깅 — print 대신 로그를 쓴다(레벨·시각·모듈명 포함). 시각은 서버 시간대와 무관하게 한국시간.

비밀값 가리기(app/redact.py): DART(crtfc_key)·EXIM(authkey)·Finnhub(token) 키는 요청 URL 에 들어가는데,
requests 는 실패하면 URL 전체를 예외 메시지에 넣는다 → 그대로 로그·배치 기록(/health)에 남던 문제.
이 모듈을 import 하는 순간(= app 패키지를 import 하는 순간, app/__init__.py) 모든 로그 레코드에 적용된다."""
import logging
import sys
import time

from app.config import settings
from app.redact import install_log_redaction, redact

_configured = False
FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATEFMT = "%m-%d %H:%M:%S"


def _kst(secs):
    """로그 시각 = 한국시간 — 학교 VM 이 UTC 여도 스케줄(평일 15:45 등, KST)·화면 기준일과 같은 시각으로 찍힌다."""
    return time.gmtime((secs or 0) + 9 * 3600)


def safe_console():
    """콘솔 인코딩이 UTF-8 이 아니면(한국어 Windows 의 cp949 — Git Bash·작업 스케줄러·'> log.txt' 리디렉션)
    출력할 수 없는 문자(—·✓ 등)를 '?' 로 바꿔 쓴다. 예전엔 UnicodeEncodeError 로 'run.py real' 이 중간에 죽었다.
    인코딩 자체는 그대로 둔다(한글은 cp949 로 정상 출력)."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if enc and enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):                  # 이미 닫힌/특수 스트림이면 그대로
                pass


class JsonFormatter(logging.Formatter):
    """한 줄 JSON — ts(UTC)·level·logger·rid(요청 ID)·msg. 접근·보안 로거의 JSON 메시지는 필드로 펼친다."""

    def format(self, record):
        import json
        from datetime import datetime, timezone
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name}
        try:
            from app.security import context
            rid = context.request_id()
            if rid:
                out["rid"] = rid
        except Exception:                                  # noqa: BLE001
            pass
        msg = record.getMessage()
        if msg.startswith("{") and record.name in ("access", "security"):
            try:
                out.update(json.loads(msg))
            except ValueError:
                out["msg"] = msg
        else:
            out["msg"] = msg
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            out["exc"] = redact(record.exc_text)[-4000:]
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level=None):
    """CLI(run.py)·스케줄러·실시간 수집기·API 서버 공용. 루트 로거에 이미 핸들러가 있으면(pytest·사용자 설정)
    핸들러·레벨은 건드리지 않고 키 가리기만 건다.
    API 서버: uvicorn 은 자기 로거(uvicorn.*)만 설정하므로, 예전엔 앱 로그(app.*)에 핸들러가 없어
    INFO(마이그레이션 적용·캐시 예열 완료)는 버려지고 WARNING 도 시각·레벨·모듈 없이 찍혔다(journald 에서 추적 불가)."""
    global _configured
    if _configured:
        return
    safe_console()
    install_log_redaction()                                  # 로그에 API 키(쿼리 파라미터 등)가 찍히지 않게(이미 켜져 있으면 그대로)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        if settings.LOG_FORMAT == "json":
            handler.setFormatter(JsonFormatter())
        else:
            fmt = logging.Formatter(FORMAT, datefmt=DATEFMT)
            fmt.converter = _kst
            handler.setFormatter(fmt)
        root.addHandler(handler)
        root.setLevel(level or settings.LOG_LEVEL)
    _configured = True


def safe_error(e, limit=200):
    """DB·화면에 남길 오류 요약 — 예외 종류 + 가린 메시지(길이 제한)."""
    return redact(f"{type(e).__name__}: {e}")[:limit]


install_redaction = install_log_redaction        # (다른 작업 라인에서 쓰던 이름)
install_log_redaction()                          # import 만 해도 적용(웹 서버·스케줄러·CLI·테스트 공통)
