# 작업 지시서: 양봉이 주간 추천 API 연결 (AI 작업용)

이 폴더를 OUTOFSTOCK 백엔드(Python · FastAPI · PostgreSQL)에 붙여서 **`GET /api/yangbong/weekly`** 를 여는 작업이다.
알고리즘과 화면은 이미 완성·검증되어 있다. **할 일은 데이터 연결·DB·배치·라우터 등록뿐**이다.

## 절대 바꾸지 말 것
- `yangbong_picks.py` 의 규칙과 숫자 (N_PICKS=5, UNIVERSE_SIZE=200, VOL_WINDOW=60, VOL_PCT=0.30, MA_WINDOW=200, MIN_HISTORY=200, HOLD_DAYS=30).
  백테스트로 검증된 값이다. 바꾸면 화면의 승률·누적 수익과 맞지 않게 된다.
- API 응답 JSON 의 키 이름과 단위 (수익률은 소수: 0.05 = +5%). `index.html` 이 이 형식을 그대로 읽는다.
- `history_baskets_2022_2026.csv` 내용 (과거 247주 결과).

## 파일
| 파일 | 역할 |
|---|---|
| `yangbong_picks.py` | 알고리즘. `select_weekly()` 5종목 선정, `score_basket()` 30일 채점, `stats()` 누적 성적, `adjusted_close()` 수정주가 |
| `api_example.py` | FastAPI router + 주간 배치. **구현할 함수 3개**: `load_market_data()`, `load_baskets()`, `save_basket()` |
| `history_baskets_2022_2026.csv` | 초기 데이터 247행 (`seed_rows_from_csv()` 로 읽음) |
| `test_yangbong.py` | 완료 확인용. `python test_yangbong.py` |
| `README.md` | 사람용 설명 (같은 내용) |

## 작업 순서
1. **패키지**: `pandas`, `numpy` (FastAPI 는 이미 있음).
2. **`load_market_data()` 구현** — 최근 **300거래일 이상**, 모두 `index=날짜(DatetimeIndex, 오름차순)`, `columns=6자리 종목코드(str)` 인 DataFrame:
   | 키 | 내용 | 주의 |
   |---|---|---|
   | `open`, `close` | **수정시가·수정종가** | 원시 종가를 쓰면 액면분할 종목의 흔들림·200일선이 틀어진다. KRX 일별매매정보(`sto/stk_bydd_trd`)의 `TDD_CLSPRC`+`FLUC_RT` 로 `Y.adjusted_close(raw_close, fluc_rt)`, 수정시가 = 원시시가 × (수정종가 ÷ 원시종가). 시가 0 은 NaN |
   | `volume` | 거래량 (`ACC_TRDVOL`) | 0 = 거래정지 |
   | `mktcap` | 그날 시가총액 (`MKTCAP`) | |
   | `common` | 추천 가능한 코드 `set` | **코스피 보통주만**: KRX 종목기본정보(`sto/stk_isu_base_info`) 증권구분 `주권` + 주식종류 `보통주`. 우선주·리츠·ETF·**스팩** 제외 |
   | `industry` | `{코드: 업종명}` | FinanceDataReader `StockListing('KRX-DESC')` 의 `Industry`. 없으면 그 종목만 단독 업종으로 처리됨 |
   | `names` | `{코드: 종목명}` | |
   | `kospi_open`, `kospi_close` | 코스피 지수 Series | 비교용 (없으면 `kospi_return` 이 None) |
   - 상장폐지 종목도 지울 필요 없음(그날 상장된 종목만 후보가 됨). 날짜는 **거래일만**.
3. **DB** — 테이블 1개 (README 의 `CREATE TABLE yangbong_basket` 참고). `codes`·`names`·`stock_returns` 는 배열.
   `load_baskets()` 는 **signal_date 오름차순** 전체, `save_basket()` 은 signal_date 기준 upsert.
4. **초기 데이터** — 테이블이 비어 있을 때 `for r in seed_rows_from_csv(): save_basket(r)` 1번.
5. **배치** — 매주 **금요일 장 마감 후(18:00 KST 이후)** `run_weekly_batch()` 1번 (APScheduler·cron 등 서버 방식대로).
   그 주 시세가 DB에 들어온 뒤에 돌려야 한다. 같은 주에 두 번 돌려도 upsert 라 안전하다.
6. **라우터** — `app.include_router(api_example.router)`. 프론트와 같은 서버라 CORS 설정 불필요.

## 완료 기준 (모두 확인할 것)
- [ ] `python test_yangbong.py` → **전부 통과**. 특히 `test_4_real_data` 가 서버 데이터로
      2026-10-23 → `['셀트리온', '메리츠금융지주', 'HMM', 'KT&G', '코웨이']` 를 내야 한다.
      다르면 대부분 **수정주가 / 보통주 목록 / 업종 데이터** 문제다 (규칙을 고치지 말고 데이터를 고칠 것).
- [ ] `GET /api/yangbong/weekly` 응답이 아래 형식이다.
- [ ] 브라우저로 `index.html` 을 열면 홈 두 카드에 API 값이 나오고 '수익 보기/손실 보기' 버튼이 사라진다
      (API 가 실패하면 예시 값과 버튼이 보이는 게 정상 동작).

```json
{
  "as_of": "2026-10-23",
  "picks": [{"rank": 1, "code": "068270", "name": "셀트리온"}],
  "last_basket": {"buy_date": "2026-09-28", "sell_date": "2026-10-28", "basket_return": -0.021677,
                  "kospi_return": -0.148121, "stocks": [{"code": "105560", "name": "KB금융", "return": -0.099424}]},
  "stats": {"weeks": 247, "wins": 136, "win_rate": 0.5506, "avg_return": 0.01515, "total_won": 3741203, "since": "2022-01-07"}
}
```
(`picks`·`stocks` 는 실제로 5개씩. 채점된 바구니가 하나도 없으면 `last_basket` 은 null)

## 규칙 요약 (설명용 — 코드가 기준)
매주 마지막 거래일 기준: 시총 상위 200 코스피 보통주 → 60거래일 흔들림 하위 30% → 종가 > 200일 평균 →
시총 큰 순, 업종 중복 없이 5개. 채점: 다음 거래일 시가 매수, 30일 뒤(휴장이면 직전 거래일) 종가 매도, 5종목 평균.
