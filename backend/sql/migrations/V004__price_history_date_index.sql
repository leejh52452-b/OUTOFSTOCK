-- V004 날짜 인덱스 — 측정 후 추가(ADR-0003, docs/perf/explain_report.md, 합성 704만 행·PostgreSQL 16):
--   최신 거래일 max(trade_date)      : 파티션만 818ms → B-tree 0.03ms (BRIN 은 777ms — max 에는 못 쓴다)
--   하루치 전 종목(거래대금 상위 50) : 파티션만 33ms  → B-tree 0.7ms (파티션 없는 표 208ms)
--   거래량 급증(최근 40일 윈도 함수) : 156ms → 106ms
-- BRIN(264kB)은 B-tree(48MB)보다 훨씬 작지만 '최신일' 조회를 줄이지 못해 B-tree 를 택했다.
-- 분할 표의 부모에 만들면 모든 연도 파티션(앞으로 만들 파티션 포함)에 같은 인덱스가 생긴다.
CREATE INDEX IF NOT EXISTS ix_price_history_date ON analytics.price_history (trade_date);
