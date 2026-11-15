-- 운영 관측: 느린 쿼리 상위(tools/pg_top_queries.py). 통계 수집은 shared_preload_libraries 에 pg_stat_statements 가 있을 때만
-- (docker-compose.prod.yml 이 켠다). 백업에는 넣지 않는다(tools/dr.py: --extension=plpgsql — 일반 계정으로도 복원되게).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
