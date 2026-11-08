# 양봉이 주간 추천 — 백엔드 연결 안내

홈 화면 두 카드("이번 주 양봉이가 고른 5종목!", "한 달 전 양봉이 바구니 결과는?")에 들어갈 데이터를 만드는 코드입니다.
**알고리즘은 `yangbong_picks.py` 에 다 들어 있고 외부 API를 부르지 않습니다.** Claude로 작업한다면 `CLAUDE.md` 를 읽게 하면 됩니다. 백엔드는 데이터를 넣어 주고, 결과를 API로 내보내기만 하면 됩니다.

## 파일
| 파일 | 내용 |
|---|---|
| `yangbong_picks.py` | 알고리즘 (pandas만 필요). `select_weekly()` 5종목 선정, `score_basket()` 30일 채점, `stats()` 누적 성적 |
| `api_example.py` | FastAPI 연결 예시. **비워 둔 함수 3개만 채우면 됨** (DB 읽기·쓰기) |
| `history_baskets_2022_2026.csv` | 2022.01~2026.09 매주 같은 규칙으로 고른 바구니 247개 (초기 데이터로 DB에 넣기, `seed_rows_from_csv()`) |
| `test_yangbong.py` | 연결 확인용 검증 스크립트 — `python test_yangbong.py` 가 전부 통과하면 완료 |
| `CLAUDE.md` | **AI(Claude)에게 줄 작업 지시서** — 작업 순서와 완료 기준 |

## 알고리즘 (매주 마지막 거래일 장 마감 후)
1. 후보: 그날 **시가총액 상위 200 코스피 보통주** (우선주·리츠·**스팩**·ETF 제외), 거래정지 아님, 상장 200거래일 이상
2. 최근 60거래일 하루 수익률 표준편차(흔들림)가 후보 중 **하위 30%**
3. 종가가 **200일 이동평균 위**
4. **시가총액 큰 순**으로 고르되 **5종목 모두 다른 업종**
5. 채점: 다음 거래일 **시가 매수** → **30일 뒤**(휴장이면 직전 거래일) **종가 매도**, 5종목 평균 = 바구니 수익률

검증: 이 코드로 2022~2026을 다시 계산하면 247주 중 136주 수익(55%), 평균 +1.51%, 매주 100만 원씩이면 +374만 원 — 백테스트 결과와 일치함을 확인함.

## 백엔드가 할 일
1. **데이터 준비** — `load_market_data()` 가 아래 표를 돌려주게 (최근 300거래일 이상)

   | 키 | 형식 | 출처 예시 |
   |---|---|---|
   | open, close | 날짜×종목코드, **수정주가** | KRX 일별매매정보 `stk_bydd_trd` (종가 + 등락률 FLUC_RT → `Y.adjusted_close()`) |
   | volume, mktcap | 날짜×종목코드 | 같은 API (ACC_TRDVOL, MKTCAP) |
   | common | 종목코드 set | KRX 종목기본정보 `stk_isu_base_info` 에서 증권구분=주권, 주식종류=보통주 (이름에 '스팩' 제외) |
   | industry, names | {코드: 업종}, {코드: 이름} | FinanceDataReader `StockListing('KRX-DESC')` 의 Industry 등 |
   | kospi_open, kospi_close | 날짜 Series | 코스피 지수 |

   ⚠️ 수정주가가 아니면 액면분할한 종목의 흔들림·200일선이 틀어집니다.

2. **DB 테이블 1개** (예시)
   ```sql
   CREATE TABLE yangbong_basket (
     signal_date   DATE PRIMARY KEY,   -- 고른 날(금)
     buy_date      DATE,               -- 매수일(월)  — 채점 때 채움
     sell_date     DATE,               -- 매도일(30일 뒤)
     codes         TEXT[],             -- 5종목 코드
     names         TEXT[],
     stock_returns DOUBLE PRECISION[], -- 종목별 수익률 (0.05 = +5%)
     basket_return DOUBLE PRECISION,   -- 5종목 평균
     kospi_return  DOUBLE PRECISION
   );
   ```
   처음에 `history_baskets_2022_2026.csv` 247줄을 넣어 두면 첫 주부터 승률·누적 수익이 나옵니다. (`|` 로 구분된 값 → 배열)

3. **주간 배치** — 매주 금요일 장 마감 후 `run_weekly_batch()` 1번 (5종목 저장 + 30일 지난 바구니 채점)

4. **API** — `GET /api/yangbong/weekly` (`api_example.py` 의 router 를 앱에 `include_router`)

## 화면이 받는 JSON
```json
{
  "as_of": "2026-10-23",
  "picks": [{"rank": 1, "code": "068270", "name": "셀트리온"}, ...5개],
  "last_basket": {
    "buy_date": "2026-09-28", "sell_date": "2026-10-28",
    "basket_return": -0.0217, "kospi_return": -0.1481,
    "stocks": [{"code": "105560", "name": "KB금융", "return": -0.0994}, ...5개]
  },
  "stats": {"weeks": 247, "wins": 136, "win_rate": 0.5506, "avg_return": 0.0151, "total_won": 3741202, "since": "2022-01-07"}
}
```
수익률은 모두 소수 (0.05 = +5%). `index.html` 은 이 API가 있으면 실제 값을, 없으면 내장된 예시 값을 보여줍니다.
