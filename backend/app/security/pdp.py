"""정책 결정 지점(PDP) — policy.json 을 읽어 '이 주체가 이 동작을 지금 해도 되는가'를 결정한다.

  · 기본 거부: 정책에 없는 동작은 거부(unknown_action). 정책 파일이 형식에 맞지 않으면 서버가 뜨지 않는다(안전 실패).
  · 입력: 동작 이름 · 주체(종류·역할·인증 강도 AAL·최근 재인증 시각·위험 신호) · 속성(금액 등)
  · 출력: 허용/거부 + 거부 코드(login_required · forbidden · mfa_required · step_up_required · unknown_action …)
  · 정책 파일의 SHA-256 을 버전과 함께 기록 — 어떤 정책으로 판단했는지 추적(감사·변경 관리).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.security.context import Principal

POLICY_PATH = Path(__file__).with_name("policy.json")
KINDS = {"anonymous", "guest", "user"}
STEP_UP_WHEN = {"always", "risk", "amount"}
TIERS = {"critical", "normal", "low"}          # 대량접속제어 중요도(capacity.py)
BINDS = {"device"}                             # 기기 결속 키 증명이 필요한 동작(dpop.py)

MESSAGES = {
    "login_required": "로그인이 필요합니다",
    "forbidden": "이 작업을 할 권한이 없어요",
    "mfa_required": "2단계 인증(OTP)을 마친 세션에서만 할 수 있어요",
    "step_up_required": "안전을 위해 비밀번호를 한 번 더 확인할게요",
    "unknown_action": "허용되지 않은 요청이에요",
    "purpose_required": "열람 목적을 5자 이상 적어 주세요",
}
STATUS = {"login_required": 401, "step_up_required": 401, "purpose_required": 400}


class PolicyConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Decision:
    allow: bool
    code: str = "ok"
    obligations: tuple = ()

    @property
    def status(self) -> int:
        return 200 if self.allow else STATUS.get(self.code, 403)

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, self.code)


def _validate(p: dict) -> dict:
    from app.security.rbac import ROLES
    if not isinstance(p.get("actions"), dict) or not p["actions"]:
        raise PolicyConfigError("actions 가 비어 있음")
    for name, a in p["actions"].items():
        kinds = set(a.get("principals") or ())
        if not kinds or not kinds <= KINDS:
            raise PolicyConfigError(f"{name}: principals 는 {sorted(KINDS)} 의 부분집합")
        su = a.get("step_up")
        if su is not None:
            when = set(su.get("when") or ())
            if not when or not when <= STEP_UP_WHEN:
                raise PolicyConfigError(f"{name}: step_up.when 은 {sorted(STEP_UP_WHEN)}")
            if "amount" in when and not isinstance(su.get("amount_gte"), int):
                raise PolicyConfigError(f"{name}: amount 조건에는 amount_gte(정수) 필요")
        roles = set(a.get("roles_any") or ())
        if not roles <= set(ROLES):
            raise PolicyConfigError(f"{name}: 알 수 없는 역할 {sorted(roles - set(ROLES))}")
        if a.get("aal_min", 1) not in (1, 2):
            raise PolicyConfigError(f"{name}: aal_min 은 1 또는 2")
        if not isinstance(a.get("csrf", True), bool):
            raise PolicyConfigError(f"{name}: csrf 는 true/false")
        if a.get("tier", "normal") not in TIERS:
            raise PolicyConfigError(f"{name}: tier 는 {sorted(TIERS)}")
        if a.get("bind") is not None and a["bind"] not in BINDS:
            raise PolicyConfigError(f"{name}: bind 는 {sorted(BINDS)}")
        if a.get("control") is not None and a["control"] not in (p.get("controls") or {}):
            raise PolicyConfigError(f"{name}: control '{a['control']}' 이 controls 목록에 없음")
    for route, t in ((p.get("admission") or {}).get("routes") or {}).items():
        if t not in TIERS or " /" not in route:
            raise PolicyConfigError(f"admission.routes '{route}': 'METHOD /경로' → {sorted(TIERS)}")
    return p


_cache: dict = {}


def load(path: Path | None = None) -> dict:
    path = path or POLICY_PATH
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if _cache.get("digest") != digest or _cache.get("path") != str(path):
        policy = _validate(json.loads(raw.decode("utf-8")))
        _cache.update(policy=policy, digest=digest, path=str(path))
    return _cache["policy"]


def info() -> dict:
    p = load()
    return {"version": p.get("version"), "sha256": _cache["digest"][:16], "actions": len(p["actions"])}


def actions() -> set[str]:
    return set(load()["actions"])


def tier(action: str | None, route_key: str | None = None) -> str:
    """대량접속제어 중요도 — 경로 예외(admission.routes) > 동작의 tier > normal."""
    p = load()
    if route_key:
        t = ((p.get("admission") or {}).get("routes") or {}).get(route_key)
        if t:
            return t
    return ((p["actions"].get(action) or {}).get("tier")) or "normal"


def control_of(action: str | None) -> str | None:
    """비상 차단 대상 기능(controls) — 송금·충전·구매·모의투자."""
    return (load()["actions"].get(action) or {}).get("control")


def controls() -> dict:
    return dict(load().get("controls") or {})


def device_bound(action: str | None) -> bool:
    """기기 결속 키(DPoP) 증명이 필요한 동작인가 — 돈·계정·관리 동작."""
    return (load()["actions"].get(action) or {}).get("bind") == "device"


def csrf_required(action: str) -> bool:
    """쿠키로 인증된 변경 요청에 CSRF 토큰을 요구하는가(기본 예). 정책에 csrf:false 로 적은 동작만 예외."""
    rule = load()["actions"].get(action) or {}
    return rule.get("csrf", True) is not False


def decide(action: str, principal: Principal, attrs: dict | None = None, fresh: bool = False) -> Decision:
    """attrs=None 은 '본문을 읽기 전' 1차 판단(금액 조건은 건너뜀) — 본문을 읽은 뒤 attrs 를 넣어 다시 부른다.
    fresh: 최근 STEP_UP max_age 안에 비밀번호를 확인했는지(pep 가 계산해 넘긴다)."""
    rule = load()["actions"].get(action)
    if rule is None:
        return Decision(False, "unknown_action")
    if principal.kind == "anonymous" and rule.get("issue_guest") and "guest" in rule["principals"]:
        return Decision(True, "ok", ("issue_guest",))      # 처음 온 비로그인 이용자 — 게스트 쿠키를 발급하고 허용
    if principal.kind not in rule["principals"]:
        return Decision(False, "login_required" if "user" in rule["principals"] and not principal.is_user
                        else "forbidden")
    roles = set(rule.get("roles_any") or ())
    if roles and not (roles & set(principal.roles)):
        return Decision(False, "forbidden")
    if principal.aal < rule.get("aal_min", 0 if not principal.is_user else 1):
        return Decision(False, "mfa_required")
    su = rule.get("step_up")
    if su and principal.is_user and not fresh:
        when = set(su["when"])
        need = "always" in when or ("risk" in when and bool(principal.risk))
        if not need and "amount" in when and attrs is not None:
            need = int(attrs.get("amount") or 0) >= su["amount_gte"]
        if need:
            return Decision(False, "step_up_required")
    if rule.get("purpose") and attrs is not None and len(str(attrs.get("purpose") or "").strip()) < 5:
        return Decision(False, "purpose_required")
    return Decision(True, "ok")
