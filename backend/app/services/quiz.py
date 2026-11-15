"""Daily Stock Quiz(웹 '용어사전' 화면) — 서버 채점·하루 한 번 기록.

화면(web/learn.html)은 '그날의 5문제'를 날짜로 정한다(web/assets/daily-quiz.js 의 buildDailyQuiz). 서버는 같은 문제은행
(app/data/daily_quiz.json — tools/build_daily_quiz.py 가 같은 파일에서 꺼낸다)과 같은 공식을 파이썬으로 옮겨,
화면이 보낸 '고른 보기 번호'만으로 직접 채점한다. 점수는 화면이 정하지 않는다(보낸 값은 답뿐).

공식: 시드 'outofstock-quiz-YYYY-MM-DD' → FNV-1a 32비트 해시 → mulberry32 난수 → 상황형 3 + 심화 2 를 Fisher–Yates 로
뽑고 문제마다 보기 순서도 섞는다(정답 = 원래 0번 보기의 새 위치). JS 의 32비트 정수 연산(Math.imul·>>>·ToInt32)을 그대로
따라 한다 — tests/test_quiz.py 가 node 로 실제 화면 코드를 돌린 결과와 여러 날짜를 대조한다.

기록: 계정·날짜마다 1행(quiz_attempt, 행 수준 보안). 같은 날 두 번째 제출은 바꾸지 않고 처음 결과를 돌려준다(duplicate).
날짜는 화면(브라우저)의 오늘 — 시간대 차이를 감안해 한국 날짜 기준 하루 앞뒤까지만 받는다(지난 퀴즈를 몰아서 채우지 못하게).

보상(팀 시안 2026-10-28 — 정답 1문제당 10만 원): 서버가 채점한 점수 × QUIZ_REWARD_PER_CORRECT 를 기록과 **같은 트랜잭션**에서
지갑·원장·분개로 지급한다(기록만 남고 돈이 안 들어가거나, 돈만 들어가는 일이 없다). 지급은 한국 날짜 하루 1번 —
(user_id, reward_day) 부분 유일 인덱스가 DB 에서 보장하므로, 앞뒤 하루 날짜를 섞어 내거나 두 탭에서 동시에 내도 두 번 받지 못한다.
그날 보상을 이미 받았으면 기록은 남기되 보상은 0(reward_skipped). 정답 키는 화면 파일에도 있어 하루 상한(5문제 × 보상)까지는
누구나 받을 수 있다 — 교육용 가상머니라 감수, 상한을 넘는 반복 지급만 막는다.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text

from app import clock, db
from app.config import settings

N = 5                       # 하루 문제 수(상황 3 + 심화 2)
PICK = (("case", 3), ("deep", 2))
OPTIONS = 4
SEED_PREFIX = "outofstock-quiz-"
_PATH = Path(__file__).resolve().parents[1] / "data" / "daily_quiz.json"
_M32 = 0xFFFFFFFF


class QuizError(ValueError):
    """제출 형식·날짜 오류 — 400."""


@lru_cache(maxsize=1)
def bank() -> list[dict]:
    return json.loads(_PATH.read_text(encoding="utf-8"))["items"]


def _imul(a: int, b: int) -> int:
    """JS Math.imul — 32비트 곱의 아래 32비트(부호 없는 표현으로 들고 다닌다. 비트는 JS 와 같다)."""
    return (a * b) & _M32


def rng(seed: str):
    """JS quizRandom(seedStr) 과 같은 수열. charCodeAt 은 UTF-16 코드 단위 — 그대로 따른다."""
    h = 2166136261
    raw = seed.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        h = _imul((h ^ (raw[i] | (raw[i + 1] << 8))) & _M32, 16777619)
    state = [h]

    def nxt() -> float:
        state[0] = (state[0] + 0x6D2B79F5) & _M32          # JS 는 이 합을 감싸지 않지만 이후 연산이 모두 mod 2^32 라 같다
        t = state[0]
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) & _M32
        return ((t ^ (t >> 14)) & _M32) / 4294967296
    return nxt


def _shuffle(arr: list, rnd) -> list:
    a = list(arr)
    for i in range(len(a) - 1, 0, -1):
        j = math.floor(rnd() * (i + 1))                     # u/2^32 × 작은 정수 — 배정밀도에서 정확(JS 와 같은 값)
        a[i], a[j] = a[j], a[i]
    return a


def build(date_str: str) -> list[dict]:
    """그날의 5문제 — 화면과 같은 순서·같은 보기 순서. answer = 정답 보기의 위치(0~3)."""
    rnd = rng(SEED_PREFIX + date_str)
    chosen: list[dict] = []
    for kind, n in PICK:                                    # JS 와 같은 순서로 난수를 쓴다(상황형 → 심화)
        chosen += _shuffle([q for q in bank() if q["type"] == kind], rnd)[:n]
    out = []
    for q in chosen:
        order = _shuffle(list(range(OPTIONS)), rnd)
        out.append({**q, "options": [q["options"][i] for i in order], "answer": order.index(0)})
    return out


def _check_date(date_str: str, today: date | None = None) -> str:
    try:
        if not isinstance(date_str, str) or len(date_str) != 10:
            raise ValueError(date_str)
        d = date.fromisoformat(date_str)
    except ValueError:
        raise QuizError("날짜는 YYYY-MM-DD 형식이어야 해요") from None
    t = today or clock.today()
    if not (t - timedelta(days=1) <= d <= t + timedelta(days=1)):
        raise QuizError("오늘의 퀴즈만 제출할 수 있어요")
    return d.isoformat()


def per_correct() -> int:
    return max(0, int(settings.QUIZ_REWARD_PER_CORRECT or 0))


def _row(r) -> dict:
    answers = [int(x) for x in r["answers"].split(",")]
    return {"date": r["quiz_date"], "done": True, "answers": answers, "score": int(r["score"]), "total": N,
            "reward": int(r["reward"] or 0)}


_COLS = "quiz_date, answers, score, reward"


def total_days(user_id: str) -> int:
    r = db.query("SELECT COUNT(*) AS n FROM quiz_attempt WHERE user_id=:u", {"u": user_id})
    return int(r[0]["n"]) if r else 0


def state(user_id: str, date_str: str | None = None) -> dict:
    """그날 퀴즈 기록(없으면 done=false) + 지금까지 푼 날 수."""
    d = _check_date(date_str) if date_str else clock.today_str()
    rows = db.query(f"SELECT {_COLS} FROM quiz_attempt WHERE user_id=:u AND quiz_date=:d",
                    {"u": user_id, "d": d})
    out = _row(rows[0]) if rows else {"date": d, "done": False, "answers": None, "score": None, "total": N, "reward": 0}
    out["total_days"] = total_days(user_id)
    out["reward_per_correct"] = per_correct()
    return out


def submit(user_id: str, date_str: str, answers: list[int]) -> dict:
    """채점·기록·보상 — 같은 날짜를 이미 냈으면 처음 결과 그대로(duplicate: true, 보상 다시 없음)."""
    d = _check_date(date_str)
    if not isinstance(answers, list) or len(answers) != N or \
            not all(isinstance(a, int) and not isinstance(a, bool) and 0 <= a < OPTIONS for a in answers):
        raise QuizError(f"{N}문제의 답(보기 번호 0~{OPTIONS - 1})을 모두 보내 주세요")
    items = build(d)
    score = sum(1 for a, q in zip(answers, items) if a == q["answer"])
    reward = score * per_correct()
    today = clock.today_str()                               # 보상 날짜는 서버(한국) 기준 — 문제 날짜와 따로
    params = {"u": user_id, "d": d, "a": ",".join(map(str, answers)), "s": score, "t": clock.utc()}
    ins = ("INSERT INTO quiz_attempt(user_id, quiz_date, answers, score, created_at, reward, reward_day) "
           "VALUES(:u, :d, :a, :s, :t, :r, :rd) ON CONFLICT DO NOTHING RETURNING quiz_date")
    skipped = False
    balance = None
    with db.transaction() as conn:
        # 보상이 있으면 '오늘 보상'까지 걸고 넣는다. 충돌 대상을 정하지 않은 ON CONFLICT 는 두 유일 조건
        # (계정·문제 날짜 PK, 계정·보상 날짜 부분 인덱스) 어느 쪽에 걸려도 아무것도 하지 않는다 — 동시에 온 요청은
        # 앞 트랜잭션이 끝날 때까지 기다렸다가 판단한다(앞이 롤백되면 이쪽이 넣는다).
        done = reward > 0 and conn.execute(text(ins), {**params, "r": reward, "rd": today}).first() is not None
        if not done:
            if reward > 0 and conn.execute(text("SELECT 1 FROM quiz_attempt WHERE user_id=:u AND quiz_date=:d"),
                                           {"u": user_id, "d": d}).first() is None:
                skipped = True                              # 이 문제는 처음인데 오늘 보상은 이미 받음(다른 날짜 문제로)
            ok = conn.execute(text(ins), {**params, "r": 0, "rd": None}).first() is not None
            if not ok:                                      # 다른 기기·다른 탭에서 먼저 냈다 — 그 결과가 원본
                row = conn.execute(text(f"SELECT {_COLS} FROM quiz_attempt WHERE user_id=:u AND quiz_date=:d"),
                                   {"u": user_id, "d": d}).mappings().first()
                out = _row(row)
                out.update(duplicate=True, total_days=_days(conn, user_id), reward_per_correct=per_correct())
                return out
            reward = 0
        if reward > 0:
            from app.services import wallet
            balance = wallet.credit(conn, user_id, reward, "quiz_reward", ref=d,
                                    memo=f"오늘의 퀴즈 {score}문제 정답", idem_key=f"quiz:{user_id}:{d}")["balance"]
        days = _days(conn, user_id)
    out = {"date": d, "done": True, "answers": list(answers), "score": score, "total": N, "reward": reward,
           "duplicate": False, "total_days": days, "reward_per_correct": per_correct()}
    if skipped:
        out["reward_skipped"] = "오늘은 이미 퀴즈 보상을 받았어요"
    if balance is not None:
        out["balance"] = balance
    return out


def _days(conn, user_id: str) -> int:
    return int(conn.execute(text("SELECT COUNT(*) FROM quiz_attempt WHERE user_id=:u"), {"u": user_id}).scalar() or 0)


def delete_user(conn, user_id: str) -> None:
    conn.execute(text("DELETE FROM quiz_attempt WHERE user_id=:u"), {"u": user_id})
