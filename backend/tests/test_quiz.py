"""Daily Stock Quiz — 서버가 화면과 '같은 문제'를 만들어 채점(점수를 화면이 정하지 않는다) · 하루 한 번 · 계정별 격리 ·
정답 보상(점수 × 단가)은 기록과 같은 트랜잭션에서 지갑·원장·분개로, 한국 날짜 하루 1번(부분 유일 인덱스)."""
import json
import shutil
import subprocess
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.api import app
from app.config import settings
from app.services import account, auth, quiz, wallet

client = TestClient(app)
WEB_QUIZ = Path(__file__).resolve().parents[1] / "web" / "assets" / "daily-quiz.js"
PW = "Quiz-pass-2026"


def _user(name):
    auth.register(name, PW)
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}


@pytest.mark.skipif(not shutil.which("node"), reason="node 없음")
def test_python_generator_equals_screen_code_for_many_dates():
    dates = [(date(2026, 1, 1) + timedelta(days=i * 11)).isoformat() for i in range(40)] + ["2024-02-29", "2031-12-31"]
    js = WEB_QUIZ.read_text(encoding="utf-8") + (
        f"\nconst ds={json.dumps(dates)};"
        "process.stdout.write(JSON.stringify(ds.map(d => buildDailyQuiz(d).map(q => [q.q, q.options, q.answer]))));")
    out = subprocess.run([shutil.which("node"), "-"], input=js, capture_output=True, text=True, timeout=60, encoding="utf-8")
    expected = json.loads(out.stdout)
    for d, exp in zip(dates, expected):
        got = [[q["q"], q["options"], q["answer"]] for q in quiz.build(d)]
        assert got == exp, d


def test_daily_set_shape_is_deterministic():
    a, b = quiz.build("2026-10-27"), quiz.build("2026-10-27")
    assert a == b and len(a) == quiz.N
    assert [q["type"] for q in a] == ["case"] * 3 + ["deep"] * 2
    assert all(sorted(q["options"]) == sorted(next(x for x in quiz.bank() if x["q"] == q["q"])["options"]) for q in a)
    assert quiz.build("2026-10-28") != a


def test_server_grades_from_answers_and_keeps_first_result():
    h = _user("quiz-a")
    today = clock.today_str()
    key = [q["answer"] for q in quiz.build(today)]
    assert client.get("/learn/quiz", headers=h).json() == {"date": today, "done": False, "answers": None, "score": None,
                                                         "total": 5, "reward": 0, "total_days": 0,
                                                         "reward_per_correct": settings.QUIZ_REWARD_PER_CORRECT}
    wrong = [(k + 1) % 4 for k in key]
    answers = key[:3] + wrong[3:]                                      # 3개 맞힘
    r = client.post("/learn/quiz", headers=h, json={"date": today, "answers": answers}).json()
    assert r["score"] == 3 and r["done"] and not r["duplicate"] and r["total_days"] == 1
    again = client.post("/learn/quiz", headers=h, json={"date": today, "answers": key}).json()   # 다시 내도 처음 결과
    assert again["duplicate"] and again["score"] == 3 and again["answers"] == answers
    assert client.get("/learn/quiz", headers=h).json()["answers"] == answers


@pytest.mark.parametrize("body", [
    {"date": "2026-1-5", "answers": [0, 0, 0, 0, 0]},
    {"date": "1999-01-01", "answers": [0, 0, 0, 0, 0]},
    {"date": None, "answers": [0, 0, 0, 0, 0]},
    {"answers": [0, 0, 0, 0]},
    {"answers": [0, 0, 0, 0, 4]},
    {"answers": [0, 0, 0, 0, -1]},
    {"answers": [0, 0, 0, 0, 0, 0]},
])
def test_bad_submissions_are_rejected(body):
    h = _user("quiz-bad")
    body = {"date": clock.today_str(), **body}
    assert client.post("/learn/quiz", headers=h, json=body).status_code in (400, 422)
    assert client.get("/learn/quiz", headers=h).json()["total_days"] == 0


def test_only_today_give_or_take_a_day():
    t = date(2026, 10, 27)
    assert quiz._check_date("2026-10-26", t) == "2026-10-26" and quiz._check_date("2026-10-28", t) == "2026-10-28"
    for bad in ("2026-10-25", "2026-10-29", "2026-02-30"):
        with pytest.raises(quiz.QuizError):
            quiz._check_date(bad, t)


def test_login_required_and_results_are_per_user():
    assert client.get("/learn/quiz").status_code == 401
    assert client.post("/learn/quiz", json={"date": clock.today_str(), "answers": [0] * 5}).status_code in (401, 403)
    a, b = _user("quiz-p1"), _user("quiz-p2")
    client.post("/learn/quiz", headers=a, json={"date": clock.today_str(), "answers": [1] * 5})
    assert client.get("/learn/quiz", headers=b).json()["done"] is False
    assert client.get("/learn/quiz", headers=a).json()["done"] is True


def test_account_deletion_removes_quiz_rows():
    h = _user("quiz-del")
    client.post("/learn/quiz", headers=h, json={"date": clock.today_str(), "answers": [2] * 5})
    account.delete_account("quiz-del")
    assert db.query("SELECT COUNT(*) AS n FROM quiz_attempt WHERE user_id='quiz-del'")[0]["n"] == 0


# ── 정답 보상(팀 시안 2026-10-28: 1문제당 10만 원) ─────────────────────────────
def _answers(day, right):
    key = [q["answer"] for q in quiz.build(day)]
    return key[:right] + [(k + 1) % 4 for k in key[right:]]


def _rewards(u):
    return db.query("SELECT amount, memo, ref, ledger_id FROM wallet_ledger WHERE username=:u AND reason='quiz_reward' "
                    "ORDER BY ledger_id", {"u": u})


