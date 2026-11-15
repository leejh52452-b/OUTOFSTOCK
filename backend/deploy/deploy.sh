#!/usr/bin/env bash
# OUTOFSTOCK 배포/업데이트 (Docker Compose 운영 오버레이 — 제로트러스트 배치) — 저장소 루트(stock-backend)에서 실행.
#
#   ./deploy/deploy.sh                 # 빌드 → DB → 최소 권한 역할·확장 → 마이그레이션(소유자 계정, 일회성) → 서비스 → 헬스체크 → 빈 DB 시드
#   ./deploy/deploy.sh --seed          # 시드 강제 재실행(주의: demo 는 실데이터가 있으면 run.py 가 거부)
#   ./deploy/deploy.sh backup          # 지금 백업(age 암호화) — 호스트 타이머가 매일 부르게(docs/dr/README.md)
#   ./deploy/deploy.sh status          # 컨테이너·헬스·백업 상태
#   SEED_CMD=real ./deploy/deploy.sh   # 빈 DB 를 데모 대신 실데이터로 시드(.env 에 KRX/DART 키 필요)
#
# 비밀값 파일 두 개(둘 다 600) — .env(앱 설정·API 키, 앱 컨테이너로 들어감) · .env.db(DB 비밀번호, compose 가 접속 문자열을 만들 때만)
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f .env ]    || die ".env 가 없습니다 — install -m 600 env.prod.example .env 로 만들고 값을 채우세요"
[ -f .env.db ] || die ".env.db 가 없습니다 — install -m 600 env.db.example .env.db (비밀번호: openssl rand -hex 24)"
chmod 600 .env .env.db 2>/dev/null || true
if grep -qE '^(POSTGRES_PASSWORD|OOS_(OWNER|APP|RO|BACKUP)_PASSWORD)=' .env; then
  warn "DB 비밀번호가 .env 에 있습니다 — .env 는 앱 컨테이너에 그대로 들어갑니다. .env.db 로 옮기고 .env 에서 지우세요"
fi
set -a; . ./.env.db; set +a                       # 역할 설정(psql 변수)에 쓴다 — 값은 영문·숫자만(env.db.example)
for v in POSTGRES_PASSWORD OOS_OWNER_PASSWORD OOS_APP_PASSWORD OOS_RO_PASSWORD OOS_BACKUP_PASSWORD; do
  val="${!v:-}"
  [ -n "$val" ] && [ "$val" != "CHANGE_ME" ] || die ".env.db 의 $v 를 채우세요(openssl rand -hex 24)"
  [[ "$val" =~ ^[A-Za-z0-9]+$ ]] || die "$v 는 영문·숫자만(접속 URL·SQL 에 그대로 들어간다)"
done

# 백업 공개키 파일 — 저장소·설치본에는 예시만 있다(업데이트가 운영 키를 덮어쓰지 않게). 없으면 예시로 만들어 둔다:
# compose 는 없는 파일을 붙이면 같은 이름의 '폴더'를 만들어 버린다. 예시(공개키 없음) 그대로면 백업이 '수신자 없음'으로 멈춘다.
RECIPIENTS="${BACKUP_RECIPIENTS_FILE:-./deploy/backup/recipients.txt}"
[ -e "$RECIPIENTS" ] || install -m 644 deploy/backup/recipients.example.txt "$RECIPIENTS"

COMPOSE=(docker compose --env-file .env --env-file .env.db -f docker-compose.yml -f docker-compose.prod.yml --profile full)
OPS=("${COMPOSE[@]}" --profile ops)
SEED_CMD="${SEED_CMD:-demo}"
PGU="${POSTGRES_USER:-oos}"; PGD="${POSTGRES_DB:-outofstock}"

wait_healthy() {   # wait_healthy <서비스> <초>
  local cid
  for _ in $(seq 1 "$2"); do
    cid="$("${COMPOSE[@]}" ps -q "$1" 2>/dev/null || true)"
    [ -n "$cid" ] && [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)" = "healthy" ] && return 0
    sleep 1
  done
  return 1
}

