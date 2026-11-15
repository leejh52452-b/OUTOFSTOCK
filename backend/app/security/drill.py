"""사고 대응 모의훈련(자동) — 금융보안 수준진단 5.4 '사고대응·복구 훈련 수행' · 5.3 '보안사고 조치'의 실측.

복구 훈련(tools/dr.py drill)이 '데이터를 되살릴 수 있나'를 재듯, 이 훈련은 '사고가 나면 자동 통제가 실제로 막고,
사람 절차(2인 재개)로 되돌아오나 — 그리고 얼마나 걸리나'를 잰다. 시나리오마다 사고 신호를 넣고
탐지(이벤트) → 차단(자동 대응) → 확인(실제 요청이 막히나) → 복구(사람 절차)까지 단계별 경과 시간을 남긴다.

  ① 원장 변조      변조 탐지 신호 → 돈 기능 4개 자동 중지 → 송금 503 → 요청자 본인 승인 거부 → 다른 관리자 승인 → 송금 재개
  ② 이상 송금 급증  FDS 차단 10건 → 송금만 자동 중지(꾸미기 구매는 그대로) → 2인 재개
  ③ 대입 공격      한 IP 로그인 실패 30건 → IP 자동 차단 → 그 IP 의 로그인 429 → 차단 해제 → 로그인 성공
  ④ 쿠키 탈취      다른 기기 키로 서명한 송금 → 401 + 세션 종료(기기 결속) → 원래 기기도 다시 로그인해야 함
  ⑤ 파일 변조      훈련용 임시 사본에 새 파일·코드 변경 → 무결성 위반 이벤트·경보 → 승인된 파일로 되돌리면 정상
                  (운영 파일은 건드리지 않는다 — 감시 로직을 임시 폴더에 그대로 돌린다)
  ⑥ 서비스 장애    정상 1번 뒤 응답 실패 40분(합성 모니터링 기록 주입) → 10분에 장애 사건·경보 → 30분 넘어 보고 대상·기한(인지+24시간)
                  → 보고서 초안(시행세칙 별지 제2호 12개 항목 + 사건 전 배포 = 변경시점 후보) → 정상 3번이면 종료·지속 시간 기록
  ⑦ 깨진 배포      구문 오류가 든 작은 설치본 → 자동 사전 점검이 서비스를 멈추기 전에 배포 중단(기록 모드에서도) →
                  승인 없는 설치본(enforce) 거부 · 요청자 본인 승인 거부 → 다른 관리자 승인 → 게이트 통과 → 적용 기록

탐지 '로직'(원장 재계산·FDS 채점·기기 키 검증·파일 해시)은 각 단위 시험이 검증하고, 이 훈련은 신호 이후의 대응 사슬을
끝에서 끝까지(HTTP 요청 포함) 확인한다 — 신호 주입형 훈련. 훈련 중 운영 알림 채널(텔레그램·웹훅·메일)로는 보내지 않는다.
안전장치(run_isolated): 운영 DB 에서 돌지 않는다 — 대상 DB 이름에 drill/test 가 있고, 운영 DB 와 다르고, 훈련 계정이 아닌
이용자가 없어야 한다. 결과는 보고서(MD·JSON)와 운영 DB 의 마지막 훈련 요약(app_meta incident_drill_last)·감사 기록.
"""
from __future__ import annotations

import base64
import json
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from app import clock, db

SCENARIOS = ("ledger_tamper", "fds_surge", "credential_stuffing", "cookie_theft", "file_tamper", "outage_report",
             "broken_deploy")
TITLES = {"ledger_tamper": "원장 변조", "fds_surge": "이상 송금 급증", "credential_stuffing": "대입 공격(한 IP)",
          "cookie_theft": "쿠키 탈취(다른 기기)", "file_tamper": "파일 변조·웹셸", "outage_report": "서비스 장애 판정·보고",
          "broken_deploy": "깨진 배포·미승인 변경"}
PW = "Drill-pass-2026!"
MONEY = ("transfer", "charge", "shop", "invest")


class DrillError(RuntimeError):
    """훈련을 시작하지 않는다(대상 DB 가 훈련용이 아님 등)."""


