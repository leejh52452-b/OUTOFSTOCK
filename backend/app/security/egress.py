"""나가는 연결(egress) 통제 — 서버가 '어디로' 나갈 수 있는지도 기본 거부 + 허용 목록으로 관리한다.

왜 필요한가
  · 서버가 침해돼도 데이터를 임의의 외부로 내보내기(유출)·내부망 탐색(SSRF)을 어렵게 한다.
  · 웹훅 주소처럼 설정값으로 들어오는 URL 이 사설·루프백·메타데이터 주소(169.254.169.254)를 가리키면 거부.
  · 외부 원천이 느려지거나 죽었을 때 서킷 브레이커로 빠르게 실패 — 배치·API 스레드가 묶이지 않게(복원력).

구현
  requests.Session.send 를 한 번 감싼다(install). 모듈 함수(requests.get)·FinanceDataReader 등 requests 를 쓰는
  모든 호출과 리다이렉트(재요청도 send 를 거침)가 같은 검사를 지난다. 웹소켓·SMTP 는 연결 전에 check_url/check_host.
  EGRESS_MODE  enforce(기본, 목록 밖 차단) · monitor(기록만 — 새 원천을 붙일 때 관찰용) · off
  EGRESS_EXTRA_HOSTS  추가 허용 호스트(쉼표) — 사내 프록시·알림 서버 등
  한계: 이름 확인(DNS)과 실제 연결 사이에 주소가 바뀌는 공격(DNS rebinding)까지 막지는 못한다 → 운영에서는
  네트워크 계층 egress 프록시(deploy/egress-proxy, 허용 목록 동일)와 방화벽을 함께 쓴다(다층 방어).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urlsplit

from app.config import settings

log = logging.getLogger("security.egress")

# 호스트 → 용도(문서·검토용). 코드에 적힌 외부 주소는 tests/test_egress.py 가 모두 여기 있는지 확인한다.
ALLOWED: dict[str, str] = {
    "data-dbg.krx.co.kr": "KRX OpenAPI — 국내 일별 시세·지수",
    "data.krx.co.kr": "KRX 정보데이터시스템(FinanceDataReader 경유)",
    "kind.krx.co.kr": "KRX KIND 상장 목록(FinanceDataReader 경유)",
    "openapi.koreainvestment.com": "한국투자증권 실전 REST",
    "openapivts.koreainvestment.com": "한국투자증권 모의 REST",
    "ops.koreainvestment.com": "한국투자증권 실시간 웹소켓",
    "opendart.fss.or.kr": "금융감독원 DART OpenAPI",
    "dart.fss.or.kr": "DART 공시 원문",
    "ecos.bok.or.kr": "한국은행 ECOS",
    "www.koreaexim.go.kr": "한국수출입은행 환율",
    "oapi.koreaexim.go.kr": "한국수출입은행 환율(신규 주소)",
    "news.google.com": "Google News RSS",
    "finnhub.io": "Finnhub REST", "ws.finnhub.io": "Finnhub 웹소켓",
    "data.sec.gov": "SEC EDGAR", "www.sec.gov": "SEC EDGAR",
    "query1.finance.yahoo.com": "야후 차트(지수 대체·FDR)", "query2.finance.yahoo.com": "야후 차트(FDR)",
    "fchart.stock.naver.com": "네이버 차트(FDR 국내 지수)", "api.stock.naver.com": "네이버 증권(FDR)",
    "finance.naver.com": "네이버 금융(FDR)",
    "raw.githubusercontent.com": "FinanceDataReader 목록 캐시", "financedata.github.io": "FinanceDataReader 목록 캐시",
    "api.tosspayments.com": "토스페이먼츠 결제 승인",
    "api.telegram.org": "운영 알림(텔레그램)",
}
# 443 이 아닌 포트를 쓰는 원천(네트워크 계층 프록시 허용 목록용) — 한국투자증권 REST(실전 9443·모의 29443)·웹소켓(21000·31000)
EXTRA_PORTS: dict[str, tuple[int, ...]] = {
    "openapi.koreainvestment.com": (9443,), "openapivts.koreainvestment.com": (29443,),
    "ops.koreainvestment.com": (21000, 31000),
}
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]


class EgressDenied(ConnectionError):
    """허용 목록 밖·내부 주소로 나가려는 연결."""


class CircuitOpen(ConnectionError):
    """원천이 연속으로 실패해 잠시 호출을 멈춘 상태(빠른 실패)."""


def mode() -> str:
    return (settings.EGRESS_MODE or "enforce").lower()


def allowed_hosts() -> set[str]:
    hosts = set(ALLOWED)
    hosts |= {h.strip().lower() for h in (settings.EGRESS_EXTRA_HOSTS or "").split(",") if h.strip()}
    for url in (settings.ALERT_WEBHOOK_URL,):            # 설정된 알림 웹훅은 그 호스트만
        h = urlsplit(url or "").hostname
        if h:
            hosts.add(h.lower())
    if settings.SMTP_HOST:
        hosts.add(settings.SMTP_HOST.lower())
    return hosts


def _internal(host: str) -> bool:
    """호스트가 사설·루프백·링크로컬(메타데이터) 주소인가 — 이름이면 확인한 주소 전부를 본다."""
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(ai[4][0].split("%")[0]) for ai in socket.getaddrinfo(host, None)}
        except OSError:
            return False                                 # 확인 실패는 연결 단계에서 어차피 실패
    return any(a in n for a in addrs for n in _BLOCKED_NETS)


def check_host(host: str | None, what: str = "") -> None:
    m = mode()
    if m == "off":
        return
    h = (host or "").lower().rstrip(".")
    extra = {x.strip().lower() for x in (settings.EGRESS_EXTRA_HOSTS or "").split(",") if x.strip()}
    reason = None
    if h not in allowed_hosts():
        reason = "not_allowlisted"
    elif h not in extra and _internal(h):                # 명시적으로 추가한 내부 호스트만 예외
        reason = "internal_address"
    if not reason:
        return
    _report(h, reason, what)
    if m == "enforce":
        raise EgressDenied(f"외부 연결 차단({reason}): {h} — 필요한 원천이면 EGRESS_EXTRA_HOSTS 에 추가")


def check_url(url: str, what: str = "") -> None:
    check_host(urlsplit(url).hostname, what)


_reported: dict[str, float] = {}
_now = time.monotonic                                    # 테스트에서 바꿔 끼운다


def _report(host, reason, what):
    now = _now()
    last = _reported.get(host)
    # 같은 호스트는 5분에 한 번만 기록. '기록한 적 없음'을 0 으로 두면 안 된다 — monotonic 은 부팅 뒤 초라서
    # 서버가 켜진 지 5분 안(재부팅 직후 = systemd 가 앱을 바로 띄우는 때)에는 차단 기록이 통째로 빠졌다(결함 19).
    if last is not None and now - last < 300:
        return
    _reported[host] = now
    log.warning("egress %s %s %s", reason, host, what)
    try:
        from app.security import events
        events.emit("egress_blocked" if mode() == "enforce" else "egress_unlisted", "medium",
                    host=host, reason=reason, via=what or None)
    except Exception:                                   # noqa: BLE001 — DB 가 없는 맥락(CLI 초기화 등)
        pass


# ── 서킷 브레이커·격벽(bulkhead) — 호스트별 ──────────────────
class Breaker:
    """연속 실패 FAILS 번 → OPEN(COOLDOWN 초 동안 즉시 실패) → HALF_OPEN(1건만 시험) → 성공하면 CLOSED."""
    FAILS, COOLDOWN = 5, 60

    def __init__(self, host):
        self.host, self.state, self.fails, self.opened_at = host, "closed", 0, 0.0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(settings.EGRESS_MAX_CONCURRENCY)

    def before(self):
        with self.lock:
            if self.state == "open":
                if time.monotonic() - self.opened_at < self.COOLDOWN:
                    raise CircuitOpen(f"{self.host} 일시 차단(연속 실패 {self.fails}회) — "
                                      f"{int(self.COOLDOWN - (time.monotonic() - self.opened_at))}초 뒤 재시도")
                self.state = "half_open"
            elif self.state == "half_open":
                raise CircuitOpen(f"{self.host} 시험 호출 진행 중")

    def success(self):
        with self.lock:
            self.state, self.fails = "closed", 0

    def failure(self):
        with self.lock:
            self.fails += 1
            if self.state == "half_open" or self.fails >= self.FAILS:
                if self.state != "open":
                    log.warning("circuit open %s (fails=%d)", self.host, self.fails)
                self.state, self.opened_at = "open", time.monotonic()


_breakers: dict[str, Breaker] = {}
_blk = threading.Lock()


def breaker(host: str) -> Breaker:
    with _blk:
        b = _breakers.get(host)
        if b is None:
            b = _breakers[host] = Breaker(host)
        return b


def status() -> list[dict]:
    return [{"host": h, "state": b.state, "fails": b.fails} for h, b in sorted(_breakers.items())]


def reset() -> None:
    with _blk:
        _breakers.clear()
    _reported.clear()


_installed = [False]


def install() -> None:
    """requests.Session.send 를 감싼다(한 번만). 모든 requests 기반 호출·리다이렉트가 지나간다."""
    if _installed[0]:
        return
    import requests

    original = requests.sessions.Session.send

    def guarded_send(self, request, **kwargs):
        host = (urlsplit(request.url).hostname or "").lower()
        check_host(host, "http")
        b = breaker(host)
        b.before()
        if not b.slots.acquire(timeout=kwargs.get("timeout") if isinstance(kwargs.get("timeout"), (int, float))
                               else 30):
            raise CircuitOpen(f"{host} 동시 호출 한도({settings.EGRESS_MAX_CONCURRENCY}) 초과")
        try:
            resp = original(self, request, **kwargs)
        except Exception:
            b.failure()
            raise
        finally:
            b.slots.release()
        if resp.status_code >= 500 or resp.status_code == 429:
            b.failure()
        else:
            b.success()
        return resp

    requests.sessions.Session.send = guarded_send
    _installed[0] = True


# ── 네트워크 계층 egress 프록시 설정(squid) — 같은 허용 목록에서 만든다(한 곳만 고치면 두 계층이 같이 바뀜) ──
def squid_conf(extra_hosts: str = "", listen: str = "3128", vm: bool = False) -> str:
    """egress 프록시 설정 생성 — 앱 안의 검사(check_host)와 같은 허용 목록으로 네트워크 계층에서 한 번 더 막는다.
      compose : python tools/egress.py --squid > deploy/egress-proxy/squid.conf
                (앱 컨테이너는 내부망에만 있어 이 프록시를 거쳐야만 바깥에 닿는다)
      VM      : python tools/egress.py --squid --vm --listen 127.0.0.1:3128 → /etc/squid/squid.conf (vm.sh 가 설치)
                (systemd 가 서비스의 직접 연결을 막고(IPAddressDeny) 프록시만 허용)
    허용: 목록의 호스트로 443(CONNECT)·80, 한국투자증권만 정해진 추가 포트. 목록의 이름이라도 사설·루프백·메타데이터 주소로
    풀리면 거부(DNS rebinding — 앱 안의 검사는 이름 확인과 연결 사이에 주소가 바뀌면 못 막는다). 그 밖은 전부 거부."""
    extras = sorted({h.strip().lower() for h in extra_hosts.split(",") if h.strip()})
    hosts = sorted(set(ALLOWED) | set(extras))
    src = "127.0.0.1/32 ::1" if vm else "172.16.0.0/12 10.0.0.0/8 192.168.0.0/16 127.0.0.1/32"
    lines = [
        "# OUTOFSTOCK egress 프록시 — 자동 생성(app/security/egress.py 의 ALLOWED). 손으로 고치지 말고 다시 생성:",
        ("#   bash deploy/vm.sh install|update (EGRESS_PROXY=1) — python tools/egress.py --squid --vm --listen "
         + listen) if vm else "#   python tools/egress.py --squid > deploy/egress-proxy/squid.conf",
        f"http_port {listen}",                                    # VM: 127.0.0.1:3128(같은 서버의 서비스만) · compose: 3128(egress 내부망)
        f"acl oos_net src {src}",
        "acl CONNECT method CONNECT",
        "acl p443 port 443",
        "acl p80 port 80",
        "acl oos_hosts dstdomain " + " ".join(hosts),
        "acl to_internal dst " + " ".join(str(n) for n in _BLOCKED_NETS),
    ]
    rules = ["http_access deny !oos_net"]
    if extras:                                            # 운영자가 명시적으로 추가한 호스트만 내부 주소 예외(앱 검사와 같은 규칙)
        lines.append("acl oos_extra dstdomain " + " ".join(extras))
        rules.append("http_access deny to_internal !oos_extra")
    else:
        rules.append("http_access deny to_internal")
    rules += ["http_access allow CONNECT oos_hosts p443",
              "http_access allow !CONNECT oos_hosts p80"]
    for i, (h, ports) in enumerate(sorted(EXTRA_PORTS.items())):
        lines += [f"acl x{i}_host dstdomain {h}", f"acl x{i}_ports port {' '.join(map(str, ports))}"]
        rules.append(f"http_access allow CONNECT x{i}_host x{i}_ports")
    rules.append("http_access deny all")
    tail = [
        "# 저장하지 않는다(시세·결제 응답이 프록시에 남지 않게) · 내부 주소·버전을 바깥에 알리지 않는다",
        "cache deny all",
        "forwarded_for delete",
        "httpd_suppress_version_string on",
        "# 로그 — 시각·결과·방법·호스트만(쿼리스트링에 든 API 키가 남지 않게 URL 경로는 기록하지 않는다)",
        "logformat oos %ts.%03tu %>a %Ss/%03>Hs %rm %>rd",
        "access_log daemon:/var/log/squid/access.log oos",
        "cache_log /var/log/squid/cache.log",
        "logfile_rotate 0",                                   # 순환은 logrotate(VM)·컨테이너 로그 정책이 맡는다
        "pid_filename /run/squid.pid" if vm else "pid_filename none",
        "coredump_dir /var/spool/squid" if vm else "coredump_dir /tmp",
        "# ICMP 측정 기능 끔 — 쓰지 않는 기능에 raw 소켓 권한(NET_RAW)을 주지 않는다",
        "pinger_enable off",
    ]
    return "\n".join(lines + rules + tail) + "\n"

