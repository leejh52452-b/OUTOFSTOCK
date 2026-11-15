-- V002 시장 데이터(토스·네이버 증권 기능의 데이터 원천) — 수급·증시자금·지표·공시·일정·배당·재무·업종.
-- 모든 표는 '출처(source)'를 남긴다: kis(한국투자증권) | krx | dart | ecos | upbit | gen(표본) | rule(규칙 계산)

-- ── 업종 매핑(통계청 표준산업분류 KSIC → 투자자용 업종) ─────────────
-- DART 기업개황의 업종코드(induty_code, 2~5자리)를 '가장 긴 접두어'로 매칭한다(예: 26110 → 261 → 반도체).
CREATE TABLE IF NOT EXISTS analytics.ksic_sector (
    prefix    varchar(5)  PRIMARY KEY,
    sector    varchar(40) NOT NULL,
    ksic_name varchar(80)
);
INSERT INTO analytics.ksic_sector(prefix, sector, ksic_name) VALUES
 ('01','농림어업','농업'),('02','농림어업','임업'),('03','농림어업','어업'),
 ('05','광업','석탄·원유·천연가스 광업'),('06','광업','금속 광업'),('07','광업','비금속광물 광업'),('08','광업','광업 지원 서비스업'),
 ('10','음식료·담배','식료품 제조업'),('11','음식료·담배','음료 제조업'),('12','음식료·담배','담배 제조업'),
 ('13','섬유·의류','섬유제품 제조업'),('14','섬유·의류','의복·의복액세서리 및 모피제품 제조업'),('15','섬유·의류','가죽·가방 및 신발 제조업'),
 ('16','종이·목재','목재 및 나무제품 제조업'),('17','종이·목재','펄프·종이 및 종이제품 제조업'),('18','종이·목재','인쇄 및 기록매체 복제업'),
 ('19','정유·에너지','코크스·연탄 및 석유정제품 제조업'),
 ('20','화학','화학물질 및 화학제품 제조업'),
 ('21','제약·바이오','의료용 물질 및 의약품 제조업'),
 ('22','고무·플라스틱','고무 및 플라스틱제품 제조업'),
 ('23','비금속광물','비금속 광물제품 제조업'),
 ('24','철강·금속','1차 금속 제조업'),('25','철강·금속','금속가공제품 제조업'),
 ('26','전자부품·장비','전자부품·컴퓨터·영상·음향 및 통신장비 제조업'),
 ('261','반도체','반도체 제조업'),('262','디스플레이·전자부품','전자부품 제조업'),
 ('263','IT하드웨어','컴퓨터 및 주변장치 제조업'),('264','통신장비','통신 및 방송 장비 제조업'),
 ('265','가전·음향','영상 및 음향기기 제조업'),
 ('27','의료·정밀기기','의료·정밀·광학기기 및 시계 제조업'),
 ('28','전기장비','전기장비 제조업'),('282','2차전지','일차전지 및 축전지 제조업'),
 ('29','기계','기타 기계 및 장비 제조업'),
 ('30','자동차·부품','자동차 및 트레일러 제조업'),
 ('31','조선·운송장비','기타 운송장비 제조업'),('311','조선','선박 및 보트 건조업'),('313','항공·방산','항공기·우주선 및 부품 제조업'),
 ('32','기타 제조','가구 제조업'),('33','기타 제조','기타 제품 제조업'),('34','기타 제조','산업용 기계 및 장비 수리업'),
 ('35','전기·가스','전기·가스·증기 및 공기조절 공급업'),('36','전기·가스','수도업'),
 ('37','환경','하수·폐수 및 분뇨 처리업'),('38','환경','폐기물 수집·운반·처리 및 원료 재생업'),('39','환경','환경 정화 및 복원업'),
 ('41','건설','종합 건설업'),('42','건설','전문직별 공사업'),
 ('45','유통','자동차 및 부품 판매업'),('46','유통','도매 및 상품 중개업'),('47','유통','소매업(자동차 제외)'),
 ('49','운송·물류','육상 운송 및 파이프라인 운송업'),('50','운송·물류','수상 운송업'),('51','운송·물류','항공 운송업'),
 ('52','운송·물류','창고 및 운송관련 서비스업'),
 ('55','숙박·음식','숙박업'),('56','숙박·음식','음식점 및 주점업'),
 ('58','소프트웨어','출판업(소프트웨어 개발·공급 포함)'),('5821','게임','게임 소프트웨어 개발 및 공급업'),
 ('59','미디어·엔터','영상·오디오 기록물 제작 및 배급업'),('60','미디어·엔터','방송업'),('90','미디어·엔터','창작·예술 및 여가관련 서비스업'),
 ('61','통신서비스','우편 및 통신업'),
 ('62','IT서비스·인터넷','컴퓨터 프로그래밍·시스템 통합 및 관리업'),('63','IT서비스·인터넷','정보서비스업'),
 ('64','금융','금융업'),('641','은행','은행 및 저축기관'),('6491','카드·캐피탈','리스업'),('6492','카드·캐피탈','신용카드 및 할부 금융업'),
 ('6499','금융지주·기타금융','그 외 기타 금융업(금융지주 포함)'),
 ('65','보험','보험 및 연금업'),
 ('66','증권·금융서비스','금융 및 보험 관련 서비스업'),('6612','증권','증권 및 선물 중개업'),
 ('68','부동산','부동산업'),
 ('70','연구개발(바이오 등)','연구개발업'),
 ('71','전문·사업서비스','전문 서비스업'),('7151','지주회사','회사 본부(지주회사)'),('72','전문·사업서비스','건축 기술·엔지니어링 서비스업'),
 ('73','전문·사업서비스','기타 전문·과학 및 기술 서비스업'),('74','전문·사업서비스','사업시설 관리 및 조경 서비스업'),
 ('75','전문·사업서비스','사업 지원 서비스업'),('76','전문·사업서비스','임대업(부동산 제외)'),
 ('84','교육·공공','공공 행정'),('85','교육·공공','교육 서비스업'),
 ('86','보건·의료서비스','보건업'),('87','보건·의료서비스','사회복지 서비스업'),
 ('91','레저·스포츠','스포츠 및 오락관련 서비스업'),
 ('94','기타 서비스','협회 및 단체'),('95','기타 서비스','개인 및 소비용품 수리업'),('96','기타 서비스','기타 개인 서비스업')
