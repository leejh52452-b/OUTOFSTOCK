"""데이터 사전·계보(lineage) 문서 생성 — 코드(메타데이터·분류 등록부·계보 선언)에서 문서를 만든다.

  python run.py data-docs  →  docs/data/data_dictionary.md · docs/data/lineage.md
문서를 손으로 고치지 않는다 — 표나 열이 바뀌면 다시 생성하고, tests/test_datadocs.py 가 '문서가 코드와 같은지' 확인한다
(문서 드리프트 방지). 계보는 '어디서 와서(원천·수집기) 무엇을 거쳐(가공) 어디에 쓰이는지(화면·모형)'를 선언한다.
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app.security import classification as cls

ROOT = Path(__file__).resolve().parents[2]

SOURCES = {
    "KRX": "KRX OpenAPI(국내 일별 시세·지수)", "KIS": "한국투자증권 Open API(실시간·당일 봉·금리)",
    "DART": "금융감독원 DART(재무·공시)", "GNEWS": "Google News RSS(종목 뉴스)", "FDR": "FinanceDataReader(야후·네이버)",
    "EDGAR": "SEC EDGAR(해외 재무·주식수)", "FINNHUB": "Finnhub(해외 실시간)", "EXIM": "한국수출입은행(환율)",
    "ECOS": "한국은행 ECOS(금리·환율)", "TOSS": "토스페이먼츠(결제 승인)", "STORE": "앱 스토어 영수증",
    "USER": "사용자 입력(화면)", "WEB": "웹 화면 계산값(브라우저 — 서버가 범위 검증·재채점)",
    "OPS": "서버 운영 지표(요청·DB 연결 — 워커 메모리)", "ADMIN": "관리자 조치·보안 규칙(자동 대응)",
    "DEVICE": "브라우저 기기 키 증명(DPoP)",
    "REPO": "배포된 코드·화면 파일(설치본 — 파일별 SHA-256)", "CATALOG": "수준진단 카탈로그·근거(코드·시험·문서)",
    "RELEASE": "설치본(자기 해제 셸 — 압축본 SHA-256 머리말)",
    "TEAM": "팀 저장소 산출물(양봉이 백테스트 247주 CSV · 가상 주가 생성기 시즌 파일 — 코드와 함께 배포)",
}
# (상류, 하류, 거치는 코드) — 상류는 원천 키 또는 표 이름
EDGES = [
    ("KRX", "stock", "collectors.krx · services.catalog"), ("KRX", "daily_price", "collectors.marketgen.seed_market"),
    ("KRX", "ohlc", "collectors.marketgen"), ("KIS", "ohlc", "collectors.realtime.refresh_latest_bars(당일 봉)"),
    ("KIS", "realtime_price", "collectors.realtime(웹소켓)"), ("KRX", "market_index", "collectors.indexes"),
    ("KIS", "market_index", "collectors.indexes"), ("market_index", "index_daily", "collectors.indexes"),
    ("DART", "financial_statement", "collectors.dart"), ("DART", "dart_corp", "collectors.dart"),
    ("GNEWS", "news", "collectors.news"), ("news", "news_stock", "collectors.news(종목 태그)"),
    ("news", "news_sentiment", "services.themes.classify_news(사전·KR-FinBert)"),
    ("daily_price", "ranking", "services.market"), ("financial_statement", "recommendation", "run._build_similar"),
    ("daily_price", "reco_snapshot", "services.reco_engine(멀티팩터)"),
    ("financial_statement", "reco_snapshot", "services.reco_engine"), ("news_sentiment", "reco_snapshot", "services.reco_engine"),
    ("FDR", "overseas_ohlc", "collectors.overseas_market"), ("EDGAR", "overseas_financial", "collectors.edgar"),
    ("FINNHUB", "overseas_quote", "collectors.overseas_realtime"), ("EXIM", "app_meta", "collectors.forex(환율 캐시)"),
    ("ECOS", "analytics.indicator_daily", "collectors.ecos"), ("KRX", "analytics.price_history", "collectors.krx_history"),
    ("analytics.price_history", "analytics.mv_stock_snapshot", "V003/V007 머티리얼라이즈드 뷰"),
    ("DART", "analytics.fin_annual", "collectors.dart_platform"), ("analytics.fin_annual", "analytics.credit_score",
                                                                   "models.credit_risk(정보계)"),
    ("USER", "app_user", "services.auth"), ("USER", "survey_result", "services.survey"),
    ("USER", "watchlist", "services.watchlist"), ("USER", "practice_session", "services.practice"),
    ("practice_session", "practice_trade", "services.practice.trade"), ("USER", "chat_message", "services.chat(봉투 암호화)"),
    ("WEB", "practice_record", "services.practice_records(브라우저 계산값 — 범위 검증)"),
    ("WEB", "quiz_attempt", "services.quiz(서버 재채점)"),
    ("quiz_attempt", "wallet_ledger", "services.quiz(정답 보상 — 같은 트랜잭션, 하루 1번)"),
    ("USER", "avatar_closet", "services.avatar(구매·착용 — 가격은 서버 카탈로그)"),
    ("avatar_closet", "wallet_ledger", "services.avatar.buy(차감과 같은 트랜잭션)"),
    ("STORE", "iap_purchase", "services.iap"), ("TOSS", "payment_order", "services.toss"),
    ("iap_purchase", "wallet_ledger", "services.wallet.apply"), ("payment_order", "wallet_ledger", "services.wallet.apply"),
    ("USER", "wallet_ledger", "services.transfer · attendance · invest"), ("wallet_ledger", "wallet", "services.wallet(잔액)"),
    ("wallet_ledger", "journal_entry", "services.ledger.post(복식부기)"), ("journal_entry", "ledger_close", "services.ledger.close_pending(놓친 날까지 차례로)"),
    ("wallet_ledger", "fds_decision", "services.fds.screen·record(송금 트랜잭션 안)"), ("wallet_ledger", "outbox", "services.transfer(아웃박스)"),
    ("OPS", "capacity_report", "services.capacity.report(부하 단계 경계·심각 진입)"),
    ("ADMIN", "service_control", "services.controls(즉시 중지 · 자동 차단 규칙)"),
    ("ADMIN", "service_control_request", "services.controls(재개 요청 → 다른 관리자 승인)"),
    ("DEVICE", "dpop_jti", "security.dpop.consume(1회용 증명)"), ("DEVICE", "auth_session", "security.sessions.create(기기 키 지문)"),
    ("CATALOG", "security_assessment", "security.assessment.run(근거 확인 + 운영 점검 — 다이제스트는 감사 체인에)"),
    ("REPO", "integrity_baseline", "security.integrity.record_baseline(배포 직후 · 소유자 계정)"),
    ("RELEASE", "change_record", "services.changes.request·gate(설치본 해시·사전 영향 분석·자동 점검)"),
    ("ADMIN", "change_record", "services.changes.decide·review(요청자 ≠ 승인자)"),
    ("OPS", "uptime_check", "services.incidents.run_check(1분 합성 모니터링)"),
    ("uptime_check", "incident", "services.incidents.evaluate(10분 실패·지연 → 장애 판정)"),
    ("ADMIN", "incident", "services.incidents.open_manual·update(원인·조치·보고 단계)"),
    ("news", "archive_log", "services.ilm.archive(보관 기한 지난 달 → gzip JSON Lines·SHA-256 → 정리)"),
    ("search_log", "archive_log", "services.ilm.archive(같은 규칙)"),
    ("OPS", "db_size_snapshot", "services.dbhealth.snapshot(매일 크기·추세)"),
    ("analytics.price_history", "yangbong_basket", "services.yangbong.run(팀 알고리즘 yangbong_picks — 끝난 주 선정·30일 채점)"),
    ("index_daily", "yangbong_basket", "services.yangbong.run(같은 기간 코스피 시가→종가)"),
    ("TEAM", "yangbong_basket", "services.yangbong.seed(팀 백테스트 247주 CSV)"),
    ("TEAM", "vmarket_season", "services.vmarket.import_season(형식·가격 검사·SHA-256 — 시작한 시즌은 변경 거부)"),
    ("USER", "vmarket_entry", "services.vmarket.join(참가금 — 지갑 출금과 같은 트랜잭션)"),
    ("vmarket_entry", "wallet_ledger", "services.vmarket.join·settle(참가금 출금·정산 입금, 멱등키)"),
    ("vmarket_season", "vmarket_trade", "services.vmarket.order(체결가 = 서버 시계의 공개된 마지막 봉 종가)"),
    ("vmarket_trade", "vmarket_position", "services.vmarket.order·settle(같은 트랜잭션)"),
]
CONSUMERS = {
    "daily_price": "국내 종목 화면·추천·위험(VaR)", "ohlc": "종목 차트·실습", "realtime_price": "실시간 시세 표시",
    "market_index": "홈 지수", "financial_statement": "재무 건전성·PER/PBR", "news": "뉴스 화면", "news_sentiment": "AI 뉴스 픽",
    "reco_snapshot": "오늘의 추천·사후 검증", "overseas_ohlc": "해외 종목 화면", "wallet": "마이페이지 지갑",
    "journal_entry": "일 마감·시산표(관리자)", "fds_decision": "FDS 검토(관리자)", "analytics.credit_score": "정보계 CLI",
    "practice_record": "실습 목록 '나의 실습 기록'·마이페이지 실습 이력", "quiz_attempt": "용어사전 퀴즈·마이페이지 퀴즈 완료",
    "avatar_closet": "양봉이 꾸미기·마이페이지 프로필·홈 로그인 카드",
    "capacity_report": "관리 /admin/capacity·운영 알림(성능관리보고서)", "service_control": "PEP(돈 동작 집행)·관리 /admin/controls",
    "dpop_jti": "PEP·토큰 갱신(재사용 거부)",
    "security_assessment": "관리 /admin/assessment(이력·퇴보 경보)·경영진 보고", "integrity_baseline": "무결성 검사(10분)·/admin/integrity",
    "change_record": "배포 게이트(vm.sh)·관리 /admin/changes·사고보고서 '변경시점'",
    "incident": "관리 /admin/incidents(보고 기한·초안)·수준진단 점검", "uptime_check": "가용률(/admin/incidents)·장애 판정",
    "archive_log": "관리 /admin/db(수명주기)·복원(run.py ilm-restore)·월간 재해시(ilm-verify)",
    "db_size_snapshot": "관리 /admin/db(증가 추세·디스크 소진 예측)·DB 진단 경보",
    "yangbong_basket": "홈 '이번 주 양봉이가 고른 5종목'·'한 달 전 양봉이 바구니 결과'(/yangbong/weekly)",
    "vmarket_season": "모의투자 화면 차트(/vmarket/season — 공개된 봉까지만)",
    "vmarket_entry": "모의투자 화면 참가·수익률·시즌 기록(/vmarket/me)·마이페이지 지갑 내역",
}


def _type(col) -> str:
    try:
        return str(col.type)
    except Exception:                                      # noqa: BLE001
        return "?"


def dictionary_md() -> str:
    lines = ["# 데이터 사전", "", "`python run.py data-docs` 로 생성 — 손으로 고치지 말 것(tests/test_datadocs.py 가 확인).", "",
             "등급: " + " · ".join(f"**{k}** {v}" for k, v in cls.LEVELS.items()) +
             " · 보호: hash(일방향) encrypt(봉투 암호화) pseudonym(가명) rls(행 수준 보안) append_only(추가만)", "",
             "| 등급 | 표 수 |", "|---|---|"] + [f"| {k} {cls.LEVELS[k]} | {v} |" for k, v in cls.summary().items()] + [""]
    for t in sorted(db.metadata.sorted_tables, key=lambda x: x.name):
        reg = cls.REGISTRY[t.name]
        up = sorted({u for u, d, _ in EDGES if d == t.name})
        down = sorted({d for u, d, _ in EDGES if u == t.name})
        lines += [f"## {t.name}", "", f"{reg.desc}  ", f"등급 **{reg.level}** · 보관 {reg.retention}" +
                  (f" · 보호 {', '.join(reg.protection)}" if reg.protection else "") +
                  (f" · RLS 주인 열 `{reg.owner}`" if reg.owner else ""), ""]
        if up or down or t.name in CONSUMERS:
            lines.append("계보: " + (f"← {', '.join(up)} " if up else "") + (f"→ {', '.join(down)} " if down else "") +
                         (f"· 쓰임: {CONSUMERS[t.name]}" if t.name in CONSUMERS else ""))
            lines.append("")
        lines += ["| 열 | 형식 | 등급 | 보호 | 설명 |", "|---|---|---|---|---|"]
        for c in t.columns:
            cc = reg.columns.get(c.name)
            lines.append(f"| {c.name}{' 🔑' if c.primary_key else ''} | {_type(c)} | {cc.level if cc else reg.level} | "
                         f"{', '.join(cc.protection) if cc and cc.protection else ''} | {cc.desc if cc else ''} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def lineage_md() -> str:
    ids = {}

    def nid(name):
        return ids.setdefault(name, f"n{len(ids)}")
    lines = ["# 데이터 계보(lineage)", "", "`python run.py data-docs` 로 생성. 원천(사각) → 표(둥근) → 쓰임.", "",
             "```mermaid", "flowchart LR"]
    for k, v in SOURCES.items():
        lines.append(f'  {nid(k)}["{v}"]')
    tables = sorted({x for e in EDGES for x in e[:2] if x not in SOURCES})
    for t in tables:
        lines.append(f'  {nid(t)}("{t}")')
    for u, d, _via in EDGES:
        lines.append(f"  {nid(u)} --> {nid(d)}")
    lines += ["```", "", "| 상류 | 하류 | 거치는 코드 |", "|---|---|---|"]
    lines += [f"| {SOURCES.get(u, u)} | {d} | {via} |" for u, d, via in EDGES]
    lines += ["", "## 쓰임", "", "| 표 | 화면·모형 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(CONSUMERS.items())]
    return "\n".join(lines).rstrip() + "\n"


def write(out_dir: Path | None = None) -> list[str]:
    out_dir = out_dir or ROOT / "docs" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_dictionary.md").write_text(dictionary_md(), encoding="utf-8")
    (out_dir / "lineage.md").write_text(lineage_md(), encoding="utf-8")
    return [str(out_dir / "data_dictionary.md"), str(out_dir / "lineage.md")]