def test_reward_is_score_times_rate_paid_once_with_balanced_journal(monkeypatch):
    monkeypatch.setattr(settings, "QUIZ_REWARD_PER_CORRECT", 100_000)
    h = _user("quiz-pay")
    today = clock.today_str()
    before = wallet.balance("quiz-pay")
    r = client.post("/learn/quiz", headers=h, json={"date": today, "answers": _answers(today, 3)}).json()
    assert r["score"] == 3 and r["reward"] == 300_000 and r["balance"] == before + 300_000 and "reward_skipped" not in r
    again = client.post("/learn/quiz", headers=h, json={"date": today, "answers": _answers(today, 5)}).json()
    assert again["duplicate"] and again["reward"] == 300_000 and "balance" not in again           # 다시 내도 다시 안 준다
    assert wallet.balance("quiz-pay") == before + 300_000
    rows = _rewards("quiz-pay")
    assert len(rows) == 1 and rows[0]["memo"] == "오늘의 퀴즈 3문제 정답" and rows[0]["ref"] == today
    je = db.query("SELECT account, amount FROM journal_entry WHERE txn_id = "
                  "(SELECT txn_id FROM journal_entry WHERE ledger_id=:l)", {"l": rows[0]["ledger_id"]})  # 두 줄 분개
    assert sum(j["amount"] for j in je) == 0 and {"sys:promotion"} <= {j["account"] for j in je}   # 상대 계정 = 판촉비
    assert wallet.reconcile("quiz-pay")["ok"]
    assert client.get("/learn/quiz", headers=h).json()["reward"] == 300_000
    logs = client.get("/me/logs/wallet?filter=reward", headers=h).json()["items"]                # 마이페이지 '내 활동'
    assert any(x["reason_ko"] == "퀴즈 보상" and x["amount"] == 300_000 for x in logs)


def test_reward_only_once_per_korean_day_even_across_quiz_dates(monkeypatch):
    """앞뒤 하루 날짜(시간대 여유)를 섞어 내도 보상은 하루 1번 — 기록은 남는다."""
    monkeypatch.setattr(settings, "QUIZ_REWARD_PER_CORRECT", 100_000)
    h = _user("quiz-once")
    today = date.fromisoformat(clock.today_str())
    days = [(today + timedelta(days=k)).isoformat() for k in (0, -1, 1)]
    got = [client.post("/learn/quiz", headers=h, json={"date": d, "answers": _answers(d, 5)}).json() for d in days]
    assert got[0]["reward"] == 500_000
    assert all(g["reward"] == 0 and g["reward_skipped"] and not g["duplicate"] for g in got[1:])
    assert len(_rewards("quiz-once")) == 1 and got[2]["total_days"] == 3


def test_parallel_submits_pay_a_single_reward(monkeypatch):
    """두 탭·두 기기에서 같은 날짜·다른 날짜를 동시에 — DB 유일 조건이 줄을 세워 정확히 1번만 지급."""
    monkeypatch.setattr(settings, "QUIZ_REWARD_PER_CORRECT", 100_000)
    auth.register("quiz-race", PW)
    today = date.fromisoformat(clock.today_str())
    days = [today.isoformat(), today.isoformat(), (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()]
    gate, out, errs = threading.Barrier(len(days)), [None] * len(days), []

    def run(i):
        gate.wait()
        try:
            out[i] = quiz.submit("quiz-race", days[i], _answers(days[i], 4))
        except Exception as e:                              # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(days))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert sum(1 for o in out if o["reward"] > 0 and not o["duplicate"]) == 1
    assert len(_rewards("quiz-race")) == 1 and _rewards("quiz-race")[0]["amount"] == 400_000
    assert db.query("SELECT COUNT(*) AS n FROM quiz_attempt WHERE user_id='quiz-race'")[0]["n"] == 3
    assert wallet.reconcile("quiz-race")["ok"]


def test_no_reward_for_zero_score_or_when_switched_off(monkeypatch):
    h = _user("quiz-zero")
    today = clock.today_str()
    r = client.post("/learn/quiz", headers=h, json={"date": today, "answers": _answers(today, 0)}).json()
    assert r["score"] == 0 and r["reward"] == 0 and not _rewards("quiz-zero")
    monkeypatch.setattr(settings, "QUIZ_REWARD_PER_CORRECT", 0)
    h2 = _user("quiz-off")
    r = client.post("/learn/quiz", headers=h2, json={"date": today, "answers": _answers(today, 5)}).json()
    assert r["score"] == 5 and r["reward"] == 0 and r["reward_per_correct"] == 0 and not _rewards("quiz-off")


def test_reward_rolls_back_with_the_attempt_when_wallet_fails(monkeypatch):
    """지급이 실패하면 기록도 남지 않는다(같은 트랜잭션) — 다시 내면 그때 기록·지급."""
    monkeypatch.setattr(settings, "QUIZ_REWARD_PER_CORRECT", 100_000)
    auth.register("quiz-fail", PW)
    today = clock.today_str()
    real = wallet.credit

    def boom(*a, **k):
        raise wallet.WalletError("다른 요청과 겹쳤어요 — 잠시 후 다시 시도해 주세요")
    monkeypatch.setattr(wallet, "credit", boom)
    with pytest.raises(wallet.WalletError):
        quiz.submit("quiz-fail", today, _answers(today, 2))
    assert db.query("SELECT COUNT(*) AS n FROM quiz_attempt WHERE user_id='quiz-fail'")[0]["n"] == 0
    monkeypatch.setattr(wallet, "credit", real)
    assert quiz.submit("quiz-fail", today, _answers(today, 2))["reward"] == 200_000
