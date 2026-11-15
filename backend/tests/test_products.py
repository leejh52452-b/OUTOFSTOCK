"""성향별 추천 상품 — OUTOFSTOCK_추천상품_선정.docx 규칙(12개·위험 라벨·조합 규칙·파생형 제외·출처)."""
from fastapi.testclient import TestClient

from app.api import app
from app.collectors import marketgen
from app.services import products as ps

client = TestClient(app)
_LABEL_OF_GRADE = {"6": "저위험", "5": "저위험", "4": "중위험", "3": "중위험", "2": "고위험", "1": "고위험",
                   "1~2": "고위험"}


def test_twelve_products_with_consistent_risk_labels():
    assert len(ps.PRODUCTS) == 12 and len(ps.BY_CODE) == 12
    for p in ps.PRODUCTS:
        assert _LABEL_OF_GRADE[p["grade"]] == p["risk"], p["name"]      # 공식등급 ↔ 라벨 일치
        assert p["desc"] and p["kind"]
    counts = {lab: sum(1 for p in ps.PRODUCTS if p["risk"] == lab) for lab in ("저위험", "중위험", "고위험")}
    assert counts == {"저위험": 4, "중위험": 4, "고위험": 4}


def test_profile_picks_follow_combo_rule_and_exclusions():
    for profile, combo in ps.COMBO.items():
        picks = [ps.BY_CODE[c] for c in ps.PICKS[profile]]
        assert len(picks) == 3
        for label, n in combo:
            assert sum(1 for p in picks if p["risk"] == label) == n, profile
    for p in ps.PRODUCTS:                                                  # 파생형 상품 제외
        assert not any(k in p["name"] for k in ps.EXCLUDED["keywords"])


def test_etf_sources_link_to_dart():
    etfs = [p for p in ps.PRODUCTS if p["type"] == "ETF"]
    assert len(etfs) == 10
    for p in etfs:
        assert p["source"]["dart_url"].startswith("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2026")
        assert "투자설명서" in p["source"]["text"] and "확인일 2026.09.22" in p["source"]["text"]
    assert ps.BY_CODE["005930"]["source"]["dart_url"] is None


def test_api_products_and_catalog():
    d = client.get("/reco/products?profile=중립형").json()
    assert d["profile"] == "중립형" and d["rule"] == "중위험 2 + 저위험 1"
    assert [p["code"] for p in d["items"]] == ["284430", "0184N0", "273130"]
    assert "투자 권유가 아니다" in d["notice"]
    assert client.get("/reco/products?profile=없는값").json()["profile"] == "안정형"   # 안전한 기본값


def test_prices_only_where_real_quotes_exist():
    marketgen.seed_market(real=False)                                     # 개별 주식 시세만 존재
    items = {p["code"]: p for p in client.get("/reco/products?profile=공격형").json()["items"]}
    assert items["000660"]["price"] and items["000660"]["change_rate"] is not None
    assert items["381170"]["price"] is None                              # ETF 시세는 지어내지 않음
