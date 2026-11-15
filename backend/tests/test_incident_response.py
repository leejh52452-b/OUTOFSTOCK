"""장애 대응 체계(2026-10-10 KRX 적재 롤백 사고의 재발 방지) — 알림·적재 검증·DART 견고성·/health.

INC-2026-10-10-01: 적재가 매번 롤백돼도 로그에만 남아 사용자가 빈 화면을 보고서야 알았다.
여기서는 '실패가 생기면 시스템이 먼저 안다'를 숫자로 확인한다."""
from datetime import date, timedelta

import pytest
import requests

from app import db
from app.config import settings
from app.services import alerts, quality


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def no_channels(monkeypatch):
    for k in ("ALERT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.setattr(settings, k, "")
    monkeypatch.setattr(settings, "ALERT_DEDUP_HOURS", 6.0)


# ── 알림 ──────────────────────────────────────────────────
def test_alert_without_channels_is_logged_and_recorded(no_channels):
    out = alerts.notify("t:1", "시험", "본문", level="warn")
    assert out == {"sent": [], "failed": [], "skipped": None}
    row = db.query("SELECT key, level, title, channels, delivered FROM alert_log")[0]
    assert row == {"key": "t:1", "level": "warn", "title": "시험", "channels": "log", "delivered": 1}


def test_alert_webhook_payload_is_redacted_and_deduplicated(no_channels, monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    sent = []
    monkeypatch.setattr(alerts.requests, "post", lambda url, json=None, timeout=None: sent.append(json) or _Resp())
    secret = "https://opendart.fss.or.kr/api/list.json?crtfc_key=0123456789abcdef0123456789abcdef01234567"
    out = alerts.notify("job:x", "배치 실패", f"오류: {secret}")
    assert out["sent"] == ["webhook"] and len(sent) == 1
    assert "0123456789abcdef0123456789abcdef01234567" not in sent[0]["content"]      # 키는 가려서 보낸다
    assert sent[0]["content"].startswith("[오류] 배치 실패")                          # 디스코드 형식
    again = alerts.notify("job:x", "배치 실패", "또 실패")
    assert again["skipped"] == "duplicate" and len(sent) == 1                          # 6시간 안 같은 key → 안 보냄
    assert alerts.notify("job:y", "다른 작업", "")["sent"] == ["webhook"]              # key 가 다르면 보냄


def test_alert_channel_failure_does_not_raise_and_is_retried_next_time(no_channels, monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    calls = []

    def down(url, json=None, timeout=None):
        calls.append(json)
        raise requests.ConnectionError("slack down")
    monkeypatch.setattr(alerts.requests, "post", down)
    out = alerts.notify("q:1", "적재 검증 실패", "x")
    assert out["failed"] == ["webhook"] and out["sent"] == []
    assert db.query("SELECT delivered FROM alert_log")[0]["delivered"] == 0
    alerts.notify("q:1", "적재 검증 실패", "x")                                          # 전달 실패는 중복으로 안 친다
    assert len(calls) == 2 and "text" in calls[0]                                      # 슬랙 형식


def test_alert_telegram_and_email_channels(no_channels, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "ALERT_EMAIL_TO", "ops@example.com")
    posted, mails = [], []
    monkeypatch.setattr(alerts.requests, "post", lambda url, json=None, timeout=None: posted.append((url, json)) or _Resp())

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            mails.append(msg)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    out = alerts.notify("t:2", "제목", "내용", level="info", dedup_hours=0)
    assert sorted(out["sent"]) == ["email", "telegram"]
    assert posted[0][1]["chat_id"] == "42" and mails[0]["Subject"] == "[OUTOFSTOCK] [알림] 제목"


def test_redact_masks_db_password_and_telegram_token():
    from app.redact import redact
    s = redact("postgresql+psycopg://oos:SuperSecret1@db:5432/x  https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWX/send")
    assert "SuperSecret1" not in s and "ABCDEFGHIJKLMNOPQRSTUVWX" not in s


def test_alert_purge_keeps_recent(no_channels):
    alerts.notify("a", "t", "")
    db.execute("UPDATE alert_log SET created_at='2020-01-01T00:00:00+09:00'")
    alerts.notify("b", "t", "")
    assert alerts.purge(older_than_days=180) == 1
    assert [r["key"] for r in alerts.recent()] == ["b"]


# ── 적재 검증(품질 게이트) ──────────────────────────────────
def _bars(ticker, end, n):
    d, k = date.fromisoformat(end), 0
    rows = []
    while k < n:
        if d.weekday() < 5:
            rows.append({"t": ticker, "d": d.isoformat(), "c": 1000.0 + k})
            k += 1
        d -= timedelta(days=1)
    db.execute("INSERT INTO ohlc(ticker, trade_date, open, high, low, close, volume) "
               "VALUES(:t, :d, :c, :c, :c, :c, 1000) ON CONFLICT DO NOTHING", rows)
    db.execute("INSERT INTO daily_price(ticker, trade_date, close, volume, change_rate) "
               "VALUES(:t, :d, :c, 1000, 0.1) ON CONFLICT DO NOTHING", rows[:1])


def _real_mode(monkeypatch, top_n=20):
    monkeypatch.setattr(settings, "KRX_API_KEY", "k" * 40)
    monkeypatch.setattr(settings, "MARKET_SOURCE", "auto")
    monkeypatch.setattr(settings, "UNIVERSE_TOP_N", top_n)


def test_gate_fails_on_the_incident_state(monkeypatch):
    # 사고 당시 VM: 시세 출처 없음 · 유니버스 17 · 종목마다 일봉 8개 · 최신일 10-08(10-10 새벽 기준)
    _real_mode(monkeypatch, top_n=150)
    for t in [r["ticker"] for r in db.query("SELECT ticker FROM stock")]:
        _bars(t, "2026-10-08", 8)
    g = quality.gate_market_load(None, today=date(2026, 10, 10))
    failed = {c["name"] for c in g["checks"] if not c["ok"]}
    assert g["level"] == "fail" and {"source", "universe", "history"} <= failed
    assert quality.gates()["kr_market"]["level"] == "fail"                         # /health 가 읽는 기록


def test_gate_passes_on_healthy_load(monkeypatch):
    n = db.query("SELECT COUNT(*) AS n FROM stock")[0]["n"]
    _real_mode(monkeypatch, top_n=n)
    for t in [r["ticker"] for r in db.query("SELECT ticker FROM stock")]:
        _bars(t, "2026-10-08", 70)
    g = quality.gate_market_load({"source": "krx"}, today=date(2026, 10, 9))      # 10-09(금) 새벽 → 기대 10-08(목)
    assert g["level"] == "ok", g["summary"]


def test_gate_warns_on_one_day_lag_and_kept_source(monkeypatch):
    n = db.query("SELECT COUNT(*) AS n FROM stock")[0]["n"]
    _real_mode(monkeypatch, top_n=n)
    for t in [r["ticker"] for r in db.query("SELECT ticker FROM stock")]:
        _bars(t, "2026-10-07", 70)
    g = quality.gate_market_load({"source": "kept"}, today=date(2026, 10, 9))     # KRX 실패·기존 유지 + 1거래일 늦음
    assert g["level"] == "warn"


def test_gate_and_alert_sends_only_on_fail(monkeypatch):
    _real_mode(monkeypatch, top_n=150)
    seen = []
    monkeypatch.setattr(alerts, "notify", lambda key, title, message="", level="error", dedup_hours=None:
                        seen.append((key, level, message)))
    quality.gate_and_alert(None, today=date(2026, 10, 10))
    assert seen and seen[0][0] == "quality:kr_market" and "reco_check" in seen[0][2]


def test_health_reports_failed_gate_and_recent_alerts(monkeypatch, no_channels):
    from app.services import monitoring
    _real_mode(monkeypatch, top_n=150)
    quality.gate_and_alert(None, today=date(2026, 10, 10))
    h = monitoring.health()
    assert "quality_gate_failed" in h["issues"] and h["status"] == "degraded"
    assert h["quality_gates"]["kr_market"]["level"] == "fail"
    assert h["alerts"]["recent"][0]["title"] == "국내 시세 적재 검증 실패"
    assert "message" not in h["alerts"]["recent"][0]                                 # 본문은 공개 응답에 없음


def test_scheduler_job_failure_sends_alert(monkeypatch):
    import scheduler
    seen = []
    monkeypatch.setattr(alerts, "notify", lambda key, title, message="", level="error", dedup_hours=None:
                        seen.append((key, title)))

    def boom():
        raise RuntimeError("KIS 토큰 만료")
    scheduler._safe("KIS 종가", boom)()                                                # 예외가 스케줄러 밖으로 안 나간다
    assert seen == [("job:KIS 종가", "예약 작업 실패: KIS 종가")]


def test_pipeline_gate_step_marks_failure(monkeypatch):
    # run.py real 의 'KRX → 적재 검증' 단계: 검증 실패면 실패 단계로 남아 PARTIAL·알림 대상이 된다
    import run
    _real_mode(monkeypatch, top_n=150)
    failed = []
    g = run._step(failed, "적재 검증", lambda: quality.gate_and_alert(None, today=date(2026, 10, 10)))
    if g["level"] == "fail":
        failed.append(f"적재 검증: {g['summary']}")
    assert any(f.startswith("적재 검증") for f in failed)


# ── DART 견고성 ───────────────────────────────────────────
def test_dart_corp_codes_are_cached_and_stale_cache_is_used_on_failure(monkeypatch):
    from app.collectors import dart
    calls = []

    def fetch(_k):
        calls.append(1)
        m = dart.CorpMap({"005930": "00126380"})
        m.names, m.modified = {"005930": "삼성전자"}, {"005930": "20260101"}
        return m
    monkeypatch.setattr(dart, "get_corp_code_map", fetch)
    assert dart.corp_codes("k") == {"005930": "00126380"}
    assert dart.corp_codes("k") == {"005930": "00126380"} and len(calls) == 1          # 7일 캐시
    assert db.query("SELECT corp_name FROM dart_corp")[0]["corp_name"] == "삼성전자"
    db.execute("UPDATE dart_corp SET fetched_at='2020-01-01T00:00:00+09:00'")       # 오래된 캐시

    def down(_k):
        raise requests.ConnectionError("blocked")
    monkeypatch.setattr(dart, "get_corp_code_map", down)
    assert dart.corp_codes("k") == {"005930": "00126380"}                             # 받기 실패 → 저장본


def test_dart_fatal_status_stops_the_run_immediately(monkeypatch):
    from app.collectors import dart
    monkeypatch.setattr(settings, "DART_API_KEY", "k" * 40)
    monkeypatch.setattr(dart, "get_corp_code_map", lambda _k: {t: "c" + t for t in ("A1", "A2", "A3")})
    calls = []
    monkeypatch.setattr(dart, "fetch_financials", lambda _k, cc: calls.append(cc) or
                        {"status": "012", "message": "접근할 수 없는 IP"})
    out = dart.collect_financials(["A1", "A2", "A3"])
    assert len(calls) == 1 and "012" in out["error"] and "IP" in out["error"]        # 예전: 3종목 모두 헛호출


def test_dart_no_data_status_is_not_fatal(monkeypatch):
    from app.collectors import dart
    monkeypatch.setattr(settings, "DART_API_KEY", "k" * 40)
    monkeypatch.setattr(dart, "get_corp_code_map", lambda _k: {"A1": "c1", "A2": "c2"})
    monkeypatch.setattr(dart, "fetch_financials", lambda _k, cc: {"status": "013", "message": "조회된 데이타가 없습니다."})
    out = dart.collect_financials(["A1", "A2"])
    assert out == {"ok": 0, "skipped": 2}


def test_dart_error_hint_names_the_network_problem():
    from app.collectors import dart
    assert "연결 못함" in dart.explain(requests.ConnectionError("x"))
    assert "TLS" in dart.explain(requests.exceptions.SSLError("cert"))
