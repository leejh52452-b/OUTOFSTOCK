"""중요정보 유출 방지 — 채팅·송금 메모에 섞인 주민등록번호·카드번호를 저장 전에 가린다(app/security/dlp.py).
가린 사실은 보안 이벤트로(내용 없이), 같은 규칙을 로그·알림의 마지막 출구(redact)에도."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.redact import redact
from app.security import dlp
from app.services import auth, friends, wallet

client = TestClient(app)
PW = "Dlp-pass-2026"


@pytest.mark.parametrize("text,kinds,shown", [
    ("제 주민번호 900101-1234567 이에요", ["rrn"], "******-*******"),
    ("9001011234567", ["rrn"], "******-*******"),                          # 구분자 없이
    ("외국인등록 020315-5123456", ["rrn"], "******-*******"),
    ("카드 4111 1111 1111 1111 로 결제", ["card"], "****-****-****-1111"),   # 룬 검사 통과(시험용 번호)
    ("5555-5555-5555-4444", ["card"], "****-****-****-4444"),
])
def test_scan_masks_identifiers(text, kinds, shown):
    out, got = dlp.scan(text)
    assert got == kinds and shown in out and "1234567" not in out and "4111 1111" not in out


@pytest.mark.parametrize("text", [
    "991332-1234567",                   # 13월 32일 — 실제 날짜가 아니다
    "900101-9234567",                   # 성별 자리 9
    "4111 1111 1111 1112",              # 룬 실패
    "주문 2026110215301234 처리",       # 2 로 시작(국제 카드 앞자리 아님)
    "송금 1,000,000원 · 010-1234-5678",  # 금액·전화번호
])
def test_scan_leaves_lookalikes_alone(text):
    assert dlp.scan(text) == (text, [])


def test_transfer_memo_and_chat_are_masked_before_storage():
    for u in ("dl-a", "dl-b"):
        auth.register(u, PW)
    wallet.credit_once("dl-a", 50_000, "iap", memo="시험 충전")
    ha = {"Authorization": "Bearer " + client.post("/auth/login", json={"username": "dl-a", "password": PW}).json()["token"]}
    r = client.post("/transfer", headers=ha, json={"to": "dl-b", "amount": 1000, "memo": "계좌 확인용 900101-1234567"}).json()
    assert r["masked"] == ["rrn"] and "900101" not in r["memo"]
    stored = db.query("SELECT memo FROM wallet_ledger WHERE username IN ('dl-a','dl-b') AND reason LIKE 'transfer%'")
    assert stored and all("900101" not in s["memo"] for s in stored)
    friends.request("dl-a", "dl-b")
    friends.accept("dl-b", "dl-a")
    m = client.post("/chat/dl-b/messages", headers=ha, json={"body": "카드 5555-5555-5555-4444 빌려줄게"}).json()
    assert m["masked"] == ["card"] and m["body"] == "카드 ****-****-****-4444 빌려줄게"
    ev = db.query("SELECT detail FROM security_event WHERE kind='dlp_masked' AND username='dl-a' ORDER BY id")
    assert len(ev) == 2 and all("4444" not in e["detail"] and "900101" not in e["detail"] for e in ev)


def test_last_exit_redaction_also_masks():
    assert "900101-1234567" not in redact("입력값 오류: memo=900101-1234567")
    assert redact("평범한 오류 12345") == "평범한 오류 12345"
