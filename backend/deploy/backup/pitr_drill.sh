#!/usr/bin/env bash
# 시점 복구(PITR) 훈련 — 베이스 백업 + 보관 WAL 로 '사고 1초 전'을 별도 인스턴스에 되살리고 시간을 잰다.
#
#   sudo AGE_IDENTITY=/안전한/곳/ops.key bash deploy/backup/pitr_drill.sh
#
# 전제: deploy/backup/pitr_setup.sh 로 WAL 보관이 켜져 있을 것. 운영 DB 에는 표식 3행을 썼다가 '사고'로 지운다(순증 0).
#  ① 베이스 백업(pg_basebackup, 매니페스트 SHA-256) → pg_verifybackup 으로 파일 하나하나 검증
#  ② 운영 DB 에 표식 3행 커밋 → 목표 시각 T → '사고'(표식 삭제) 커밋 → WAL 전환 + 보관될 때까지 대기
#  ③ 새 데이터 디렉터리 = 베이스 백업. 보관 WAL 을 age 로 풀어 재생(restore_command), T 에서 멈추고 승격.
#     별도 포트·전용 소켓 폴더(외부 접속 없음)·WAL 보관 끔(운영 보관소를 더럽히지 않게).
#  ④ 확인: 표식 3행이 있다(T 이전 커밋 반영) + '사고'는 반영되지 않았다 + 감사 체인·원장 검증 → 보고서(RTO 실측)
# 정리: 훈련 인스턴스 정지·삭제, 복사한 복호화 키 삭제. KEEP=1 이면 인스턴스를 남긴다(조사용).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "root(sudo)로 실행하세요" >&2; exit 1; }
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${OOS_PY:-$APP_DIR/.venv/bin/python}"
VER="${PG_VERSION:-$(pg_lsclusters -h | awk 'NR==1{print $1}')}"
BIN="/usr/lib/postgresql/$VER/bin"
DB="${DB:-outofstock}"
ARCHIVE_DIR="${ARCHIVE_DIR:-/var/lib/postgresql/wal_archive}"
PORT_DRILL="${PORT_DRILL:-5441}"
REPORT_DIR="${REPORT_DIR:-$APP_DIR/var/dr}"
AGE_IDENTITY="${AGE_IDENTITY:?복호화 키 파일(AGE_IDENTITY) 경로가 필요합니다}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="/var/lib/postgresql/pitr-drill-$TS"
as_pg() { runuser -u postgres -- "$@"; }
pg()    { as_pg psql -X -v ON_ERROR_STOP=1 -qAt -d "$DB" "$@"; }
drill_psql() { as_pg psql -X -v ON_ERROR_STOP=1 -qAt -h "$WORK" -p "$PORT_DRILL" -d "$DB" "$@"; }
now()   { date +%s.%N; }
secs()  { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.2f", b-a}'; }
STEPS=()
step()  { STEPS+=("| $1 | $2 | $3 | $4 |"); printf '  %s %-34s %6ss  %s\n' "$3" "$1" "$2" "$4"; }

cleanup() {
    if [ -d "$WORK/pgdata" ] && [ "${KEEP:-}" != "1" ]; then
        as_pg "$BIN/pg_ctl" -D "$WORK/pgdata" stop -m immediate >/dev/null 2>&1 || true
    fi
    rm -f "$WORK/identity" 2>/dev/null || true
    if [ "${KEEP:-}" != "1" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT

[ "$(as_pg psql -XAtc 'SHOW archive_mode')" = "on" ] || { echo "WAL 보관이 꺼져 있습니다 — deploy/backup/pitr_setup.sh 먼저" >&2; exit 1; }
install -d -o postgres -g postgres -m 700 "$WORK"
install -o postgres -g postgres -m 600 "$AGE_IDENTITY" "$WORK/identity"
T0="$(now)"
echo "[*] PITR 훈련 $TS — 작업 폴더 $WORK"

# ① 베이스 백업 + 검증 ---------------------------------------------------------
t="$(now)"
as_pg "$BIN/pg_basebackup" -D "$WORK/base" -X stream -c fast --manifest-checksums=SHA256 -h /var/run/postgresql
as_pg "$BIN/pg_verifybackup" -q "$WORK/base"
step "베이스 백업 + pg_verifybackup" "$(secs "$t" "$(now)")" "✓" "$(du -sh "$WORK/base" | cut -f1) · 파일별 SHA-256 일치"

# ② 표식 → 목표 시각 → 사고 -----------------------------------------------------
t="$(now)"
TAG="dr:pitr:$TS"
pg -c "INSERT INTO app_meta(key, value, updated_at) SELECT '$TAG:' || g, 'canary', now()::text FROM generate_series(1, 3) g"
sleep 1
TARGET="$(pg -c "SELECT clock_timestamp()")"
sleep 1
pg -c "DELETE FROM app_meta WHERE key LIKE '$TAG:%'"                    # '사고' — 실수로 지운 데이터
ACCIDENT_SEG="$(pg -c "SELECT pg_walfile_name(pg_current_wal_lsn())")"
pg -c "SELECT pg_switch_wal()" >/dev/null
for _ in $(seq 1 60); do [ -f "$ARCHIVE_DIR/$ACCIDENT_SEG.gz.age" ] && break; sleep 1; done
[ -f "$ARCHIVE_DIR/$ACCIDENT_SEG.gz.age" ] || { echo "[x] 사고가 담긴 WAL($ACCIDENT_SEG)이 보관되지 않음" >&2; exit 1; }
LIVE="$(pg -c "SELECT count(*) FROM app_meta WHERE key LIKE '$TAG:%'")"
step "표식 3행 → 목표 시각 → 사고(삭제)" "$(secs "$t" "$(now)")" "✓" "목표 $TARGET · 운영에는 표식 ${LIVE}행(지워짐) · WAL $ACCIDENT_SEG 보관"

# ③ 새 인스턴스로 시점 복구 -----------------------------------------------------
t="$(now)"
as_pg cp -a "$WORK/base" "$WORK/pgdata"
rm -f "$WORK/pgdata/backup_manifest"
# 복구 중에는 이 값들이 원본보다 작으면 멈춘다(hot standby 제약) — 원본 값을 그대로 쓴다
HS="$(pg -c "SELECT string_agg(name || ' = ' || setting, E'\n') FROM pg_settings WHERE name IN
      ('max_connections', 'max_worker_processes', 'max_wal_senders', 'max_prepared_transactions',
       'max_locks_per_transaction')")"
cat > "$WORK/pgdata/postgresql.conf" <<CONF
# PITR 훈련 인스턴스 — 외부 접속 없음(전용 소켓 폴더), 보관 끔
port = $PORT_DRILL
listen_addresses = ''
unix_socket_directories = '$WORK'
shared_buffers = 128MB
$HS
CONF
printf 'local all all peer\n' > "$WORK/pgdata/pg_hba.conf"
: > "$WORK/pgdata/pg_ident.conf"
# 베이스 백업에 딸려 온 postgresql.auto.conf(운영의 ALTER SYSTEM 값: archive_mode=on …)를 뒤에서 덮는다(뒤가 이긴다)
cat >> "$WORK/pgdata/postgresql.auto.conf" <<CONF
archive_mode = 'off'
archive_command = ''
restore_command = '/usr/local/lib/outofstock/wal-restore.sh %f %p $ARCHIVE_DIR $WORK/identity'
recovery_target_time = '$TARGET'
recovery_target_action = 'promote'
CONF
chown postgres:postgres "$WORK/pgdata/postgresql.conf" "$WORK/pgdata/pg_hba.conf" "$WORK/pgdata/pg_ident.conf"
as_pg touch "$WORK/pgdata/recovery.signal"
t_start="$(now)"
as_pg "$BIN/pg_ctl" -D "$WORK/pgdata" -l "$WORK/recovery.log" -w -t 600 start >/dev/null
for _ in $(seq 1 600); do
    [ "$(as_pg psql -X -qAt -h "$WORK" -p "$PORT_DRILL" -d postgres -c 'SELECT pg_is_in_recovery()' 2>/dev/null)" = "f" ] && break
    sleep 0.5
done
[ "$(as_pg psql -X -qAt -h "$WORK" -p "$PORT_DRILL" -d postgres -c 'SELECT pg_is_in_recovery()')" = "f" ] || \
    { echo "[x] 승격되지 않음 — $WORK/recovery.log" >&2; tail -20 "$WORK/recovery.log" >&2; exit 1; }
REPLAYED="$(grep -c 'restored log file' "$WORK/recovery.log" || true)"
STOP_LINE="$(grep -m1 'recovery stopping before' "$WORK/recovery.log" | sed 's/.*LOG: *//' || true)"
step "시점 복구(WAL 재생 → 승격)" "$(secs "$t" "$(now)")" "✓" "WAL ${REPLAYED}조각 재생 · 기동~승격 $(secs "$t_start" "$(now)")초 · ${STOP_LINE:-목표 도달}"

# ④ 검증 -------------------------------------------------------------------
t="$(now)"
GOT="$(drill_psql -c "SELECT count(*) FROM app_meta WHERE key LIKE '$TAG:%'")"
if [ "$GOT" = "3" ]; then
    step "표식 복원(사고 직전 상태)" "$(secs "$t" "$(now)")" "✓" "표식 3/3행 — 목표 이전 커밋은 반영, 사고(삭제)는 미반영"
else
    step "표식 복원(사고 직전 상태)" "$(secs "$t" "$(now)")" "✗" "표식 ${GOT}/3행"
fi
t="$(now)"
URL="postgresql+psycopg://postgres@/$DB?host=$WORK&port=$PORT_DRILL"
AUD="$(cd "$APP_DIR" && runuser -u postgres -- env DATABASE_URL="$URL" WARMUP_ON_START=false PYTHONDONTWRITEBYTECODE=1 "$PY" run.py audit-verify 2>/dev/null | head -1 || true)"
LED="$(cd "$APP_DIR" && runuser -u postgres -- env DATABASE_URL="$URL" WARMUP_ON_START=false PYTHONDONTWRITEBYTECODE=1 "$PY" run.py ledger-verify 2>/dev/null | tail -1 || true)"
if [[ "$AUD" == *정상* ]] && [[ "$LED" == *"시산표 합 0"* ]] && [[ "$LED" != *"깨진 날짜 ["* ]]; then
    step "감사 체인 · 원장 검증" "$(secs "$t" "$(now)")" "✓" "${AUD} · ${LED}"
else
    step "감사 체인 · 원장 검증" "$(secs "$t" "$(now)")" "✗" "${AUD:-실행 실패} · ${LED:-실행 실패}"
fi
RTO="$(secs "$T0" "$(now)")"

# 보고서 ---------------------------------------------------------------------
mkdir -p "$REPORT_DIR"
OK="통과"; for s in "${STEPS[@]}"; do [[ "$s" == *"| ✗ |"* ]] && OK="실패"; done
REPORT="$REPORT_DIR/pitr-$TS.md"
{
    echo "# 시점 복구(PITR) 훈련 보고서 — $TS"
    echo
    echo "| 항목 | 값 |"; echo "|---|---|"
    echo "| 결과 | **$OK** |"
    echo "| 복구 목표 시각 | $TARGET (사고 1초 전) |"
    echo "| RTO 실측(베이스 백업 포함 전 과정) | ${RTO}초 |"
    echo "| RPO | 0 — 목표 시각까지 커밋된 변경 전부 복구(WAL 보관 주기 60초: 서버·디스크 통째 소실 시 최악 60초) |"
    echo "| 대상 | 별도 인스턴스(포트 $PORT_DRILL, 외부 접속 없음, WAL 보관 끔) — 훈련 뒤 삭제 |"
    echo "| PostgreSQL | $VER · 보관 WAL: gzip 압축 + age 공개키 암호화 · 베이스 백업 pg_verifybackup 검증 |"
    echo
    echo "## 단계"; echo; echo "| 단계 | 소요(초) | 결과 | 내용 |"; echo "|---|---:|:---:|---|"
    printf '%s\n' "${STEPS[@]}"
} > "$REPORT"
echo "[*] 결과 $OK · RTO ${RTO}초 · 보고서 $REPORT"
[ "$OK" = "통과" ]