cmd_backup() { say "백업(암호화)…"; "${OPS[@]}" run --rm backup; }

cmd_status() {
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" exec -T app python -c \
    "import json,os,urllib.request; h=json.load(urllib.request.urlopen(os.environ.get('APP_HEALTH_URL','http://127.0.0.1:8000/health'), timeout=3)); print('health', h['status'], h['issues'])" \
    || warn "앱 헬스 확인 실패"
  ls -lt "${BACKUP_HOST_DIR:-./var/backups}" 2>/dev/null | head -4 || true
}

cmd_deploy() {
  say "egress 허용 목록 확인 — 코드(app/security/egress.py)와 프록시 설정이 같은지"
  "${OPS[@]}" build
  if ! "${OPS[@]}" run --rm --no-deps -T migrate python tools/egress.py --squid | diff -q - deploy/egress-proxy/squid.conf >/dev/null; then
    "${OPS[@]}" run --rm --no-deps -T migrate python tools/egress.py --squid > deploy/egress-proxy/squid.conf
    warn "egress 프록시 허용 목록을 코드 기준으로 다시 만들었습니다(deploy/egress-proxy/squid.conf) — 변경을 검토·커밋하세요"
  fi

  say "DB 기동…"
  "${COMPOSE[@]}" up -d db
  wait_healthy db 90 || die "DB 가 healthy 가 되지 않습니다 — ${COMPOSE[*]} logs db"

  say "최소 권한 역할(oos_owner·oos_app·oos_ro·oos_backup)·확장 맞추기 — 여러 번 실행해도 같다"
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -q -U "$PGU" -d "$PGD" \
      -v owner_pw="'$OOS_OWNER_PASSWORD'" -v app_pw="'$OOS_APP_PASSWORD'" -v ro_pw="'$OOS_RO_PASSWORD'" \
      -v backup_pw="'$OOS_BACKUP_PASSWORD'" -f /sql/roles.sql
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -q -U "$PGU" -d "$PGD" \
      -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"

  say "스키마 반영(소유자 계정 — 이 작업 컨테이너에만 소유자 비밀번호가 들어간다)"
  "${OPS[@]}" run --rm migrate || die "마이그레이션 실패 — ${OPS[*]} run --rm migrate 로 다시 확인"

  say "서비스 기동(edge · app · scheduler · realtime · egress-proxy)…"
  "${COMPOSE[@]}" up -d --remove-orphans
  wait_healthy app 120 || die "앱이 healthy 가 되지 않습니다 — ${COMPOSE[*]} logs --tail=80 app"
  ok "앱 정상(healthy)"

  local empty
  empty="$("${OPS[@]}" run --rm --no-deps -T migrate python -c \
    "from app import db; print('yes' if not db.query('SELECT 1 FROM ohlc LIMIT 1') else 'no')" 2>/dev/null | tail -n1 | tr -d '\r')"
  if [ "${1:-}" = "--seed" ] || [ "$empty" = "yes" ]; then
    say "초기 데이터 시드: python run.py ${SEED_CMD}"
    "${OPS[@]}" run --rm migrate python run.py "${SEED_CMD}"
  else
    say "DB 에 데이터가 있어 시드는 건너뜀(새로 수집: ${OPS[*]} run --rm migrate python run.py real)"
  fi
  grep -qs '^age1' "$RECIPIENTS" \
    || warn "백업 암호화 공개키가 없습니다 — $RECIPIENTS 에 age 공개키를 넣어야 './deploy/deploy.sh backup' 이 동작합니다"
  local port; port="$(grep -E '^HTTP_PORT=' .env | tail -1 | cut -d= -f2)"
  ok "배포 완료 — 브라우저: http://<서버>:${port:-80}/  (TLS 는 EDGE_MODE=tls + deploy/pki/make-pki.sh compose)"
}

case "${1:-}" in
  backup) cmd_backup ;;
  status) cmd_status ;;
  ""|--seed) cmd_deploy "${1:-}" ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
