#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# OUTOFSTOCK — 우분투 VM 운영 스크립트 (가상환경 + PostgreSQL + systemd)
# stock-backend 폴더에서 실행한다. 본인 계정으로 실행(필요한 곳만 sudo 를 묻는다).
#
#   bash deploy/vm.sh install              처음 한 번: 패키지·DB·가상환경·.env·스키마·서비스 3개·매일 백업
#   bash deploy/vm.sh update [설치파일]     새 버전: 백업 → 코드 교체(.env·.venv 유지, 새 버전에서 지운 파일 정리) → 패키지 → 마이그레이션 → 재시작 → 확인
#   bash deploy/vm.sh status               서비스·헬스체크·마지막 배치·디스크·DB 크기·최근 백업
#   bash deploy/vm.sh restart              서비스 재시작 + 헬스체크
#   bash deploy/vm.sh backup               DB 백업(일관 스냅샷 + age 암호화 + manifest, 최근 14개 + 월별 12개 보관)
#   bash deploy/vm.sh backups              백업 목록(시각·크기·암호화·행 수)
#   bash deploy/vm.sh restore <백업파일>    백업 시점으로 되돌리기 — 새 DB 에 복원 → 검증 → 이름 바꿔 전환(실패 시 운영 DB 그대로)
#   bash deploy/vm.sh drill [백업파일]      복구 훈련(운영 DB 는 건드리지 않음) — 격리 DB 에 복원·검증·RTO/RPO 실측 보고서
#   bash deploy/vm.sh rollback             직전 update 전 코드로 되돌리기(DB 는 그대로 — 마이그레이션은 추가만 함)
#   bash deploy/vm.sh logs [api|scheduler|realtime]
#   bash deploy/vm.sh units                서비스 파일 다시 쓰기 + 재시작(아래 값을 바꿨을 때)
#   bash deploy/vm.sh harden               서버 기본 보안: 방화벽(ufw)·SSH·자동 보안 업데이트·커널 설정·DB 접속 기록
#   bash deploy/vm.sh db-tune [--plan|--reset]  DB 서버 설정을 메모리·CPU 에 맞게(install·update 때 자동 — .env DB_AUTOTUNE=off 로 끔)
#
# 바꿀 수 있는 값(환경변수 — 한 번 정하면 서비스 파일에 기록돼 update 때도 유지):
#   PORT(8000) · WORKERS(1)
#   BIND_HOST  앱이 들을 주소. 기본 auto = nginx 경계(deploy/nginx.conf)가 깔려 있으면 127.0.0.1(직접 접속 차단), 없으면 0.0.0.0
#   HARDEN=1   systemd 샌드박스(코드·시스템 읽기 전용, 홈 폴더 숨김, 권한 상승·커널 조작 금지, 시스템 호출 제한) — 비교용 HARDEN=0
#   EGRESS_PROXY=1  같은 서버의 squid(허용 목록, 127.0.0.1:3128)로만 바깥에 나가게 — 직접 연결은 커널이 막는다(IPAddressDeny)
#     EGRESS_ALLOW_IPS='10.0.0.5/32 …'  프록시 없이 직접 닿아야 하는 주소(다른 서버의 DB·SMTP)
#   BACKUP_DIR · BACKUP_KEEP(14) · BACKUP_RW_PATHS(외부 보관 도구가 쓰는 폴더) · YES=1(확인 생략)
#   harden: ADMIN_CIDR=관리자 IP 대역(SSH 를 그 대역만) · SSH_KEYS_ONLY=1(비밀번호 로그인 끄기 — 키 등록 뒤에)
#   백업 암호화: .env 의 BACKUP_AGE_RECIPIENTS=공개키 파일 · 복원/훈련: AGE_IDENTITY=오프라인 보관 키 파일
#   훈련 대상: DRILL_ADMIN_URL(데이터베이스를 만들 수 있는 계정) — 자세한 절차 docs/dr/README.md
# tmux 로 띄워 둔 uvicorn·scheduler·realtime 이 있으면 먼저 끌 것(같은 포트를 쓰면 서비스가 못 뜬다).
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-$(id -un)}"
APP_HOME="${HOME:-}"
[ -n "$APP_HOME" ] || APP_HOME="$(getent passwd "$APP_USER" 2>/dev/null | cut -d: -f6 || true)"
BACKUP_DIR="${BACKUP_DIR:-$APP_HOME/outofstock-backups}"
RELEASE_DIR="${RELEASE_DIR:-$APP_HOME/outofstock-releases}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
PY="${OOS_PY:-$APP_DIR/.venv/bin/python}"            # OOS_PY: 다른 가상환경을 쓸 때(검증용)
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/outofstock}"
SQUID_CONF="${SQUID_CONF:-/etc/squid/squid.conf}"
PROXY_URL="http://127.0.0.1:3128"

# 배포 선택값은 서비스 파일에 '# oos: 키=값' 으로 남긴다 → update·restore 가 서비스 파일을 다시 써도 그대로 유지.
# (표식이 없는 예전 서비스 파일이면 ExecStart 의 --port/--workers 를 읽는다 — 예전엔 update 때 기본값으로 돌아갔다)
unit_setting() {
    local f="$UNIT_DIR/outofstock-api.service" v
    [ -f "$f" ] || return 0
    v="$(sed -n "s/^# oos: $1=//p" "$f" | head -1)"
    if [ -z "$v" ]; then
        case "$1" in
            PORT)    v="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$f" | head -1)" ;;
            WORKERS) v="$(sed -n 's/.*--workers \([0-9][0-9]*\).*/\1/p' "$f" | head -1)" ;;
        esac
    fi
    printf '%s' "$v"
}
PORT="${PORT:-$(unit_setting PORT)}";                  PORT="${PORT:-8000}"
WORKERS="${WORKERS:-$(unit_setting WORKERS)}";         WORKERS="${WORKERS:-1}"
BIND_HOST="${BIND_HOST:-$(unit_setting BIND_HOST)}";   BIND_HOST="${BIND_HOST:-auto}"
HARDEN="${HARDEN:-$(unit_setting HARDEN)}";            HARDEN="${HARDEN:-1}"
EGRESS_PROXY="${EGRESS_PROXY:-$(unit_setting EGRESS_PROXY)}"; EGRESS_PROXY="${EGRESS_PROXY:-0}"
EGRESS_ALLOW_IPS="${EGRESS_ALLOW_IPS:-$(unit_setting EGRESS_ALLOW_IPS)}"
BACKUP_RW_PATHS="${BACKUP_RW_PATHS:-$(unit_setting BACKUP_RW_PATHS)}"
SERVICES=(outofstock-api outofstock-scheduler outofstock-realtime)
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

say()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

has_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }
is_loopback() { case "$1" in 127.*|::1|localhost) return 0 ;; *) return 1 ;; esac; }
bind_host() {   # auto → nginx 경계가 있으면 루프백(바깥에서 앱 포트로 직접 들어와 nginx 의 TLS·관리 기기 인증을 건너뛰지 못하게)
    if [ "$BIND_HOST" != "auto" ]; then echo "$BIND_HOST"
    elif [ -e "$NGINX_SITE" ]; then echo 127.0.0.1
    else echo 0.0.0.0; fi
}
env_value() {   # .env 의 값(따옴표 제거) — 표시·점검용
    [ -f "$APP_DIR/.env" ] || return 0
    sed -n "s/^$1=//p" "$APP_DIR/.env" | tail -1 | tr -d '\r' | sed "s/^[\"']//; s/[\"']\$//"
}
units_installed() { [ -f "$UNIT_DIR/outofstock-api.service" ]; }
as_postgres() { if [ "$(id -u)" -eq 0 ]; then runuser -u postgres -- "$@"; else sudo -u postgres "$@"; fi; }
run_app() { (cd "$APP_DIR" && "$PY" "$@"); }

# 앱과 똑같은 규칙(.env → 환경변수 → 기본값)으로 DB 주소를 읽는다. pg_dump/psql 용으로 '+psycopg' 는 뗀다.
db_url() {
    local u
    if [ -x "$PY" ]; then
        u="$(cd "$APP_DIR" && "$PY" -c 'from app.config import settings; print(settings.DATABASE_URL)' 2>/dev/null || true)"
    fi
    u="${u:-postgresql+psycopg://oos:oos@localhost:5432/outofstock}"
    echo "${u/+psycopg/}"
}

pick_python() {
    local c
    for c in python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            echo "$c"; return 0
        fi
    done
    return 1
}

health() {   # $1=최대 대기 초
    local _
    for _ in $(seq 1 "${1:-60}"); do
        if curl -fsS -o /dev/null -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

port_busy_by_other() {   # 우리 서비스가 아닌 프로세스가 포트를 잡고 있으면 0
    command -v ss >/dev/null 2>&1 || return 1
    ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN || return 1
    if has_systemd && systemctl is-active --quiet outofstock-api 2>/dev/null; then return 1; fi
    return 0
}

# ── 설치 ──────────────────────────────────────────────────────────────
ensure_packages() {
    local need=()
    pick_python >/dev/null || need+=(python3 python3-venv)
    command -v psql >/dev/null 2>&1 || need+=(postgresql postgresql-client)
    command -v curl >/dev/null 2>&1 || need+=(curl)
    command -v age >/dev/null 2>&1 || need+=(age)                   # 백업 암호화(공개키) — 우분투 22.04+ 기본 저장소
    local py; py="$(pick_python || echo python3)"
    if ! "$py" -c 'import venv, ensurepip' 2>/dev/null; then
        need+=("python$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)-venv")
    fi
    if [ "${#need[@]}" -gt 0 ]; then
        say "패키지 설치: ${need[*]}"
        $SUDO apt-get update -y >/dev/null
        $SUDO apt-get install -y "${need[@]}"
    fi
    pick_python >/dev/null || die "Python 3.11 이상이 필요합니다(우분투 22.04 면: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv)"
    if has_systemd; then $SUDO systemctl enable --now postgresql >/dev/null 2>&1 || true; fi
}

ensure_database() {
    local url; url="$(db_url)"
    local parts; parts="$(python3 - "$url" <<'PY'
import sys, urllib.parse as u
p = u.urlparse(sys.argv[1])
print("\x1f".join([p.hostname or "localhost", p.username or "", u.unquote(p.password or ""),
                  (p.path or "/").lstrip("/") or "outofstock"]))
PY
)"
    local host user pass name
    IFS=$'\x1f' read -r host user pass name <<<"$parts"
    if [ "$host" != "localhost" ] && [ "$host" != "127.0.0.1" ]; then
        say "DB 가 다른 서버($host) — 계정·DB 생성은 건너뜀"; return 0
    fi
    case "$user$name" in *[!A-Za-z0-9_]*) die "DB 계정·이름은 영문·숫자·_ 만(현재: $user / $name)";; esac
    as_postgres psql -tAc "SELECT 1" >/dev/null 2>&1 || die "PostgreSQL 이 꺼져 있습니다 — sudo systemctl start postgresql 후 다시 실행"
    if ! as_postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$user'" | grep -q 1; then
        say "PostgreSQL 계정 만들기: $user"
        as_postgres psql -v ON_ERROR_STOP=1 -q -v pw="$pass" <<SQL
