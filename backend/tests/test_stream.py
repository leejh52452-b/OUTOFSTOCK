"""실시간 알림(SSE) — 커밋된 변경만, 세션이 끝나면 스트림도 닫힘(app/services/stream.py).
실제 서버(uvicorn)를 띄워 진짜 스트리밍으로 확인한다(테스트 클라이언트는 응답이 끝나야 본문을 돌려준다)."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.api import app
from app.services import auth, stream

PW = "Ss-pass-2026"


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT", 1)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


def _login(base, u):
    auth.register(u, PW)
    tok = httpx.post(base + "/auth/login", json={"username": u, "password": PW}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _reader(base, h, out, stop_on):
    with httpx.stream("GET", base + "/events", headers=h, timeout=30) as r:
        out.append(("status", r.status_code, r.headers.get("content-type")))
        for line in r.iter_lines():
            out.append(("line", line))
            if any(s in line for s in stop_on):
                break


def _wait(cond, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_committed_wallet_change_is_pushed_and_rolled_back_one_is_not(live):
    ha, hb = _login(live, "ss-a"), _login(live, "ss-b")
    httpx.get(live + "/wallet", headers=ha)
    httpx.get(live + "/wallet", headers=hb)
    out = []
    t = threading.Thread(target=_reader, args=(live, ha, out, ['"balance":1000777']), daemon=True)
    t.start()
    assert _wait(lambda: any("event: hello" in str(x) for x in out))
    assert _wait(lambda: stream.broker.stats()["listener"])
    time.sleep(0.5)                                                    # LISTEN 이 붙을 시간
    assert httpx.post(live + "/transfer", headers=hb, json={"to": "ss-b", "amount": 1}).status_code == 400
    assert httpx.post(live + "/transfer", headers=hb, json={"to": "ss-a", "amount": 777}).status_code == 200
    t.join(10)
    assert out[0][1] == 200 and out[0][2].startswith("text/event-stream")
    events = [x[1] for x in out if x[0] == "line" and x[1].startswith("event:")]
    assert "event: wallet" in events
    data = [x[1] for x in out if x[0] == "line" and x[1].startswith("data:") and "balance" in x[1]]
    assert len(data) == 1 and '"balance":1000777' in data[0]           # 실패한 송금은 알림이 없었다


def test_stream_closes_when_the_session_is_revoked(live):
    h = _login(live, "ss-c")
    out = []
    t = threading.Thread(target=_reader, args=(live, h, out, ["__never__"]), daemon=True)
    t.start()
    assert _wait(lambda: any("event: hello" in str(x) for x in out))
    time.sleep(0.5)
    httpx.post(live + "/auth/logout-all", headers=h)
    t.join(10)
    assert not t.is_alive()                                            # 서버가 스트림을 닫았다
    assert any("event: session" in str(x) for x in out)


def test_events_require_login(live):
    assert httpx.get(live + "/events").status_code == 401
