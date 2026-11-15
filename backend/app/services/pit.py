"""Point-in-time(시점) 재무 조회 — look-ahead bias(미래참조 편향) 방지.

재무제표는 두 개의 시간축을 가진다:
  - fiscal_year : 어느 '기간'의 실적인가 (valid time)
  - disclosed_at: 그 실적이 '언제 공시됐나' = 우리가 알게 된 시점 (knowledge time)

예) 2024년 연간실적은 보통 2025년 3월에 공시된다. 그런데 "2025-01-15 시점의 전략"을
2024 실적으로 평가하면, 그날 아직 존재하지도 않던 정보를 쓰는 것 = look-ahead bias.
financials_asof 는 'as_of 시점까지 공시된' 최신 재무만 돌려줘 이 오류를 원천 차단한다.
은행/퀀트 데이터 거버넌스에서 백테스트 신뢰성의 핵심(bitemporal)."""
from datetime import date

from app import clock
from app import db


def financials_asof(ticker, as_of=None):
    """as_of(기본 오늘) 시점에 '공시로 알 수 있었던' 최신 재무 1건. 없으면 None.
    disclosed_at > as_of 인(아직 공시 전) 실적은 절대 반환하지 않는다."""
    # 문자열로 비교하므로 반드시 YYYY-MM-DD 로 정규화('20250305'·'2025-3-5' 는 미래 공시를 흘렸다)
    as_of = date.fromisoformat(str(as_of)[:10]).isoformat() if as_of else clock.today_str()
    rows = db.query(
        "SELECT fiscal_year, report_code, revenue, operating_profit, net_income, total_equity, "
        "debt_ratio, disclosed_at, ingested_at, total_assets, current_assets, current_liabilities, "
        "total_liabilities, retained_earnings, fs_basis FROM financial_statement "
        "WHERE ticker=:t AND disclosed_at IS NOT NULL AND disclosed_at <= :a "
        "ORDER BY disclosed_at DESC, fiscal_year DESC LIMIT 1",
        {"t": ticker, "a": as_of})
    return rows[0] if rows else None


