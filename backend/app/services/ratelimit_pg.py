"""시도 제한 — PostgreSQL 공유 저장소(슬라이딩 로그). ratelimit.py 와 같은 의미·같은 함수 이름.

메모리 방식은 uvicorn 워커가 4개면 한도도 4배가 된다(워커마다 따로 셈). 여기서는 모든 워커·서버가 같은 표를 본다.
  · 원자성: 키마다 트랜잭션 advisory lock(키 이름순으로 잡아 교착 없음) 안에서 '확인 + 기록' — 동시에 몰려도 정확히 한도만 통과.
  · 시각: DB 시계(clock_timestamp) 하나로만 — 서버마다 시계가 조금 달라도 창 계산이 어긋나지 않는다.
  · 표는 UNLOGGED(빠름·충돌 시 비워짐) + 가끔 1시간 지난 기록 정리.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy import text

from app import db

MAX_WINDOW = 3600


def take(rules, window):
    with db.transaction() as conn:
        for key, _limit in sorted(rules):
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('rl:' || :k))"), {"k": key})
        now = conn.execute(text("SELECT clock_timestamp()")).scalar()
        since = now - timedelta(seconds=window)
        wait = 0
        for key, limit in rules:
            recent = conn.execute(text("SELECT at FROM rate_event WHERE key=:k AND at > :s ORDER BY at DESC LIMIT :n"),
                                  {"k": key, "s": since, "n": limit}).scalars().all()
            if len(recent) >= limit:
                wait = max(wait, max(1, int((recent[limit - 1] + timedelta(seconds=window) - now).total_seconds()) + 1))
        if wait:
            return wait
        conn.execute(text("INSERT INTO rate_event(key, at) VALUES " + ", ".join(f"(:k{i}, :n)" for i in range(len(rules)))),
                     {"n": now, **{f"k{i}": k for i, (k, _l) in enumerate(rules)}})
        if random.random() < 0.01:                       # noqa: S311 — 정리 주기(보안 무관)
            conn.execute(text("DELETE FROM rate_event WHERE at < :c"), {"c": now - timedelta(seconds=MAX_WINDOW)})
        return 0


def undo(key):
    db.execute("DELETE FROM rate_event WHERE id = (SELECT id FROM rate_event WHERE key=:k ORDER BY id DESC LIMIT 1)",
               {"k": key})


def reset(key):
    db.execute("DELETE FROM rate_event WHERE key=:k", {"k": key})


def count(key, window):
    return db.query("SELECT COUNT(*) AS n FROM rate_event WHERE key=:k AND at > clock_timestamp() - make_interval(secs => :w)",
                    {"k": key, "w": window})[0]["n"]


def clear():
    db.execute("DELETE FROM rate_event")
