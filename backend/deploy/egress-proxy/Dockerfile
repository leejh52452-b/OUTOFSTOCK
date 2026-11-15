# egress 프록시(나가는 길의 문지기) — 우분투 저장소의 squid.
# 설정(squid.conf)은 compose 가 읽기 전용으로 붙인다 — app/security/egress.py 의 허용 목록에서 생성:
#   python tools/egress.py --squid > deploy/egress-proxy/squid.conf
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends squid \
 && rm -rf /var/lib/apt/lists/*
EXPOSE 3128
# 루트로 시작해 squid 가 스스로 proxy 사용자(uid 13)로 내려간다 — compose 는 SETUID·SETGID 만 허용하고 나머지 권한은 모두 뺀다
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
