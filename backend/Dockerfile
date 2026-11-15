# syntax=docker/dockerfile:1
# 배포 이미지 — API(uvicorn)·scheduler·realtime 이 같은 이미지(runtime), 백업 작업은 pg_dump·age 를 더한 backup 단계.
#
# 보안(컨테이너 최소 권한)
#   · 빌드 도구·pip 캐시는 build 단계에만 — 실행 이미지에는 가상환경(/opt/venv)과 코드만 복사
#   · 루트가 아닌 사용자(uid 10001, 로그인 셸 없음)로 실행. 코드는 root 소유(읽기만) — 침해돼도 코드를 고치지 못한다
#   · requirements.lock 이 있으면 해시 고정 설치(--require-hashes): 레지스트리·미러에서 바뀐 패키지가 들어오면 설치 실패
#   · 쓰기 위치는 /var/lib/oos(볼륨: 감사 증인·백업)와 /tmp(tmpfs) 뿐 — compose 가 루트 파일시스템을 읽기 전용으로 띄운다
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /w
COPY requirements.txt requirements.lock* ./
RUN python -m venv /opt/venv \
 && if [ -f requirements.lock ]; then \
        /opt/venv/bin/pip install --require-hashes --no-deps -r requirements.lock; \
    else \
        /opt/venv/bin/pip install -r requirements.txt; \
    fi \
 && /opt/venv/bin/pip uninstall -y pip                 # 실행 이미지에 패키지 설치 도구를 남기지 않는다

FROM ${PYTHON_IMAGE} AS runtime
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/venv/bin:$PATH HOME=/tmp
RUN groupadd --system --gid 10001 oos \
 && useradd --system --uid 10001 --gid oos --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin oos \
 && install -d -o oos -g oos -m 0750 /var/lib/oos \
 && python -m pip uninstall -y -q pip \
 && rm -rf /usr/local/lib/python3*/ensurepip            # 베이스 이미지의 pip 도 지운다 — 가상환경 것만 지우면 PATH 에 남는다
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY . .
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(os.environ.get('APP_HEALTH_URL', 'http://127.0.0.1:8000/health'), timeout=2)" || exit 1
# keep-alive 를 클라이언트(브라우저·연결 풀 ~5초)보다 길게 — 같으면 서버가 닫는 순간 재사용돼 연결 끊김(부하 시험에서 발견)
# 전달 헤더(X-Forwarded-For·Proto)는 기본으로 같은 컨테이너(127.0.0.1)에서 온 것만 믿는다 — 이미지만 따로 띄워 포트를 열어도
# 바깥 클라이언트가 IP 를 속이지 못한다. compose 는 앱이 edge 만 닿는 주소에서 듣게 하고 그때만 믿는다(docker-compose.prod.yml).
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "65", "--proxy-headers", "--no-server-header"]

# 백업 작업(docker compose run --rm backup) — 서버와 같은 메이저 버전의 pg_dump(16) + age(공개키 암호화)
#   PG_APT=pgdg(기본): PostgreSQL 공식 저장소(데비안 bookworm 기본 저장소는 15라 16 서버를 덤프할 수 없다)
#   PG_APT=distro   : 배포판 저장소에 16이 있는 베이스(우분투 24.04 등)
FROM runtime AS backup
ARG PG_APT=pgdg
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates age \
 && if [ "$PG_APT" = "pgdg" ]; then \
        apt-get install -y --no-install-recommends curl gnupg \
     && install -d /usr/share/postgresql-common/pgdg \
     && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
     && . /etc/os-release \
     && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
          > /etc/apt/sources.list.d/pgdg.list \
     && apt-get update \
     && apt-get purge -y curl gnupg; \
    fi \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
USER 10001:10001
HEALTHCHECK NONE
CMD ["python", "tools/dr.py", "backup"]
