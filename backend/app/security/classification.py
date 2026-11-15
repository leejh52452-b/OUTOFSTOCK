"""데이터 분류 등록부 — 모든 표·열의 보안 등급·보호 조치·보관 기간·설명(데이터 사전의 원천).

등급(개인정보 보호법·금융보안원 데이터 분류 관행을 단순화)
  L1 공개      누구에게 보여도 되는 데이터(시세·뉴스·추천 결과)
  L2 내부      운영 정보(배치 기록·지표) — 외부 비공개, 개인 식별 없음
  L3 개인정보  특정 개인을 알아볼 수 있거나 개인 활동 기록(계정·지갑·관심종목·학습·접속)
  L4 민감·비밀  인증정보·통신 내용·키·결제 비밀(유출 시 즉시 악용) — 해시 또는 암호화로만 저장
보호 조치 표기
  hash(일방향) · encrypt(AES-256-GCM 봉투 암호화) · pseudonym(HMAC 가명) · rls(행 수준 보안) · append_only(감사)
규칙(tests/test_classification.py 가 강제): 메타데이터의 모든 표가 등록돼 있어야 하고(새 표를 만들면 분류부터),
L4 열은 hash/encrypt 중 하나, rls 로 표시한 표는 DB 에 FORCE RLS 가 걸려 있어야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LEVELS = {"L1": "공개", "L2": "내부", "L3": "개인정보", "L4": "민감·비밀"}


@dataclass(frozen=True)
class Col:
    level: str
    protection: tuple = ()
    desc: str = ""


@dataclass(frozen=True)
class Table:
    level: str
    desc: str
    retention: str
    protection: tuple = ()
    owner: str | None = None                  # RLS 주인 열(두 사람이면 'a,b')
    columns: dict = field(default_factory=dict)


_PUB = "영구(공개 데이터 — 원천 정정 시 갱신)"

REGISTRY: dict[str, Table] = {
    # ── 공개 시장 데이터(L1) ──
    "stock": Table("L1", "국내 종목 마스터(유니버스)", _PUB),
    "daily_price": Table("L1", "국내 일별 시세(종가·거래량·시총)", _PUB),
    "ohlc": Table("L1", "국내 일봉(시고저종)", _PUB),
    "realtime_price": Table("L1", "국내 실시간 체결가(최신 1건)", "최신값만 유지(덮어씀)"),
    "market_index": Table("L1", "시장 지수 최신값(출처·기준일 포함)", "최신값만 유지"),
    "index_daily": Table("L1", "지수 일별 이력", _PUB),
    "financial_statement": Table("L1", "DART 재무(공시일·적재일 — 시점 조회)", _PUB),
    "dart_corp": Table("L1", "DART 고유번호 ↔ 종목코드", _PUB),
    "ranking": Table("L1", "종목 순위(배치 계산)", "배치마다 교체"),
    "recommendation": Table("L1", "재무 유사 종목", "배치마다 교체"),
    "yangbong_basket": Table("L1", "양봉이 주간 추천 바구니(고른 5종목·30일 채점·코스피 비교 — 팀 백테스트 247주 포함)", _PUB),
    "reco_snapshot": Table("L1", "오늘의 추천 Top 5 공개 기록(사후 검증·감사 추적)", "영구(모형 감사 추적)"),
    "news": Table("L1", "뉴스 제목·링크(Google News RSS)", "DB 약 13개월(NEWS_RETENTION_DAYS) → 압축 보관 파일(복원 가능, archive_log)"),
    "news_stock": Table("L1", "뉴스 ↔ 종목 연결", "뉴스와 같음"),
    "news_sentiment": Table("L1", "뉴스 감성(호재·악재) 분류 결과·모형", "뉴스와 같음"),
    "overseas_stock": Table("L1", "해외 종목 마스터", _PUB),
    "overseas_ohlc": Table("L1", "해외 일봉", _PUB),
    "overseas_quote": Table("L1", "해외 실시간·최신 시세", "최신값만 유지"),
    "overseas_financial": Table("L1", "해외 재무(EDGAR)", _PUB),
    "search_log": Table("L2", "검색 기록(인기 검색어 집계용 — 사용자 식별 없음)",
                        "DB 약 13개월(SEARCH_LOG_RETENTION_DAYS, 집계는 최근 90일) → 압축 보관 파일(archive_log)",
                        columns={"keyword": Col("L2", (), "검색어(계정·IP 와 연결하지 않음)")}),
    # ── 운영(L2) ──
    "app_meta": Table("L2", "키-값 운영 메타(출처·기준일·배치 상태·암호화된 외부 토큰)", "값마다 다름",
                      columns={"value": Col("L2", ("encrypt",), "외부 API 토큰은 enc:v1/v2 암호문(AAD=app_meta:<키>)")}),
    "pipeline_run": Table("L2", "배치 실행 기록(상태·건수·메모 — 비밀값 가림)", "1년"),
    "data_quality_issue": Table("L2", "데이터 품질 규칙 위반 기록", "1년"),
    "alert_log": Table("L2", "운영 알림 발송 기록", "180일"),
    "rate_event": Table("L2", "시도 제한 기록(키는 해시 — 아이디·IP 원문 아님)", "1시간(UNLOGGED)",
                        ("pseudonym",)),
    # ── 계정·인증(L3/L4) ──
    "app_user": Table("L3", "회원 계정(아이디·상태·잠금)", "탈퇴 시 즉시 파기",
                      columns={"password_hash": Col("L4", ("hash",), "PBKDF2-SHA256 600,000회 + 페퍼(HMAC)"),
                               "salt": Col("L4", ("hash",), "계정별 무작위 솔트(해시 입력)")}),
    "auth_session": Table("L3", "로그인 세션(기기 계열·IP 대역·만료·위험 신호·기기 결속 키 지문)", "만료·폐기 후 7일",
                          columns={"refresh_hash": Col("L4", ("hash",), "갱신 토큰 SHA-256(원문 미저장)"),
                                   "refresh_prev_hash": Col("L4", ("hash",), "직전 갱신 토큰 해시(재사용 탐지)"),
                                   "csrf_hash": Col("L4", ("hash",), "CSRF 토큰 해시"),
                                   "device_hash": Col("L3", ("hash",), "기기 계열 지문(원문 UA 미저장)")}),
    "auth_token": Table("L4", "접근 토큰(10분) — 해시만", "만료 즉시 정리", ("hash",)),
    "auth_refresh_used": Table("L4", "사용한 갱신 토큰 해시(재사용 탐지)", "12시간", ("hash",)),
    "user_mfa": Table("L4", "2단계 인증 비밀키(암호화)·복구 코드(해시)", "해제·탈퇴 시 즉시 파기", ("encrypt", "hash"),
                      columns={"secret_enc": Col("L4", ("encrypt",), "TOTP 비밀키 AES-256-GCM(AAD=user_mfa:<아이디>)"),
                               "recovery_hashes": Col("L4", ("hash",), "복구 코드 SHA-256 목록")}),
    "user_role": Table("L2", "관리 역할 자격(2인 승인·만료)", "회수 후 감사 로그로 추적"),
    "role_activation": Table("L2", "관리 권한 JIT 활성 기록(사유·시간)", "3년(권한 내역 — 안전성 확보조치 기준 제5조③) 후 파기"),
    "role_request": Table("L2", "역할 부여 요청·승인(2인)", "3년(권한 내역 — 안전성 확보조치 기준 제5조③) 후 파기"),
    # ── 운영 복원력(2026-11-02) ──
    "capacity_report": Table("L2", "성능관리보고서(부하 단계·지표·느린 경로·권고 — 개인정보 없음)", "1년"),
    "service_control": Table("L2", "서비스 비상 차단 상태(기능별 운영/중지·사유·변경자)", "영구(변경 이력은 감사 로그)"),
    "service_control_request": Table("L2", "서비스 재개 요청·승인(2인)", "3년(권한 내역과 같게) 후 파기"),
    "dpop_jti": Table("L2", "기기 결속 증명 재사용 방지(키 지문·증명 ID — 아이디 없음)", "10분"),
    "security_assessment": Table("L2", "금융보안 수준진단 결과(항목별 등급·근거 종류·운영 점검 — 개인정보 없음, 추가만)", "5년(진단 증빙)", ("append_only",)),
    "integrity_baseline": Table("L2", "파일 무결성 기준선(배포 파일별 SHA-256 — 소유자 계정만 기록, 앱은 읽기만)", "영구(배포 변경 이력)", ("append_only",)),
    # ── IT 내부통제(2026-11-02) ──
    "change_record": Table("L2", "변경 기록(요청·사전 영향 분석·자동 점검·승인·적용·사후 검토 — 개인정보 없음, 앱은 삭제·해시 수정 불가)",
                           "영구(배포 변경 이력)"),
    "incident": Table("L2", "장애·사고 사건(지속 시간·영향 이용자 수·보고 대상·기한·단계·원인·대책 — 개인정보 없음, 앱은 삭제 불가)",
                      "5년(사고 보고 근거)"),
    "uptime_check": Table("L2", "가용성 확인 기록(1분 합성 모니터링 — 응답 코드·지연, 개인정보 없음)", "35일"),
    # ── DB 운영(2026-11-03 · ADR-0027) ──
    "archive_log": Table("L2", "데이터 수명주기 보관 기록(기간·파일·SHA-256·행 수·정리·복원 시각 — 개인정보 없음, 앱은 삭제·해시 수정 불가)",
                         "영구(보관 파일의 목록·무결성 근거)", ("append_only",)),
    "db_size_snapshot": Table("L2", "DB 크기 일일 기록(표별 크기·행 수 — 증가 추세·디스크 소진 예측, 개인정보 없음)", "2년"),
    # ── 모의투자 시즌 데이터(2026-11-09) — 진행 중 시즌의 미래 봉을 담아 외부 비공개(API 가 공개된 봉까지만 잘라 준다) ──
    "vmarket_season": Table("L2", "모의투자 가상 시장 시즌 데이터(가상 종목 5개 × 봉 185개·SHA-256 — 실제 시세 아님, 미래 봉 비공개)",
                            "영구(지난 시즌 결과 다시 보기 근거)"),
    # ── 개인 활동(L3, 행 수준 보안) ──
    "watchlist": Table("L3", "관심종목", "탈퇴 시 즉시 파기", ("rls",), "user_id"),
    "survey_result": Table("L3", "투자 성향 분석 결과(이력)", "탈퇴 시 즉시 파기", ("rls",), "user_id"),
    "learning_log": Table("L3", "학습 열람·진도 기록", "탈퇴 시 즉시 파기", ("rls",), "user_id"),
    "access_log": Table("L3", "접속 기록(로그인·로그아웃·기기 계열)", "1년(접속기록 보관) 후 파기", ("rls",), "user_id"),
    "practice_session": Table("L3", "과거 사례 실습 진행 상태(계정 또는 게스트 쿠키 ID)", "탈퇴 시 즉시 파기",
                              ("rls",), "user_id"),
    "practice_trade": Table("L3", "실습 체결 기록(소속 실습의 주인 기준 RLS)", "탈퇴 시 즉시 파기", ("rls",)),
    "practice_record": Table("L3", "웹 실습 기록(기법·방식·수익률 — 브라우저 계산값, 보상과 무관한 표시용)", "탈퇴 시 즉시 파기",
                             ("rls",), "user_id"),
    "quiz_attempt": Table("L3", "Daily Stock Quiz 결과(날짜·고른 답·서버 채점 점수 — 하루 1행)", "탈퇴 시 즉시 파기",
                          ("rls",), "user_id"),
    "avatar_closet": Table("L3", "양봉이 꾸미기 — 산 아이템·산 가격·착용 상태", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "wallet": Table("L3", "가상머니 지갑 잔액(낙관적 잠금 version)", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "wallet_ledger": Table("L3", "지갑 원장(모든 입출금 — 잔액 대사 근거)", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "attendance": Table("L3", "출석 기록·보상", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "invest_position": Table("L3", "장기 모의투자 보유·실현손익", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "vmarket_entry": Table("L3", "모의투자 시즌 참가(참가금·시즌 현금·정산 금액)", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "vmarket_position": Table("L3", "모의투자 시즌 종목별 보유(수량·평균단가·실현손익)", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "vmarket_trade": Table("L3", "모의투자 시즌 체결(매수·매도·정산 자동 매도)", "탈퇴 시 즉시 파기", ("rls",), "username"),
    "iap_purchase": Table("L3", "인앱 충전 기록(영수증은 해시)", "탈퇴 시 아이디 분리(영수증 재사용 차단용으로 해시만 보존)",
                          ("rls",), "username",
                          columns={"receipt": Col("L4", ("hash",), "스토어 영수증 SHA-256('h:' 접두)")}),
    "payment_order": Table("L3", "토스 결제 주문", "탈퇴 시 즉시 파기", ("rls",), "username",
                           columns={"payment_key": Col("L4", ("encrypt",), "결제 키 AES-256-GCM")}),
    "friendship": Table("L3", "친구 관계(양방향 두 행)", "탈퇴 시 즉시 파기", ("rls",), "owner,friend"),
    "friend_request": Table("L3", "친구 요청", "탈퇴 시 즉시 파기", ("rls",), "sender,receiver"),
    "chat_message": Table("L3", "1:1 채팅", "탈퇴 시 즉시 파기", ("rls",), "sender,receiver",
                          columns={"body": Col("L4", ("encrypt",), "통신 내용 — 봉투 암호화(AAD=chat:<보낸이>:<받는이>)")}),
    "journal_entry": Table("L2", "복식부기 분개(계좌번호·시스템 계정 — 아이디 없음, 추가만)", "5년(거래 기록 보존 관행)",
                           ("append_only", "pseudonym")),
    "dq_result": Table("L2", "데이터 계약 측정 결과(품질 차원별 값·통과 여부)", "1년"),
    "outbox": Table("L2", "트랜잭셔널 아웃박스(전달 대기 사건 — 가명·금액만)", "전달 후 30일", ("pseudonym",)),
    "fds_decision": Table("L3", "이상거래탐지 판정(점수·규칙·재료) — 관리자 검토용", "1년 후 파기"),
    "ledger_close": Table("L2", "일 마감 결과(합계·대사·해시 사슬, 추가만)", "5년", ("append_only",)),
    # ── 보안·감사 기록 ──
    "audit_log": Table("L3", "감사 로그(해시 체인 — 누가·언제·무엇을)", "3년 후 파기(체인 시작점 이동 — 권한 내역 3년)",
                       ("append_only", "pseudonym"),
                       columns={"ip_hash": Col("L2", ("pseudonym",), "IP HMAC 가명값")}),
    "security_event": Table("L3", "보안 이벤트·자동 대응 결과", "1년 후 파기(탈퇴 시 아이디 가명화)", ("pseudonym",),
                            columns={"ip_hash": Col("L2", ("pseudonym",), "IP HMAC 가명값")}),
    "pii_access_log": Table("L3", "관리자 개인정보 열람 기록(목적 포함)", "2년 후 파기", ("append_only",),
                            columns={"ip_hash": Col("L2", ("pseudonym",), "IP HMAC 가명값")}),
}


def level_of(table: str, column: str | None = None) -> str:
    t = REGISTRY[table]
    if column and column in t.columns:
        return t.columns[column].level
    return t.level


def rls_tables() -> set[str]:
    return {name for name, t in REGISTRY.items() if "rls" in t.protection}


def summary() -> dict:
    out = {lv: 0 for lv in LEVELS}
    for t in REGISTRY.values():
        out[t.level] += 1
    return out
