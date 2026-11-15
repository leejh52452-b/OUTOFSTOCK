#!/usr/bin/env bash
# WAL 보관(아카이빙) 켜기 — 시점 복구(PITR)의 전제.
#   논리 백업(pg_dump, 하루 1번)만으로는 사고 때 최대 하루치를 잃는다(RPO ≈ 24시간).
#   WAL(변경 기록)을 1분마다(archive_timeout) 보관해 두면 '사고 1초 전'으로 되돌릴 수 있다(RPO ≤ 1분).
#
#   sudo RECIPIENTS=/etc/outofstock/backup.recipients bash deploy/backup/pitr_setup.sh
#
# · 보관 WAL 은 압축(gzip -1, 16MB → 약 1MB) 후 백업과 같은 age 공개키로 암호화한다 — 서버에는 공개키만.
#   보관/복원 명령은 wal-archive.sh·wal-restore.sh(pipefail: 압축 실패가 '빈 조각' 보관으로 숨지 않게).
# · 보관소 정리: 가장 오래 남길 베이스 백업보다 앞선 조각은 pg_archivecleanup 으로(docs/dr/README.md).
# · 이미 보관된 조각은 다시 쓰지 않는다(완료된 WAL 조각은 바뀌지 않으므로 '있으면 성공'이 안전).
# · PostgreSQL 을 한 번 재시작한다(archive_mode 는 재시작해야 바뀜) — 연결이 잠깐 끊긴다(앱은 자동 재연결).
# · 운영 규모에서는 pgBackRest/WAL-G(병렬·압축·암호화·보관 주기·검증 내장)로 같은 일을 한다. 이 스크립트는 원리 시연용.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "root(sudo)로 실행하세요" >&2; exit 1; }
command -v age >/dev/null || { echo "age 없음 — apt install age" >&2; exit 1; }
VER="${PG_VERSION:-$(pg_lsclusters -h | awk 'NR==1{print $1}')}"
CLUSTER="${PG_CLUSTER:-main}"
ARCHIVE_DIR="${ARCHIVE_DIR:-/var/lib/postgresql/wal_archive}"
RECIPIENTS="${RECIPIENTS:?age 공개키 파일(RECIPIENTS) 경로가 필요합니다}"
REC_INSTALLED="/etc/postgresql/oos-wal.recipients"
LIB="/usr/local/lib/outofstock"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
psql_pg() { runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -q "$@"; }

grep -q '^age1' "$RECIPIENTS" || { echo "공개키(age1…)가 없습니다: $RECIPIENTS" >&2; exit 1; }
install -d -o postgres -g postgres -m 700 "$ARCHIVE_DIR"
install -o root -g postgres -m 640 "$RECIPIENTS" "$REC_INSTALLED"
install -d -m 755 "$LIB"
install -m 755 "$HERE/wal-archive.sh" "$HERE/wal-restore.sh" "$LIB/"      # root 소유 — postgres 는 실행만
cmd="$LIB/wal-archive.sh %p %f $ARCHIVE_DIR"
psql_pg -c "ALTER SYSTEM SET wal_level = 'replica'" \
        -c "ALTER SYSTEM SET archive_mode = 'on'" \
        -c "ALTER SYSTEM SET archive_command = '$cmd'" \
        -c "ALTER SYSTEM SET archive_timeout = '60s'"
if [ "$(runuser -u postgres -- psql -XAtc 'SHOW archive_mode')" != "on" ]; then
    if [ "${YES:-}" != "1" ]; then
        read -r -p "PostgreSQL $VER/$CLUSTER 를 재시작합니다(수 초간 연결 끊김). 계속하려면 yes: " a
        [ "$a" = "yes" ] || { echo "취소 — 설정은 다음 재시작 때 적용됩니다"; exit 0; }
    fi
    pg_ctlcluster "$VER" "$CLUSTER" restart
fi
psql_pg -c "SELECT pg_reload_conf()" >/dev/null
seg="$(runuser -u postgres -- psql -XAtc 'SELECT pg_walfile_name(pg_current_wal_lsn())')"
runuser -u postgres -- psql -XAtc 'SELECT pg_switch_wal()' >/dev/null
for _ in $(seq 1 30); do [ -f "$ARCHIVE_DIR/$seg.gz.age" ] && break; sleep 1; done
if [ -f "$ARCHIVE_DIR/$seg.gz.age" ]; then
    echo "[✓] WAL 보관 동작 — $seg.gz.age ($(du -h "$ARCHIVE_DIR/$seg.gz.age" | cut -f1), 16MB 조각을 압축·암호화) · 1분마다 보관"
else
    echo "[x] 30초 안에 보관되지 않음 — SELECT * FROM pg_stat_archiver; 와 PostgreSQL 로그 확인" >&2; exit 1
fi