@contextmanager
def _quiet_alerts():
    """훈련 중에는 운영 알림 채널로 보내지 않는다 — alert_log 에만 남는다(훈련 경보가 실제 당번을 깨우지 않게)."""
    from app.config import settings
    keys = ("ALERT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "SMTP_HOST")
    saved = {k: getattr(settings, k) for k in keys}
    try:
        for k in keys:
            setattr(settings, k, "")
        yield
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


class _Clock:
    def __init__(self):
        self.t0, self.steps = time.perf_counter(), []

    def step(self, phase: str, what: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"phase": phase, "step": what, "ok": bool(ok), "ms": round((time.perf_counter() - self.t0) * 1000, 1),
                           "detail": str(detail)[:200]})
        return bool(ok)


def _name(tag: str) -> str:
    return f"drill-{tag}-{secrets.token_hex(3)}"


def _user(c, tag: str, topup: int = 0) -> dict:
    from app.services import auth, wallet
    name = _name(tag)
    auth.register(name, PW)
    if topup:
        wallet.credit_once(name, topup, "iap", memo="훈련 충전")
    r = c.post("/auth/login", json={"username": name, "password": PW})
    return {"name": name, "h": {"Authorization": f"Bearer {r.json().get('token')}"}, "login": r.status_code}


def _events(kind: str, since) -> list[dict]:
    return db.query("SELECT kind, response FROM security_event WHERE kind=:k AND at >= :t ORDER BY id", {"k": kind, "t": since})


def _two_person_resume(features, ck: _Clock) -> bool:
    from app.services import controls
    refused = True
    for f in features:
        req = controls.request_resume(f, "훈련 — 원인 확인 완료(대사·로그 정상)", "drill-ops-1")
        try:
            controls.decide(req["request_id"], True, "drill-ops-1")
            refused = False
        except controls.ControlError as e:
            refused = refused and e.code == "four_eyes"
        controls.decide(req["request_id"], True, "drill-ops-2")
    ck.step("verify", "요청자 본인 승인 거부(2인 통제)", refused)
    controls.clear_cache()
    return ck.step("recover", "다른 관리자 승인 후 재개", not any(controls.is_paused(f) for f in features))


# ── 시나리오 ───────────────────────────────────────────────────────────────
def _sc_ledger_tamper(c, ck: _Clock):
    from app.security import events
    from app.services import controls
    a, b = _user(c, "lt-a", 100_000), _user(c, "lt-b")
    since = clock.utc()
    ck.t0 = time.perf_counter()
    events.emit("ledger_tamper", "critical", day="drill", drill=True)
    ev = _events("ledger_tamper", since)
    ck.step("detect", "원장 변조 이벤트 기록·규칙 대응", bool(ev) and "pause" in (ev[-1]["response"] or ""), ev[-1]["response"] if ev else "")
    controls.clear_cache()
    paused = [f for f in MONEY if controls.is_paused(f)]
    ck.step("contain", "돈 기능 4개 자동 중지", len(paused) == 4, ",".join(paused))
    r = c.post("/transfer", headers=a["h"], json={"to": b["name"], "amount": 1000})
    ck.step("verify", "송금 요청이 503(service_paused)", r.status_code == 503 and r.json().get("code") == "service_paused", r.status_code)
    _two_person_resume(paused, ck)
    r = c.post("/transfer", headers=a["h"], json={"to": b["name"], "amount": 1000})
    ck.step("recover", "송금 다시 처리", r.status_code == 200, r.status_code)


def _sc_fds_surge(c, ck: _Clock):
    from app.security import events
    from app.services import controls
    since = clock.utc()
    ck.t0 = time.perf_counter()
    for i in range(10):
        events.emit("fds_block", "high", username=f"drill-fds-{i}", score=80, rules=["drill"])
    ev = _events("fds_block", since)
    ck.step("detect", "FDS 차단 10건 → 규칙 대응", any("pause" in (e["response"] or "") for e in ev), len(ev))
    controls.clear_cache()
    ck.step("contain", "송금만 자동 중지(다른 기능은 운영)", controls.is_paused("transfer") and not controls.is_paused("shop"))
    _two_person_resume(["transfer"], ck)


