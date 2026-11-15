"""PostgreSQL 서버 설정 권장값 — 서버 메모리·CPU·버전으로 계산(ADR-0027). vm.sh update 가 이 값을 ALTER SYSTEM 으로 적용한다.

계산을 셸이 아니라 여기 두는 이유: 시험할 수 있게(tests/test_dbtune.py) — 2GB·4GB·16GB VM, PG14·16 에서 값이 맞는지.
이 VM 은 DB 와 앱(API·스케줄러·실시간)이 같은 서버라 DB 몫을 보수적으로 잡는다(8GB 미만이면 메모리의 20%).

  메모리   shared_buffers(공유 버퍼) · effective_cache_size(계획기 힌트, 실제 할당 아님) · work_mem(정렬·해시 한 번) ·
           maintenance_work_mem(VACUUM·인덱스 생성)
  디스크   random_page_cost 1.1·effective_io_concurrency 200(SSD — 무작위 읽기가 순차와 비슷)
  WAL      wal_compression(전체 페이지 이미지 압축 — PG15+ lz4) · max_wal_size 2GB(체크포인트 간격)·완료 목표 0.9
  저장     default_toast_compression lz4(PG14+ — 큰 값 압축·해제가 pglz 보다 몇 배 빠르다)
  관측     pg_stat_statements(느린 쿼리 상위 — 재시작 필요) · track_io_timing · 느린 쿼리 500ms·잠금 대기·임시 파일·자동 VACUUM 기록
  정리     자동 VACUUM 기준 10%·통계 5%(기본 20%·10% — 큰 표가 몇 달 동안 안 도는 일을 막는다)
  안전망   idle_in_transaction_session_timeout 10분(앱 연결은 60초 — 사람이 psql 로 열어 둔 트랜잭션도 끊는다)
  JIT      off — 작은 OLTP 쿼리에 컴파일 지연(수십 ms)만 더한다
"""
from __future__ import annotations

MB = 1024 * 1024

# 재시작해야 적용되는 설정(나머지는 pg_reload_conf 로 즉시)
RESTART = {"shared_buffers", "shared_preload_libraries"}


def _mb(n: float) -> str:
    n = int(n)
    return f"{n // 1024}GB" if n >= 1024 and n % 1024 == 0 else f"{n}MB"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def recommend(ram_mb: int, cpus: int = 2, pg_major: int = 16, ssd: bool = True, max_connections: int = 100,
              preload: str = "") -> dict[str, str]:
    """권장 설정 {이름: 값}. preload = 지금 shared_preload_libraries(다른 모듈이 있으면 지우지 않고 덧붙인다)."""
    ram_mb = max(512, int(ram_mb))
    share = 0.20 if ram_mb < 8192 else 0.25                       # 앱과 같은 서버 — 작은 VM 은 DB 몫을 줄인다
    sb = _clamp(int(ram_mb * share) // 16 * 16, 128, 8192)
    out = {
        "shared_buffers": _mb(sb),
        "effective_cache_size": _mb(_clamp(int(ram_mb * 0.5) // 16 * 16, 256, 65536)),
        "work_mem": _mb(_clamp(ram_mb // (max_connections * 4), 4, 32)),
        "maintenance_work_mem": _mb(_clamp(ram_mb // 16 // 16 * 16, 64, 1024)),
        "random_page_cost": "1.1" if ssd else "4",
        "effective_io_concurrency": "200" if ssd else "2",
        "max_wal_size": "2GB",
        "min_wal_size": "256MB",
        "checkpoint_completion_target": "0.9",
        "wal_compression": "lz4" if pg_major >= 15 else "on",
        "track_io_timing": "on",
        "log_min_duration_statement": "500ms",
        "log_autovacuum_min_duration": "10s",
        "log_lock_waits": "on",
        "log_temp_files": "10MB",
        "log_checkpoints": "on",
        "autovacuum_vacuum_scale_factor": "0.1",
        "autovacuum_analyze_scale_factor": "0.05",
        "idle_in_transaction_session_timeout": "10min",
        "jit": "off",
        "max_parallel_workers_per_gather": str(_clamp(cpus // 2, 0, 4)),
    }
    if pg_major >= 13:
        out["autovacuum_vacuum_insert_scale_factor"] = "0.1"
    if pg_major >= 14:
        out["default_toast_compression"] = "lz4"
    libs = [x.strip() for x in (preload or "").split(",") if x.strip()]
    if "pg_stat_statements" not in libs:
        libs.append("pg_stat_statements")
    out["shared_preload_libraries"] = ",".join(libs)
    return out


def plan_lines(ram_mb: int, cpus: int, pg_major: int, ssd: bool = True, preload: str = "") -> list[str]:
    """vm.sh 가 읽는 형식 — '이름<TAB>값<TAB>재시작(1/0)' 줄들."""
    return [f"{k}\t{v}\t{1 if k in RESTART else 0}" for k, v in recommend(ram_mb, cpus, pg_major, ssd,
                                                                         preload=preload).items()]
