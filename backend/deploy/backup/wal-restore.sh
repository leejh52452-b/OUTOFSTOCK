#!/bin/bash
# PostgreSQL restore_command — 보관된 WAL 조각을 age 로 풀고 압축을 풀어 %p 에 쓴다(복구·PITR 때만).
#   restore_command = '/usr/local/lib/outofstock/wal-restore.sh %f %p /var/lib/postgresql/wal_archive /키/경로'
# 없는 조각(타임라인 history 등)을 물으면 1 로 끝낸다 — PostgreSQL 이 '보관소 끝'으로 이해한다.
set -euo pipefail
name="$1"; dst="$2"; dir="$3"; key="$4"
src="$dir/$name.gz.age"
[ -f "$src" ] || exit 1
age --decrypt -i "$key" "$src" | gunzip -c > "$dst"