CREATE ROLE "$user" LOGIN PASSWORD :'pw';
SQL
    fi
    local db
    for db in "$name" "${name}_test"; do
        if ! as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
            say "데이터베이스 만들기: $db"
            as_postgres psql -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE \"$db\" OWNER \"$user\""
        fi
    done
    psql "$url" -tAc "SELECT 1" >/dev/null 2>&1 || die "DB 접속 실패($url) — .env 의 DATABASE_URL 비밀번호가 계정과 같은지 확인"
}

ensure_venv() {
    if [ ! -x "$PY" ]; then
        local py; py="$(pick_python)"
        say "가상환경 만들기(.venv, $py)"
        "$py" -m venv "$APP_DIR/.venv"
    fi
    "$PY" -m pip install -q -U pip
    # CI 가 시험한 판 그대로(해시 고정) — 바뀐 패키지는 설치 실패. 이 파이썬 판에 맞는 휠이 잠금에 없으면 범위(requirements.txt)로.
    if [ -f "$APP_DIR/requirements.lock" ] && \
       say "패키지 설치/갱신(requirements.lock — 해시 고정)" && \
       "$PY" -m pip install -q --require-hashes --no-deps -r "$APP_DIR/requirements.lock"; then
        "$PY" -m pip check >/dev/null || warn "pip check 경고 — .venv/bin/pip check 로 확인"
    else
        warn "잠금 파일로 설치하지 못해 범위(requirements.txt)로 설치합니다 — CI 가 시험한 판과 다를 수 있음"
        "$PY" -m pip install -q -r "$APP_DIR/requirements.txt"
    fi
}

