"""기기 지문·IP 대역 — 세션을 '처음 로그인한 기기'에 결속하는 데 쓴다.

지문 = 브라우저 계열 + OS 계열 + 기기 종류(모바일/데스크톱). 버전 번호는 넣지 않는다 — 브라우저가 자동 업데이트될
때마다 세션이 끊기면 안 되기 때문이다. 같은 토큰이 다른 계열의 브라우저·OS 에서 오면 '탈취된 토큰'으로 보고
세션을 폐기한다(정상 사용자는 브라우저 계열이 요청마다 바뀌지 않는다).
IP 는 대역(IPv4 /24, IPv6 /48)만 본다 — 모바일 망은 같은 기기라도 주소가 자주 바뀌므로 '바뀌었다'는 사실은
폐기가 아니라 위험 신호(민감 거래 시 추가 인증)로만 쓴다.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re

_BROWSERS = (("Edg/", "Edge"), ("EdgA/", "Edge"), ("SamsungBrowser/", "Samsung Internet"), ("Whale/", "Whale"),
             ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_OSES = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iPadOS"),
         ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def _first(ua: str, table) -> str:
    for needle, name in table:
        if needle in ua:
            return name
    return "기타"


def family(ua: str | None) -> str:
    """'Chrome · Windows · desktop' 같은 계열 문자열(버전 없음). 비면 'unknown'."""
    ua = (ua or "")[:512]
    if not ua.strip():
        return "unknown"
    if re.search(r"python-requests|httpx|testclient|curl|okhttp|Go-http", ua, re.I):
        kind = "client"                                     # 브라우저가 아닌 API 클라이언트
        name = ua.split("/", 1)[0].strip()[:20] or "client"
        return f"{name} · {kind}"
    mobile = "mobile" if re.search(r"Mobile|Android|iPhone|iPad", ua) else "desktop"
    return f"{_first(ua, _BROWSERS)} · {_first(ua, _OSES)} · {mobile}"


def label(ua: str | None) -> str:
    """화면·접속 기록용 짧은 이름 — 'Chrome · Windows'."""
    parts = family(ua).split(" · ")
    return " · ".join(parts[:2])[:60]


def fingerprint(ua: str | None) -> str:
    """기기 지문(해시 32자) — 원문 UA 는 저장하지 않는다(최소 수집)."""
    return hashlib.sha256(("dev:" + family(ua)).encode()).hexdigest()[:32]


def ip_net(ip: str | None) -> str:
    """IP → 대역 문자열(IPv4 /24, IPv6 /48). IP 가 아니면(테스트 클라이언트 등) 앞 45자 그대로."""
    raw = (ip or "").strip()
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw[:45] or "unknown"
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{addr}/24", strict=False))
    if addr.ipv4_mapped:
        return str(ipaddress.ip_network(f"{addr.ipv4_mapped}/24", strict=False))
    return str(ipaddress.ip_network(f"{addr}/48", strict=False))
