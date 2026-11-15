"""나가는 연결 허용 목록 보기 · egress 프록시(squid) 설정 생성 — 허용 목록은 app/security/egress.py 한 곳에만 있다.

    python tools/egress.py                                      # 허용 호스트·포트·용도 표
    python tools/egress.py --squid > deploy/egress-proxy/squid.conf          # compose 운영 오버레이(egress-proxy 컨테이너)
    python tools/egress.py --squid --vm --listen 127.0.0.1:3128              # VM(같은 서버의 squid — vm.sh 가 설치)
EGRESS_EXTRA_HOSTS(.env) 의 추가 호스트도 함께 들어간다.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.security import egress  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--squid", action="store_true", help="squid.conf 를 표준 출력으로")
    ap.add_argument("--vm", action="store_true", help="VM 용(루프백만·PID 파일·logrotate)")
    ap.add_argument("--listen", default="3128", help="프록시가 들을 주소(기본 3128, VM 은 127.0.0.1:3128)")
    a = ap.parse_args(argv)
    if a.squid:
        sys.stdout.write(egress.squid_conf(settings.EGRESS_EXTRA_HOSTS or "", a.listen, vm=a.vm))
        return 0
    for h, why in sorted(egress.ALLOWED.items()):
        print(f"{h:34s} {','.join(map(str, (443,) + egress.EXTRA_PORTS.get(h, ()))):18s} {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