ensure_env() {
    if [ ! -f "$APP_DIR/.env" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        warn ".env 를 새로 만들었습니다 — 실데이터를 쓰려면 KRX_API_KEY·DART_API_KEY(·KIS_*) 를 채우세요:  nano .env"
    fi
    chmod 600 "$APP_DIR/.env"                     # 같은 VM 의 다른 계정이 API 키를 못 읽게
}

# ── 서비스(systemd) ───────────────────────────────────────────────
# 샌드박스 — 서비스가 침해돼도 할 수 있는 일을 줄인다(최소 권한을 프로세스 단위로). systemd-analyze security 로 점수 확인.
#   · 코드·시스템은 읽기만(ProtectSystem=strict). 홈 폴더는 통째로 숨기고(ProtectHome=tmpfs) 코드 폴더만 읽기 전용으로
#     다시 보이게 — 앱이 뚫려도 같은 계정의 SSH 키·셸 기록·다른 파일을 읽지 못한다. 쓰기는 var/·상태·캐시 폴더만.
#   · 권한 상승·SUID·커널 설정/모듈/로그·새 네임스페이스·실시간 스케줄링 금지, capability 없음, 시스템 호출은 일반 서비스 묶음만
#   · 코어 덤프 금지(메모리의 키·세션이 파일로 남지 않게) · 새 파일은 본인만 읽기(UMask 0077)
#   · MemoryDenyWriteExecute 는 쓰지 않는다 — psycopg(ctypes)·libffi 콜백이 쓰기+실행 메모리를 쓴다
sandbox_block() {   # $1 = service|backup
    [ "$HARDEN" = "1" ] || return 0
    local kind="$1" p rw="-$APP_DIR/var"
    echo "NoNewPrivileges=yes"
    echo "PrivateTmp=yes"
    echo "PrivateDevices=yes"
    echo "PrivateIPC=yes"
    echo "ProtectSystem=strict"
    if [ "$kind" = "backup" ]; then
        # 백업은 외부 보관 도구(rclone·scp 등)가 홈의 설정·키를 읽어야 할 수 있어 '읽기 전용'까지만
        echo "ProtectHome=read-only"
        rw="$rw -$BACKUP_DIR"
        for p in $BACKUP_RW_PATHS; do rw="$rw -$p"; done
    else
        echo "ProtectHome=tmpfs"
        echo "BindReadOnlyPaths=$APP_DIR"
        p="$(py_prefix)"                                  # pyenv 등 홈 아래 파이썬이면 그 폴더도 읽기 전용으로
        case "$p" in /home/*|/root/*) [ "${APP_DIR#"$p"}" = "$APP_DIR" ] && echo "BindReadOnlyPaths=$p" ;; esac
        case "$BACKUP_DIR" in /home/*|/root/*) ;; *) echo "InaccessiblePaths=-$BACKUP_DIR" ;; esac
    fi
    echo "ReadWritePaths=$rw"
    cat <<EOF
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service
EOF
    [ "$kind" = "backup" ] || echo "SystemCallFilter=~@privileged @resources"
    cat <<EOF
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077
LimitCORE=0
IPAccounting=yes
EOF
}

# 나가는 연결 — EGRESS_PROXY=1 이면 프록시 주소를 주고, 직접 연결은 커널(cgroup BPF)이 막는다(루프백·EGRESS_ALLOW_IPS 만).
# 바깥 클라이언트가 앱 포트로 직접 들어오는 구성(BIND_HOST=0.0.0.0)에서는 API 의 IP 차단을 걸 수 없다(들어오는 연결도 막히므로).
egress_block() {   # $1 = api|worker|backup
    [ "$EGRESS_PROXY" = "1" ] || return 0
    [ "$1" = "backup" ] && return 0                       # 백업은 외부 보관(BACKUP_OFFSITE_CMD)이 있을 수 있다 — 데이터는 암호문뿐
    local np="localhost,127.0.0.1,::1"
    echo "Environment=HTTP_PROXY=$PROXY_URL HTTPS_PROXY=$PROXY_URL http_proxy=$PROXY_URL https_proxy=$PROXY_URL NO_PROXY=$np no_proxy=$np"
    if [ "$1" = "api" ] && ! is_loopback "$(bind_host)"; then return 0; fi
    echo "IPAddressDeny=any"
    echo "IPAddressAllow=localhost${EGRESS_ALLOW_IPS:+ $EGRESS_ALLOW_IPS}"
}

py_prefix() { "$PY" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true; }

# 스케줄러의 가용성 감시(합성 모니터링) 주소 — API 가 실제로 듣는 포트로(.env 에 UPTIME_URL 이 있으면 그 값을 쓴다)
uptime_block() {
    [ -z "$(env_value UPTIME_URL)" ] || return 0
    local h; h="$(bind_host)"
    case "$h" in 0.0.0.0|::|localhost|127.*|::1) h=127.0.0.1 ;; *:*) h="[$h]" ;; esac
    echo "Environment=UPTIME_URL=http://$h:$PORT/health"
}

# 상태·캐시 폴더(/var/lib/outofstock · /var/cache/outofstock) — 샌드박스와 무관하게 항상. 감사 증인 파일(AUDIT_WITNESS_FILE)의 기본 위치
dirs_block() {
    cat <<EOF
StateDirectory=outofstock
StateDirectoryMode=0700
CacheDirectory=outofstock
CacheDirectoryMode=0700
Environment=XDG_CACHE_HOME=%C/outofstock
EOF
}

settings_block() {   # update 때 다시 읽는 배포 선택값(위 unit_setting)
    echo "# oos: PORT=$PORT"
    echo "# oos: WORKERS=$WORKERS"
    echo "# oos: BIND_HOST=$BIND_HOST"
    echo "# oos: HARDEN=$HARDEN"
    echo "# oos: EGRESS_PROXY=$EGRESS_PROXY"
    [ -z "$EGRESS_ALLOW_IPS" ] || echo "# oos: EGRESS_ALLOW_IPS=$EGRESS_ALLOW_IPS"
    [ -z "$BACKUP_RW_PATHS" ] || echo "# oos: BACKUP_RW_PATHS=$BACKUP_RW_PATHS"
}

check_unit_inputs() {
    local p
    for p in "$APP_DIR" "$BACKUP_DIR" $BACKUP_RW_PATHS; do
        case "$p" in *[[:space:]]*|*%*|*\\*) die "경로에 공백·%·\\ 가 있으면 서비스 파일에 쓸 수 없습니다: $p" ;; esac
    done
    case "$HARDEN$EGRESS_PROXY" in 00|01|10|11) ;; *) die "HARDEN·EGRESS_PROXY 는 0 또는 1" ;; esac
    [[ "$PORT" =~ ^[0-9]+$ && "$WORKERS" =~ ^[0-9]+$ ]] || die "PORT·WORKERS 는 숫자"
    local host; host="$(bind_host)"
    [[ "$host" =~ ^[0-9a-fA-F.:]+$ || "$host" == localhost ]] || die "BIND_HOST 는 IP 주소(예: 127.0.0.1) 또는 auto"
    if [ "$(env_value ADMIN_REQUIRE_CLIENT_CERT)" = "true" ] && ! is_loopback "$host"; then
        warn "ADMIN_REQUIRE_CLIENT_CERT=true 인데 앱이 바깥에 직접 열려 있습니다($host:$PORT) — 인증서 확인 헤더를 위조할 수 있으니"
        warn "  nginx(deploy/nginx.conf) 뒤에서 BIND_HOST=127.0.0.1 로 두세요"
    fi
    if [ "$EGRESS_PROXY" = "1" ] && [ -z "$EGRESS_ALLOW_IPS" ]; then
        local dbh; dbh="$(url_part "$(db_url)" host 2>/dev/null || true)"
        case "${dbh:-localhost}" in localhost|127.*|::1|/*) ;;
            *) die "DB 가 다른 서버($dbh)입니다 — EGRESS_PROXY=1 이면 직접 연결이 막히므로 EGRESS_ALLOW_IPS='<DB IP>/32' 를 함께 주세요" ;;
        esac
    fi
}

write_units() {
    check_unit_inputs
    mkdir -p "$APP_DIR/var" "$BACKUP_DIR" && chmod 700 "$APP_DIR/var" "$BACKUP_DIR"   # 샌드박스가 쓰기를 열어 줄 폴더는 미리 있어야 한다
    local tmp host API_TIMEOUT; tmp="$(mktemp -d)"; host="$(bind_host)"
    API_TIMEOUT="$(env_value API_STATEMENT_TIMEOUT_MS)"; API_TIMEOUT="${API_TIMEOUT:-5000}"   # 한 쿼리 시간 제한 — API 에만(배치는 끊지 않는다)
    [[ "$API_TIMEOUT" =~ ^[0-9]+$ ]] || die ".env 의 API_STATEMENT_TIMEOUT_MS 는 숫자(밀리초)"
    cat > "$tmp/outofstock-api.service" <<EOF
[Unit]
Description=OUTOFSTOCK API (FastAPI / uvicorn)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service
$(settings_block)

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/.venv/bin/uvicorn app.api:app --host $host --port $PORT --workers $WORKERS --timeout-keep-alive 65 --no-server-header
Restart=always
RestartSec=3
LimitNOFILE=65536
Environment=DB_STATEMENT_TIMEOUT_MS=$API_TIMEOUT
$(dirs_block)
$(sandbox_block service)
$(egress_block api)

[Install]
WantedBy=multi-user.target
EOF
    cat > "$tmp/outofstock-scheduler.service" <<EOF
[Unit]
Description=OUTOFSTOCK 스케줄러 (매일 23:00 run.py real · 08:40 KRX · 15:45 KIS 종가 · 07:30 해외)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
$(uptime_block)
ExecStart=$APP_DIR/.venv/bin/python scheduler.py
Restart=always
RestartSec=10
$(dirs_block)
$(sandbox_block service)
$(egress_block worker)

[Install]
WantedBy=multi-user.target
EOF
    cat > "$tmp/outofstock-realtime.service" <<EOF
[Unit]
Description=OUTOFSTOCK 실시간 시세 (국내 KIS + 해외 Finnhub — .env 에 키가 있는 쪽만)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/.venv/bin/python run.py realtime
Restart=on-failure
RestartSec=10
$(dirs_block)
$(sandbox_block service)
$(egress_block worker)

[Install]
WantedBy=multi-user.target
EOF
    cat > "$tmp/outofstock-backup.service" <<EOF
[Unit]
Description=OUTOFSTOCK DB 백업 (일관 스냅샷 + age 암호화 + manifest — 최근 $BACKUP_KEEP 개 + 월별)
After=postgresql.service

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment="BACKUP_DIR=$BACKUP_DIR" "BACKUP_KEEP=$BACKUP_KEEP" "PORT=$PORT" PYTHONDONTWRITEBYTECODE=1
ExecStart=/bin/bash $APP_DIR/deploy/vm.sh backup
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
$(dirs_block)
$(sandbox_block backup)
$(egress_block backup)
EOF
    cat > "$tmp/outofstock-backup.timer" <<EOF
[Unit]
Description=OUTOFSTOCK DB 매일 백업 (한국시간 04:30)

[Timer]
OnCalendar=*-*-* 04:30:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
EOF
    sed -i '/^$/N;/^\n$/D' "$tmp"/outofstock-*          # 빈 블록이 남긴 연속 빈 줄 정리
    if [ "${DRY_UNITS:-}" = "1" ]; then                # 검증용: 설치하지 않고 내용만 남김(DRY_UNITS_DIR, 기본 deploy/generated)
        local gen="${DRY_UNITS_DIR:-$APP_DIR/deploy/generated}"
        mkdir -p "$gen" && cp "$tmp"/* "$gen/"; rm -rf "$tmp"; return 0
    fi
    $SUDO install -m 644 "$tmp"/outofstock-* "$UNIT_DIR/"
    rm -rf "$tmp"
    $SUDO systemctl daemon-reload
    say "서비스: 앱 $host:$PORT(워커 $WORKERS) · 샌드박스 $([ "$HARDEN" = 1 ] && echo 켬 || echo 끔) · egress 프록시 $([ "$EGRESS_PROXY" = 1 ] && echo 켬 || echo 끔)"
    if [ "$EGRESS_PROXY" = 1 ] && ! is_loopback "$host"; then
        warn "앱이 바깥에 직접 열려 있어(BIND_HOST=$host) API 서비스에는 직접 연결 차단을 걸지 못했습니다 — nginx 뒤 BIND_HOST=127.0.0.1 권장"
    fi
}

start_services() {
    if ! has_systemd || ! units_installed; then
        warn "systemd 서비스가 없어 재시작을 건너뜀 — tmux 로 돌리고 있다면 각 창에서 Ctrl+C 후 다시 실행하세요"
        return 0
    fi
    if port_busy_by_other; then
        die "포트 $PORT 를 다른 프로세스(tmux 의 uvicorn?)가 쓰고 있습니다 — 끈 뒤 다시: bash deploy/vm.sh restart"
    fi
    # 짧은 시간에 여러 번 재시작하면(키가 없어 바로 끝나는 realtime 등) systemd 의 시작 횟수 제한에 걸린다 → 먼저 풀어 준다
    $SUDO systemctl reset-failed "${SERVICES[@]}" >/dev/null 2>&1 || true
    $SUDO systemctl enable "${SERVICES[@]}" >/dev/null 2>&1 || true
    $SUDO systemctl enable --now outofstock-backup.timer >/dev/null 2>&1 || true
    local s
    for s in "${SERVICES[@]}"; do
        $SUDO systemctl restart "$s" || warn "$s 시작 실패 — bash deploy/vm.sh logs ${s#outofstock-}"
    done
}

stop_services() {
    if has_systemd && units_installed; then
        $SUDO systemctl stop outofstock-api outofstock-scheduler outofstock-realtime 2>/dev/null || true
    fi
}

check_up() {
    say "헬스체크(최대 60초)…"
    if health 60; then
        local ip url; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; ip="${ip:-<서버주소>}"
        if is_loopback "$(bind_host)" && [ -e "$NGINX_SITE" ]; then
            if grep -qs 'listen 443' "$NGINX_SITE"; then url="https://$ip/ (nginx 경계)"; else url="http://$ip/ (nginx 경계)"; fi
        elif is_loopback "$(bind_host)"; then
            url="http://127.0.0.1:$PORT/ (이 서버 안에서만 — BIND_HOST=$(bind_host))"
        else
            url="http://$ip:$PORT/"
        fi
        ok "API 정상 — $url"
    else
        warn "API 가 응답하지 않습니다. 로그:  bash deploy/vm.sh logs api"
        if has_systemd && units_installed; then $SUDO journalctl -u outofstock-api -n 30 --no-pager || true; fi
        return 1
    fi
}

# 파일 무결성 기준선 — 배포가 끝난 바로 그 파일들이 '정상'(app/security/integrity.py). 이후 바뀌거나 새로 생긴 파일
# (웹셸·손으로 고친 코드)은 서비스가 10분마다 찾아 경보한다. 실패해도 배포는 계속(경고만) — 기준선이 없으면 감시를 건너뛴다.
# $2 = 설치본·스냅샷이 가져온 파일 목록(있으면 그 파일만 기준선에 — 배포 전부터 있던 낯선 파일은 계속 경보)
record_integrity_baseline() {
    say "파일 무결성 기준선 기록(run.py integrity-baseline)"
    local only=()
    if [ -n "${2:-}" ] && [ -s "$2" ] && grep -q -- '--only-listed' "$APP_DIR/run.py" 2>/dev/null; then only=(--only-listed "$2"); fi
    run_app run.py integrity-baseline --release "${1:-manual}" "${only[@]}" \
        || warn "기준선 기록 실패 — 나중에: .venv/bin/python run.py integrity-baseline"
}

# 변경관리(IT 내부통제) — 지금 배포된 코드가 새 설치본을 확인한다: 승인 여부·배포 금지 시간·자동 사전 점검(구문·적용된
# 마이그레이션·정책 형식). CHANGE_GATE=record(기본)는 막지 않고 기록, enforce 는 승인된 설치본만(app/services/changes.py).
# 긴급 배포는 CHANGE_REASON='무엇이 왜 급한지'(10자 이상) — 3일 안에 다른 관리자가 사후 검토.
change_gate() {   # $1 = 설치본 → CHANGE_ID(전역)
    CHANGE_ID=""
    grep -q '"change-gate"' "$APP_DIR/run.py" 2>/dev/null || return 0      # 변경관리 이전 버전에서 올라오는 첫 업데이트
    say "변경관리 확인(run.py change-gate — 승인·배포 금지 시간·자동 사전 점검)"
    local out rc=0
    out="$(run_app run.py change-gate "$1" --by "$(id -un)" ${CHANGE_REASON:+--emergency "$CHANGE_REASON"} 2>&1)" || rc=$?
    printf '%s\n' "$out" | grep -v '^CHANGE_ID=' | sed 's/^/    /' || true
    [ "$rc" -eq 0 ] || die "변경관리 확인에서 멈췄습니다(서비스는 그대로) — 승인을 받거나, 긴급이면 CHANGE_REASON='사유' 를 붙여 다시"
    CHANGE_ID="$(printf '%s\n' "$out" | sed -n 's/^CHANGE_ID=//p' | tail -1 | tr -dc 0-9)"
}

change_record() {   # $1 = applied|failed — 배포 결과(무결성 기준선 번호와 함께)
    [ -n "${CHANGE_ID:-}" ] && grep -q '"change-applied"' "$APP_DIR/run.py" 2>/dev/null || return 0
    local flag=(); [ "$1" = "failed" ] && flag=(--failed)
    run_app run.py change-applied "$CHANGE_ID" --by "$(id -un)" "${flag[@]}" || warn "변경 기록 실패(배포 자체는 계속)"
}

# ── DB 서버 설정 자동 튜닝(ADR-0027) — install·update 때 ─────────────────────────────────────
#   서버 메모리·CPU·PostgreSQL 버전으로 계산한 권장값(app/services/dbtune.py — run.py db-tune-plan)을 ALTER SYSTEM 으로.
#   · 같은 서버의 PostgreSQL 일 때만(DATABASE_URL 이 localhost·127.0.0.1·소켓) · .env 에 DB_AUTOTUNE=off 면 건너뜀
#   · 바뀐 값만 적용 · 재시작이 필요한 값(shared_buffers·shared_preload_libraries)이 바뀌면 PostgreSQL 을 한 번 재시작
#     (update 는 서비스가 멈춘 동안) — 다시 뜨지 않으면 바꾸기 전 postgresql.auto.conf 로 되돌리고 다시 띄운다
#   · 되돌리기: bash deploy/vm.sh db-tune --reset (우리가 넣은 값만 RESET)
db_tune() {
    local mode="${1:-apply}"
    if [ "$(env_value DB_AUTOTUNE)" = "off" ] && [ "$mode" = "apply" ]; then ok "DB 자동 튜닝 꺼짐(DB_AUTOTUNE=off)"; return 0; fi
    local url host; url="$(db_url)"; host="$(url_part "$url" host 2>/dev/null || true)"
    case "$host" in ""|localhost|127.0.0.1|::1|/*) ;; *) ok "DB 가 다른 서버($host) — 자동 튜닝 건너뜀(그 서버에서 직접)"; return 0 ;; esac
    if ! as_postgres psql -tAc "SELECT 1" >/dev/null 2>&1; then warn "postgres 관리자 계정으로 접속 불가 — 자동 튜닝 건너뜀"; return 0; fi
    local ram cpus pgv preload plan datadir bak name val rs cur changed=() restart=0
    ram=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 4194304) / 1024 ))
    cpus="$(nproc 2>/dev/null || echo 2)"
    pgv="$(as_postgres psql -tAc "SHOW server_version_num" | tr -dc 0-9)"; pgv="$(( ${pgv:-160000} / 10000 ))"
    preload="$(as_postgres psql -tAc "SHOW shared_preload_libraries" | tr -d ' ')"
    plan="$(cd "$APP_DIR" && "$PY" run.py db-tune-plan --ram-mb "$ram" --cpus "$cpus" --pg "$pgv" --preload "$preload" 2>/dev/null)" \
        || { warn "DB 튜닝 계산 실패 — 건너뜀"; return 0; }
    datadir="$(as_postgres psql -tAc "SHOW data_directory")"
    if [ "$mode" = "reset" ]; then
        while IFS=$'\t' read -r name val rs; do
            [ -n "$name" ] || continue
            as_postgres psql -qc "ALTER SYSTEM RESET $name" >/dev/null && [ "$rs" = "1" ] && restart=1
        done <<< "$plan"
        as_postgres psql -tAc "SELECT pg_reload_conf()" >/dev/null
        [ "$restart" = "1" ] && has_systemd && $SUDO systemctl restart postgresql && wait_pg
        ok "DB 설정을 기본값으로 되돌림(우리가 넣은 값만)"; return 0
    fi
    bak="$datadir/postgresql.auto.conf.oos-$(date +%Y%m%d%H%M%S)"
    while IFS=$'\t' read -r name val rs; do
        [ -n "$name" ] || continue
        cur="$(as_postgres psql -tAc "SELECT current_setting('$name', true)" 2>/dev/null | tr -d ' ')"
        [ "$cur" = "$(echo "$val" | tr -d ' ')" ] && continue
        if [ "$mode" = "plan" ]; then echo "    $name: $cur → $val$([ "$rs" = "1" ] && echo ' (재시작)')"; continue; fi
        [ "${#changed[@]}" -gt 0 ] || as_postgres cp "$datadir/postgresql.auto.conf" "$bak" 2>/dev/null || true
        if as_postgres psql -v ON_ERROR_STOP=1 -qc "ALTER SYSTEM SET $name = '$val'" >/dev/null 2>&1; then
            changed+=("$name=$val"); [ "$rs" = "1" ] && restart=1
        else
            warn "설정 거부(이 서버에서 지원 안 함?): $name=$val"
        fi
    done <<< "$plan"
    [ "$mode" = "plan" ] && return 0
    if [ "${#changed[@]}" -eq 0 ]; then ok "DB 설정 — 권장값 그대로(바꿀 것 없음 · 메모리 ${ram}MB · CPU $cpus · PG$pgv)"; ensure_pg_stat_statements; return 0; fi
    as_postgres psql -tAc "SELECT pg_reload_conf()" >/dev/null
    say "DB 설정 ${#changed[@]}개 적용(메모리 ${ram}MB · CPU $cpus · PG$pgv): ${changed[*]}"
    if [ "$restart" = "1" ] && has_systemd; then
        say "PostgreSQL 재시작(공유 버퍼·확장 모듈은 재시작해야 적용)"
        if ! { $SUDO systemctl restart postgresql && wait_pg; }; then
            warn "PostgreSQL 이 새 설정으로 뜨지 않음 — 바꾸기 전 설정으로 되돌립니다"
            [ -f "$bak" ] && as_postgres cp "$bak" "$datadir/postgresql.auto.conf"
            $SUDO systemctl restart postgresql && wait_pg || die "PostgreSQL 이 뜨지 않습니다 — sudo journalctl -u postgresql 확인"
            return 1
        fi
    elif [ "$restart" = "1" ]; then
        warn "재시작이 필요한 설정이 있습니다(shared_buffers 등) — PostgreSQL 을 직접 재시작하세요"
    fi
    ensure_pg_stat_statements
}
wait_pg() { local _; for _ in $(seq 1 30); do as_postgres psql -tAc "SELECT 1" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
ensure_pg_stat_statements() {    # 느린 쿼리 통계 — 모듈이 적재된 뒤에만(재시작 전이면 다음 update 에서)
    local db; db="$(url_part "$(db_url)" database 2>/dev/null || echo outofstock)"
    if as_postgres psql -tAc "SHOW shared_preload_libraries" | grep -q pg_stat_statements; then
        as_postgres psql -d "$db" -qc "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" >/dev/null 2>&1 \
            && ok "느린 쿼리 통계(pg_stat_statements) 켜짐" || warn "pg_stat_statements 확장 생성 실패"
    fi
}

cmd_install() {
    [ -f "$APP_DIR/app/api.py" ] || die "stock-backend 폴더 구조가 아닙니다($APP_DIR)"
    [[ "$APP_DIR" != *" "* ]] || die "설치 경로에 공백이 있으면 서비스가 못 뜹니다 — 공백 없는 곳으로 옮기세요($APP_DIR)"
    grep -qi ubuntu /etc/os-release 2>/dev/null || warn "우분투가 아닌 것 같습니다 — apt 단계가 실패할 수 있음"
    ensure_packages
    ensure_env
    ensure_venv
    ensure_database
    say "스키마 생성/마이그레이션(run.py initdb)"
    run_app run.py initdb
    db_tune || warn "DB 자동 튜닝 실패(서비스는 기본 설정으로 동작)"
    record_integrity_baseline "install"
    mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
    if has_systemd; then
        ensure_egress_proxy
        say "systemd 서비스 등록(api·scheduler·realtime·매일 백업) — 계정 $APP_USER, 포트 $PORT"
        write_units
        start_services
        check_up || true
    else
        warn "systemd 가 없는 환경 — 서비스 등록은 건너뜀(tmux 로 직접 실행)"
    fi
    if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
        if is_loopback "$(bind_host)"; then
            say "방화벽(ufw): 앱 포트 $PORT 는 열지 않음(nginx 뒤 — 80/443 만)"
        else
            $SUDO ufw allow "$PORT/tcp" >/dev/null && say "방화벽(ufw) $PORT/tcp 허용"
        fi
    fi
    ok "설치 완료. 처음이면 데이터 적재:  .venv/bin/python run.py real   (키가 없으면 표본 — 미리보기는 run.py demo)"
    echo "    상태 보기: bash deploy/vm.sh status   ·   새 버전: bash deploy/vm.sh update ~/install_outofstock_full.sh"
    echo "    서버 보안 기본값(방화벽·SSH·자동 업데이트): bash deploy/vm.sh harden"
}

# ── 나가는 연결 허용 목록 프록시(같은 서버의 squid) ─────────────────────
# 허용 목록은 app/security/egress.py 한 곳 — 앱 안의 검사와 같은 목록으로 네트워크 계층에서 한 번 더(다층 방어).
# 목록의 이름이라도 사설·루프백·메타데이터 주소로 풀리면 거부(DNS rebinding). 프록시는 127.0.0.1 에서만 듣는다.
ensure_egress_proxy() {
    [ "$EGRESS_PROXY" = "1" ] || return 0
    has_systemd || die "EGRESS_PROXY=1 은 systemd 가 있는 서버에서만(서비스별 직접 연결 차단을 systemd 가 건다)"
    if ! command -v squid >/dev/null 2>&1; then
        say "squid 설치(나가는 연결 허용 목록 프록시)"
        $SUDO apt-get install -y squid >/dev/null
    fi
    local tmp; tmp="$(mktemp)"
    run_app tools/egress.py --squid --vm --listen 127.0.0.1:3128 > "$tmp" || { rm -f "$tmp"; die "프록시 설정 생성 실패(tools/egress.py)"; }
    if ! $SUDO squid -k parse -f "$tmp" >/dev/null 2>&1; then
        $SUDO squid -k parse -f "$tmp" 2>&1 | grep -iE 'error|fatal|bungled' | head -5 >&2 || true
        rm -f "$tmp"; die "squid 설정 검사 실패 — 운영 중인 설정은 그대로"
    fi
    local updated=0                                          # (db_tune 의 배열 changed 와 이름을 나눈다 — shellcheck SC2178)
    if ! $SUDO cmp -s "$tmp" "$SQUID_CONF"; then
        if [ -f "$SQUID_CONF" ] && [ ! -f "$SQUID_CONF.dist" ] && ! grep -qs 'OUTOFSTOCK egress' "$SQUID_CONF"; then
            $SUDO cp -p "$SQUID_CONF" "$SQUID_CONF.dist"     # 배포판 기본 설정 보관(한 번만)
        fi
        $SUDO install -m 644 "$tmp" "$SQUID_CONF"
        updated=1
    fi
    rm -f "$tmp"
    $SUDO systemctl enable squid >/dev/null 2>&1 || true
    if [ "$updated" = 1 ] || ! systemctl is-active --quiet squid; then
        $SUDO systemctl restart squid || die "squid 기동 실패 — sudo journalctl -u squid -n 30"
    fi
    local _ code=""
    for _ in $(seq 1 15); do                               # 확인: 허용 목록 밖은 프록시가 거부(403)해야 한다
        code="$(curl -s -o /dev/null -m 5 -w '%{http_connect}' -x "$PROXY_URL" https://example.com 2>/dev/null || true)"
        [ "$code" = "403" ] && break
        sleep 1
    done
    [ "$code" = "403" ] || die "egress 프록시 확인 실패(허용 목록 밖 요청의 응답 $code, 기대 403) — sudo journalctl -u squid -n 30"
    ok "egress 프록시 동작 — 허용 목록 $(grep -c '' <(run_app tools/egress.py)) 개 호스트만, 그 밖은 거부($([ "$updated" = 1 ] && echo '설정 갱신' || echo '변경 없음'))"
}

cmd_units() {
    has_systemd || die "systemd 가 없는 환경"
    ensure_egress_proxy
    write_units
    start_services
    check_up
}

# ── 서버 기본 보안(harden) — 되돌리기 쉬운 설정만, 바꾼 파일을 끝에 보여 준다 ─────────
# 근거: 주요정보통신기반시설 기술적 취약점 분석·평가 가이드(유닉스 서버: root 원격 접속 제한·로그인 시도 제한·불필요 기능 끄기·
#       보안 패치), 전자금융감독규정 제15조(해킹 등 방지 — 침입 차단·탐지), CIS Ubuntu Benchmark 의 네트워크 커널 설정.
HARDEN_FILES=()

ufw_q() { $SUDO ufw "$@" >/dev/null 2>&1; }

harden_firewall() {
    if ! command -v ufw >/dev/null 2>&1; then $SUDO apt-get install -y ufw >/dev/null || return 1; fi
    local ssh_port client
    ssh_port="$($SUDO sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}')"; ssh_port="${ssh_port:-22}"
    if [ -n "${ADMIN_CIDR:-}" ]; then
        client="${SSH_CONNECTION%% *}"                     # 지금 접속한 주소가 대역 밖이면 스스로를 잠그게 된다 → 아무것도 바꾸지 않고 중단
        if [ -n "$client" ] && ! python3 -c 'import ipaddress,sys; sys.exit(ipaddress.ip_address(sys.argv[1]) not in ipaddress.ip_network(sys.argv[2], strict=False))' "$client" "$ADMIN_CIDR"; then
            die "지금 SSH 접속 주소($client)가 ADMIN_CIDR($ADMIN_CIDR) 밖입니다 — 이대로면 접속이 끊긴 뒤 다시 못 들어옵니다"
        fi
    fi
    ufw_q default deny incoming || return 1
    ufw_q default allow outgoing || return 1
    if [ -n "${ADMIN_CIDR:-}" ]; then
        ufw_q allow from "$ADMIN_CIDR" to any port "$ssh_port" proto tcp comment 'outofstock ssh(admin)' || return 1
    else
        ufw_q limit "$ssh_port/tcp" comment 'outofstock ssh' || return 1     # 30초에 6번 넘는 새 접속은 차단(무차별 대입 완화)
    fi
    if [ -e "$NGINX_SITE" ]; then
        ufw_q allow 80/tcp comment 'outofstock http->https' || return 1
        ufw_q allow 443/tcp comment 'outofstock https' || return 1
        if is_loopback "$(bind_host)"; then ufw_q delete allow "$PORT/tcp" || true; fi
    else
        ufw_q allow "$PORT/tcp" comment 'outofstock app(nginx 없음)' || return 1
    fi
    ufw_q --force enable || return 1
    ok "방화벽: 들어오는 연결 기본 거부 · SSH($([ -n "${ADMIN_CIDR:-}" ] && echo "$ADMIN_CIDR 만" || echo '시도 횟수 제한')) · $([ -e "$NGINX_SITE" ] && echo '80/443' || echo "$PORT") 만 허용"
    if command -v docker >/dev/null 2>&1; then
        warn "Docker 가 공개(-p)한 포트는 ufw 를 거치지 않습니다 — compose 를 함께 쓰면 공개 포트를 edge 하나로(docker-compose.prod.yml)"
    fi
}

harden_ssh() {
    command -v sshd >/dev/null 2>&1 || { say "SSH 서버 없음 — 건너뜀"; return 0; }
    local f=/etc/ssh/sshd_config.d/10-outofstock-hardening.conf tmp
    tmp="$(mktemp)"
    {
        echo "# OUTOFSTOCK 서버 보안 기본값(bash deploy/vm.sh harden) — sshd 는 먼저 읽은 값을 쓰므로 10- 으로 앞에 둔다"
        if [ "$APP_USER" != "root" ]; then echo "PermitRootLogin no"; fi
        echo "MaxAuthTries 3"
        echo "LoginGraceTime 30"
        echo "PermitEmptyPasswords no"
        echo "X11Forwarding no"
        echo "AllowAgentForwarding no"
        echo "ClientAliveInterval 300"
        echo "ClientAliveCountMax 2"
        if [ "${SSH_KEYS_ONLY:-}" = "1" ]; then
            [ -s "$APP_HOME/.ssh/authorized_keys" ] || die "SSH_KEYS_ONLY=1 인데 $APP_HOME/.ssh/authorized_keys 가 비어 있습니다 — 키부터 등록(ssh-copy-id)"
            echo "PasswordAuthentication no"
            echo "KbdInteractiveAuthentication no"
        fi
    } > "$tmp"
    $SUDO install -m 644 "$tmp" "$f" || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
    $SUDO install -d -m 0755 /run/sshd                  # 검사에 필요한 폴더(우분투 24.04 는 첫 접속 때 만들어져 아직 없을 수 있다)
    if ! $SUDO sshd -t 2>/dev/null; then
        $SUDO rm -f "$f"; warn "sshd 설정 검사 실패 — 추가한 파일을 지우고 원래대로 두었습니다"; return 1
    fi
    $SUDO systemctl try-reload-or-restart ssh 2>/dev/null || $SUDO systemctl try-reload-or-restart sshd 2>/dev/null || true
    HARDEN_FILES+=("$f")
    ok "SSH: root 원격 로그인 금지 · 인증 3회 · 전달 기능 끔$([ "${SSH_KEYS_ONLY:-}" = 1 ] && echo ' · 비밀번호 로그인 끔(키만)')"
    [ "${SSH_KEYS_ONLY:-}" = "1" ] || say "  키 로그인을 등록했다면 SSH_KEYS_ONLY=1 bash deploy/vm.sh harden 으로 비밀번호 로그인까지 끌 것"
}

harden_updates() {
    if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then $SUDO apt-get install -y unattended-upgrades >/dev/null || return 1; fi
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        | $SUDO tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null || return 1
    HARDEN_FILES+=(/etc/apt/apt.conf.d/20auto-upgrades)
    ok "보안 업데이트 자동 설치(우분투 기본 정책 — 보안 저장소만, 자동 재부팅 없음)"
}

harden_sysctl() {
    local f=/etc/sysctl.d/60-outofstock-hardening.conf
    $SUDO tee "$f" >/dev/null <<'EOF' || return 1
# OUTOFSTOCK 서버 보안 기본값(bash deploy/vm.sh harden) — 커널 주소·로그 노출 줄이기, 위조 경로·리다이렉트 무시
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
fs.protected_symlinks = 1
fs.protected_hardlinks = 1
fs.suid_dumpable = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.conf.all.log_martians = 1
EOF
    $SUDO sysctl -q -p "$f" 2>/dev/null || warn "일부 커널 설정을 적용하지 못했습니다(컨테이너형 VM 일 수 있음) — 재부팅 뒤 적용"
    HARDEN_FILES+=("$f")
    ok "커널: 주소·로그 노출 제한 · ICMP 리다이렉트·소스 라우팅 무시 · SYN 쿠키"
}

harden_postgres() {
    if ! as_postgres psql -tAc "SELECT 1" >/dev/null 2>&1; then warn "PostgreSQL 에 관리자로 접속할 수 없어 건너뜀"; return 0; fi
    local la weak
    la="$(as_postgres psql -tAc 'SHOW listen_addresses' | tr -d ' ')"
    case "$la" in
        localhost|127.0.0.1|localhost,::1|127.0.0.1,::1|::1,127.0.0.1) ok "PostgreSQL: 이 서버 안에서만 접속(listen_addresses=$la)" ;;
        *) warn "PostgreSQL 이 바깥 주소도 듣습니다(listen_addresses=$la) — 다른 서버의 앱이 붙는 구조가 아니면 localhost 로" ;;
    esac
    as_postgres psql -v ON_ERROR_STOP=1 -q \
        -c "ALTER SYSTEM SET password_encryption = 'scram-sha-256'" \
        -c "ALTER SYSTEM SET log_connections = on" \
        -c "ALTER SYSTEM SET log_disconnections = on" \
        -c "SELECT pg_reload_conf()" >/dev/null || return 1
    weak="$(as_postgres psql -tAc "SELECT count(*) FROM pg_hba_file_rules WHERE type LIKE 'host%' AND auth_method IN ('trust','password','md5')" 2>/dev/null | tr -d ' ')"
    ok "PostgreSQL: 접속·종료 기록 켬 · 새 비밀번호는 SCRAM-SHA-256"
    [ "${weak:-0}" = "0" ] || warn "pg_hba.conf 에 약한 원격 인증(trust/password/md5) 규칙 $weak 개 — scram-sha-256 으로 바꾸고 해당 계정 비밀번호를 다시 설정"
}

cmd_harden() {
    has_systemd || die "systemd 가 있는 우분투 서버에서만"
    say "서버 기본 보안 — 방화벽 · SSH · 자동 보안 업데이트 · 커널 설정 · DB 접속 기록"
    local fail=0
    harden_firewall || { warn "방화벽(ufw) 설정 실패 — 클라우드·하이퍼바이저의 보안 그룹에서 같은 규칙(SSH·80/443 만)을 거세요"; fail=1; }
    harden_ssh      || { warn "SSH 설정 실패"; fail=1; }
    harden_updates  || { warn "자동 보안 업데이트 설정 실패(apt)"; fail=1; }
    harden_sysctl   || { warn "커널 설정 실패"; fail=1; }
    harden_postgres || { warn "PostgreSQL 설정 실패"; fail=1; }
    if [ "$HARDEN" != "1" ]; then warn "서비스 샌드박스가 꺼져 있습니다 — HARDEN=1 bash deploy/vm.sh units"; fi
    if [ "$EGRESS_PROXY" != "1" ]; then say "나가는 연결까지 허용 목록으로 묶으려면: EGRESS_PROXY=1 bash deploy/vm.sh units"; fi
    if [ ! -e "$NGINX_SITE" ]; then say "TLS·관리 기기 인증(mTLS) 경계: deploy/nginx.conf 머리말 순서대로 설치 → bash deploy/vm.sh units(앱은 127.0.0.1 로)"; fi
    if [ "${#HARDEN_FILES[@]}" -gt 0 ]; then
        echo "  바꾼 파일(지우면 되돌아감): ${HARDEN_FILES[*]}"
    fi
    return "$fail"
}

# ── 업데이트 / 되돌리기 ───────────────────────────────────────────────
find_installer() {
    local f="${1:-}"
    if [ -z "$f" ]; then
        for c in "$APP_HOME/install_outofstock_full.sh" "$APP_DIR/install_outofstock_full.sh"; do
            [ -f "$c" ] && { f="$c"; break; }
        done
    fi
    [ -n "$f" ] && [ -f "$f" ] || die "설치 파일을 찾을 수 없습니다 — bash deploy/vm.sh update <install_outofstock_full.sh 경로>"
    local headtxt; headtxt="$(head -c 4000 "$f")"
    [[ "$headtxt" == *"OUTOFSTOCK 전체 프로젝트 원클릭 설치"* ]] || die "OUTOFSTOCK 설치 파일이 아닙니다: $f"
    readlink -f "$f"
}

# ── 예전 버전에만 있던 파일 정리 — 새 코드를 덮어쓰기(cp)만 하면 삭제된 파일(예: 옛 legacy.html)이 VM 에 남아 계속 서비스된다 ──
MANIFEST_NAME=".release-files"     # 설치한 릴리스의 파일 목록 — 다음 update·rollback 에서 '빠진 파일'을 찾는 기준
# 목록 파일이 생기기 전 버전에서 지운 파일(7차) — 첫 update 때 기준으로 쓴다
REMOVED_BEFORE_MANIFEST="setup_deploy.sh app/static/legacy.html app/pipeline.py app/collectors/prices.py app/services/ranking.py tests/test_universe.py"
# 2026-10 웹 화면 교체(app/static 의 옛 한 장짜리 화면 → web/ 팀 확정 디자인) — 목록 파일이 없는 설치에서도 옛 화면이 남지 않게
REMOVED_BEFORE_MANIFEST="$REMOVED_BEFORE_MANIFEST app/static/index.html app/static/vendor/lightweight-charts.js
 app/static/assets/favicon.png app/static/assets/happy.png app/static/assets/logo-mark.png app/static/assets/logo.png
 app/static/assets/risk-aggressive.png app/static/assets/risk-neutral.png app/static/assets/risk-stable.png
 app/static/assets/sad.png app/static/assets/study.png tests/test_frontend_runtime.py"

# $1 = 남길 파일 목록(상대경로, 줄마다 하나). 예전 릴리스 목록(+위 목록)에 있는데 $1 에 없는 파일만 지운다.
# 사용자가 만든 파일·.env·.venv·deploy/generated 는 릴리스 목록에 없으므로 건드리지 않는다(지운 파일은 이전 코드 tar 에 보관됨).
prune_stale() {
    local keep="$1" label="${2:-이 버전에 없는 예전 파일}" f n=0
    while IFS= read -r f; do
        case "$f" in ""|/*|*..*|.env|.env.*|.venv|.venv/*|"$MANIFEST_NAME") continue ;; esac
        grep -qxF -- "$f" "$keep" && continue
        if [ -f "$APP_DIR/$f" ] || [ -L "$APP_DIR/$f" ]; then
            rm -f -- "$APP_DIR/$f" && n=$((n + 1)) && echo "    - $f"
        fi
    done < <({ if [ -f "$APP_DIR/$MANIFEST_NAME" ]; then cat "$APP_DIR/$MANIFEST_NAME"; fi
               printf '%s\n' $REMOVED_BEFORE_MANIFEST; } | LC_ALL=C sort -u)
    if [ "$n" -gt 0 ]; then ok "$label $n 개 정리"; fi
}

cmd_update() {
    local inst; inst="$(find_installer "${1:-}")"
    UPDATE_TMP="$(mktemp -d)"
    UPDATE_LIST="$(mktemp)"
    trap 'rm -rf "${UPDATE_TMP:-}" "${UPDATE_LIST:-}"' EXIT
    local tmp="$UPDATE_TMP"
    say "새 코드 풀기(임시 폴더에서 먼저 검증): $inst"
    (cd "$tmp" && bash "$inst" >/dev/null) || die "설치 파일 실행 실패 — 파일이 잘렸는지(WinSCP 전송 완료) 확인"
    local want got
    want="$(grep -m1 -o 'OUTOFSTOCK 전체 [0-9]*개 파일' "$inst" | grep -o '[0-9]*' || echo 0)"
    got="$(find "$tmp" -type f | wc -l)"
    [ "$want" -gt 0 ] && [ "$got" -eq "$want" ] || die "파일 수가 맞지 않습니다(기대 $want · 실제 $got) — 설치 파일이 손상된 것 같습니다"
    for f in app/api.py run.py requirements.txt; do [ -s "$tmp/$f" ] || die "필수 파일 없음: $f"; done
    ok "검증 통과 — 파일 $got 개"
    change_gate "$inst"
    export CHANGE_ID

    # 사전 점검 — 서비스를 멈추기 전에. 남의 소유(예: sudo 로 한 번 실행해 root 가 된 파일)면 복사가 중간에 실패해
    # 코드가 반쯤 바뀐 채 서비스가 멈춘다(검증 컨테이너에서 실제로 재현).
    local foreign
    foreign="$(find "$APP_DIR" -path "$APP_DIR/.venv" -prune -o ! -user "$(id -u)" -print 2>/dev/null | head -3)"
    [ -z "$foreign" ] || die "다른 계정 소유 파일이 있어 덮어쓸 수 없습니다(예: ${foreign//$'\n'/, }) — sudo chown -R $(id -un): $APP_DIR 후 다시"

    cmd_backup || { [ "${FORCE:-}" = "1" ] || die "백업 실패로 업데이트를 멈춥니다(무시하려면 FORCE=1)"; }

    mkdir -p "$RELEASE_DIR" && chmod 700 "$RELEASE_DIR"
    local snap; snap="$RELEASE_DIR/prev-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -czf "$snap" -C "$APP_DIR" --exclude=./.venv --exclude=./.env --exclude='*/__pycache__' \
        --exclude=./.pytest_cache --exclude=./.ruff_cache --exclude='./Claude outputs' --exclude=./var/archive .   # 보관 파일은 코드가 아니다(ADR-0027)
    { ls -1t "$RELEASE_DIR"/prev-*.tar.gz 2>/dev/null || true; } | tail -n +4 | xargs -r rm -f      # 최근 3개만
    say "이전 코드 보관: $snap"
    if [ -n "${CHANGE_ID:-}" ]; then echo "$CHANGE_ID" > "$RELEASE_DIR/last-change-id"; else rm -f "$RELEASE_DIR/last-change-id"; fi

    stop_services
    trap 'rm -rf "${UPDATE_TMP:-}" "${UPDATE_LIST:-}"; [ "${UPDATE_DONE:-}" = 1 ] || warn "업데이트가 중간에 멈췄습니다 — 이전 코드로: bash deploy/vm.sh rollback"' EXIT
    (cd "$tmp" && find . -type f -printf '%P\n' | LC_ALL=C sort) > "$UPDATE_LIST"
    prune_stale "$UPDATE_LIST"
    cp -a "$tmp"/. "$APP_DIR"/
    cp "$UPDATE_LIST" "$APP_DIR/$MANIFEST_NAME"
    ensure_venv
    say "마이그레이션(run.py initdb)"
    run_app run.py initdb
    UPDATE_DONE=1                                   # 코드·패키지·스키마 교체 끝 — 이 뒤의 실패는 서비스 기동 문제(되돌리기 안내는 아래)
    rm -rf "${UPDATE_TMP:-}" "${UPDATE_LIST:-}"; trap - EXIT
    if ! has_systemd || ! units_installed; then
        record_integrity_baseline "$(basename "$inst")" "$APP_DIR/$MANIFEST_NAME"
        change_record applied
        ok "코드·패키지·마이그레이션 완료 — tmux 로 돌리는 중이면 각 창(uvicorn·scheduler·realtime)에서 Ctrl+C 후 다시 실행"
        return 0
    fi
    # 서비스 파일·프록시 설정은 '새' vm.sh 가 만든다 — 지금 돌고 있는 건 옛 버전이다(bash 는 함수를 시작할 때 읽어 둔다).
    # (검증 컨테이너에서 확인: 옛 함수가 서비스 파일을 쓰면 새 버전의 설정이 다음 실행 때까지 빠진다)
    exec bash "$APP_DIR/deploy/vm.sh" _post-update
}

post_update() {
    record_integrity_baseline "update" "$APP_DIR/$MANIFEST_NAME"   # 새 vm.sh 에서 — 옛 버전에서 올라오는 첫 업데이트도 기준선이 생기게
    db_tune || warn "DB 자동 튜닝 실패(서비스는 이전 설정으로 동작)"   # 서비스가 멈춘 동안 — 재시작이 필요해도 영향 없음
    ensure_egress_proxy
    write_units
    start_services
    if check_up; then
        change_record applied
        ok "업데이트 완료"
    else
        change_record failed
        warn "새 버전이 뜨지 않으면 되돌리기:  bash deploy/vm.sh rollback"
        exit 1
    fi
}

cmd_rollback() {
    local snap; snap="$(ls -1t "$RELEASE_DIR"/prev-*.tar.gz 2>/dev/null | head -1 || true)"
    [ -n "$snap" ] || die "되돌릴 이전 코드가 없습니다($RELEASE_DIR)"
    say "되돌리기: $snap"
    stop_services
    local cid=""                                   # 이번에 되돌리는 배포의 변경 번호(update 가 남김) — 지금 코드로 '되돌림' 기록
    [ -f "$RELEASE_DIR/last-change-id" ] && cid="$(tr -dc 0-9 < "$RELEASE_DIR/last-change-id")"
    if [ -n "$cid" ] && grep -q '"change-rolled-back"' "$APP_DIR/run.py" 2>/dev/null; then
        run_app run.py change-rolled-back "$cid" --by "$(id -un)" || warn "변경 기록(되돌림) 실패"
    fi
    rm -f "$RELEASE_DIR/last-change-id"
    local keep; keep="$(mktemp)"
    tar -tzf "$snap" | sed 's#^\./##' | LC_ALL=C sort -u > "$keep"
    # 되돌릴 코드가 DB 타입 정리(V018 · ADR-0027) 전 버전인데 DB 는 이미 바뀌었으면 — 코드만 되돌리면 날짜·시각을 못 읽는다.
    # 업데이트 직전 백업(update 가 자동으로 남김)으로 DB 도 함께 되돌려야 한다. 알고 진행하려면 FORCE=1.
    if ! grep -q "sql/migrations/V018__" "$keep" && [ "${FORCE:-}" != "1" ] && \
            psql "$(db_url)" -tAc "SELECT 1 FROM analytics.schema_migrations WHERE version = 18" 2>/dev/null | grep -q 1; then
        rm -f "$keep"
        die "되돌릴 코드는 DB 타입 정리(V018) 전 버전입니다 — 코드만 되돌리면 이전 코드가 새 저장 형식을 못 읽습니다.
    1) bash deploy/vm.sh backups 에서 업데이트 직전 백업을 골라  2) bash deploy/vm.sh restore <그 백업>  3) FORCE=1 bash deploy/vm.sh rollback"
    fi
    prune_stale "$keep" "되돌릴 버전에 없던 파일"   # 이번 update 로 새로 생긴 파일은 지운다
    tar -xzf "$snap" -C "$APP_DIR"
    grep -qxF -- "$MANIFEST_NAME" "$keep" || rm -f "$APP_DIR/$MANIFEST_NAME"   # 목록이 없던 버전으로 돌아가면 목록도 없앰
    ensure_venv
    # 되돌린 파일이 이제 '정상' — 기준선을 다시 기록하지 않으면 무결성 감시가 바뀐 파일 전부를 변조로 경보한다(스냅샷 목록에 있는 것만)
    if grep -q '"integrity-baseline"' "$APP_DIR/run.py" 2>/dev/null; then record_integrity_baseline "rollback" "$keep"; fi
    rm -f "$keep"
    if ! has_systemd || ! units_installed; then
        ok "코드 되돌리기 완료 — tmux 각 창에서 다시 실행(DB 는 그대로 — 데이터를 되돌리려면 restore)"
        return 0
    fi
    start_services
    check_up && ok "되돌리기 완료(DB 는 그대로 — 데이터를 되돌리려면 restore)"
}

