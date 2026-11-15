"""이상거래탐지(FDS) — 송금 직전에 규칙 점수를 매겨 허용 / 추가 인증 / 차단을 정한다.

구성(평가 가능하게 분리)
  features()  DB 에서 판단 재료를 모은다(보내는 사람 이력·받는 사람 유입·세션 위험 신호). 받는 쪽 유입은 남의 행 —
              송금 트랜잭션(RLS 상승 사유 'transfer')의 연결로 읽는다.
  score()     재료 → 점수·걸린 규칙(순수 함수 — DB 없이 시험·평가: tools/fds_eval.py 가 합성 시나리오로 정밀도·재현율 측정)
  screen()    점수 → 결정(allow | step_up | block). 송금(transfer.send)이 두 지갑을 잠그고 같은 키의 처리 여부를 확인한
              '뒤' 같은 트랜잭션에서 부른다 — 판정과 실행 사이에 틈이 없다(TOCTOU 제거, 결함 20).
              예전엔 API 가 잠금 밖에서 먼저 채점해, 동시에 보낸 송금들이 서로를 못 보고 '최근 10분 0건'으로 통과했다.
  record()    fds_decision 에 기록 — 실행된 송금은 같은 트랜잭션에서(송금 키 ref 로 연결: '판정 없이 실행된 송금 0건'을
              데이터 계약이 매일 확인), 막힌 시도는 롤백 뒤 따로(막힌 기록도 남는다).
규칙(점수는 policy 와 같은 방식으로 코드 한곳에서 관리 — RULES)
  velocity      최근 10분 송금 5건 이상                          +40  (계정 탈취 후 연속 인출)
  spike         평소(최근 30건 중앙값)의 5배 이상 + 30만원 이상     +25
  drain         송금 후 잔액이 1% 미만 + 100만원 이상            +20  (털어 가기)
  new_payee     처음 보내는 사람에게 100만원 이상                  +20
  ato_session   이 세션에 IP 대역 변경 신호                       +20
  recent_pw     24시간 안에 비밀번호 변경                         +15
  fan_in        받는 사람이 최근 1시간 5명 이상에게서 받음          +35  (대포통장·모집책 계좌 패턴)
  round_trip    30분 안 반대 방향 송금 + 100만원 이상              +20  (세탁·자전거래)
  charge_out    1시간 안 충전(50만원 이상)한 금액과 비슷한 금액을 송금 +25
  night_large   01~05시(KST) 100만원 이상                        +10
결정: 70점 이상 차단 · 40점 이상 추가 인증(최근 5분 재인증이면 통과) · 그 외 허용.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

from sqlalchemy import text

from app import clock, db

BLOCK, STEP_UP = 70, 40
LARGE = 1_000_000


@dataclass(frozen=True)
class Rule:
    key: str
    points: int
    desc: str


RULES = {r.key: r for r in (
    Rule("velocity", 40, "최근 10분 송금 5건 이상"),
    Rule("spike", 25, "평소 송금액(중앙값)의 5배 이상(30만원 이상)"),
    Rule("drain", 20, "송금 후 잔액 1% 미만(100만원 이상 송금)"),
    Rule("new_payee", 20, "처음 보내는 사람에게 100만원 이상"),
    Rule("ato_session", 20, "세션 IP 대역 변경(계정 탈취 의심)"),
    Rule("recent_pw", 15, "24시간 안 비밀번호 변경"),
    Rule("fan_in", 35, "받는 사람이 최근 1시간 5명 이상에게서 받음"),
    Rule("round_trip", 20, "30분 안 반대 방향 송금(100만원 이상)"),
    Rule("charge_out", 25, "1시간 안 충전(50만원 이상)한 금액과 비슷한 금액을 바로 송금"),
    Rule("night_large", 10, "새벽(01~05시) 100만원 이상"),
)}


def score(f: dict) -> tuple[int, list[str]]:
    """순수 함수 — 재료(dict) → (점수, 걸린 규칙 키 목록)."""
    hits = []
    amt = int(f.get("amount") or 0)
    if f.get("sent_10m", 0) >= 5:
        hits.append("velocity")
    med = f.get("median_amount")
    if med and f.get("history", 0) >= 5 and amt >= 5 * med and amt >= 300_000:
        hits.append("spike")
    if amt >= LARGE and f.get("balance_after", 1) < 0.01 * max(1, f.get("balance_before", 1)):
        hits.append("drain")
    if amt >= LARGE and not f.get("known_payee"):
        hits.append("new_payee")
    if f.get("ip_change"):
        hits.append("ato_session")
    if f.get("pw_changed_24h"):
        hits.append("recent_pw")
    if f.get("payee_senders_1h", 0) >= 5:
        hits.append("fan_in")
    if f.get("reverse_30m") and amt >= LARGE:
        hits.append("round_trip")
    ch = f.get("charged_1h", 0)
    if ch >= LARGE // 2 and 0.8 * ch <= amt <= 1.25 * ch:        # 방금 충전한 돈을 거의 그대로 빼냄
        hits.append("charge_out")
    if f.get("night") and amt >= LARGE:
        hits.append("night_large")
    return sum(RULES[h].points for h in hits), hits


def decide(points: int) -> str:
    return "block" if points >= BLOCK else "step_up" if points >= STEP_UP else "allow"


def features(conn, sender: str, payee: str, amount: int, risk=frozenset()) -> dict:
    """판단 재료 — conn 은 송금 트랜잭션(두 지갑을 잠근 뒤)의 연결. 같은 보내는 사람·받는 사람의 동시 송금은 지갑 잠금에서
    줄을 서므로, 여기서 읽는 '최근 10분 송금'·'받는 사람 유입'에는 앞서 끝난 송금이 모두 들어 있다."""
    q = text(
        "SELECT "
        " (SELECT COUNT(*) FROM wallet_ledger WHERE username=:s AND reason='transfer_out' "
        "    AND created_at >= :t10) AS sent_10m, "
        " (SELECT COUNT(*) FROM wallet_ledger WHERE username=:s AND reason='transfer_out' AND ref=:p) AS paid_before, "
        " (SELECT COUNT(DISTINCT ref) FROM wallet_ledger WHERE username=:p AND reason='transfer_in' "
        "    AND created_at >= :t60) AS payee_senders_1h, "
        " (SELECT COUNT(*) FROM wallet_ledger WHERE username=:p AND reason='transfer_out' AND ref=:s "
        "    AND created_at >= :t30) AS reverse_30m, "
        " (SELECT COALESCE(SUM(amount), 0) FROM wallet_ledger WHERE username=:s AND reason='iap' "
        "    AND created_at >= :t60) AS charged_1h, "
        " (SELECT balance FROM wallet WHERE username=:s) AS balance, "
        " (SELECT password_changed_at FROM app_user WHERE username=:s) AS pw_at")
    out = conn.execute(q, {"s": sender, "p": payee, "t10": _ago(10), "t30": _ago(30), "t60": _ago(60)}).mappings().first()
    hist = [abs(int(r[0])) for r in conn.execute(text(
        "SELECT amount FROM wallet_ledger WHERE username=:s AND reason='transfer_out' ORDER BY ledger_id DESC LIMIT 30"),
        {"s": sender})]
    bal = int(out["balance"] or 0)
    pw_at = out["pw_at"]
    now = clock.now()
    return {"amount": int(amount), "sent_10m": int(out["sent_10m"]), "history": len(hist),
            "median_amount": statistics.median(hist) if hist else None,
            "balance_before": bal, "balance_after": bal - int(amount), "known_payee": int(out["paid_before"]) > 0,
            "ip_change": "ip_change" in risk,
            "pw_changed_24h": bool(pw_at and (clock.utc() - pw_at).total_seconds() < 86400),
            "payee_senders_1h": int(out["payee_senders_1h"]), "reverse_30m": int(out["reverse_30m"]) > 0,
            "charged_1h": int(out["charged_1h"]), "night": 1 <= now.hour < 5}


def _ago(minutes: int) -> str:
    from datetime import timedelta
    return (clock.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def screen(conn, sender: str, payee: str, amount: int, risk=frozenset(), fresh: bool = False) -> dict:
    """송금 판정(기록 없음). fresh: 최근 5분 안에 재인증했는가(그렇다면 step_up 은 통과로).
    반환 {decision, score, rules, model, features, sender, payee, amount} — decision: allow | allow_after_step_up |
    step_up | block. 결정 문턱을 넘는지는 이 값 하나로 본다(호출자는 'allow' 로 시작하면 실행)."""
    f = features(conn, sender, payee, amount, risk)
    points, hits = score(f)
    model = decide(points)
    final = "allow_after_step_up" if model == "step_up" and fresh else model
    return {"decision": final, "score": points, "rules": hits, "model": model, "features": f,
            "sender": sender, "payee": payee, "amount": int(amount)}


def executes(verdict: dict) -> bool:
    return verdict["decision"] in ("allow", "allow_after_step_up")


def record(verdict: dict, conn=None, ref: str | None = None) -> None:
    """판정 기록(fds_decision). conn 을 넘기면 그 트랜잭션에 포함 — 실행된 송금과 판정이 함께 커밋된다(ref = 송금 키).
    차단이면 보안 이벤트(fds_block)도 남긴다(자동 대응 규칙의 입력)."""
    from app.security import context
    f = verdict["features"]
    row = {"at": clock.utc(), "u": verdict["sender"], "c": verdict["payee"], "a": verdict["amount"],
           "sc": verdict["score"], "d": verdict["decision"], "ref": ref,
           "r": json.dumps({"hits": verdict["rules"], "features": {k: v for k, v in f.items() if k != "median_amount"},
                            "model": verdict["model"]}, ensure_ascii=False, default=str),
           "rid": context.current().request_id[:64]}
    sql = text("INSERT INTO fds_decision(at, username, action, counterparty, amount, score, rules, decision, request_id, ref) "
               "VALUES(:at, :u, 'transfer', :c, :a, :sc, :r, :d, :rid, :ref)")
    if conn is not None:
        conn.execute(sql, row)
    else:
        with db.transaction() as c:
            c.execute(sql, row)
    if verdict["decision"] == "block":
        from app.security import events
        events.emit("fds_block", "high", username=verdict["sender"], score=verdict["score"], rules=verdict["rules"])


def review(days: int = 7) -> dict:
    """최근 판정 요약 — 결정별 건수·자주 걸린 규칙(튜닝·오탐 검토)."""
    from collections import Counter
    from datetime import timedelta
    rows = db.query("SELECT decision, rules FROM fds_decision WHERE at >= :t", {"t": clock.utc() - timedelta(days=days)})
    dec, rules = Counter(), Counter()
    for r in rows:
        dec[r["decision"]] += 1
        for h in json.loads(r["rules"] or "{}").get("hits", []):
            rules[h] += 1
    return {"days": days, "total": len(rows), "decisions": dict(dec), "rules": dict(rules.most_common()),
            "thresholds": {"block": BLOCK, "step_up": STEP_UP}}
