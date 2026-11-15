# 인덱스·파티션 실측(EXPLAIN ANALYZE) — 7,041,600행

- 데이터: 합성 2,700종목 × 10년(2016-10-01~2026-09-30, 평일), PostgreSQL 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
- 각 쿼리 3회 실행의 중앙값(ms). 버퍼 = 읽은 8KB 블록 수(캐시 적중 포함). '표' = 실제로 읽은 표·파티션 수.
- 재현: `.venv/bin/python tools/bench_explain.py` (환경마다 숫자는 다르다 — 구성 간 **비율**을 본다)

| 쿼리 | A heap(파티션 없음) | B 연도 파티션 | C 파티션+B-tree(trade_date) | D 파티션+BRIN(trade_date) |
|---|---|---|---|---|
| Q1 최신 거래일 | 504.5ms · 버퍼 80,019 · 표 1 | 818.58ms · 버퍼 80,022 · 표 11 | 0.03ms · 버퍼 4 · 표 11 | 776.88ms · 버퍼 80,022 · 표 11 |
| Q2 그날 거래대금 상위 50 | 208.19ms · 버퍼 80,091 · 표 1 | 33.35ms · 버퍼 6,055 · 표 1 | 0.71ms · 버퍼 36 · 표 1 | 0.72ms · 버퍼 33 · 표 1 |
| Q3 한 종목 1년 | 0.32ms · 버퍼 265 · 표 1 | 0.38ms · 버퍼 267 · 표 2 | 0.4ms · 버퍼 267 · 표 2 | 0.36ms · 버퍼 267 · 표 2 |
| Q4 거래량 급증 | 330.79ms · 버퍼 80,109 · 표 1 | 156.47ms · 버퍼 5,983 · 표 1 | 105.91ms · 버퍼 928 · 표 1 | 109.45ms · 버퍼 865 · 표 1 |
| Q5 52주 신고가 | 274.96ms · 버퍼 80,035 · 표 1 | 154.84ms · 버퍼 14,007 · 표 2 | 148.88ms · 버퍼 8,167 · 표 2 | 148.4ms · 버퍼 8,068 · 표 2 |
| Q6 월별 거래대금 3년 | 500.7ms · 버퍼 80,035 · 표 1 | 477.77ms · 버퍼 30,024 · 표 4 | 465.81ms · 버퍼 24,198 · 표 4 | 459.9ms · 버퍼 24,101 · 표 4 |

## 실행 계획 요약

**A heap(파티션 없음)** — 표 크기 902 MB
- Q1 최신 거래일: Aggregate → Gather → Seq Scan
- Q2 그날 거래대금 상위 50: Limit → Gather Merge → Sort → Seq Scan
- Q3 한 종목 1년: Sort → Bitmap Heap Scan → Bitmap Index Scan(ph_heap_pkey)
- Q4 거래량 급증: Limit → Sort → Subquery Scan → WindowAgg → Gather Merge → Seq Scan
- Q5 52주 신고가: Aggregate → Gather Merge → Sort → Seq Scan
- Q6 월별 거래대금 3년: Aggregate → Gather Merge → Sort → Seq Scan

**B 연도 파티션**
- Q1 최신 거래일: Aggregate → Gather → Append → Seq Scan
- Q2 그날 거래대금 상위 50: Limit → Gather Merge → Sort → Seq Scan
- Q3 한 종목 1년: Sort → Append → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2025_pkey) → Bitmap Index Scan(ph_part_2026_pkey)
- Q4 거래량 급증: Limit → Sort → Subquery Scan → WindowAgg → Seq Scan
- Q5 52주 신고가: Aggregate → Gather Merge → Sort → Append → Seq Scan
- Q6 월별 거래대금 3년: Aggregate → Gather Merge → Sort → Append → Seq Scan

**C 파티션+B-tree(trade_date)** — 인덱스 크기 48 MB
- Q1 최신 거래일: Result → Limit → Append → Index Only Scan(ph_part_2026_trade_date_idx) → Index Only Scan(ph_part_2025_trade_date_idx) → Index Only Scan(ph_part_2024_trade_date_idx)
- Q2 그날 거래대금 상위 50: Limit → Sort → Index Scan(ph_part_2026_trade_date_idx)
- Q3 한 종목 1년: Sort → Append → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2025_pkey) → Bitmap Index Scan(ph_part_2026_pkey)
- Q4 거래량 급증: Limit → Sort → Subquery Scan → WindowAgg → Index Scan(ph_part_2026_trade_date_idx)
- Q5 52주 신고가: Aggregate → Gather → Append → Index Scan(ph_part_2025_trade_date_idx) → Seq Scan
- Q6 월별 거래대금 3년: Aggregate → Gather Merge → Sort → Append → Index Scan(ph_part_2023_trade_date_idx) → Seq Scan

**D 파티션+BRIN(trade_date)** — 인덱스 크기 264 kB
- Q1 최신 거래일: Aggregate → Gather → Append → Seq Scan
- Q2 그날 거래대금 상위 50: Limit → Sort → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2026_trade_date_idx)
- Q3 한 종목 1년: Sort → Append → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2025_pkey) → Bitmap Index Scan(ph_part_2026_pkey)
- Q4 거래량 급증: Limit → Sort → Subquery Scan → WindowAgg → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2026_trade_date_idx)
- Q5 52주 신고가: Aggregate → Gather → Append → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2025_trade_date_idx) → Seq Scan
- Q6 월별 거래대금 3년: Aggregate → Gather Merge → Sort → Append → Bitmap Heap Scan → Bitmap Index Scan(ph_part_2023_trade_date_idx)