ON CONFLICT (prefix) DO NOTHING;

-- 업종코드 → 업종(가장 긴 접두어). 매칭이 없으면 NULL.
CREATE OR REPLACE FUNCTION analytics.sector_of(code text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT sector FROM analytics.ksic_sector
    WHERE code IS NOT NULL AND code LIKE prefix || '%'
    ORDER BY length(prefix) DESC LIMIT 1
$$;

-- ── 투자자별 매매동향 ───────────────────────────────────────
-- 시장 전체(일별) — 긴 형식: (날짜, 시장, 투자자) 한 행. 누적 순매수 같은 윈도 함수 분석이 쉬워진다.
CREATE TABLE IF NOT EXISTS analytics.investor_flow_market (
    trade_date date        NOT NULL,
    market     varchar(6)  NOT NULL CHECK (market IN ('KOSPI', 'KOSDAQ')),
    investor   varchar(10) NOT NULL,     -- prsn frgn orgn scrt ivtr pe_fund bank insu mrbn fund etc_orgt etc_corp etc
    net_qty    bigint,
    net_value  bigint,                   -- 원(출처 단위를 원으로 환산해 저장)
    source     varchar(10) NOT NULL,
    PRIMARY KEY (trade_date, market, investor)
);
-- 종목별(일별) — 개인·외국인·기관계
CREATE TABLE IF NOT EXISTS analytics.investor_flow_stock (
    ticker         varchar(12) NOT NULL,
    trade_date     date        NOT NULL,
    prsn_net_qty   bigint, frgn_net_qty bigint, orgn_net_qty bigint,
    prsn_net_value bigint, frgn_net_value bigint, orgn_net_value bigint,   -- 원
    source         varchar(10) NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);
-- 외국인·기관 순매수(순매도) 상위 — 장중 가집계/장 마감 후 스냅샷
CREATE TABLE IF NOT EXISTS analytics.investor_rank (
    as_of       date        NOT NULL,
    investor    varchar(4)  NOT NULL CHECK (investor IN ('frgn', 'orgn')),
    side        varchar(4)  NOT NULL CHECK (side IN ('buy', 'sell')),
    rank        smallint    NOT NULL,
    ticker      varchar(12) NOT NULL,
    name        varchar(100),
    net_qty     bigint,
    net_value   bigint,                  -- 원
    price       integer,
    change_rate real,
    source      varchar(10) NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of, investor, side, rank)
);