# ── 백업 / 복구 ─────────────────────────────────────────────────────
# 백업·검증 로직은 tools/dr.py 하나에 — 이 스크립트는 VM 쪽(서비스 중지·전환·postgres 권한)만 맡는다.
#   · 개인 데이터 표는 FORCE RLS 라 일반 계정의 pg_dump 는 실패한다(2026-10 RLS 도입 뒤 예전 백업이 매일 실패하던 원인).
#     dr.py 가 일관 스냅샷 + 시스템 문맥(또는 BACKUP_DATABASE_URL 의 백업 전용 계정)으로 빠짐없이 덤프한다.
#   · 암호화 키(BACKUP_AGE_RECIPIENTS)가 아직 없으면 평문으로 받되 경고하고, manifest·관리자 헬스에 '평문'으로 남는다.
py_url() {   # py_url <url> <driver|-> <database|-> → 바꾼 URL (비밀번호 포함, 출력은 변수로만 받는다)
    "$PY" - "$@" <<'PYURL'
import sys
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1])
if sys.argv[2] != "-":
    u = u.set(drivername=sys.argv[2])
if sys.argv[3] != "-":
    u = u.set(database=sys.argv[3])
print(u.render_as_string(hide_password=False))
PYURL
}
url_part() { "$PY" -c 'import sys; from sqlalchemy.engine import make_url; print(getattr(make_url(sys.argv[1]), sys.argv[2]) or "")' "$1" "$2"; }

