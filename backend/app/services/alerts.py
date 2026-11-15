"""운영 알림 — 배치 실패·적재 품질 게이트 위반·수집기 중단을 '사람이 알게' 한다(장애 대응).

배경(2026-10-10 장애): KRX 시세 적재가 설치 직후부터 매번 롤백됐는데 로그에만 남아 아무도 몰랐다.
사용자가 홈 화면의 빈 카드를 보고서야 알았다 → 실패가 생기면 바로 알리는 경로를 만든다.

채널(설정한 것만, 여러 개 동시 가능 — .env):
  · ALERT_WEBHOOK_URL                       Slack·Discord·일반 웹훅(JSON POST)
  · TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID    텔레그램 봇
  · SMTP_HOST·SMTP_PORT·SMTP_USER·SMTP_PASSWORD + ALERT_EMAIL_TO   메일(STARTTLS)
하나도 없으면 로그 + alert_log 표 + /health 에만 남긴다(알림이 실패해도 배치는 멈추지 않는다).

원칙
  · 같은 key 는 ALERT_DEDUP_HOURS(기본 6시간) 안에 다시 보내지 않는다 — 알림 폭주(피로) 방지
  · 보내기 전에 비밀값을 가린다(redact) — 오류 메시지 속 URL 에 API 키가 섞여 있을 수 있다
  · 채널 하나가 실패해도 나머지는 보낸다, 알림 실패가 호출한 작업을 죽이지 않는다
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy import text

from app import clock, db
from app.config import settings
from app.redact import redact

log = logging.getLogger(__name__)

LEVELS = ("info", "warn", "error")
TIMEOUT = 10                                   # 채널마다(초) — 알림 때문에 배치가 오래 멈추지 않게
_LABEL = {"info": "[알림]", "warn": "[주의]", "error": "[오류]"}


def channels() -> list[str]:
    """설정된 채널 이름(값은 노출하지 않음)."""
    out = []
    if settings.ALERT_WEBHOOK_URL:
        out.append("webhook")
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        out.append("telegram")
    if settings.SMTP_HOST and settings.ALERT_EMAIL_TO:
        out.append("email")
    return out


def _recently_sent(key: str, hours: float) -> bool:
    if hours <= 0:
        return False
    since = (clock.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        return bool(db.query("SELECT 1 FROM alert_log WHERE key=:k AND delivered = 1 "
                             "AND created_at >= :s LIMIT 1", {"k": key, "s": since}))
    except Exception:                          # noqa: BLE001 — 표가 없는 구버전 DB: 중복 판정 없이 보낸다
        return False


def _webhook_payload(url: str, title: str, body: str) -> dict[str, Any]:
    host = (urlparse(url).hostname or "").lower()
    text_ = f"{title}\n{body}"
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return {"content": text_[:1900]}                    # 디스코드 메시지 한도 2000자
    if host.endswith("slack.com"):
        return {"text": text_[:3000]}
    return {"title": title, "text": body}                    # 일반 웹훅(직접 만든 수신 서버 등)


def _send_webhook(title: str, body: str) -> None:
    url = settings.ALERT_WEBHOOK_URL
    r = requests.post(url, json=_webhook_payload(url, title, body), timeout=TIMEOUT)
    r.raise_for_status()


def _send_telegram(title: str, body: str) -> None:
    r = requests.post(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": f"{title}\n{body}"[:4000],
                            "disable_web_page_preview": True}, timeout=TIMEOUT)
    r.raise_for_status()


def _send_email(title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"[OUTOFSTOCK] {title}"
    msg["From"] = settings.SMTP_USER or "outofstock@localhost"
    msg["To"] = settings.ALERT_EMAIL_TO
    msg.set_content(body)
    from app.security import egress
    egress.check_host(settings.SMTP_HOST, "smtp")
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=TIMEOUT) as s:
        s.starttls()
        if settings.SMTP_USER:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        s.send_message(msg)


_SENDERS = {"webhook": _send_webhook, "telegram": _send_telegram, "email": _send_email}


def notify(key: str, title: str, message: str = "", level: str = "error",
           dedup_hours: float | None = None) -> dict[str, Any]:
    """알림 1건 — 설정된 모든 채널로 보내고 alert_log 에 남긴다. 예외를 올리지 않는다.
    반환 {sent: [채널], failed: [채널], skipped: 'duplicate'|None}."""
    level = level if level in LEVELS else "error"
    title = redact(title)[:200]
    body = redact(message or "")[:3500]
    hours = settings.ALERT_DEDUP_HOURS if dedup_hours is None else dedup_hours
    now = clock.iso_now()
    log_fn = log.error if level == "error" else log.warning if level == "warn" else log.info
    log_fn("[알림] %s — %s", title, body.replace("\n", " / ")[:300])
    if _recently_sent(key, hours):
        _record(key, level, title, body, "", None, now)
        return {"sent": [], "failed": [], "skipped": "duplicate"}
    sent, failed = [], []
    for name in channels():
        try:
            _SENDERS[name](f"{_LABEL[level]} {title}", f"{body}\n\n— {now} (KST)")
            sent.append(name)
        except Exception as e:                  # noqa: BLE001 — 한 채널 실패가 다른 채널·호출한 작업을 막지 않게
            failed.append(name)
            log.warning("알림 채널 %s 전송 실패: %s", name, redact(f"{type(e).__name__}: {e}")[:200])
    used = ",".join(sent + failed) or "log"
    # 전달 성공(또는 채널이 없어 로그로 처리) = 1 → 중복 판정 대상. 전부 실패 = 0 → 다음 호출 때 다시 시도
    _record(key, level, title, body, used, 1 if sent or not failed else 0, now)
    return {"sent": sent, "failed": failed, "skipped": None}


def _record(key, level, title, body, used, delivered, now) -> None:
    try:
        db.execute("INSERT INTO alert_log(key, level, title, message, channels, delivered, created_at) "
                   "VALUES(:k, :lv, :t, :m, :c, :d, :at)",
                   {"k": key[:100], "lv": level, "t": title, "m": body, "c": used or None, "d": delivered, "at": now})
    except Exception as e:                      # noqa: BLE001 — 기록 실패가 알림·배치를 막지 않게
        log.warning("alert_log 기록 실패: %s", type(e).__name__)


def recent(limit: int = 10, with_message: bool = False) -> list[dict[str, Any]]:
    """최근 알림 — /health 에는 제목·수준·시각만(본문에는 내부 오류 요약이 있을 수 있어 공개하지 않음)."""
    cols = "alert_id, key, level, title, channels, delivered, created_at" + (", message" if with_message else "")
    try:
        return db.query(f"SELECT {cols} FROM alert_log ORDER BY alert_id DESC LIMIT :n",
                        {"n": max(1, min(int(limit), 100))})
    except Exception:                           # noqa: BLE001
        return []


# ── 관제: 확인(ack)·상향(escalation)·확인까지 걸린 시간(MTTA) ─────────────────────────
# 금융위(2026-10) 점검에서 지적된 '24시간 관제 공백'을 작은 서비스 크기로 옮긴 것 — 알림을 보내는 데서 끝나지 않고
# 누가 언제 확인했는지 남기고, 오류 경보를 ALERT_ACK_MINUTES(기본 15분) 안에 아무도 확인하지 않으면 한 번 더(상향) 보낸다.
ESCALATE_PREFIX = "escalate:"


def ack(alert_id: int, actor: str) -> dict[str, Any]:
    """경보 확인 — 같은 key(같은 사건)의 아직 확인 안 된 경보와 그 상향 알림을 함께 확인한다."""
    rows = db.query("SELECT alert_id, key, level FROM alert_log WHERE alert_id=:i", {"i": int(alert_id)})
    if not rows:
        raise LookupError("없는 경보")
    key = rows[0]["key"]
    base = key[len(ESCALATE_PREFIX):] if key.startswith(ESCALATE_PREFIX) else key
    n = db.execute("UPDATE alert_log SET ack_by=:by, ack_at=:t WHERE ack_at IS NULL AND key IN (:k, :e)",
                   {"by": actor[:50], "t": clock.iso_now(), "k": base, "e": ESCALATE_PREFIX + base})
    from app.services import audit
    audit.record("alert.ack", actor, target=f"alert:{alert_id}", detail={"key": base[:80], "count": n})
    return {"alert_id": int(alert_id), "key": base, "acked": n}


def escalate_unacked(minutes: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """확인되지 않은 오류 경보 상향(스케줄러 5분) — key 마다 한 번만. 하루 넘은 것은 대상에서 뺀다(첫 배포 때 옛 기록 폭주 방지)."""
    minutes = settings.ALERT_ACK_MINUTES if minutes is None else minutes
    now = now or clock.now()
    cut = (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = db.query("SELECT key, MIN(created_at) AS first, COUNT(*) AS n, MAX(title) AS title FROM alert_log "
                    "WHERE level='error' AND ack_at IS NULL AND escalated_at IS NULL AND key NOT LIKE :p "
                    "AND created_at >= :s AND created_at <= :c GROUP BY key ORDER BY 2 LIMIT 20",
                    {"p": ESCALATE_PREFIX + "%", "s": since, "c": cut})
    done = []
    for r in rows:
        waited = int((now - datetime.fromisoformat(r["first"])).total_seconds() // 60)
        notify(ESCALATE_PREFIX + r["key"], f"[상향] {waited}분째 아무도 확인하지 않은 경보: {r['title']}"[:200],
               f"경보 {r['n']}건(key {r['key']}) — 확인: POST /admin/alerts/{{id}}/ack · 목록: GET /admin/alerts",
               level="error", dedup_hours=0)
        db.execute("UPDATE alert_log SET escalated_at=:t WHERE key=:k AND ack_at IS NULL AND escalated_at IS NULL",
                   {"t": now.isoformat(timespec="seconds"), "k": r["key"]})
        done.append(r["key"])
    return {"escalated": done} if done else {}


def ack_stats(days: int = 30) -> dict[str, Any]:
    """오류 경보의 확인까지 걸린 시간(MTTA)·미확인 수·상향 수 — 관제 품질 지표."""
    since = (clock.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.query("SELECT created_at, ack_at, escalated_at FROM alert_log WHERE level='error' AND delivered IS NOT NULL "
                    "AND key NOT LIKE :p AND created_at >= :s", {"p": ESCALATE_PREFIX + "%", "s": since})
    due = (clock.now() - timedelta(minutes=settings.ALERT_ACK_MINUTES)).isoformat(timespec="seconds")
    waits = [(datetime.fromisoformat(r["ack_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds() / 60
             for r in rows if r["ack_at"]]
    waits.sort()
    return {"days": days, "errors": len(rows), "acked": len(waits), "unacked": sum(1 for r in rows if not r["ack_at"]),
            "unacked_overdue": sum(1 for r in rows if not r["ack_at"] and r["created_at"] <= due),
            "escalated": sum(1 for r in rows if r["escalated_at"]), "ack_target_min": settings.ALERT_ACK_MINUTES,
            "mtta_min": round(sum(waits) / len(waits), 1) if waits else None,
            "p90_min": round(waits[min(len(waits) - 1, int(len(waits) * 0.9))], 1) if waits else None}


def board(limit: int = 50) -> dict[str, Any]:
    """관리 화면 — 최근 경보(확인·상향 표시)와 관제 지표."""
    rows = db.query("SELECT alert_id, key, level, title, channels, delivered, created_at, ack_by, ack_at, escalated_at "
                    "FROM alert_log ORDER BY alert_id DESC LIMIT :n", {"n": max(1, min(int(limit), 200))})
    return {"alerts": rows, "unacked_errors": [r["alert_id"] for r in rows if r["level"] == "error" and not r["ack_at"]],
            "stats": ack_stats(), "channels": channels()}


def purge(older_than_days: int = 180) -> int:
    """오래된 알림 기록 정리(보관 기간)."""
    cut = (datetime.fromisoformat(clock.iso_now()) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        return conn.execute(text("DELETE FROM alert_log WHERE created_at < :c"), {"c": cut}).rowcount or 0


def test_alert() -> dict[str, Any]:
    """설정 확인용 — 'python run.py alert-test'. 중복 판정 없이 바로 보낸다."""
    return notify("alert-test", "알림 시험", "OUTOFSTOCK 알림 채널이 정상 동작합니다.", level="info", dedup_hours=0)
