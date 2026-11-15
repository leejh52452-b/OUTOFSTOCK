"""중요정보 유출 방지(DLP) — 사용자가 자유롭게 쓰는 글(채팅·송금 메모)에 섞인 주민등록번호·카드번호를 저장 전에 가린다.

왜     이 서비스는 주민등록번호·카드번호를 모으지 않는다(개인정보 보호법 — 최소 수집, 주민등록번호 처리 제한 제24조의2).
       그런데 채팅·메모는 자유 입력이라 사용자가 스스로 붙여 넣을 수 있다. 한 번 저장되면 백업·상대방 화면·감사 대상으로 퍼진다.
       → 저장하기 전에 가리고(되돌릴 수 없게), 가린 사실만 보안 이벤트로 남긴다(내용은 남기지 않는다).
       같은 규칙을 로그·운영 알림의 '마지막 출구'(app/redact.py)에도 건다.
찾는 것
  · 주민등록번호·외국인등록번호  YYMMDD-GNNNNNN — 생년월일이 실제 날짜이고 성별 자리가 1~8 일 때만(검증 숫자는 2020-10 이후
                                  임의 번호라 쓰지 않는다). 구분자 없이 13자리여도 같은 조건이면 가린다.
  · 카드번호                      13~19자리(공백·하이픈 허용) + 룬(Luhn) 검사 + 국제 카드 앞자리(3·4·5·6).
  금액·전화번호·계좌번호는 형식만으로 구분되지 않아(오탐) 가리지 않는다.
근거   개인정보 보호법 제24조의2 · 안전성 확보조치 기준 · 전자금융감독규정시행세칙 별표 7(2026.1 개정 예고) '중요정보 입력·처리·유출 모니터링·통제'.
"""
from __future__ import annotations

import re
from datetime import date

_RRN = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[-\s]?([1-8])(\d{6})(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _real_date(yy: str, mm: str, dd: str, g: str) -> bool:
    century = {"9": 1800, "0": 1800, "1": 1900, "2": 1900, "5": 1900, "6": 1900}.get(g, 2000)
    try:
        date(century + int(yy), int(mm), int(dd))
        return True
    except ValueError:
        return False


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def scan(text: str) -> tuple[str, list[str]]:
    """(가린 글, 찾은 종류 목록). 종류: rrn · card. 찾은 게 없으면 원문 그대로."""
    if not text:
        return text, []
    kinds: list[str] = []

    def rrn(m):
        if not _real_date(m.group(1), m.group(2), m.group(3), m.group(4)):
            return m.group(0)
        kinds.append("rrn")
        return "******-*******"

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if not (13 <= len(digits) <= 19) or digits[0] not in "3456" or not _luhn(digits):
            return m.group(0)
        kinds.append("card")
        return "****-****-****-" + digits[-4:]

    out = _RRN.sub(rrn, text)
    out = _CARD.sub(card, out)
    return out, kinds


def guard(text: str, field: str, username: str | None = None) -> tuple[str, list[str]]:
    """저장 직전 — 가리고, 찾았으면 보안 이벤트(종류·필드만, 내용 없음)."""
    out, kinds = scan(text)
    if kinds:
        from app.security import events
        events.emit("dlp_masked", "low", username=username, field=field, kinds=sorted(set(kinds)))
    return out, sorted(set(kinds))
