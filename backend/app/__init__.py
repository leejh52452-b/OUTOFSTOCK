# 어떤 진입점(uvicorn·scheduler·run.py·테스트)이든 app 을 import 하는 순간 로그의 API 키 가리기가 켜진다.
from app import logging_conf as _logging_conf  # noqa: F401
# 나가는 연결 허용 목록·서킷 브레이커 — requests 기반 모든 외부 호출에 적용(app/security/egress.py)
from app.security import egress as _egress  # noqa: E402

_egress.install()
