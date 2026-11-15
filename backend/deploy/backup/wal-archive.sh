#!/bin/bash
# PostgreSQL archive_command — WAL 조각 1개를 압축(gzip -1) → age 공개키 암호화 → 원자적 이름 바꾸기.
#   archive_command = '/usr/local/lib/outofstock/wal-archive.sh %p %f /var/lib/postgresql/wal_archive'
# 이미 보관된 조각이면 성공으로 끝낸다(완료된 WAL 조각은 바뀌지 않는다 — 재시도 때 막히지 않게).
# pipefail: 압축이 실패했는데 암호화만 성공해 '빈 조각'이 보관되는 일이 없게.
set -euo pipefail
src="$1"; name="$2"; dir="$3"
rec="${OOS_WAL_RECIPIENTS:-/etc/postgresql/oos-wal.recipients}"
dst="$dir/$name.gz.age"
[ -f "$dst" ] && exit 0
gzip -1 -c "$src" | age -R "$rec" -o "$dst.part"
sync "$dst.part" 2>/dev/null || true
mv "$dst.part" "$dst"
