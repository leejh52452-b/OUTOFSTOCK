"""DB 용량 실험실 — 운영 1년 규모의 합성 데이터를 '실험용 DB'에 채우고(seed), 표·인덱스·TOAST 크기를 잰다(measure).
타입 정리·인덱스 정리·수명주기(압축 보관) 같은 저장 구조 변경을 '말'이 아니라 같은 데이터의 전/후 숫자로 비교하려고 만든다.

    python tools/db_lab.py seed    --db postgresql://oos:oos@127.0.0.1:5432/outofstock_lab [--scale 1.0]
    python tools/db_lab.py measure --db … --label before --out var/db_lab/before.json
    python tools/db_lab.py bench   --db … --label before --out var/db_lab/bench_before.json
    python tools/db_lab.py compare var/db_lab/before.json var/db_lab/after.json      # 마크다운 표

안전장치: DB 이름에 'lab' 또는 'test' 가 없으면 아무것도 하지 않는다(운영 DB 에 합성 데이터를 넣는 사고 방지).
seed 는 열의 '지금' 타입으로 CAST 해서 넣는다 — 예전 스키마(날짜가 문자열)든 새 스키마(date·timestamp)든 같은 값이 들어간다.
예전 스키마(문자열 열)일 때만 형식이 깨진 값 몇 개를 일부러 넣는다(마이그레이션의 격리·NULL 처리 확인용).
규모(--scale 1.0, 1년): 뉴스 100만 · 종목 태깅 약 130만 · 감성 100만 · 검색 36.5만 · 접속 36.5만 · 원장 20만 · 출석 15만 ·
일봉(OHLC·daily_price) 150종목 × 3년 · 해외 일봉 50종목 × 3년 · 채팅 10만 · 학습 10만 · 경보 5만 …
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg

ROOT = Path(__file__).resolve().parents[1]

NAMES = ["삼성전자", "SK하이닉스", "LG에너지솔루션", "삼성바이오로직스", "현대차", "기아", "셀트리온", "KB금융", "신한지주",
         "POSCO홀딩스", "NAVER", "카카오", "삼성SDI", "LG화학", "현대모비스", "하나금융지주", "삼성물산", "HD현대중공업",
         "한화에어로스페이스", "에코프로비엠", "알테오젠", "HLB", "크래프톤", "SK텔레콤", "KT", "한국전력", "우리금융지주",
         "삼성생명", "메리츠금융지주", "LG전자"]
PHRASES = ["3분기 실적 시장 기대 웃돌아", "목표주가 상향…외국인 순매수 이어져", "신규 수주 소식에 장중 강세",
           "대규모 투자 계획 발표", "증권가 '저평가 구간' 분석", "자사주 매입·소각 결정", "수출 회복세에 반등 기대",
           "업황 둔화 우려로 약세", "배당 확대 정책 공개", "해외 공장 증설 착수", "공매도 재개 앞두고 변동성 확대",
           "AI 반도체 수요 증가 수혜 전망", "규제 리스크 부각", "신제품 출시 효과 주목", "기관 매도세에 하락 마감",
           "환율 상승에 수출주 강세", "실적 발표 앞두고 관망세", "지분 매각 추진설", "합병 기대감 반영", "신용등급 상향"]
SOURCES = ["매일경제", "한국경제", "연합뉴스", "머니투데이", "이데일리", "서울경제", "조선비즈", "파이낸셜뉴스", "아시아경제", "뉴스1"]

KST_NAIVE = "to_char({x}, 'YYYY-MM-DD\"T\"HH24:MI:SS')"                 # clock.iso_now() 형식(시간대 없음 = 한국시간)
KST_OFFSET = "to_char({x}, 'YYYY-MM-DD\"T\"HH24:MI:SS') || '+09:00'"     # quotes.iso_now() 형식
DAY = "to_char({x}, 'YYYY-MM-DD')"


def _arr(xs):
    return "ARRAY[" + ",".join("'" + x.replace("'", "''") + "'" for x in xs) + "]"


def _guard(url):
    name = urlparse(url.replace("+psycopg", "")).path.lstrip("/")
    if "lab" not in name and "test" not in name:
        sys.exit(f"거부: DB 이름 '{name}' 에 lab/test 가 없습니다 — 실험용 DB 에서만 실행하세요.")
    return url.replace("postgresql+psycopg://", "postgresql://")


def _types(cur, table):
    cur.execute("SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped", (table,))
    return dict(cur.fetchall())


def _insert(cur, table, cols: dict, src: str):
    """INSERT INTO table(cols) SELECT CAST(식 AS 열의 현재 타입) … FROM src — 예전/새 스키마 모두에서 같은 값."""
    ty = _types(cur, table)
    names = [c for c in cols if c in ty]
    sel = ", ".join(f"CAST(({cols[c]}) AS {ty[c]})" for c in names)
    t0 = time.perf_counter()
    cur.execute(f"INSERT INTO {table} ({', '.join(names)}) SELECT {sel} FROM {src}")
    print(f"  {table:<22} {cur.rowcount:>10,} 행  {time.perf_counter() - t0:6.1f}s", flush=True)


def seed(url, scale=1.0):
    url = _guard(url)
    n_news, n_search, n_access = int(1_000_000 * scale), int(365_000 * scale), int(365_000 * scale)
    users, tick_n = 2000, 150
    tk = "lpad((100000 + ({i}) * 1237)::text, 6, '0')"                   # 종목 코드 150개(겹치지 않음)
    usr = "'user' || lpad(({i})::text, 5, '0')"
    days = ("(SELECT d::date AS d, row_number() OVER (ORDER BY d) AS di FROM generate_series("
            "date '2023-11-06', date '2026-10-30', interval '1 day') d WHERE extract(isodow FROM d) < 6) dd")
    grid = f"{days} CROSS JOIN generate_series(1, {tick_n}) t(i)"          # 150종목 × 약 780 거래일
    news_ts = f"(timestamp '2025-11-03 00:00:00' + g * interval '{365 * 86400 / max(n_news, 1):.4f} seconds')"
    with psycopg.connect(url, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET app.rls = 'off'")
        cur.execute("SET synchronous_commit = off")
        cur.execute("SELECT count(*) FROM news")
        if cur.fetchone()[0]:
            sys.exit("이미 데이터가 있습니다 — 빈 실험 DB 에서 실행하세요(dropdb/createdb 후 init_db).")
        print(f"seed: scale={scale} → {url.rsplit('/', 1)[-1]}", flush=True)
        _insert(cur, "stock", {"ticker": tk.format(i="i"), "name": f"({_arr(NAMES)})[1 + i % 30] || i",
                               "market": "CASE WHEN i % 3 = 0 THEN 'KOSDAQ' ELSE 'KOSPI' END",
                               "sector": "'업종' || (i % 20)"}, f"generate_series(1, {tick_n}) i")
        price = "(10000 + (abs(hashtext(i::text)) % 90000) + (di * (i % 7 - 3) * 13))"
        _insert(cur, "ohlc", {"ticker": tk.format(i="i"), "trade_date": DAY.format(x="d"),
                              "open": f"({price})::float8", "high": f"({price} * 1.02)::int::float8",
                              "low": f"({price} * 0.98)::int::float8", "close": f"({price} + (di % 5 - 2) * 50)::float8",
                              "volume": "100000 + abs(hashtext(i::text || di::text)) % 5000000"}, grid)
        _insert(cur, "daily_price", {"ticker": tk.format(i="i"), "trade_date": DAY.format(x="d"),
                                     "close": f"({price} + (di % 5 - 2) * 50)::float8",
                                     "volume": "100000 + abs(hashtext(i::text || di::text)) % 5000000",
                                     "change_rate": "round(((di % 11) - 5) * 0.37, 2)",
                                     "market_cap": f"({price})::bigint * 50000000"}, grid)
        _insert(cur, "index_daily", {"code": "(ARRAY['KOSPI','KOSDAQ','KOSPI200','USDKRW'])[i]",
                                     "trade_date": DAY.format(x="d"), "open": "2500 + di * 0.7 + i",
                                     "high": "2520 + di * 0.7 + i", "low": "2480 + di * 0.7 + i",
                                     "close": "2505 + di * 0.7 + i", "change": "((di % 9) - 4) * 3.1",
                                     "change_rate": "((di % 9) - 4) * 0.12", "source": "'krx'"},
                f"{days} CROSS JOIN generate_series(1, 4) t(i)")
        _insert(cur, "overseas_stock", {"symbol": "'S' || i", "name": "'Company ' || i", "name_ko": "'해외' || i",
                                        "exchange": "'NASDAQ'", "sector": "'Tech'", "shares": "1000000000",
                                        "market_cap": "1000000000000", "source": "'fdr'",
                                        "updated_at": KST_OFFSET.format(x="timestamp '2026-11-02 07:00:00'")},
                "generate_series(1, 50) i")
        _insert(cur, "overseas_ohlc", {"symbol": "'S' || i", "trade_date": DAY.format(x="d"),
                                       "open": "100 + i + di * 0.13", "high": "101.5 + i + di * 0.13",
                                       "low": "98.7 + i + di * 0.13", "close": "100.42 + i + di * 0.13",
                                       "volume": "1000000 + di"}, f"{days} CROSS JOIN generate_series(1, 50) t(i)")
        _insert(cur, "overseas_quote", {"symbol": "'S' || i", "price": "150.25", "change": "1.25", "change_rate": "0.84",
                                        "prev_close": "149", "open": "149.5", "high": "151", "low": "148.8",
                                        "volume": "1234567", "session_date": "'2026-10-30'", "source": "'finnhub'",
                                        "ts": KST_OFFSET.format(x="timestamp '2026-11-02 06:00:00'")},
                "generate_series(1, 50) i")
        _insert(cur, "overseas_financial", {"symbol": "'S' || i", "payload": "repeat('{\"rev\": 123456789, ', 60) || '}'",
                                            "updated_at": KST_OFFSET.format(x="timestamp '2026-11-01 07:00:00'")},
                "generate_series(1, 50) i")
        # ── 뉴스(1년 100만 건) — 구글 뉴스 RSS 처럼 긴 주소, 10% 는 발행 시각 없음(예전 수집분) ──
        _insert(cur, "news", {
            "news_id": "g",
            "title": f"({_arr(NAMES)})[1 + g % 30] || ', ' || ({_arr(PHRASES)})[1 + (g / 30) % 20] || ' (' || g % 97 || ')'",
            "url": "'https://news.google.com/rss/articles/CBMi' || md5(g::text) || md5((g * 31)::text) || "
                   "md5((g * 17)::text) || md5((g * 7)::text) || '?oc=5'",
            "source": f"({_arr(SOURCES)})[1 + g % 10]",
            "published_at": DAY.format(x=news_ts),
            "published_ts": f"CASE WHEN g % 10 = 0 THEN NULL ELSE {KST_OFFSET.format(x=news_ts)} END"},
            f"generate_series(1, {n_news}) g")
        cur.execute("SELECT setval(pg_get_serial_sequence('news', 'news_id'), %s)", (n_news,))
        _insert(cur, "news_stock", {"news_id": "g", "ticker": tk.format(i="1 + (g + k * (1 + (g / 150) % 148)) % 150")},
                f"generate_series(1, {n_news}) g CROSS JOIN (VALUES (0), (1)) v(k) WHERE k = 0 OR g % 10 < 3")
        _insert(cur, "news_sentiment", {"news_id": "g", "label": "(ARRAY['POS','NEG','NEU'])[1 + g % 3]",
                                        "score": "((g % 200) - 100) / 100.0", "model": "'lexicon-v1'",
                                        "classified_at": KST_NAIVE.format(x=f"({news_ts} + interval '30 minutes')")},
                f"generate_series(1, {n_news}) g")
        s_ts = f"(timestamp '2025-11-03 00:00:00' + g * interval '{365 * 86400 / max(n_search, 1):.4f} seconds')"
        _insert(cur, "search_log", {"keyword": f"({_arr(NAMES)})[1 + (g * 7) % 30]",
                                    "searched_at": KST_OFFSET.format(x=s_ts),
                                    "ticker": f"CASE WHEN g % 2 = 0 THEN {tk.format(i='1 + g % 150')} END"},
                f"generate_series(1, {n_search}) g")
        a_ts = f"(timestamp '2025-11-03 00:00:00' + g * interval '{365 * 86400 / max(n_access, 1):.4f} seconds')"
        _insert(cur, "app_user", {"username": usr.format(i="i"), "password_hash": "encode(sha256(i::text::bytea), 'hex')",
                                  "salt": "md5(i::text)", "created_at": KST_NAIVE.format(x="timestamp '2025-01-01' + i * interval '3 hours'")},
                f"generate_series(1, {users}) i")
        _insert(cur, "access_log", {"user_id": usr.format(i="1 + g % 2000"),
                                    "event": "(ARRAY['login','login','logout','register'])[1 + g % 4]",
                                    "detail": "(ARRAY['Chrome · Windows','Safari · iOS','Chrome · Android','Edge · Windows'])[1 + g % 4]",
                                    "at": KST_NAIVE.format(x=a_ts)}, f"generate_series(1, {n_access}) g")
        w_ts = "(timestamp '2025-11-03 00:00:00' + g * interval '157.68 seconds')"
        _insert(cur, "wallet", {"username": usr.format(i="i"), "balance": "100000 + i * 13", "version": "i % 50",
                                "updated_at": KST_NAIVE.format(x="timestamp '2026-11-01 12:00:00'"),
                                "account_no": "'W' || upper(substr(md5(i::text), 1, 12))"}, f"generate_series(1, {users}) i")
        _insert(cur, "wallet_ledger", {"username": usr.format(i="1 + g % 2000"),
                                       "reason": "(ARRAY['attendance','quiz_reward','invest_buy','invest_sell','transfer_in','transfer_out','shop_buy'])[1 + g % 7]",
                                       "amount": "((g % 13) - 6) * 1000", "balance_after": "100000 + g % 77777",
                                       "ref": "'pos:' || g % 9000", "memo": "'메모 ' || g % 100", "idem_key": "'w:' || g",
                                       "created_at": KST_NAIVE.format(x=w_ts)}, f"generate_series(1, {int(200_000 * scale)}) g")
        _insert(cur, "attendance", {"username": usr.format(i="1 + g % 2000"),
                                    "attend_date": DAY.format(x="date '2026-08-01' + (g / 2000)"),
                                    "reward": "100", "streak": "1 + (g / 2000) % 30",
                                    "created_at": KST_NAIVE.format(x="(date '2026-08-01' + (g / 2000)) + interval '9 hours'")},
                f"generate_series(0, {int(150_000 * scale) - 1}) g")
        _insert(cur, "practice_session", {"user_id": usr.format(i="1 + g % 2000"), "scenario_id": "'s' || g % 12",
                                          "cursor_date": DAY.format(x="date '2024-01-02' + g % 300"), "cash": "10000000",
                                          "holding_qty": "g % 100", "holding_avg": "71234.5", "start_cash": "10000000",
                                          "version": "1", "status": "'done'",
                                          "started_at": KST_NAIVE.format(x="timestamp '2026-01-01' + g * interval '50 minutes'"),
                                          "ended_at": KST_NAIVE.format(x="timestamp '2026-01-01' + g * interval '55 minutes'"),
                                          "final_return": "0.0123", "best_return": "0.05", "attempts": "1"},
                "generate_series(1, 10000) g")
        _insert(cur, "practice_trade", {"session_id": "1 + g % 10000", "trade_date": DAY.format(x="date '2024-01-02' + g % 300"),
                                        "side": "(ARRAY['BUY','SELL'])[1 + g % 2]", "quantity": "1 + g % 50",
                                        "price": "(50000 + (g % 400) * 100)::float8", "idem_key": "'p:' || g",
                                        "created_at": KST_NAIVE.format(x="timestamp '2026-01-01' + g * interval '10 minutes'")},
                "generate_series(1, 50000) g")
        c_ts = "(timestamp '2025-11-03 00:00:00' + g * interval '315 seconds')"
        c_read = c_ts[:-1] + " + interval '2 minutes')"
        _insert(cur, "chat_message", {"sender": usr.format(i="1 + g % 2000"), "receiver": usr.format(i="1 + (g * 7 + 1) % 2000"),
                                      "body": "'v2:' || encode(sha256(g::text::bytea), 'base64') || encode(sha256((g + 1)::text::bytea), 'base64')",
                                      "created_at": KST_OFFSET.format(x=c_ts),
                                      "read_at": f"CASE WHEN g % 5 = 0 THEN NULL ELSE {KST_OFFSET.format(x=c_read)} END"},
                "generate_series(1, 100000) g")
        fa, fb = usr.format(i="1 + g % 2000"), usr.format(i="1 + (g % 2000 + 1 + g / 2000) % 2000")
        _insert(cur, "friendship", {"owner": f"CASE WHEN k = 0 THEN {fa} ELSE {fb} END",      # 친구 = 두 방향 행
                                    "friend": f"CASE WHEN k = 0 THEN {fb} ELSE {fa} END",
                                    "created_at": KST_OFFSET.format(x="timestamp '2026-01-01' + g * interval '1 hour'")},
                "generate_series(0, 3999) g CROSS JOIN (VALUES (0), (1)) v(k)")
        _insert(cur, "friend_request", {"sender": usr.format(i="1 + g % 2000"), "receiver": usr.format(i="1 + (g + 5) % 2000"),
                                        "status": "'accepted'",
                                        "created_at": KST_OFFSET.format(x="timestamp '2026-01-01' + g * interval '1 hour'"),
                                        "responded_at": KST_OFFSET.format(x="timestamp '2026-01-01' + g * interval '1 hour' + interval '5 minutes'")},
                "generate_series(1, 5000) g")
        _insert(cur, "invest_position", {"username": usr.format(i="1 + g % 2000"), "ticker": tk.format(i="1 + g % 150"),
                                         "quantity": "1 + g % 30", "buy_price": "(30000 + (g % 500) * 100)::float8",
                                         "buy_date": DAY.format(x="date '2025-11-03' + g % 360"), "cost": "(30000 + (g % 500) * 100) * (1 + g % 30)",
                                         "status": "CASE WHEN g % 3 = 0 THEN 'closed' ELSE 'open' END",
                                         "sell_price": "CASE WHEN g % 3 = 0 THEN (31000 + (g % 500) * 100)::float8 END",
                                         "sell_date": f"CASE WHEN g % 3 = 0 THEN {DAY.format(x='date ' + chr(39) + '2026-10-01' + chr(39) + ' + g % 30')} END",
                                         "proceeds": "0", "realized_pnl": "0",
                                         "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '25 minutes'"),
                                         "sold_qty": "0", "adj_through": "NULL"}, "generate_series(1, 20000) g")
        _insert(cur, "reco_snapshot", {"as_of": DAY.format(x="d"), "profile": "(ARRAY['all','안정형','중립형','공격형'])[p]",
                                       "model_version": "'v3.2'", "rank": "r", "ticker": tk.format(i="1 + (di + r * 7) % 150"),
                                       "name": "'종목' || r", "score": "100 - r * 3.1", "composite": "1.5 - r * 0.05",
                                       "entry_price": "(50000 + r * 1000)::float8", "tag": "'모멘텀'",
                                       "detail": "'{\"factors\": {\"mom\": 0.8, \"val\": 0.2}, \"why\": \"' || repeat('근거 ', 40) || '\"}'",
                                       "created_at": KST_NAIVE.format(x="d + interval '18 hours'")},
                f"(SELECT * FROM {days} WHERE d >= date '2025-11-03') x CROSS JOIN generate_series(1, 4) p "
                f"CROSS JOIN generate_series(1, 20) r")
        _insert(cur, "learning_log", {"user_id": usr.format(i="1 + g % 2000"), "kind": "(ARRAY['term','indicator','technique'])[1 + g % 3]",
                                      "item_key": "'k' || g % 300", "item_name": "'용어 ' || g % 300",
                                      "viewed_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '315 seconds'")},
                "generate_series(1, 100000) g")
        _insert(cur, "survey_result", {"user_id": usr.format(i="1 + g % 2000"), "type": "(ARRAY['안정형','중립형','공격형'])[1 + g % 3]",
                                       "score": "g % 40", "description": "repeat('설명 ', 20)",
                                       "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '100 minutes'")},
                "generate_series(1, 5000) g")
        _insert(cur, "watchlist", {"user_id": usr.format(i="1 + g % 2000"), "ticker": tk.format(i="1 + (g / 2000) % 150"),
                                   "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '50 minutes'")},
                "generate_series(0, 9999) g")
        _insert(cur, "alert_log", {"key": "(ARRAY['job:KRX 일봉','quality:kr_market','job:news','security:login_fail','capacity'])[1 + g % 5]",
                                   "level": "(ARRAY['info','warn','error'])[1 + g % 3]", "title": "'경보 제목 ' || g % 50",
                                   "message": "repeat('알림 본문 ', 12)", "channels": "'webhook'", "delivered": "1",
                                   "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '630 seconds'"),
                                   "ack_by": "CASE WHEN g % 3 = 0 THEN 'admin' END",
                                   "ack_at": f"CASE WHEN g % 3 = 0 THEN {KST_NAIVE.format(x='timestamp ' + chr(39) + '2025-11-03' + chr(39) + ' + g * interval ' + chr(39) + '640 seconds' + chr(39))} END"},
                "generate_series(1, 50000) g")
        _insert(cur, "pipeline_run", {"started_at": KST_NAIVE.format(x="timestamp '2025-11-03 23:00' + g * interval '1 day'"),
                                      "finished_at": KST_NAIVE.format(x="timestamp '2025-11-03 23:20' + g * interval '1 day'"),
                                      "status": "'SUCCESS'", "price_rows": "150", "dart_rows": "10", "news_rows": "2700",
                                      "quality_ok": "1", "quality_issues": "0", "note": "'ok'"}, "generate_series(0, 999) g")
        _insert(cur, "data_quality_issue", {"table_name": "'ohlc'", "ticker": tk.format(i="1 + g % 150"),
                                            "trade_date": DAY.format(x="date '2024-01-02' + g % 600"), "rule": "'gap'",
                                            "detail": "'시가 0 → 종가로 채움'",
                                            "detected_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '1 hour'")},
                "generate_series(1, 5000) g")
        _insert(cur, "app_meta", {"key": "'lab:' || g", "value": "'{\"n\": ' || g || ', \"pad\": \"' || repeat('x', 150) || '\"}'",
                                  "updated_at": KST_OFFSET.format(x="timestamp '2026-11-01' + g * interval '1 minute'")},
                "generate_series(1, 200) g")
        _insert(cur, "realtime_price", {"ticker": tk.format(i="i"), "price": "(70000 + i * 100)::float8", "change_rate": "0.71",
                                        "volume": "1234567", "ts": KST_OFFSET.format(x="timestamp '2026-10-30 15:30:00'"),
                                        "change": "500", "open": "(69500 + i * 100)::float8", "high": "(71000 + i * 100)::float8",
                                        "low": "(69000 + i * 100)::float8", "value": "86400000000",
                                        "session_date": "'2026-10-30'", "source": "'kis'"}, f"generate_series(1, {tick_n}) i")
        _insert(cur, "market_index", {"code": "(ARRAY['KOSPI','KOSDAQ','USDKRW','BOND3Y'])[i]", "name": "'지수' || i",
                                      "value": "2600.12 + i", "change": "1.2", "change_rate": "0.05",
                                      "ts": KST_OFFSET.format(x="timestamp '2026-10-30 15:31:00'"), "source": "'krx'",
                                      "as_of": "'2026-10-30'"}, "generate_series(1, 4) i")
        _insert(cur, "payment_order", {"order_id": "'ord_' || md5(g::text)", "username": usr.format(i="1 + g % 2000"),
                                       "product_id": "'coin_3300'", "coins": "3300", "amount": "3300", "order_name": "'코인 3,300'",
                                       "status": "'paid'", "payment_key": "'v2:' || encode(sha256(g::text::bytea), 'base64')",
                                       "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '100 minutes'"),
                                       "paid_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '100 minutes' + interval '1 minute'")},
                "generate_series(1, 5000) g")
        _insert(cur, "iap_purchase", {"username": usr.format(i="1 + g % 2000"), "store": "'google'", "product_id": "'coin_1100'",
                                      "coins": "1100", "price": "'₩1,100'", "receipt": "'h:' || encode(sha256(g::text::bytea), 'hex')",
                                      "status": "'verified'",
                                      "created_at": KST_NAIVE.format(x="timestamp '2025-11-03' + g * interval '4 hours'")},
                "generate_series(1, 2000) g")
        _insert(cur, "financial_statement", {"ticker": tk.format(i="i"), "fiscal_year": "y", "report_code": "rc",
                                             "revenue": "1000000000000", "operating_profit": "100000000000",
                                             "net_income": "80000000000", "total_equity": "500000000000", "debt_ratio": "85.2",
                                             "disclosed_at": DAY.format(x="make_date(y + 1, 3, 15)"),
                                             "ingested_at": KST_NAIVE.format(x="make_date(y + 1, 3, 16) + interval '10 hours'"),
                                             "total_assets": "2000000000000", "fs_basis": "'CFS'"},
                f"generate_series(1, {tick_n}) i CROSS JOIN generate_series(2021, 2025) y "
                f"CROSS JOIN (VALUES ('11011'), ('11012'), ('11013'), ('11014')) r(rc)")
        _insert(cur, "ranking", {"ranking_type": "(ARRAY['volume','value','up','down','mktcap'])[t]", "rank": "r",
                                 "ticker": tk.format(i="1 + (t * 31 + r) % 150"), "metric_value": "r * 1.5", "as_of": "'2026-10-30'"},
                "generate_series(1, 5) t CROSS JOIN generate_series(1, 50) r")
        _insert(cur, "dart_corp", {"corp_code": "lpad(g::text, 8, '0')", "corp_name": "'회사 ' || g",
                                   "stock_code": "lpad((g * 7)::text, 6, '0')", "modify_date": "'20260101'",
                                   "fetched_at": KST_OFFSET.format(x="timestamp '2026-10-28 03:00:00'")},
                "generate_series(1, 3000) g")
        # ── 예전 스키마(문자열 열)일 때만: 형식이 깨진 값 — 마이그레이션이 격리소에 남기고 NULL 로 바꾸는지 확인 ──
        broken = [("news", "published_ts", "news_id", ["", "N/A", "2026-02-30T10:00:00+09:00"]),
                  ("access_log", "at", "log_id", ["2026-13-01T00:00:00"]),
                  ("attendance", "attend_date", "id", ["2026/10/31"])]
        for table, col, pk, vals in broken:
            if "character" not in _types(cur, table).get(col, ""):
                continue
            for k, v in enumerate(vals, start=1):
                cur.execute(f"UPDATE {table} SET {col} = %s WHERE {pk} = %s", (v, 1000 + k))
            print(f"  (깨진 값 {len(vals)}개 → {table}.{col})")
        conn.commit()
    with psycopg.connect(url, autocommit=True) as conn:
        t0 = time.perf_counter()
        conn.execute("VACUUM (ANALYZE)")
        print(f"VACUUM ANALYZE {time.perf_counter() - t0:.1f}s")


def measure(url, label="", tables=None):
    url = _guard(url)
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET app.rls = 'off'")                     # 행 수준 보안(FORCE RLS) 표도 전체 행 수를 센다
        cur.execute("SELECT version(), current_setting('server_version_num')::int, pg_database_size(current_database())")
        ver, vnum, dbsize = cur.fetchone()
        cur.execute(
            "SELECT c.relname, pg_relation_size(c.oid) AS heap, "
            "COALESCE(pg_total_relation_size(NULLIF(c.reltoastrelid, 0)), 0) AS toast, "
            "pg_indexes_size(c.oid) AS idx, pg_total_relation_size(c.oid) AS total, "
            "pg_relation_size(c.oid, 'fsm') + pg_relation_size(c.oid, 'vm') AS maps "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY pg_total_relation_size(c.oid) DESC")
        rows = [dict(zip(["table", "heap", "toast", "idx", "total", "maps"], r)) for r in cur.fetchall()]
        for r in rows:
            if tables and r["table"] not in tables:
                continue
            cur.execute(f'SELECT count(*) FROM public."{r["table"]}"')
            r["rows"] = cur.fetchone()[0]
            r["bytes_per_row"] = round(r["heap"] / r["rows"], 1) if r["rows"] else None
        cur.execute(
            "SELECT t.relname, i.relname, pg_relation_size(i.oid) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' ORDER BY 3 DESC")
        idx = [{"table": a, "index": b, "bytes": c} for a, b, c in cur.fetchall()]
        cur.execute("SELECT a.attrelid::regclass::text, a.attname, format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped")
        types = {f"{t}.{c}": ty for t, c, ty in cur.fetchall()}
    return {"label": label, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "server": ver.split(",")[0], "server_num": vnum,
            "db_bytes": dbsize, "tables": rows, "indexes": idx, "types": types}


BENCH = {
    "뉴스 7일 집계": "SELECT count(*) FROM news WHERE published_at >= '2026-10-26'",
    "종목 뉴스 최신 20": "SELECT n.title FROM news_stock s JOIN news n USING (news_id) WHERE s.ticker = '101237' "
                    "ORDER BY n.news_id DESC LIMIT 20",
    "캔들 1년(1종목)": "SELECT trade_date, open, high, low, close FROM ohlc WHERE ticker = '101237' "
                   "AND trade_date >= '2025-10-30' ORDER BY trade_date",
    "전 종목 최신 종가(전체 정렬)": "SELECT DISTINCT ON (ticker) ticker, close FROM daily_price ORDER BY ticker, trade_date DESC",
    "인기 검색 7일": "SELECT keyword, count(*) FROM search_log WHERE searched_at >= '2026-10-26' GROUP BY 1 "
                 "ORDER BY 2 DESC LIMIT 10",
    "원장 1인 최근 50": "SELECT * FROM wallet_ledger WHERE username = 'user00042' ORDER BY ledger_id DESC LIMIT 50",
}


def bench(url, label="", reps=31):
    url = _guard(url)
    out = {"label": label, "queries": {}}
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET app.rls = 'off'")
        for name, sql in BENCH.items():
            ts = []
            for _ in range(reps):
                t0 = time.perf_counter()
                cur.execute(sql)
                cur.fetchall()
                ts.append((time.perf_counter() - t0) * 1000)
            out["queries"][name] = {"median_ms": round(statistics.median(ts[1:]), 2), "min_ms": round(min(ts), 2)}
    return out


def _mb(b):
    return f"{b / 1048576:,.1f}"


def compare(a, b):
    """두 measure 결과 → 마크다운 표(크기가 바뀐 표만, 큰 순)."""
    ta, tb = {r["table"]: r for r in a["tables"]}, {r["table"]: r for r in b["tables"]}
    lines = [f"| 표 | 행 | {a['label']} 전체(MB) | {b['label']} 전체(MB) | 차이 | 본문 B/행 전→후 | 인덱스 MB 전→후 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name in sorted(set(ta) | set(tb), key=lambda n: -(ta.get(n, {}).get("total", 0))):
        x, y = ta.get(name, {}), tb.get(name, {})
        if not x.get("total") and not y.get("total"):
            continue
        d = y.get("total", 0) - x.get("total", 0)
        if abs(d) < 8192 * 2:
            continue
        pct = f"{d / x['total'] * 100:+.0f}%" if x.get("total") else "새 표"
        lines.append(f"| {name} | {y.get('rows', x.get('rows', 0)):,} | {_mb(x.get('total', 0))} | {_mb(y.get('total', 0))} | "
                     f"{pct} | {x.get('bytes_per_row') or '-'} → {y.get('bytes_per_row') or '-'} | "
                     f"{_mb(x.get('idx', 0))} → {_mb(y.get('idx', 0))} |")
    d = b["db_bytes"] - a["db_bytes"]
    lines.append(f"| **DB 전체** | | **{_mb(a['db_bytes'])}** | **{_mb(b['db_bytes'])}** | **{d / a['db_bytes'] * 100:+.0f}%** | | |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--db", required=True)
    s.add_argument("--scale", type=float, default=1.0)
    for name in ("measure", "bench"):
        m = sub.add_parser(name)
        m.add_argument("--db", required=True)
        m.add_argument("--label", default="")
        m.add_argument("--out")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = ap.parse_args(argv)
    if args.cmd == "seed":
        seed(args.db, args.scale)
    elif args.cmd in ("measure", "bench"):
        res = measure(args.db, args.label) if args.cmd == "measure" else bench(args.db, args.label)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.cmd == "measure":
            print(f"DB {_mb(res['db_bytes'])} MB · {res['server']}")
            for r in res["tables"][:15]:
                print(f"  {r['table']:<22} {r.get('rows', 0):>10,} 행  전체 {_mb(r['total']):>8} MB  "
                      f"(본문 {_mb(r['heap'])} · 인덱스 {_mb(r['idx'])} · TOAST {_mb(r['toast'])})")
        else:
            for k, v in res["queries"].items():
                print(f"  {k:<16} 중앙값 {v['median_ms']:8.2f} ms")
    else:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.a, args.b))
        print(compare(a, b))


if __name__ == "__main__":
    main()