cmd_backup() {
    command -v pg_dump >/dev/null 2>&1 || die "pg_dump 없음 — sudo apt install postgresql-client"
    mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
    local free_kb; free_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR==2{print $4}')"
    [ "${free_kb:-0}" -lt 1048576 ] && warn "백업 폴더 남은 공간이 1GB 미만입니다"
    say "DB 백업 → $BACKUP_DIR"
    if ! (cd "$APP_DIR" && BACKUP_DIR="$BACKUP_DIR" BACKUP_KEEP_DAILY="$BACKUP_KEEP" "$PY" tools/dr.py backup --allow-plaintext); then
        warn "백업 실패(관리자 헬스·알림에 기록됨)"; return 1
    fi
    ok "백업 완료"
}

cmd_backups() { (cd "$APP_DIR" && BACKUP_DIR="$BACKUP_DIR" "$PY" tools/dr.py list); }

cmd_restore() {
    local f="${1:-}"
    [ -n "$f" ] && [ -f "$f" ] || die "사용법: bash deploy/vm.sh restore <백업파일>   (목록: bash deploy/vm.sh backups)"
    command -v pg_restore >/dev/null 2>&1 || die "pg_restore 없음 — sudo apt install postgresql-client"
    local reader=(cat "$f")
    case "$f" in
        *.age)
            command -v age >/dev/null 2>&1 || die "age 없음 — sudo apt install age"
            [ -n "${AGE_IDENTITY:-}" ] && [ -f "$AGE_IDENTITY" ] || \
                die "암호화된 백업 — 오프라인에 보관한 복호화 키로:  AGE_IDENTITY=/키/경로 bash deploy/vm.sh restore $f"
            reader=(age --decrypt -i "$AGE_IDENTITY" "$f") ;;
    esac
    local m="${f%%.dump*}.manifest.json"
    if [ -f "$m" ]; then        # 암호문 SHA-256 → 복호화 → 덤프 SHA-256 → 목록(TOC) — 평문을 디스크에 쓰지 않고 끝까지 읽는다
        local vargs=(verify "$f")
        [ -n "${AGE_IDENTITY:-}" ] && vargs+=(--identity "$AGE_IDENTITY")
        (cd "$APP_DIR" && "$PY" tools/dr.py "${vargs[@]}" >/dev/null) || die "백업을 읽을 수 없습니다(변조·손상 또는 키 불일치): $f"
        ok "무결성 확인(암호문·덤프 SHA-256 = manifest, 복호화 정상)"
    else
        warn "manifest 없음(예전 형식 백업) — 해시 대조·복원 검증 없이 진행합니다"
        pg_restore --list "$f" >/dev/null 2>&1 || die "백업 파일이 아니거나 손상됐습니다: $f"
    fi

    local url db owner ts new old
    url="$(db_url)"; db="$(url_part "$url" database)"; owner="$(url_part "$url" username)"
    ts="$(date +%Y%m%d%H%M%S)"; new="${db}_restore_$ts"; old="${db}_pre_$ts"
    if [ "${YES:-}" != "1" ]; then
        read -r -p "DB 를 $(basename "$f") 시점으로 되돌립니다(그 뒤의 가입·거래 기록은 사라짐 — 지금 DB 는 $old 로 보관). 계속하려면 yes: " a
        [ "$a" = "yes" ] || die "취소"
    fi
    cmd_backup || warn "되돌리기 전 현재 상태 백업 실패(계속 진행)"

    # 1) 새 DB 에 복원 — 운영 DB·서비스는 그대로 돈다. 한 트랜잭션이라 실패하면 아무것도 남지 않는다.
    #    (예전 방식 'pg_restore --clean' 으로 덮어쓰기는 분할 표 제약 삭제에서 실패해 반쯤 복구된 DB 가 남을 수 있었다)
    say "새 DB($new)에 복원 — 서비스는 계속 동작"
    as_postgres psql -v ON_ERROR_STOP=1 -qc "CREATE DATABASE \"$new\" OWNER \"$owner\"" || die "복원용 DB 생성 실패"
    if ! "${reader[@]}" | pg_restore --no-owner --single-transaction --exit-on-error --dbname="$(py_url "$url" - "$new")"; then
        as_postgres psql -qc "DROP DATABASE IF EXISTS \"$new\"" || true
        die "복원 실패 — 운영 DB 는 그대로입니다"
    fi
    # 2) 검증 — manifest 기준값과 행 수·감사 체인·원장·암호화 키·보안 통제가 같은가(읽기만)
    if [ -f "$m" ]; then
        say "복원 검증…"
        if ! (cd "$APP_DIR" && DATABASE_URL="$(py_url "$url" postgresql+psycopg "$new")" MIGRATION_DATABASE_URL='' \
                WARMUP_ON_START=false "$PY" tools/dr.py _verify "$m" --no-write >/dev/null); then
            as_postgres psql -qc "DROP DATABASE IF EXISTS \"$new\"" || true
            die "복원 검증 실패(위 ✗ 항목) — 전환하지 않았습니다. 운영 DB 는 그대로입니다"
        fi
        ok "복원 검증 통과"
    fi
    # 3) 전환 — 서비스를 잠깐 멈추고 이름 두 개를 한 트랜잭션으로 바꾼다(중간 상태 없음)
    stop_services
    say "전환: $db → $old, $new → $db"
    if ! as_postgres psql -v ON_ERROR_STOP=1 -q \
            -c "ALTER DATABASE \"$db\" ALLOW_CONNECTIONS false" \
            -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '$db' AND pid <> pg_backend_pid()" \
            -c "BEGIN; ALTER DATABASE \"$db\" RENAME TO \"$old\"; ALTER DATABASE \"$new\" RENAME TO \"$db\"; COMMIT;" >/dev/null; then
        as_postgres psql -qc "ALTER DATABASE \"$db\" ALLOW_CONNECTIONS true" >/dev/null 2>&1 || true
        start_services
        die "전환 실패 — 운영 DB 는 그대로입니다(복원본 $new 는 남겨 둠)"
    fi
    as_postgres psql -qc "ALTER DATABASE \"$old\" ALLOW_CONNECTIONS true" >/dev/null 2>&1 || true
    start_services
    if has_systemd && units_installed; then check_up; fi
    ok "복구 완료 — 이전 DB 는 $old 로 보관(확인 뒤 삭제: sudo -u postgres dropdb $old)"
    say "DB 권한 분리(deploy/sql/roles.sql)를 쓰고 있다면 한 번 더 실행해 데이터베이스 단위 권한을 맞추세요"
}