-- ── 증시자금(금융투자협회 자료, 한국투자증권 API 경유) — 단위 억원 ─────────
CREATE TABLE IF NOT EXISTS analytics.market_funds (
    trade_date       date PRIMARY KEY,
    customer_deposit bigint,             -- 고객예탁금
    credit_balance   bigint,             -- 신용융자잔고
    uncollected      bigint,             -- 미수금
    futures_deposit  bigint,             -- 선물예수금
    equity_fund      bigint,             -- 주식형 펀드
    mixed_fund       bigint,             -- 혼합형
    bond_fund        bigint,             -- 채권형
    mmf              bigint,
    collateral_loan  bigint,             -- 예탁증권담보융자(담보대출) 잔고
    source           varchar(10) NOT NULL
);

-- ── 시장지표(금리·환율·원자재) 시계열 ──────────────────────────
CREATE TABLE IF NOT EXISTS analytics.indicator_series (
    series   varchar(24) PRIMARY KEY,
    name     varchar(60) NOT NULL,
    unit     varchar(12) NOT NULL,
    category varchar(10) NOT NULL CHECK (category IN ('rate', 'fx', 'commodity')),
    sort     smallint    NOT NULL DEFAULT 0,
    note     text
);
INSERT INTO analytics.indicator_series(series, name, unit, category, sort, note) VALUES
 ('BASE',      '한국은행 기준금리',  '%',      'rate', 10, '통화정책 기준'),
 ('CALL',      '콜금리(1일)',        '%',      'rate', 20, '금융기관 간 초단기 자금'),
 ('CD91',      'CD(91일)',           '%',      'rate', 30, '변동금리 대출의 기준'),
 ('KTB3Y',     '국고채 3년',         '%',      'rate', 40, '무위험 지표금리'),
 ('KTB10Y',    '국고채 10년',        '%',      'rate', 50, '장기 지표금리'),
 ('KDB1Y',     '산금채 1년',         '%',      'rate', 60, '산업금융채권 — 한국산업은행의 조달금리'),
 ('CORP_AA3Y', '회사채 3년(AA-)',    '%',      'rate', 70, '우량 회사채'),
 ('CORP_BBB3Y','회사채 3년(BBB-)',   '%',      'rate', 80, '투자적격 하단 회사채'),
 ('US10Y',     '미국채 10년',        '%',      'rate', 90, NULL),
 ('USDKRW',    '원/달러',            '원',     'fx',  110, NULL),
 ('JPYKRW',    '원/100엔',           '원',     'fx',  120, NULL),
 ('EURKRW',    '원/유로',            '원',     'fx',  130, NULL),
 ('CNYKRW',    '원/위안',            '원',     'fx',  140, NULL),
 ('WTI',       'WTI 원유',           '달러/배럴', 'commodity', 210, NULL),
 ('GOLD',      '국제 금',            '달러/온스', 'commodity', 220, NULL),
 ('COPPER',    '구리',               '달러/파운드', 'commodity', 230, NULL)
ON CONFLICT (series) DO NOTHING;

CREATE TABLE IF NOT EXISTS analytics.indicator_daily (
    series     varchar(24)   NOT NULL REFERENCES analytics.indicator_series(series),
    trade_date date          NOT NULL,
    value      numeric(14,4) NOT NULL,
    source     varchar(10)   NOT NULL,
    PRIMARY KEY (series, trade_date)
);

-- ── 가상자산(업비트 원화마켓 시세 스냅샷) ─────────────────────────
CREATE TABLE IF NOT EXISTS analytics.crypto_quote (
    market        varchar(20) PRIMARY KEY,           -- KRW-BTC
    korean_name   varchar(40),
    price         numeric(20,4) NOT NULL,
    change_rate   real,                              -- 전일 대비(%)
    acc_value_24h numeric(24,2),                     -- 24시간 거래대금(원)
    warning       boolean NOT NULL DEFAULT false,    -- 거래소 '유의 종목'
    source        varchar(10) NOT NULL,
    captured_at   timestamptz NOT NULL
);

