"""시각 기준 — 서버(VM) 시간대가 UTC 여도 저장 시각·'오늘'은 한국시간.
예전: 모듈마다 datetime.now()(서버 로컬)를 써서 UTC VM 에서 지갑·실습·채팅 기록이 9시간 느리게(새벽엔 하루 전 날짜로) 저장됐다."""
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import clock, db
from app.services import attendance, wallet

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def utc_server(monkeypatch):
    """프로세스 시간대를 UTC 로(학교 VM 기본값) — 끝나면 원래대로."""
    if not hasattr(time, "tzset"):
        pytest.skip("tzset 없는 플랫폼")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_clock_is_korean_time_on_utc_server(utc_server):
    kst_now = datetime.now(KST).replace(tzinfo=None)
    assert abs((clock.now() - kst_now).total_seconds()) < 5
    assert abs((datetime.now() - kst_now).total_seconds()) > 8 * 3600 - 5     # 서버 로컬 시각은 정말 9시간 차이
    assert clock.today() == kst_now.date() and clock.iso_now()[:13] == kst_now.isoformat()[:13]


def test_records_are_stamped_in_korean_time_on_utc_server(utc_server):
    from app.services import auth
    auth.register("clock@oos.io", "Tz-pass-2026")  # 지갑은 실제 계정에만(결함 23) — 시험도 계정부터 만든다
    wallet.credit_once("clock@oos.io", 1000, "reward", memo="시각 확인", idem_key="clock-1")
    at = db.query("SELECT created_at FROM wallet_ledger WHERE username='clock@oos.io' ORDER BY ledger_id DESC LIMIT 1")
    stored = datetime.fromisoformat(at[0]["created_at"])
    assert abs((stored - datetime.now(KST).replace(tzinfo=None)).total_seconds()) < 60
    assert attendance.status("clock@oos.io")["today"] == datetime.now(KST).date().isoformat()


def test_no_server_local_time_calls_left():
    # 서버 로컬 시각(datetime.now()·date.today())은 app/clock.py 만 — 새 코드가 다시 쓰지 않게
    bad = []
    for p in [*ROOT.joinpath("app").rglob("*.py"), ROOT / "run.py", ROOT / "scheduler.py"]:
        if p.name == "clock.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])(datetime\.now\(\)|date\.today\(\)|datetime\.utcnow\(\))", line):
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, bad