cmd_drill() {
    [ -n "${DRILL_ADMIN_URL:-}" ] || die "DRILL_ADMIN_URL 필요 — 데이터베이스를 만들 수 있는 계정(docs/dr/README.md '훈련 계정')"
    local args=()
    [ -n "${1:-}" ] && args+=("$1")
    local reports="$BACKUP_DIR/drills"
    (cd "$APP_DIR" && export BACKUP_DIR && "$PY" tools/dr.py drill "${args[@]}" --report-dir "$reports")
}

# ── 상태 / 로그 ─────────────────────────────────────────────────────
cmd_status() {
    if has_systemd && units_installed; then
        local s
        for s in "${SERVICES[@]}" outofstock-backup.timer; do
            printf '  %-26s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || true)"
        done
        echo "  다음 백업: $(systemctl list-timers outofstock-backup.timer --no-legend 2>/dev/null | awk '{print $1, $2, $3}')"
        echo "  앱 바인딩: $(bind_host):$PORT (워커 $WORKERS)$(is_loopback "$(bind_host)" && echo ' — 바깥 접속은 nginx 경계로만')"
        local score egress
        score="$(systemd-analyze security outofstock-api.service --no-pager 2>/dev/null | sed -n 's/.*Overall exposure level for [^:]*: *\([0-9.]* [A-Z]*\).*/\1/p' | head -1)"
        echo "  샌드박스: $([ "$HARDEN" = 1 ] && echo 켬 || echo 끔)${score:+ · 노출 점수 $score (0~10, 낮을수록 안전)}"
        egress="$(systemctl show -p IPEgressBytes --value outofstock-api 2>/dev/null || true)"
        echo "  나가는 연결: $([ "$EGRESS_PROXY" = 1 ] && echo "허용 목록 프록시만(squid $(systemctl is-active squid 2>/dev/null || true))" || echo '앱 안의 허용 목록 검사(EGRESS_PROXY=0)')$( [[ "$egress" =~ ^[0-9]+$ ]] && echo " · API 송신 $((egress / 1024)) KiB(재시작 뒤)")"
    else
        say "systemd 서비스 없음(tmux 실행 중이면 정상)"
    fi
    local h; h="$(curl -fsS -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
    if [ -n "$h" ]; then
        python3 - "$h" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"  상태 {d.get('status')} · 최신 시세일 {d.get('latest_price_date') or '-'} ({d.get('data_age_days', '-')}일 전) "
      f"· DB {'정상' if d.get('db_ok') else '오류'}")