-- ── 공시(DART) — 시장조치(관리·정지·경고)·주요사항(부도·회생 등)·정기공시 ────────
CREATE TABLE IF NOT EXISTS analytics.disclosure (
    rcept_no   varchar(14)  PRIMARY KEY,
    corp_code  varchar(8),
    ticker     varchar(12),
    corp_name  varchar(100),
    corp_cls   char(1),                              -- Y 유가 K 코스닥 N 코넥스 E 기타
    report_nm  text         NOT NULL,
    rcept_dt   date         NOT NULL,
    flr_nm     varchar(100),
    rm         varchar(10),
    category   varchar(12)  NOT NULL,                -- 시장조치 | 주요사항 | 정기 | 기타
    action     varchar(20),                          -- 분류된 조치/사건(관리지정·투자경고·거래정지·부도 …), 없으면 NULL
    source     varchar(10)  NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_disclosure_ticker_dt ON analytics.disclosure (ticker, rcept_dt DESC);
CREATE INDEX IF NOT EXISTS ix_disclosure_dt ON analytics.disclosure (rcept_dt DESC);
CREATE INDEX IF NOT EXISTS ix_disclosure_action ON analytics.disclosure (action, rcept_dt DESC) WHERE action IS NOT NULL;

-- ── 종목 상태(관리·정지·시장경고 등) 스냅샷 — 한국투자증권 현재가 시세의 상태 코드 ─────────
CREATE TABLE IF NOT EXISTS analytics.stock_status (
    ticker         varchar(12) PRIMARY KEY,
    as_of          date        NOT NULL,
    managed        boolean,            -- 관리종목
    halted         boolean,            -- 매매거래정지
    warn_level     smallint CHECK (warn_level BETWEEN 0 AND 3),   -- 0 없음 1 투자주의 2 투자경고 3 투자위험
    caution        boolean,            -- 투자유의
    short_overheat boolean,            -- 단기과열
    liquidation    boolean,            -- 정리매매
    vi             boolean,            -- 변동성완화장치 발동 중
    status_code    varchar(4),
    sector_name    varchar(60),        -- 업종명(거래소 분류)
    per real, pbr real, eps real, bps real,
    w52_high integer, w52_low integer,
    foreign_ratio  real,               -- 외국인 소진율(%)
    source         varchar(10) NOT NULL,
    captured_at    timestamptz NOT NULL DEFAULT now()
);

-- ── 기업 일정(배당·주주총회·공모·상장·증자) ─────────────────────────
CREATE TABLE IF NOT EXISTS analytics.corp_event (
    event_key   varchar(120) PRIMARY KEY,           -- 출처별 자연키(같은 일정 중복 방지)
    event_date  date        NOT NULL,
    ticker      varchar(12),
    kind        varchar(20) NOT NULL,               -- 배당기준일 배당지급일 주주총회 공모청약 상장 …
    title       text        NOT NULL,
    detail      jsonb,
    source      varchar(10) NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_corp_event_date ON analytics.corp_event (event_date);
CREATE INDEX IF NOT EXISTS ix_corp_event_ticker ON analytics.corp_event (ticker, event_date);

-- ── 배당 이력(예탁원) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS analytics.dividend (
    ticker         varchar(12) NOT NULL,
    record_date    date        NOT NULL,            -- 배당 기준일
    kind           varchar(10) NOT NULL,            -- 결산 | 중간 | 분기
    dps            numeric(14,2),                   -- 주당 현금배당금(원)
    div_rate       real,                            -- 현금배당률(액면 대비 %)
    stock_div_rate real,
    pay_date       date,
    source         varchar(10) NOT NULL,
    PRIMARY KEY (ticker, record_date, kind)
);

-- ── 연간 재무(DART, 다년) — 골라보기·신용위험 모형의 재무 입력 ─────────────
CREATE TABLE IF NOT EXISTS analytics.fin_annual (
    ticker              varchar(12) NOT NULL,
    fiscal_year         smallint    NOT NULL,
    fs_basis            char(3)     NOT NULL,        -- CFS 연결 | OFS 별도
    revenue             bigint,
    gross_profit        bigint,
    operating_profit    bigint,
    net_income          bigint,
    total_assets        bigint,
    total_liabilities   bigint,
    total_equity        bigint,
    capital_stock       bigint,                      -- 자본금(자본잠식 판정)
    current_assets      bigint,
    current_liabilities bigint,
    retained_earnings   bigint,
    disclosed_at        date,                        -- 공시일(이 날부터 '알 수 있었다' — PIT 기준)
    rcept_no            varchar(14),
    source              varchar(10) NOT NULL,
    PRIMARY KEY (ticker, fiscal_year)
);

-- ── 증권사 투자의견(한국투자증권 API) ────────────────────────────
-- 같은 날 여러 증권사 의견이 올 수 있어 행 내용 해시(row_key)까지 키에 넣는다(재적재해도 중복 없음).
CREATE TABLE IF NOT EXISTS analytics.analyst_opinion (
    ticker       varchar(12) NOT NULL,
    opinion_date date        NOT NULL,
    row_key      varchar(16) NOT NULL,
    opinion      varchar(20),
    prev_opinion varchar(20),
    target_price integer,
    source       varchar(10) NOT NULL,
    PRIMARY KEY (ticker, opinion_date, row_key)
);