def _sc_credential_stuffing(c, ck: _Clock):
    from app.security import events
    u = _user(c, "cs")
    iph = events.ip_hash("testclient")                       # 훈련 클라이언트(TestClient)의 접속 주소
    ck.t0 = time.perf_counter()
    for _ in range(30):
        events.emit("login_fail", "low", ip_hash=iph, reason="drill")
    ck.step("detect", "한 IP 로그인 실패 30건 → 규칙 대응", events.blocked(iph))
    r = c.post("/auth/login", json={"username": u["name"], "password": PW})
    ck.step("contain", "그 IP 의 로그인 차단(429 ip_blocked)", r.status_code == 429 and r.json().get("code") == "ip_blocked", r.status_code)
    events.unblock_ip(iph, "drill-ops-1")
    r = c.post("/auth/login", json={"username": u["name"], "password": PW})
    ck.step("recover", "차단 해제 후 정상 로그인", r.status_code == 200, r.status_code)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class _Device:
    """브라우저 WebCrypto 와 같은 형식(ES256 · r‖s)의 기기 키."""
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": _b64u(n.x.to_bytes(32, "big")), "y": _b64u(n.y.to_bytes(32, "big"))}

    def proof(self, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        h = _b64u(json.dumps({"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}).encode())
        p = _b64u(json.dumps({"htm": method, "htu": "https://drill.invalid" + path, "iat": int(time.time()),
                              "jti": secrets.token_hex(16)}).encode())
        r, s = decode_dss_signature(self.key.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{h}.{p}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _sc_cookie_theft(c, ck: _Clock):
    from fastapi.testclient import TestClient
    from app.api import app
    from app.services import auth, wallet
    victim, friend = _name("ct-v"), _name("ct-f")
    auth.register(victim, PW)
    auth.register(friend, PW)
    wallet.credit_once(victim, 50_000, "iap", memo="훈련 충전")
    dev = _Device()
    web = TestClient(app)
    web.post("/auth/login", json={"username": victim, "password": PW}, headers={"X-Client": "web", "DPoP": dev.proof("POST", "/auth/login")})
    thief = TestClient(app)
    thief.cookies.update(dict(web.cookies))
    ck.t0 = time.perf_counter()
    r = thief.post("/transfer", headers={"X-CSRF-Token": thief.cookies.get("oos_csrf") or "",
                                         "DPoP": _Device().proof("POST", "/transfer")}, json={"to": friend, "amount": 1000})
    ck.step("detect", "다른 기기 키 서명 → 401 dpop_mismatch", r.status_code == 401 and r.json().get("code") == "dpop_mismatch", r.status_code)
    s = db.query("SELECT revoked_at, revoke_reason FROM auth_session WHERE username=:u ORDER BY created_at DESC LIMIT 1", {"u": victim})
    ck.step("contain", "세션 종료(탈취로 판단)", bool(s) and s[0]["revoked_at"] is not None, (s[0]["revoke_reason"] if s else ""))
    moved = db.query("SELECT COUNT(*) AS n FROM wallet_ledger WHERE username=:u AND reason='transfer_out'", {"u": victim})[0]["n"]
    ck.step("verify", "돈이 움직이지 않음", int(moved) == 0, moved)
    again = TestClient(app)
    r = again.post("/auth/login", json={"username": victim, "password": PW},
                   headers={"X-Client": "web", "DPoP": dev.proof("POST", "/auth/login")})
    ck.step("recover", "원래 기기로 다시 로그인", r.status_code == 200, r.status_code)


def _sc_file_tamper(c, ck: _Clock):
    from app.security import integrity
    with tempfile.TemporaryDirectory(prefix="oos-drill-") as d:
        root = Path(d)
        (root / "app").mkdir()
        (root / "web").mkdir()
        original = "def handler():\n    return 'ok'\n"
        (root / "app" / "api.py").write_text(original, encoding="utf-8")
        (root / "web" / "index.html").write_text("<!doctype html><title>drill</title>\n", encoding="utf-8")
        integrity.record_baseline(root=root, actor="drill", release="drill")
        ck.t0 = time.perf_counter()
        (root / "web" / "drill-unexpected.php").write_text("drill: 기준선에 없는 파일\n", encoding="utf-8")
        (root / "app" / "api.py").write_text(original + "# drill: 승인 안 된 변경\n", encoding="utf-8")
        since = clock.utc()
        r = integrity.check(emit=True, root=root)
        ck.step("detect", "새 파일·변경 탐지", r["state"] == "violation" and "web/drill-unexpected.php" in r["unexpected"]
                and "app/api.py" in r["changed"], r.get("counts"))
        ev = _events("file_integrity", since)
        ck.step("contain", "보안 이벤트(심각)·경보", bool(ev) and "alert" in (ev[-1]["response"] or ""), ev[-1]["response"] if ev else "")
        (root / "web" / "drill-unexpected.php").unlink()
        (root / "app" / "api.py").write_text(original, encoding="utf-8")
        r = integrity.check(emit=True, root=root)
        ck.step("recover", "승인된 파일로 되돌리면 정상", r["state"] == "ok", r["state"])
    integrity.status_reset()


def _sc_outage_report(c, ck: _Clock):
    from datetime import timedelta
    from app.services import incidents
    now = clock.utc()
    db.write_query("INSERT INTO change_record(created_at, kind, title, reason, requested_by, release_sha256, risk, status, "
                   "approved_at, applied_at, applied_by) VALUES(:t, 'unapproved', '훈련 — 장애 직전 배포', "
                   "'훈련용 변경 기록(변경시점 후보 확인)', 'drill', :sha, 'high', 'applied', :t, :t, 'drill') RETURNING id",
                   {"t": now - timedelta(minutes=50), "sha": "d" * 64})
    incidents.record_check({"ok": True, "ms": 20, "code": 200}, at=now - timedelta(minutes=41))      # 그 전엔 정상이었다
    for m in range(40, 0, -1):
        incidents.record_check({"ok": False, "ms": 10_000, "code": 0}, at=now - timedelta(minutes=m))
    ck.t0 = time.perf_counter()
    iid = incidents.evaluate(now).get("opened")
    alerted = bool(iid) and bool(db.query("SELECT 1 FROM alert_log WHERE key=:k", {"k": f"incident:{iid}"}))
    ck.step("detect", "응답 실패 10분 이상 → 장애 사건 자동 등록·경보", alerted, f"사건 #{iid}")
    incidents.evaluate(now)
    inc = incidents.get(iid) if iid else None
    ck.step("contain", "30분 넘김 → 보고 대상·최초 보고 기한(인지 + 24시간)", bool(inc and inc["reportable"] and inc["report_due"]),
            inc and inc["report_due"])
    md = incidents.report_md(iid) if iid else ""
    ck.step("verify", "보고서 초안 — 별지 제2호 12개 항목 + 사건 전 배포(변경시점 후보)",
            all(f"| {k} |" in md for k in incidents.REPORT_FIELDS) and "훈련 — 장애 직전 배포" in md)
    for m in (1, 2, 3):
        incidents.record_check({"ok": True, "ms": 30, "code": 200}, at=now + timedelta(minutes=m))
    incidents.evaluate(now + timedelta(minutes=3))
    inc = incidents.get(iid) if iid else None
    mins = (inc or {}).get("details", {}).get("minutes")
    ck.step("recover", "정상 3번 → 사건 종료·지속 시간 기록", bool(inc and inc["status"] == "resolved" and (mins or 0) >= 40),
            f"{mins}분")


def _mini_release(root: Path, broken: bool = False) -> Path:
    """변경관리 훈련용 작은 릴리스 — 지금 정책·마이그레이션은 그대로(적용된 마이그레이션 보존 점검 통과), 코드 한 파일."""
    import shutil
    from app.security import pdp
    (root / "app" / "security").mkdir(parents=True)
    (root / "sql" / "migrations").mkdir(parents=True)
    shutil.copy(pdp.POLICY_PATH, root / "app" / "security" / "policy.json")
    for p in sorted((Path(__file__).resolve().parents[2] / "sql" / "migrations").glob("V*.sql")):
        shutil.copy(p, root / "sql" / "migrations" / p.name)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    body = "def handler():\n    return 'ok'\n" + ("\n\ndef broken(:\n    pass\n" if broken else "")
    (root / "app" / "handler.py").write_text(body, encoding="utf-8")
    (root / "run.py").write_text("print('drill release')\n", encoding="utf-8")
    return root


def _sc_broken_deploy(c, ck: _Clock):
    from app.config import settings
    from app.services import changes
    saved = (settings.CHANGE_GATE, settings.CHANGE_FREEZE)
    try:
        settings.CHANGE_GATE, settings.CHANGE_FREEZE = "enforce", "none"
        with tempfile.TemporaryDirectory(prefix="oos-drill-chg-") as d:
            d = Path(d)
            good, bad = d / "good.sh", d / "bad.sh"
            changes.build_installer(_mini_release(d / "good"), good)
            changes.build_installer(_mini_release(d / "bad", broken=True), bad)
            ck.t0 = time.perf_counter()
            g = changes.gate(bad, actor="drill-ops-1")
            ck.step("detect", "구문 오류가 든 설치본 → 자동 사전 점검 실패", not g["allow"] and "파이썬 구문" in g["reason"],
                    g["reason"][:90])
            settings.CHANGE_GATE = "record"
            g = changes.gate(bad, actor="drill-ops-1")
            ck.step("contain", "기록 모드에서도 서비스를 멈추기 전에 배포 중단", not g["allow"], g["reason"][:60])
            settings.CHANGE_GATE = "enforce"
            g = changes.gate(good, actor="drill-ops-1")
            req = changes.request(good, "drill-ops-1", "훈련 — 정상 설치본", "변경관리 훈련(승인 흐름 확인)",
                                  evidence="훈련 — 시험 통과 가정")
            try:
                changes.decide(req["id"], True, "drill-ops-1")
                self_refused = False
            except changes.ChangeError as e:
                self_refused = e.code == "four_eyes"
            ck.step("verify", "승인 없는 설치본 거부(enforce) · 요청자 본인 승인 거부(제3자 검증)",
                    not g["allow"] and self_refused, g["reason"][:60])
            changes.decide(req["id"], True, "drill-ops-2")
            g = changes.gate(good, actor="drill-ops-1")
            changes.applied(g.get("change_id"), "drill-ops-1", None)
            st = db.query("SELECT status FROM change_record WHERE id=:i", {"i": req["id"]})
            ck.step("recover", "다른 관리자 승인 → 게이트 통과 → 적용 기록",
                    g["allow"] and g.get("change_id") == req["id"] and bool(st) and st[0]["status"] == "applied", g["reason"])
    finally:
        settings.CHANGE_GATE, settings.CHANGE_FREEZE = saved


_FNS = {"ledger_tamper": _sc_ledger_tamper, "fds_surge": _sc_fds_surge, "credential_stuffing": _sc_credential_stuffing,
        "cookie_theft": _sc_cookie_theft, "file_tamper": _sc_file_tamper, "outage_report": _sc_outage_report,
        "broken_deploy": _sc_broken_deploy}


def _first(steps, phase):
    return next((s["ms"] for s in steps if s["phase"] == phase), None)


def run(client=None, only: tuple[str, ...] | None = None) -> dict:
    """지금 연결된 DB(훈련 DB 또는 시험 DB)에서 시나리오를 돌린다."""
    from fastapi.testclient import TestClient
    from app.api import app
    from app.security import events
    from app.services import controls
    c = client or TestClient(app)
    started = clock.utc()
    out = []
    with _quiet_alerts():
        for name in only or SCENARIOS:
            controls.clear_cache()
            events.clear_cache()
            ck = _Clock()
            try:
                _FNS[name](c, ck)
            except Exception as e:                              # noqa: BLE001 — 한 시나리오 실패가 훈련 전체를 멈추지 않게
                ck.step("error", "예외", False, f"{type(e).__name__}: {str(e)[:160]}")
            steps = ck.steps
            out.append({"name": name, "title": TITLES[name], "ok": bool(steps) and all(s["ok"] for s in steps),
                        "detect_ms": _first(steps, "detect"), "contain_ms": _first(steps, "contain"),
                        "recover_ms": _first([s for s in steps if s["phase"] == "recover"][-1:], "recover"),
                        "steps": steps})
    return {"ok": all(r["ok"] for r in out), "at": clock.utc().isoformat(timespec="seconds"),
            "started_at": started.isoformat(timespec="seconds"), "scenarios": len(out),
            "passed": sum(1 for r in out if r["ok"]), "results": out}


# ── 격리 실행(CLI) · 기록 · 보고서 ──────────────────────────────────────────
def guard_target(target_url: str, main_url: str) -> str:
    """훈련 대상 DB 확인 — 이름에 drill/test, 운영 DB 와 다름. 반환: 대상 DB 이름."""
    from sqlalchemy.engine import make_url
    t, m = make_url(target_url), make_url(main_url)
    name = (t.database or "").lower()
    if not any(k in name for k in ("drill", "test")):
        raise DrillError(f"훈련 DB 이름에 drill 또는 test 가 있어야 합니다(지금: {t.database})")
    if (t.host, t.port, t.database) == (m.host, m.port, m.database):
        raise DrillError("운영 DB 와 같은 DB 에서는 훈련하지 않습니다")
    return t.database


def run_isolated(target_url: str, out_dir: Path | None = None) -> dict:
    """훈련 DB 로 바꿔 끼워 돌리고, 운영 DB 로 돌아와 결과를 기록한다."""
    from sqlalchemy import text
    from app.config import settings
    main_url, mig = settings.DATABASE_URL, settings.MIGRATION_DATABASE_URL
    guard_target(target_url, main_url)
    settings.MIGRATION_DATABASE_URL = ""                  # 훈련 중 스키마·기준선 쓰기도 훈련 DB 로만
    db.reset_engine(target_url)
    try:
        db.init_db()
        others = db.query("SELECT COUNT(*) AS n FROM app_user WHERE username NOT LIKE 'drill-%'")[0]["n"]
        if others:
            raise DrillError(f"훈련 DB 에 훈련 계정이 아닌 이용자 {others}명 — 운영 사본일 수 있어 멈춥니다")
        names = ", ".join(f'"{t.name}"' for t in db.metadata.sorted_tables)
        with db.get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        rep = run()
    finally:
        db.reset_engine(main_url)
        settings.MIGRATION_DATABASE_URL = mig
    rep["target"] = guard_target(target_url, main_url)
    record(rep)
    if out_dir is not None:
        rep["files"] = write_report(rep, out_dir)
    return rep


def record(rep: dict) -> None:
    """(지금 연결된 = 운영) DB 에 마지막 훈련 요약 + 감사 기록. 실패하면 경보."""
    from app.services import alerts, audit
    failed = [r["name"] for r in rep["results"] if not r["ok"]]
    db.set_meta("incident_drill_last", json.dumps({"ok": rep["ok"], "at": rep["at"], "scenarios": rep["scenarios"],
                                                   "passed": rep["passed"], "failed": failed}, ensure_ascii=False))
    audit.record("incident.drill", "system", target=rep.get("target") or "current",
                 detail={"ok": rep["ok"], "passed": rep["passed"], "scenarios": rep["scenarios"],
                         "contain_ms": {r["name"]: r["contain_ms"] for r in rep["results"]}},
                 result="ok" if rep["ok"] else "fail")
    if failed:
        alerts.notify("incident:drill", "[훈련] 사고 대응 모의훈련 실패", f"실패 시나리오: {', '.join(failed)}", level="error")


def render_md(rep: dict) -> str:
    L = ["# 사고 대응 모의훈련 보고서", "",
         f"- 시각: {rep['at']} (UTC) · 대상: {rep.get('target', '현재 DB')} · 결과: **{rep['passed']}/{rep['scenarios']} 통과**",
         "- 방식: 신호 주입형 — 사고 신호 → 탐지(이벤트) → 차단(자동 대응) → 확인(실제 요청) → 복구(사람 절차). "
         "탐지 로직 자체는 단위 시험이 검증한다. 시간은 신호를 넣은 순간부터의 경과(ms, 같은 프로세스 안 — 자동화 구간).", "",
         "| 시나리오 | 결과 | 탐지 | 차단 | 복구 |", "|---|---|---:|---:|---:|"]
    for r in rep["results"]:
        f = lambda v: "—" if v is None else f"{v:.0f}ms"      # noqa: E731
        L.append(f"| {r['title']} | {'✓' if r['ok'] else '✗'} | {f(r['detect_ms'])} | {f(r['contain_ms'])} | {f(r['recover_ms'])} |")
    for r in rep["results"]:
        L += ["", f"## {r['title']}", "", "| 단계 | 할 일 | 결과 | 경과 | 비고 |", "|---|---|---|---:|---|"]
        for s in r["steps"]:
            L.append(f"| {s['phase']} | {s['step']} | {'✓' if s['ok'] else '✗'} | {s['ms']:.0f}ms | {s['detail']} |")
    return "\n".join(L) + "\n"


def write_report(rep: dict, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "incident-drill-" + rep["at"][:19].replace(":", "").replace("-", "")
    md, js = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md.write_text(render_md(rep), encoding="utf-8")
    js.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return [str(md), str(js)]