iss = d.get("issues") or []
print("  이슈: " + (", ".join(i if isinstance(i, str) else str(i.get("code", i)) for i in iss) if iss else "없음"))
lr = d.get("last_run") or {}
if lr:
    print(f"  마지막 배치 {lr.get('status')} · {lr.get('finished_at') or lr.get('started_at')} · {str(lr.get('note') or '')[:80]}")
reco = d.get("reco") or {}
if reco:
    print(f"  추천 스냅샷 기준일 {reco.get('as_of')} · 모형 {reco.get('model_version')}")
PY
    else
        warn "API 응답 없음(http://127.0.0.1:$PORT/health)"
    fi
    df -h "$APP_DIR" | awk 'NR==2{printf "  디스크 %s 중 %s 사용(%s) · 남음 %s\n", $2, $3, $5, $4}'
    psql "$(db_url)" -tAc "SELECT '  DB 크기 ' || pg_size_pretty(pg_database_size(current_database()))" 2>/dev/null || true
    local last; last="$({ ls -1t "$BACKUP_DIR"/oos-*.dump "$BACKUP_DIR"/oos-*.dump.age "$BACKUP_DIR"/outofstock-*.dump 2>/dev/null || true; } | head -1)"
    if [ -n "$last" ]; then echo "  최근 백업 $(basename "$last") ($(du -h "$last" | cut -f1))"; else echo "  최근 백업 없음"; fi
    local sc; sc="$(run_app tools/security_check.py --no-db --json 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("FAIL %s · WARN %s" % (d["fail"], d["warn"]))' 2>/dev/null || true)"
    [ -z "$sc" ] || echo "  보안 설정 점검 $sc (자세히: .venv/bin/python tools/security_check.py)"
    local drill; drill="$(ls -1t "$BACKUP_DIR"/drills/drill-*.md 2>/dev/null | head -1 || true)"
    if [ -n "$drill" ]; then echo "  최근 복구 훈련 $(basename "$drill" .md) — $(grep -m1 '결과' "$drill" | sed 's/[|*]//g; s/결과//; s/^ *//')"; else echo "  복구 훈련 기록 없음(bash deploy/vm.sh drill)"; fi
}

