"""가명정보 내보내기 — 분석·연구용 사용자 활동 데이터를 '다시 알아보기 어렵게' 만들어 내보낸다.

개인정보 보호법 제28조의2(가명정보 처리)·가명정보 처리 가이드라인의 절차를 코드로:
  1. 목적 명시      purpose 필수 — 감사 로그에 남긴다(누가·언제·무엇을·왜)
  2. 식별자 처리    아이디 → 내보낼 때마다 새 무작위 솔트로 HMAC(서로 다른 내보내기끼리 연결 불가)
  3. 일반화        가입일 → 분기, 잔액·실습·학습·출석 → 구간(범주)으로
  4. k-익명성      준식별자(가입 분기·성향·구간들) 조합이 같은 사람이 k 명 미만이면 더 거칠게 일반화,
                   그래도 작으면 그 행을 뺀다(억제). 결과 보고서에 k·억제 건수·일반화 단계를 남긴다.
  5. 결합 금지     내보낸 파일에는 아이디·원 시각·원 금액이 없다. 솔트는 저장하지 않는다(재식별 키 폐기).
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import os
import secrets
from collections import Counter

from app import db

QI = ("join_period", "survey_type", "balance_band", "practice_band", "learning_band", "attendance_band")

_LEVELS = [   # 일반화 단계: 0(세밀) → 2(거침)
    {"period": "quarter", "balance": (0, 1_000_000, 3_000_000, 10_000_000, 30_000_000),
     "count": (0, 1, 3, 6, 11)},
    {"period": "half", "balance": (0, 3_000_000, 30_000_000), "count": (0, 1, 6)},
    {"period": "year", "balance": (0, 10_000_000), "count": (0, 1)},
]


def _band(v: int, edges: tuple) -> str:
    v = int(v or 0)
    for i in range(len(edges) - 1, -1, -1):
        if v >= edges[i]:
            return f"{edges[i]:,}+" if i == len(edges) - 1 else f"{edges[i]:,}–{edges[i + 1] - 1:,}"
    return f"<{edges[0]:,}"


def _period(created_at: str, how: str) -> str:
    y, m = str(created_at or "0000-01")[:4], int(str(created_at or "0000-01")[5:7] or 1)
    if how == "quarter":
        return f"{y}Q{(m - 1) // 3 + 1}"
    if how == "half":
        return f"{y}H{1 if m <= 6 else 2}"
    return y


def _raw_rows() -> list[dict]:
    from app.security import rls
    with rls.elevated("pseudonymized_export"):             # 전체 사용자 집계(개별 행은 밖으로 나가지 않는다)
        return db.query(
            "SELECT u.username, u.created_at, "
            " (SELECT type FROM survey_result s WHERE s.user_id = u.username ORDER BY result_id DESC LIMIT 1) AS survey_type, "
            " COALESCE((SELECT balance FROM wallet w WHERE w.username = u.username), 0) AS balance, "
            " (SELECT COUNT(*) FROM practice_session p WHERE p.user_id = u.username AND p.status = 'done') AS practice, "
            " (SELECT COUNT(DISTINCT item_key) FROM learning_log l WHERE l.user_id = u.username) AS learning, "
            " (SELECT COUNT(*) FROM attendance a WHERE a.username = u.username) AS attendance "
            "FROM app_user u")


def _generalize(rows: list[dict], level: int) -> list[dict]:
    lv = _LEVELS[level]
    return [{"join_period": _period(r["created_at"], lv["period"]), "survey_type": r["survey_type"] or "미분석",
             "balance_band": _band(r["balance"], lv["balance"]), "practice_band": _band(r["practice"], lv["count"]),
             "learning_band": _band(r["learning"], lv["count"]), "attendance_band": _band(r["attendance"], lv["count"]),
             "_user": r["username"]} for r in rows]


def build(k: int = 5) -> tuple[list[dict], dict]:
    """k-익명 표 + 보고서. 가장 세밀한 단계부터 시도해 억제 비율이 가장 낮은 단계를 고른다."""
    raw = _raw_rows()
    best = None
    for level in range(len(_LEVELS)):
        rows = _generalize(raw, level)
        groups = Counter(tuple(r[q] for q in QI) for r in rows)
        kept = [r for r in rows if groups[tuple(r[q] for q in QI)] >= k]
        cand = (len(rows) - len(kept), level, kept, groups)
        if best is None or cand[0] < best[0]:
            best = cand
        if cand[0] == 0:
            break
    suppressed, level, kept, groups = best
    salt = secrets.token_bytes(32)                          # 이번 내보내기 전용 — 저장하지 않는다
    out = [{"pid": hmac.new(salt, r["_user"].encode(), hashlib.sha256).hexdigest()[:16],
            **{q: r[q] for q in QI}} for r in kept]
    sizes = [n for n in groups.values() if n >= k]
    report = {"k": k, "records_in": len(raw), "records_out": len(out), "suppressed": suppressed,
              "generalization_level": level, "quasi_identifiers": list(QI),
              "min_group": min(sizes) if sizes else 0, "groups": len(sizes)}
    del salt
    return out, report


def export(path: str, k: int = 5, purpose: str = "", operator: str = "cli") -> dict:
    if len((purpose or "").strip()) < 5:
        raise ValueError("가명정보 처리 목적을 5자 이상 적어 주세요")
    rows, report = build(k)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pid", *QI])
        w.writeheader()
        w.writerows(rows)
    from app.services import audit
    audit.record("privacy.pseudonymized_export", operator, target=os.path.basename(path),
                 detail={"purpose": purpose.strip()[:200], **report})
    return {**report, "path": path, "purpose": purpose.strip()}
