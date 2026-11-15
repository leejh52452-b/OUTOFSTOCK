"""실습하기 '실전 모드'용 실제 시세 파일을 직접 받아 만든다.

실제 시세 원본은 데이터 제공처의 재배포 제한(KRX 시장정보 · Yahoo Finance 이용약관) 때문에
공개 저장소에 넣지 않았다. 이 스크립트로 각자 받아서 쓴다.

    pip install finance-datareader
    python tools/fetch_real_prices.py

만드는 파일 (저장소에는 올라가지 않음 — .gitignore)
    frontend/assets/real-stocks-kr.js   국내 8종목 실전 구간 일봉 (원)
    frontend/assets/soxl-real.js        SOXL 일봉 (달러, Yahoo Finance 수정주가)
"""
from __future__ import annotations

from pathlib import Path

import FinanceDataReader as fdr

ASSETS = Path(__file__).resolve().parents[1] / "frontend" / "assets"

# 종목코드: (이름, 시작일, 종료일) — 실습 화면(practice.html 의 REAL_PICKS)이 쓰는 구간
KR_STOCKS = {
    "005380": ("현대차", "2020-04-29", "2021-05-03"),
    "051910": ("LG화학", "2024-03-14", "2025-03-26"),
    "035420": ("NAVER", "2022-12-07", "2023-12-11"),
    "035720": ("카카오", "2021-04-15", "2022-01-13"),
    "006400": ("삼성SDI", "2020-05-27", "2021-05-28"),
    "068270": ("셀트리온", "2021-01-29", "2022-02-03"),
    "066570": ("LG전자", "2021-05-10", "2022-05-12"),
    "000660": ("SK하이닉스", "2023-12-27", "2025-01-07"),
}
SOXL_RANGE = ("2016-01-04", "2026-10-24")   # 종료일은 다음 날까지 줘야 10-23 이 들어감


def ohlc_rows(df, digits: int) -> list[str]:
    rows = []
    for day, r in df.iterrows():
        vals = [r["Open"], r["High"], r["Low"], r["Close"]]
        if any(v != v or v <= 0 for v in vals):          # 빈 값·거래정지(0) 날은 건너뜀
            continue
        nums = ",".join(f"{round(v):d}" if digits == 0 else f"{v:.{digits}f}" for v in vals)
        rows.append(f'["{day:%Y-%m-%d}",{nums}]')
    return rows


def build_kr() -> Path:
    parts = []
    for code, (name, start, end) in KR_STOCKS.items():
        rows = ohlc_rows(fdr.DataReader(code, start, end), 0)
        print(f"{name}({code}) {len(rows)}일")
        parts.append(f'    "{code}": {{ name: "{name}", data: [{",".join(rows)}] }},')
    out = ASSETS / "real-stocks-kr.js"
    out.write_text(
        "// 실습하기 실전 모드용 국내 종목 실제 시세 (일봉 시가·고가·저가·종가, 원)\n"
        "// 출처: FinanceDataReader(KRX) · tools/fetch_real_prices.py 로 생성 — 재배포 금지(저장소 미포함)\n"
        "const REAL_STOCKS_KR = {\n" + "\n".join(parts) + "\n};\n",
        encoding="utf-8",
    )
    return out


def build_soxl() -> Path:
    df = fdr.DataReader("YAHOO:SOXL", *SOXL_RANGE)
    k = df["Adj Close"] / df["Close"]                    # 수정주가(분할·배당 반영) 비율을 시가·고가·저가에도 똑같이
    for c in ("Open", "High", "Low", "Close"):
        df[c] = df[c] * k
    rows = ohlc_rows(df, 2)
    print(f"SOXL {len(rows)}일")
    out = ASSETS / "soxl-real.js"
    out.write_text(
        "// 실습하기 실전 모드용 SOXL 실제 시세 (일봉 시가·고가·저가·종가, 달러 · 수정주가)\n"
        "// 출처: Yahoo Finance(FinanceDataReader) · tools/fetch_real_prices.py 로 생성 — 재배포 금지(저장소 미포함)\n"
        "const SOXL_REAL = [\n" + ",\n".join(rows) + "\n];\n",
        encoding="utf-8",
    )
    return out


if __name__ == "__main__":
    for path in (build_kr(), build_soxl()):
        print("생성:", path)