cmd_logs() {
    local s="${1:-api}"
    has_systemd && units_installed || die "systemd 서비스가 없습니다(tmux 창에서 로그 확인)"
    exec $SUDO journalctl -u "outofstock-$s" -n 200 -f
}

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "${1:-}" != "logs" ]; then
    # sudo 로 통째로 돌리면 .venv·.env 가 root 소유가 되어 서비스(본인 계정)가 .env 를 못 읽는다
    die "sudo 없이 본인 계정으로 실행하세요:  bash deploy/vm.sh ${1:-}   (필요한 단계만 sudo 비밀번호를 묻습니다)"
fi

# 아래 { … } 는 통째로 읽힌 뒤 실행된다 — update·rollback 이 실행 중인 이 파일을 다른 버전으로 바꿔도, 끝난 뒤 바뀐 파일의
# 나머지를 이어 읽다가 'syntax error' 로 끝나지 않게(예전: 업데이트는 다 됐는데 마지막에 문법 오류 · 종료 코드 2).
{
case "${1:-}" in
    install)  cmd_install ;;
    update)   shift; cmd_update "${1:-}" ;;
    rollback) cmd_rollback ;;
    backup)   cmd_backup ;;
    backups)  cmd_backups ;;
    restore)  shift; cmd_restore "${1:-}" ;;
    drill)    shift; cmd_drill "${1:-}" ;;
    status)   cmd_status ;;
    restart)  start_services; check_up ;;
    units)    cmd_units ;;
    _post-update) post_update ;;                      # update 가 새 코드로 갈아 끼운 뒤 '새' 스크립트로 이어서 실행
    _units-dry) DRY_UNITS=1 write_units ;;           # 시험용: 서비스 파일을 deploy/generated/ 에만 쓴다(tests/test_deploy_config.py)
    harden)   cmd_harden ;;
    db-tune)  shift; case "${1:-}" in --reset) db_tune reset ;; --plan) db_tune plan ;; *) db_tune apply ;; esac ;;
    logs)     shift; cmd_logs "${1:-api}" ;;
    *) sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
exit $?
}
