"""FDS 규칙 평가 — 합성 시나리오(정상 vs 사기 유형)로 정밀도·재현율·오탐률을 잰다(python run.py fds-eval).

왜 합성인가: 실제 사기 라벨이 없는 교육용 서비스라, 금융권에서 알려진 수법을 행동 특성으로 옮겨 시험한다.
  정상     월급날 송금·더치페이(소액 반복)·가끔 큰돈(아는 사람)·새벽 소액 등 — 오탐이 나기 쉬운 경계 사례 포함
  사기 유형  계정 탈취 후 털어 가기(ATO drain) · 대포통장 모집(fan-in) · 연속 인출(velocity) · 충전 후 즉시 이체 ·
            자전거래(round trip)
한계(문서에 명시): 합성 분포에서의 성능은 실제 성능이 아니다 — 규칙이 '의도한 수법을 잡는지, 정상 경계 사례를
틀리게 막지 않는지'를 회귀 시험하는 용도. 운영에선 fds_decision 기록으로 오탐을 검토해 임계값을 조정한다.
"""
from __future__ import annotations

import random

from app.services import fds

LARGE = fds.LARGE


def _normal(r: random.Random) -> dict:
    kind = r.random()
    bal = r.randint(500_000, 30_000_000)
    hist = r.randint(0, 30)
    med = r.choice([10_000, 30_000, 50_000, 200_000, 500_000]) if hist else None
    if kind < 0.5:                                   # 소액 더치페이·용돈
        amt = r.randint(5_000, 100_000)
    elif kind < 0.8:                                 # 평소 수준
        amt = int((med or 50_000) * r.uniform(0.5, 2.5))
    elif kind < 0.95:                                # 아는 사람에게 큰돈(이사·경조사)
        amt = r.randint(LARGE, 5 * LARGE)
        bal = max(bal, amt * 3)
    else:                                            # 처음 보내는 사람에게 큰돈(중고거래 등) — 경계 사례
        amt = r.randint(LARGE, 3 * LARGE)
        bal = max(bal, amt * 2)
    known = kind < 0.95 or r.random() < 0.3
    return {"amount": amt, "sent_10m": r.choice([0, 0, 0, 1, 2]), "history": hist, "median_amount": med,
            "balance_before": bal, "balance_after": bal - amt, "known_payee": known,
            "ip_change": r.random() < 0.1, "pw_changed_24h": r.random() < 0.03,
            "payee_senders_1h": r.choice([0, 0, 1, 2]), "reverse_30m": r.random() < 0.05,
            "charged_1h": r.choice([0, 0, 0, 50_000]), "night": r.random() < 0.05}


def _fraud(r: random.Random) -> tuple[str, dict]:
    kind = r.choice(["ato_drain", "mule_fan_in", "velocity", "charge_out", "round_trip"])
    bal = r.randint(2_000_000, 50_000_000)
    base = {"amount": 0, "sent_10m": 0, "history": r.randint(5, 30), "median_amount": r.choice([20_000, 50_000, 100_000]),
            "balance_before": bal, "balance_after": 0, "known_payee": False, "ip_change": False,
            "pw_changed_24h": False, "payee_senders_1h": 0, "reverse_30m": False, "charged_1h": 0, "night": False}
    if kind == "ato_drain":                           # 탈취한 세션: 다른 망, 비밀번호 바꾸고, 처음 보는 계좌로 전액
        amt = bal - r.randint(0, bal // 200)
        base.update(amount=amt, ip_change=True, pw_changed_24h=r.random() < 0.7, night=r.random() < 0.4)
    elif kind == "mule_fan_in":                       # 여러 피해자 돈이 한 계좌로 모임
        amt = r.randint(LARGE, 5 * LARGE)
        base.update(amount=amt, payee_senders_1h=r.randint(5, 30), balance_before=amt * 3)
    elif kind == "velocity":                          # 짧은 시간 연속 인출
        amt = r.randint(300_000, 2 * LARGE)
        base.update(amount=amt, sent_10m=r.randint(5, 15), ip_change=r.random() < 0.6,
                    median_amount=r.choice([20_000, 50_000]))
    elif kind == "charge_out":                        # 도용 결제로 충전 → 바로 빼냄
        amt = r.randint(LARGE, 3 * LARGE)
        base.update(amount=amt, charged_1h=int(amt / r.uniform(0.85, 1.0)), balance_before=amt + r.randint(0, 50_000),
                    ip_change=r.random() < 0.5)
    else:                                             # 두 계좌 사이 왕복(세탁)
        amt = r.randint(LARGE, 4 * LARGE)
        base.update(amount=amt, reverse_30m=True, sent_10m=r.randint(2, 6), payee_senders_1h=r.randint(1, 4))
    base["balance_after"] = base["balance_before"] - base["amount"]
    return kind, base


def evaluate(n: int = 5000, fraud_share: float = 0.1, seed: int = 20261020) -> dict:
    """결정별로 따로 센다 — 차단(고객 피해가 큰 오탐)과 추가 인증(마찰)은 무게가 다르다."""
    r = random.Random(seed)
    normal = {"allow": 0, "step_up": 0, "block": 0}
    fraud = {"allow": 0, "step_up": 0, "block": 0}
    by_kind: dict[str, list[int]] = {}
    for _ in range(n):
        if r.random() < fraud_share:
            kind, f = _fraud(r)
            d = fds.decide(fds.score(f)[0])
            fraud[d] += 1
            by_kind.setdefault(kind, [0, 0, 0])
            by_kind[kind][0] += int(d != "allow")
            by_kind[kind][1] += int(d == "block")
            by_kind[kind][2] += 1
        else:
            normal[fds.decide(fds.score(_normal(r))[0])] += 1
    nf, nn = sum(fraud.values()), sum(normal.values())
    tp = fraud["step_up"] + fraud["block"]
    fp = normal["step_up"] + normal["block"]
    prec = tp / (tp + fp) if tp + fp else 0.0
    return {"n": n, "fraud_share": fraud_share, "seed": seed, "normal": normal, "fraud": fraud,
            "fraud_caught_rate": round(tp / nf, 4) if nf else 0.0,               # 차단 + 추가 인증(재현율)
            "fraud_blocked_rate": round(fraud["block"] / nf, 4) if nf else 0.0,
            "normal_block_rate": round(normal["block"] / nn, 4) if nn else 0.0,  # 정상 거래를 막은 비율(가장 중요)
            "normal_step_up_rate": round(normal["step_up"] / nn, 4) if nn else 0.0,   # 정상 거래의 마찰
            "flag_precision": round(prec, 4),
            "by_kind": {k: {"caught": round(c / t, 4), "blocked": round(b / t, 4)} for k, (c, b, t) in sorted(by_kind.items())},
            "thresholds": {"block": fds.BLOCK, "step_up": fds.STEP_UP}}
