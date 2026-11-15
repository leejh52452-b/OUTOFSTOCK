"""간단 CLI.

  python run.py demo            # 오프라인(생성 데이터): 빈 DB 를 채워 미리보기 — 실데이터가 있으면 거부(--force 로 강행)
  python run.py real            # 실데이터 전체: KRX 시세/지수(+KIS 오늘 종가) + 실습 + DART + 뉴스 + 해외
                                #   → scheduler.py 가 매일 23:00(KST) 자동 실행(REAL_PIPELINE_TIME 으로 변경)
  python run.py fetch-market    #   └ KRX 일별 시세 + 시총 상위 UNIVERSE_TOP_N 유니버스 적재(+지수)
  python run.py fetch-index     #   └ 코스피·코스닥 지수만(KIS 실시간 → 야후 → KRX → FDR, 출처·기준일 기록)
  python run.py fetch-practice  #   └ KRX 실습 시나리오 과거 구간(이미 실데이터면 건너뜀 — 다시 받으려면 --force)
  python run.py fetch-dart      #   └ DART 재무(PER/PBR 산출용) — 유니버스 대상
  python run.py fetch-news      #   └ Google News RSS(종목별) — 유니버스 대상
  python run.py fetch-overseas  #   └ 해외(미국) 일봉·지수·환율(FDR, 키 불필요) + 발행주식수(EDGAR)
  python run.py fetch-latest    #   └ KIS REST 로 국내 최근 봉(오늘 종가 포함) 갱신 — KIS 키 필요
  python run.py initdb          # 스키마 생성 + 기존 DB 무손실 마이그레이션(업데이트 후 1번)
  python run.py quality         # 국내 시세 적재 검증(출처·유니버스·최근 일봉·최신일) — 실패 항목 표시
  python run.py alert-test      # 알림 채널(웹훅·텔레그램·메일) 시험 발송
  python run.py platform-load [--years 10] [--budget 5000]  # 분석 계층: 전 종목 × N년 일별 시세 백필(이어받기)
  python run.py platform-history # 10년 이력: 시장조치/주요사항 공시·업종·다년 재무·금리(예산 안에서 이어받기)
  python run.py platform-status # 분석 계층 현황(행 수·파티션·최신일·적재 이력·마이그레이션)
  python run.py platform-refresh # 종목 스냅샷(골라보기·산업 화면 입력) 새로 고침
  python run.py credit-train [--synthetic]     # 4번 기업 신용위험 모형 학습·평가·점수(합성: 파이프라인 확인용)
  python run.py return-validate [--synthetic]  # 5번 5일 수익률 예측 검증(무작위 vs 시간순, 누수 크기)
  python run.py recompute       # 뉴스 감성·추천만 다시 계산(수집·시세는 그대로 — 실데이터 서버에서 안전)
  python run.py snapshot-export [파일]        # 팀원 공유용 — 시세·뉴스·해외 등 공개 데이터만(계정·토큰 제외)
  python run.py model-report credit|return     # 정보계 — 신용위험·수익률 모형의 최근 학습 기록(서비스 API 로는 노출 안 함)
  python run.py analytics-query <이름> [k=v …]  # 정보계 — 이름 있는 분석 쿼리(sql/analytics, 읽기 전용·시간 제한)

보안 운영(관리 화면이 없으므로 CLI·관리 API 로):
  python run.py security-status    # 정책 버전·해시, 활성 세션 수, 최근 24시간 보안 이벤트, 역할 자격·재검토 대상
  python run.py security-purge     # 보관 기간 파기(접속 기록·보안 이벤트·세션·알림·감사) — 스케줄러가 매일 06:10
  python run.py audit-verify       # 감사 로그 해시 체인 검증
  python run.py data-docs          # 데이터 사전·계보 문서 생성(docs/data) — 표·분류·계보 선언에서
  python run.py self-assess [--static] [--json 파일]  # 금융보안 수준진단(45개 항목) — 운영 점검 포함·결과 저장(스케줄러 매월 1일)
  python run.py assurance-docs     # 수준진단 보고서·레이더·자산·위험 등록부 문서 생성(docs/security)
  python run.py integrity-baseline [--release 이름] [--only-listed 목록]  # 배포 직후 파일 무결성 기준선(vm.sh 가 실행)
  python run.py integrity-check    # 기준선 대비 바뀐·사라진·새 파일(웹셸 의심) — 위반이면 종료 코드 1
  python run.py incident-drill --target-db URL  # 사고 대응 모의훈련(훈련용 DB 에서 5개 시나리오) → docs/incidents/drills
IT 내부통제(변경관리·장애 판정·관제 — 금감원 IT 내부통제·시행세칙 제7조의4·금융위 24시간 관제 점검):
  python run.py change-request <설치본> --by 아이디 --title 제목 --reason 사유 [--evidence 시험·부하 결과]  # 변경 요청(사전 영향 분석·자동 점검)
  python run.py change-gate <설치본> [--by 아이디] [--emergency 사유]  # 배포 직전 확인(vm.sh update 가 실행) — 막히면 종료 코드 3
  python run.py change-applied <번호> [--failed]   # 배포 결과 기록(무결성 기준선 번호와 함께) · change-rolled-back [번호] · change-list
  python run.py incident-report <번호> [--stage 최초|중간|종결] [--out 파일]  # 사고보고서 초안(별지 제2호 항목 순서)
  python run.py uptime-check       # 가용성 확인 1회 + 장애 판정(스케줄러가 1분마다)
  python run.py siem-export [--hours 24] [--format cef|jsonl] [--out 파일]  # 외부 관제(SIEM) 형식 내보내기(아이디 가명)
  python run.py pentest --base URL [--register | --user 아이디 --password 비번] [--authorized 승인 근거]  # 취약점 자가 점검(비파괴)
DB 운영(용량·수명주기·진단 — ADR-0027):
  python run.py db-report [--md 파일|--json]   # DB 진단 — 크기·증가 추세·디스크 소진 예측·죽은 행·겹치는/안 쓰는 인덱스·캐시·XID·느린 쿼리
  python run.py db-snapshot        # 오늘 크기 기록 + 판정·경보(스케줄러가 매일 04:50)
  python run.py ilm-status         # 수명주기 정책·기한 지난 행·보관 파일
  python run.py ilm-archive [--dataset news|search] [--dry-run] [--max-months N]  # 기한 지난 달 → 압축 보관·검증 → DB 정리(매월 2일)
  python run.py ilm-restore <manifest.json>  # 보관 파일을 다시 DB 로(해시 확인, 있는 행은 건너뜀)
  python run.py ilm-verify         # 보관 파일 전부 재해시(없어진·바뀐 파일)
  python run.py db-tune-plan --ram-mb N --cpus N --pg N  # 서버 설정 권장값(vm.sh update 가 적용)
화면 기능(2026-11-09):
  python run.py yangbong           # 양봉이 주간 추천 — 끝난 주 선정 + 30일 지난 바구니 채점(스케줄러가 매일 08:55·23:55) + 상태
  python run.py yangbong-status    # 바구니 수·최근 고른 주·채점 대기·마지막 배치
  python run.py vmarket-import <season_XX.json|season_XX.js> …  # 모의투자 시즌 파일 가져오기(형식·가격 검사 — 시작한 시즌은 변경 거부)
  python run.py vmarket-status     # 이번·다음 시즌 데이터 준비·남은 주·열린 참가·정산 대기
  python run.py vmarket-settle     # 끝난 시즌 정산(스케줄러가 매일 00:05)
  python run.py export-pseudonymized [파일] --purpose "목적" [--k 5]  # 분석용 가명정보(k-익명) 내보내기(감사 기록)
  python run.py dq                 # 데이터 계약(완전성·정확성·일관성·적시성·유효성) 측정·기록(스케줄러 매일 07:50)
  python run.py fds-eval           # 이상거래탐지 규칙 합성 평가(탐지율·오탐률) → docs/models/fds_eval.md
  python run.py ledger-close [날짜] # 원장 일 마감(기본 어제) — 분개 균형·시산표·잔액 대사(스케줄러가 매일 00:20)
  python run.py ledger-verify      # 지난 마감 전부 재계산(해시 사슬) + 시산표 합 0 확인
  python run.py crypto-rotate      # 키 암호화 키(KEK) 교체 후 — 암호화 열의 데이터 키를 새 KEK 로 다시 포장(rewrap)
  python run.py admin-bootstrap    # ADMIN_USERS 계정에 관리 역할 '자격' 부여(활성은 TOTP + JIT 로)
  python run.py user-unlock <아이디>  # 연속 실패 잠금 해제
  python run.py snapshot-import <파일> --yes  # 팀원 PC 에서 — 그 표들만 바꿈(계정·지갑·채팅·로그 그대로)

실시간(상시 프로세스 — tmux 창 하나 또는 systemd):
  python run.py realtime        # 국내(KIS)+해외(Finnhub) 동시 — 키가 있는 쪽만 켜짐
  python run.py realtime-kr     # 국내만 (KIS_APP_KEY/SECRET)
  python run.py realtime-us     # 해외만 (FINNHUB_API_KEY)
  python run.py realtime-sim    # 키 없이 시뮬레이션(가짜 가격, 화면에 '시뮬레이션' 표시)
데이터 소스: 국내 시세·지수=KRX OpenAPI, 재무=DART, 뉴스=Google RSS, 실시간=KIS /
            해외 일봉=FinanceDataReader, 실시간=Finnhub, 재무·주식수=SEC EDGAR, 환율=수출입은행/FDR.
"""
import sys
from app import clock
from app.logging_conf import setup_logging


def _seed_demo():
    """네트워크 없이 OUTOFSTOCK 전 화면을 생성 데이터로 채운다(오프라인 미리보기)."""
    from app import db
    from app.services import catalog, practice
    from app.collectors import marketgen
    from app.services.themes import seed_demo_news, classify_news
    db.init_db()
    with db.transaction() as conn:
        catalog.seed_stocks(conn)
    marketgen.seed_market(real=False)
    for sc in practice.SCENARIOS:
        marketgen.seed_scenario_ohlc(sc, real=False)
    practice.seed_demo_progress("guest")
    seed_demo_news()
    classify_news()
    _seed_market_news()
    _seed_demo_financials()
    _build_similar()
    from app.collectors import overseas_market
    overseas_market.seed_overseas(real=False)
    try:
        _seed_demo_wallet()
    except Exception as e:                       # 데모 시드는 실패해도 본 데모를 막지 않는다
        import logging
        logging.getLogger(__name__).warning("지갑 데모 시드 건너뜀: %s", e)
    _publish_reco()
    try:                                         # 분석 계층(정보계 — 신용·수익률 모형 입력)을 표본으로 채움 — 출처 gen
        from app.collectors import krx_history
        krx_history.seed_from_app_tables()
    except Exception as e:                       # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("분석 계층 표본 채우기 건너뜀: %s", e)


def _publish_reco():
    """오늘의 추천 Top 5 를 계산해 스냅샷(감사 추적)으로 남긴다 — 전체 + 성향별 3종. 실패해도 배치는 계속."""
    try:
        from app.services import signals, reco_engine
        signals.clear_cache()
        out = {"all": len(signals.today(5)["items"])}
        for prof in reco_engine.PROFILES:
            out[prof] = len(signals.today(5, prof)["items"])
        return out
    except Exception as e:                       # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("추천 스냅샷 건너뜀: %s", e)
        return None


def _seed_demo_financials():
    """데모 재무(표본) — 오프라인에서도 추천 모델의 가치·재무 건전성 팩터와 상세 PER/PBR 이 보이게.
    값은 dart.demo_statement()(데모 PER·PBR 역산, DART 실데이터 아님). 실데이터로 바뀌면
    marketgen 이 dart.purge_demo_statements() 로 지우고, DART 수집이 실제 값으로 채운다."""
    from sqlalchemy import text
    from app import db
    from app.collectors import dart
    from app.config import settings
    from app.services import catalog
    year = int(settings.DART_BSNS_YEAR or clock.today().year - 1)
    now = clock.iso_now()
    with db.transaction() as conn:
        for t in catalog.STOCKS:
            d = dart.demo_statement(t, year)
            if not d:
                continue
            conn.execute(text(
                "INSERT INTO financial_statement(ticker, fiscal_year, report_code, revenue, operating_profit, "
                "net_income, total_equity, debt_ratio, disclosed_at, ingested_at, total_assets, current_assets, "
                "current_liabilities, total_liabilities, retained_earnings) "
                "VALUES(:ticker,:fiscal_year,:rc,:revenue,:operating_profit,:net_income,:total_equity,:debt_ratio,"
                ":disclosed_at,:ing,:total_assets,:current_assets,:current_liabilities,:total_liabilities,"
                ":retained_earnings) ON CONFLICT(ticker, fiscal_year, report_code) DO NOTHING"),
                {**d, "rc": settings.DART_REPRT_CODE, "ing": now})


def _build_similar():
    """재무 유사 종목(종목 상세 '유사 종목'·/stocks/{t} 의 similar) — 재무가 바뀐 뒤 다시 계산.
    예전엔 레거시 'run.py pipeline'(FDR 배치)에서만 돌아, real·demo 로 운영하면 늘 비어 있었다."""
    from app.services.recommend import build_recommendations
    return build_recommendations()


def _step(failed, name, fn):
    """파이프라인 한 단계 — 실패해도 다음 단계는 계속(실패는 모아 PARTIAL 로 기록).
    예전엔 KIS 토큰·DART corpCode 하나만 실패해도 뒤의 실습·재무·뉴스·해외·추천이 전부 건너뛰어졌다.
    예외뿐 아니라 {'error': …} 를 돌려준 단계(KIS 토큰·DART 키 오류를 삼키고 알려 주는 수집기)도 실패로 센다.
    오류 문구는 API 키를 가린 뒤에 남긴다(공개 /health·실행 기록에 키가 새지 않게)."""
    import logging
    from app.logging_conf import redact, safe_error
    try:
        out = fn()
    except Exception as e:                       # noqa: BLE001
        msg = safe_error(e, 160)
        logging.getLogger("run").exception("%s 실패 — 다음 단계 계속", name)
        print(f"   ✗ {name} 실패: {msg}")
        failed.append(f"{name}: {msg}")
        return None
    if isinstance(out, dict) and out.get("error"):
        msg = redact(str(out["error"]))[:160]
        logging.getLogger("run").warning("%s 실패 — 다음 단계 계속: %s", name, msg)
        print(f"   ✗ {name} 실패: {msg}")
        failed.append(f"{name}: {msg}")
    return out


def _real_all():
    """실데이터 전체 파이프라인 — KRX(+KIS 오늘 종가) + 실습 구간 + DART + 뉴스 + 해외. (실시간은 별도 프로세스)
    단계마다 독립 — 한 단계가 실패해도 나머지는 돌고, 실패한 단계는 반환값 failed(=errors)에 남는다(→ PARTIAL)."""
    from app import db
    from app.services import practice, signals
    from app.collectors import marketgen, dart, news, realtime
    from app.services.themes import classify_news
    db.init_db()
    failed = []
    from app.collectors import krx_history
    if krx_history.enabled():
        # 분석 계층(전 종목) 먼저 — 여기서 받은 날은 유니버스 적재가 KRX 를 다시 부르지 않는다(하루 360회 → 몇 회)
        pl = _step(failed, "분석 계층 적재", _platform_daily)
        print("0) 분석 계층(전 종목 일별 시세) 증분 + 10년 백필 일부…", pl)
    m = _step(failed, "KRX 시세", lambda: marketgen.seed_market(real=True))
    print("1) KRX 시세·유니버스(top-N) + 코스피·코스닥 지수…", m)
    _warn_banner(m)
    # 적재 직후 품질 게이트 — 출처·유니버스·최근 일봉 수·최신일. 실패면 알림(10-10: 롤백이 1주일간 조용히 반복된 장애)
    from app.services import quality
    gate = _step(failed, "적재 검증", lambda: quality.gate_and_alert(m if isinstance(m, dict) else None))
    if isinstance(gate, dict):
        print(f"   적재 검증: {gate['level']} — {gate['summary']}")
        if gate["level"] == "fail":
            failed.append(f"적재 검증: {gate['summary']}")
    if realtime.enabled():                       # KRX 는 D+1 제공 → 오늘 종가는 KIS 로 바로 이어 붙인다
        print("2) KIS 최근 봉(오늘 종가 포함)…", _step(failed, "KIS 오늘 종가", realtime.refresh_latest_bars))
    else:
        print("2) KIS 키 없음 — 오늘 종가는 다음 날 아침 KRX 수집 때 반영")
    from app.services import invest                # 권리 이벤트(액면분할·무상증자)를 모의투자 보유 수량·매수가에 반영
    print("   모의투자 권리 반영…", _step(failed, "모의투자 권리 반영", invest.apply_corporate_actions))
    print("3) KRX 실습 시나리오 구간(이미 받은 구간은 건너뜀)…")
    for sc in practice.SCENARIOS:
        n = _step(failed, f"실습 {sc['scenario_id']}", lambda sc=sc: marketgen.seed_scenario_ohlc(sc, real=True))
        print(f"   {sc['name']}: {n}건")
    d = _step(failed, "DART 재무", dart.collect_financials)
    print("4) DART 재무(PER/PBR)…", d)
    print("   재무 유사 종목…", _step(failed, "재무 유사 종목", _build_similar))
    md = _step(failed, "모형 입력 데이터", _market_data_daily)
    print("4-1) 모형 입력 데이터(금리·공시·업종·다년 재무)…", md)
    nw = _step(failed, "뉴스", news.collect_news) or {}
    print("5) Google RSS 뉴스(하루 1번)…", nw)
    if nw.get("stopped"):
        _warn_banner({"warning": f"뉴스 수집 중단: {nw['stopped']} — 받은 종목만 반영, 다음 실행 때 다시 시도"})
    _step(failed, "뉴스 감성", classify_news)
    from app.collectors import overseas_market
    o = _step(failed, "해외", lambda: overseas_market.seed_overseas(real=True))
    print("6) 해외(미국) 일봉·지수·환율…", o)
    _warn_banner(o)
    signals.clear_cache()                        # 홈 '오늘의 추천 종목' 을 새 일봉으로 다시 계산
    print("7) 오늘의 추천 Top 5(멀티팩터) 계산·스냅샷 기록…", _publish_reco())
    if failed:
        _warn_banner({"warning": "일부 단계 실패(나머지는 반영됨): " + " / ".join(failed)})
    g = lambda x, k: x.get(k) if isinstance(x, dict) else None          # noqa: E731
    out = {"market": g(m, "source"), "stocks": g(m, "stocks"), "as_of": g(m, "as_of"),
           "dart": g(d, "ok"), "news": g(nw, "total"), "overseas": g(o, "source"), "failed": failed,
           "quality": g(gate, "level")}
    if failed:
        out["errors"] = list(failed)                      # 예전 이름(다른 판 호환) — failed 와 같은 내용
    return out


def _platform_daily():
    """매일: 최근 빠진 날(증분) + 10년 백필을 호출 예산만큼 이어서. 끝나면 종목 스냅샷 새로 고침."""
    from app.collectors import krx_history
    from app.config import settings
    inc = krx_history.incremental()
    back = krx_history.backfill(budget=settings.ANALYTICS_BACKFILL_BUDGET, refresh=False)
    loaded = [d for d in (inc.get("min_date"), back.get("min_date")) if d]
    # 새로 받은 가장 이른 날부터 권리 이벤트(액면분할 등)를 다시 찾고 스냅샷을 수정주가 기준으로 새로 고친다
    snap = krx_history.refresh_snapshot(adj_since=min(loaded) if loaded else None)
    out = {"incremental_days": inc.get("days"), "backfill_days": back.get("days"), "calls": inc["calls"] + back["calls"],
           "remaining_days": back.get("remaining"), "snapshot": snap.get("rows")}
    if inc.get("failed") and not inc.get("days"):
        out["error"] = f"KRX 증분 적재 실패 {inc['failed']}건"
    return out


def _market_data_daily(history=False):
    """정보계(분석 계층) 모형 입력 — 금리(KIS·ECOS)·시장조치/주요사항 공시·업종(KSIC)·다년 재무.
    신용위험 모형(credit-train)·수익률 검증(return-validate)·종목 위험 화면의 입력만 받는다
    (시장 분석 화면용 수급·증시자금·가상자산 등은 팀 결정으로 제거, 2026-10).
    원천마다 따로 실패해도 나머지는 계속(실패는 errors 에 이름만). history=True 면 10년 이력을 예산 안에서 크게 받는다."""
    from datetime import timedelta
    from app.config import settings
    from app.collectors import dart_platform, ecos, kis_market, krx_history
    from app.logging_conf import safe_error
    today = clock.now().date()
    years = settings.ANALYTICS_YEARS
    out, errs = {}, {}

    def sub(name, fn):
        try:
            out[name] = fn()
        except Exception as e:                   # noqa: BLE001 — 한 원천 실패가 다른 원천을 막지 않게
            errs[name] = safe_error(e, 160)
    if kis_market.enabled():
        sub("금리(KIS)", kis_market.interest_rates)
    if ecos.enabled():
        sub("ECOS 금리·환율", lambda: ecos.collect(start=None if history else today - timedelta(days=40)))
    if settings.DART_API_KEY:
        start = today - timedelta(days=365 * years) if history else today - timedelta(days=10)
        sub("시장조치·주요사항 공시", lambda: dart_platform.disclosures(start, today, budget=3000 if history else 60))
        sub("업종(KSIC)", lambda: dart_platform.company_sectors(budget=3000 if history else 300))
        sub("다년 재무", lambda: dart_platform.financials(budget=400 if history else 40))
    snap = krx_history.refresh_snapshot()
    res = {"ok": sorted(out), "errors": errs, "snapshot": snap.get("rows")}
    if errs and not out:
        res["error"] = "모형 입력 데이터 전부 실패: " + ", ".join(errs)
    return res


def _windows(fn, start, end, days):
    """기간 조회 API 를 days 일 창으로 나눠 부른다(한 번에 너무 많은 쪽을 받지 않게)."""
    from datetime import timedelta
    total, a = 0, start
    while a <= end:
        b = min(end, a + timedelta(days=days - 1))
        total += (fn(a, b) or {}).get("rows", 0)
        a = b + timedelta(days=1)
    return {"rows": total}


def real_pipeline(trigger="manual"):
    """python run.py real 과 스케줄러(매일 23:00)가 함께 쓰는 진입점.
    · 동시 실행 방지: PostgreSQL advisory lock — 이미 다른 곳에서 돌고 있으면 건너뜀(데이터 꼬임 방지).
    · 실행 기록: app_meta 에 시작/종료 시각·결과(마지막 성공 시각은 스케줄러의 '놓친 실행 보충'에 사용)."""
    import json
    import time
    from app import db
    from app.services import quotes
    db.init_db()
    with db.advisory_lock("real") as got:
        if not got:
            msg = "다른 곳에서 run.py real 이 이미 실행 중 — 이번 실행은 건너뜀"
            print(msg)
            return {"skipped": True, "reason": msg}
        from app.services.monitoring import start_run, finish_run, close_stale_runs
        t0 = time.monotonic()
        close_stale_runs(holding_real_lock=True)               # 잠금을 잡았다 = 다른 real 은 없다 → 남은 RUNNING 은 죽은 실행
        run_id = start_run()                                   # /health · /pipeline/runs 이력
        db.set_meta("pipeline_real_started", quotes.iso_now())
        db.set_meta("pipeline_real_trigger", trigger)
        from app.logging_conf import safe_error
        try:
            out = _real_all() or {}
        except Exception as e:
            from app.logging_conf import safe_error            # 예외 문자열의 URL 에 API 키가 섞일 수 있다
            err = safe_error(e, 300)
            finish_run(run_id, "FAILED", note=f"run.py real({trigger}) 실패: {err}"[:200])
            from app.services import alerts
            alerts.notify("pipeline:real", "정기 수집 전체 실패", f"run.py real({trigger}): {err}", level="error")
            db.set_meta("pipeline_real_status", json.dumps(
                {"ok": False, "error": err, "at": quotes.iso_now(), "trigger": trigger},
                ensure_ascii=False))
            raise
        out["seconds"] = round(time.monotonic() - t0, 1)
        failed = out.get("failed") or []
        note = f"run.py real({trigger}) 국내={out.get('market')} 해외={out.get('overseas')}"
        if failed:
            note += " · 실패: " + ", ".join(f.split(":")[0] for f in failed)
        q = out.get("quality")
        finish_run(run_id, "PARTIAL" if failed else "SUCCESS",
                   counts={"prices": out.get("stocks"), "dart": out.get("dart"), "news": out.get("news")},
                   quality=None if q is None else {"ok": q != "fail", "total_issues": 0 if q == "ok" else 1},
                   note=note[:200])
        if failed:                                             # 일부 단계 실패 — 사람이 알게(같은 문제는 6시간에 1번)
            from app.services import alerts
            alerts.notify("pipeline:real", f"정기 수집 일부 실패({len(failed)}단계)",
                          "\n".join(f"- {f}" for f in failed) + f"\n실행: {trigger} · {out['seconds']}초",
                          level="error" if any(f.startswith(("KRX 시세", "적재 검증")) for f in failed) else "warn")
        db.set_meta("pipeline_real_finished", quotes.iso_now())
        # 마지막 '성공' 시각(스케줄러의 놓친 실행 보충 기준)은 일부 실패여도 갱신 — 실패 단계는 다음 날 다시
        db.set_meta("pipeline_real_status", json.dumps(
            {"ok": not failed, "partial": bool(failed), "at": quotes.iso_now(), "trigger": trigger, **out},
            ensure_ascii=False))
        return out


def _warn_banner(result):
    """실데이터를 못 받아 표본으로 대체됐으면 눈에 띄게 경고(예전엔 조용히 'krx'로 보고)."""
    for k in ("warning", "index_warning"):
        w = (result or {}).get(k)
        if w:
            bar = "!" * 72
            print(f"\n{bar}\n[경고] {w}\n{bar}\n")


RT_RESTART_BASE = 30          # 죽은 실시간 수집기 재시작 대기(초) — 30 → 60 → 120 … 최대 RT_RESTART_MAX
RT_RESTART_MAX = 300


def _rt_alert(key, title, message, level):
    """실시간 수집기 알림 — 실패해도 수집기 감시를 방해하지 않는다."""
    try:
        from app.services import alerts
        alerts.notify(key, title, message, level=level)
    except Exception as e:                   # noqa: BLE001
        import logging
        logging.getLogger("run").warning("실시간 알림 실패: %s", type(e).__name__)


def _run_realtime(which, quit_event=None):
    """실시간 수집기 실행 — 국내(KIS)·해외(Finnhub)를 스레드로 동시에. Ctrl+C(또는 systemd stop)로 전체 종료.
    수집기 하나가 죽으면(시작할 때 DB 연결 실패 등) 그 수집기만 잠시 뒤 다시 띄우고 나머지는 계속 돈다.
    예전엔 죽은 수집기를 그대로 둔 채 프로세스가 살아 있어(종료 코드도 0) tmux·systemd 어느 쪽에서도 다시 뜨지 않았다."""
    import logging
    import signal
    import threading
    import time
    from app.collectors import realtime, overseas_realtime
    log = logging.getLogger("run")
    jobs = []
    if which in ("all", "kr"):
        if realtime.enabled():
            jobs.append(("국내 KIS", lambda ev: realtime.run_realtime(stop=ev)))
        else:
            print("· 국내(KIS) 건너뜀 — .env 에 KIS_APP_KEY / KIS_APP_SECRET 필요")
    if which in ("all", "us"):
        if overseas_realtime.enabled():
            jobs.append(("해외 Finnhub", lambda ev: overseas_realtime.run_realtime(stop=ev)))
        else:
            print("· 해외(Finnhub) 건너뜀 — .env 에 FINNHUB_API_KEY 필요")
    if which == "sim":
        jobs = [("국내 시뮬", lambda ev: realtime.run_sim(stop=ev)),
                ("해외 시뮬", lambda ev: overseas_realtime.run_sim(stop=ev))]
    if not jobs:
        print("실행할 실시간 수집기가 없습니다. 키를 넣거나, 화면 확인만 하려면: python run.py realtime-sim")
        return
    quit_ev = quit_event or threading.Event()
    in_main = threading.current_thread() is threading.main_thread()      # 신호 처리기는 메인 스레드에서만 걸 수 있다
    if in_main:
        old_int = signal.signal(signal.SIGINT, signal.default_int_handler)    # 백그라운드/nohup 실행에서도 Ctrl+C 동작
        old_term = signal.signal(signal.SIGTERM, lambda *_: quit_ev.set())    # systemd stop → 깔끔하게 종료
    st, gave_up = {}, []

    def guarded(name, fn, ev):
        try:
            fn(ev)
        except Exception:                    # noqa: BLE001 — 로거로 남겨 키 가리기 적용(스레드 기본 출력은 안 가림)
            st[name]["crashed"] = True       # 예외로 죽음 → 잠시 뒤 이 수집기만 다시 띄운다
            log.exception("%s 수집기 오류", name)

    def launch(name, fn, fails=0):
        ev = threading.Event()               # 수집기마다 따로 — 죽은 수집기가 띄운 보조 스레드(봉 갱신·폴러)만 멈추게
        t = threading.Thread(target=guarded, args=(name, fn, ev), name=name, daemon=True)
        st[name] = {"fn": fn, "t": t, "ev": ev, "at": time.monotonic(), "fails": fails, "retry": None,
                    "crashed": False}
        t.start()

    for name, fn in jobs:
        launch(name, fn)
    print("실시간 수집 시작:", ", ".join(n for n, _ in jobs), "— 종료: Ctrl+C")
    try:
        while not quit_ev.is_set():
            now = time.monotonic()
            for name, s in list(st.items()):
                if s["t"].is_alive():
                    continue
                if not s["crashed"] and not s["ev"].is_set():
                    # 예외 없이 스스로 끝남 = 설정(키)·인증 문제로 수집기가 포기한 것 — 같은 프로세스에서 다시 띄워도
                    # 그대로라 전체를 내리고 종료 코드 1 → 서비스 관리자(systemd Restart=on-failure)가 간격을 두고 다시 띄움
                    gave_up.append(name)
                    continue
                if s["retry"] is None:
                    s["ev"].set()
                    fails = 1 if now - s["at"] > 600 else s["fails"] + 1    # 10분 넘게 잘 돌다 죽었으면 대기 처음부터
                    wait = min(RT_RESTART_BASE * 2 ** (fails - 1), RT_RESTART_MAX)
                    s["fails"], s["retry"] = fails, now + wait
                    msg = f"{name} 수집기가 멈췄습니다 — {wait:g}초 뒤 다시 시작({fails}번째)"
                    print(msg)
                    log.warning(msg)
                    # 알림은 별도 스레드로 — 채널 응답이 늦어도 감시 루프(재시작 타이머)가 멈추지 않게.
                    # 같은 수집기는 ALERT_DEDUP_HOURS 안에 한 번만(재시작이 반복돼도 알림 폭주 없음)
                    threading.Thread(target=_rt_alert, args=(f"realtime:{name}", f"실시간 수집기 재시작: {name}",
                                                             msg, "warn"), daemon=True).start()
                elif now >= s["retry"]:
                    launch(name, s["fn"], s["fails"])
            if gave_up:
                break
            time.sleep(min(1.0, RT_RESTART_BASE))
    except KeyboardInterrupt:
        pass
    finally:
        if in_main:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
    print("\n종료 중…")
    for s in st.values():
        s["ev"].set()
    for s in st.values():
        s["t"].join(timeout=10)
    if gave_up:
        msg = f"실시간 수집기가 스스로 멈춤: {', '.join(gave_up)} — 종료 코드 1(서비스 관리자가 다시 시작)"
        print(msg)
        log.error(msg)
        _rt_alert("realtime:gave_up", "실시간 수집기 중단(설정·인증 문제 의심)",
                  msg + "\n확인: .env 의 KIS/Finnhub 키, 'python run.py realtime' 로그", "error")   # 종료 전에 보낸다
        sys.exit(1)
    print("실시간 수집 종료")


# 데모 시장 뉴스(종목 태그 없음) — (며칠 전, 시각, 제목). 언론사명은 가상(themes.DEMO_SOURCES).
_MARKET_NEWS = [
    (0, "09:05", "외국인 5거래일 연속 순매수… 반도체 업종 중심"),
    (0, "11:40", "한국은행 기준금리 동결, 채권시장 영향 제한적"),
    (1, "10:20", "코스닥 2차전지 업종 차익 매물에 조정"),
    (1, "16:05", "원/달러 환율 1,350원선 앞에서 등락"),
    (2, "08:50", "증권가 \"3분기 실적 시즌 기대치 상향 조정\""),
    (2, "15:30", "개인 투자자 거래대금 3개월 만에 최고 수준"),
    (3, "09:40", "반도체 수출 증가세 지속… 8월 수출액 발표"),
    (3, "14:10", "기관, 금융업종 비중 확대… 배당주 관심 이어져"),
    (5, "15:50", "코스피 시가총액 상위 종목 등락 엇갈려"),
    (6, "08:20", "미국 고용지표 발표 앞두고 관망세 짙어져"),
    (7, "10:10", "자동차 업종 신차 효과 기대… 목표주가 상향"),
    (8, "13:35", "ETF 순자산 총액 사상 최대치 경신"),
    (10, "09:15", "바이오 업종 임상 결과 발표 앞두고 관망"),
]


def _seed_market_news():
    from datetime import datetime, timedelta, timezone
    from app import db
    from app.services.themes import DEMO_SOURCES
    from sqlalchemy import text
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).replace(second=0, microsecond=0)
    with db.transaction() as conn:
        for i, (days, hm, title) in enumerate(_MARKET_NEWS):
            h, m = (int(x) for x in hm.split(":"))
            at = (now - timedelta(days=days)).replace(hour=h, minute=m)
            if at > now:
                at = now - timedelta(minutes=5 + i * 9)
            conn.execute(text("INSERT INTO news(title,url,source,published_at,published_ts) "
                              f"VALUES(:t,:u,:s,:p,:ts) {db.NEWS_URL_CONFLICT} DO UPDATE SET "
                              "published_at=EXCLUDED.published_at, published_ts=EXCLUDED.published_ts, "
                              "source=EXCLUDED.source"),
                         {"t": title, "u": f"news://market/{i}", "s": DEMO_SOURCES[(i + 2) % len(DEMO_SOURCES)],
                          "p": at.date().isoformat(), "ts": at.isoformat(timespec="seconds")})


def _seed_demo_wallet():
    """추가 기능 데모 — 데모 계정에 지갑·출석·충전·장기투자·송금 이력을 채운다.
    로그인(demo@oos.io / demo1234)하면 마이페이지에서 전 기능이 채워진 상태로 보인다."""
    from datetime import datetime, timedelta, timezone
    from app import db, clock
    from app.services import (auth as auth_svc, wallet, attendance,
                              iap, transfer)
    KST = timezone(timedelta(hours=9))
    demo, friend = "demo@oos.io", "friend@oos.io"
    for u in (demo, friend):
        try:
            auth_svc.register(u, "demo1234", enforce_policy=False)   # 공개 데모 계정(정책 예외 — 운영 DB 에선 거부됨)
        except auth_svc.AuthError:
            pass
    # 3일 연속 출석(그제·어제·오늘)
    today = datetime.now(KST).date()
    for d in (today - timedelta(days=2), today - timedelta(days=1), today):
        attendance.check_in(demo, today=d.isoformat())
    # 인앱결제 충전(데모 영수증)
    iap.purchase(demo, "google", "coins_large", "demo-receipt-google-large")
    # 장기 투자 — 삼성전자 약 60거래일 전 매수(현재가 대비 손익이 보이도록 과거 시점 삽입)
    bars = db.query("SELECT trade_date, close FROM ohlc WHERE ticker='005930' "
                    "ORDER BY trade_date DESC LIMIT 90")
    if len(bars) >= 60 and bars[59]["close"]:
        b = bars[59]
        bp, qty = int(b["close"]), 30
        cost = bp * qty
        with db.transaction() as conn:
            wallet.ensure(conn, demo)
            wallet.debit(conn, demo, cost, "invest_buy", ref="005930",
                         memo="[데모] 삼성전자 30주 매수")
            conn.execute(db.invest_position.insert().values(
                username=demo, ticker="005930", quantity=qty, buy_price=bp,
                buy_date=b["trade_date"], cost=cost, status="open",
                created_at=clock.iso_now()))
    # 친구에게 송금
    transfer.send(demo, friend, 300_000, memo="[데모] 용돈")


USAGE = ("사용법: python run.py [demo [--force]|real|initdb|recompute|fetch-market|fetch-index|fetch-overseas|"
         "fetch-latest|fetch-practice [--force]|fetch-dart|fetch-news|snapshot-export|snapshot-import|"
         "realtime|realtime-kr|realtime-us|realtime-sim|quality|alert-test|"
         "platform-load [--years N] [--budget N]|platform-history|platform-status|platform-refresh|"
         "credit-train [--synthetic]|return-validate [--synthetic]|model-report credit|return|"
         "analytics-query <이름> [k=v …]|security-status|security-purge|audit-verify|crypto-rotate|admin-bootstrap|"
         "ledger-close [날짜]|ledger-verify|fds-eval|dq|data-docs|export-pseudonymized|"
         "self-assess|assurance-docs|integrity-baseline|integrity-check|incident-drill --target-db URL|"
         "change-request|change-gate|change-applied|change-rolled-back|change-list|incident-report|uptime-check|"
         "siem-export|pentest --base URL|db-report|db-snapshot|ilm-status|ilm-archive|ilm-restore|ilm-verify|"
         "db-tune-plan|yangbong|yangbong-status|vmarket-import <파일>…|vmarket-status|vmarket-settle|"
         "user-unlock <아이디>]")


def _has_real_data():
    """이 DB 에 실데이터가 들어 있나(국내 KRX 또는 해외 FDR) — 데모 시드가 덮어쓰지 않게 확인."""
    from app import db
    return db.get_meta("market_source") == "krx" or db.get_meta("overseas_source") in ("fdr", "mixed")


def _refuse_demo_on_real_db():
    """실데이터 DB 에 demo 를 덮지 않게 — 시세·실습 구간·해외가 표본으로 바뀌고 공개 비밀번호 데모 계정
    (demo@oos.io / friend@oos.io)이 생긴다. 정말 덮으려면 --force. 반환: 거부했으면 True."""
    from app import db
    db.init_db()
    if _has_real_data() and "--force" not in sys.argv[2:]:
        print("[중단] 이 DB 에는 실데이터(KRX/해외)가 있어요 — demo 는 시세·실습 구간·해외 데이터를 생성 데이터로 덮고\n"
              "데모 계정(demo@oos.io·friend@oos.io)을 만듭니다.\n"
              "  실데이터를 새로 받으려면: python run.py real · 감성·추천만 다시 계산: python run.py recompute\n"
              "  정말 표본으로 덮으려면: python run.py demo --force")
        return True
    return False


def _opt(name, typ=str):
    """--name 값 형태의 선택 인자."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return typ(sys.argv[i + 1])
    return None


def _internal_control(cmd) -> bool:
    """IT 내부통제 명령 — 처리했으면 True."""
    import json
    from pathlib import Path
    from app import db
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    opts = {a for a in sys.argv[2:] if a.startswith("--")}
    if cmd == "change-request":
        from app.services import changes
        if not args:
            print("사용법: python run.py change-request <설치본> --by 아이디 --title 제목 --reason 사유 [--evidence 근거]")
            sys.exit(2)
        db.init_db()
        try:
            r = changes.request(Path(args[0]), _opt("--by") or "cli", _opt("--title") or "", _opt("--reason") or "",
                                _opt("--evidence") or "")
        except changes.ChangeError as e:
            print("거부:", e)
            sys.exit(2)
        im = r["impact"]
        print(f"변경 요청 #{r['id']} — 위험도 {im['risk']} · 바뀐 {im['changed']} · 새 {im['added']} · 사라진 {im['removed']} 파일"
              f"{' · 검증 근거 필요' if im['needs_evidence'] else ''}")
        for k, v in im["areas"].items():
            print(f"  {k}: {v['count']}개 (예: {', '.join(v['sample'][:3])})")
        for c in r["prechecks"]:
            print(f"  {'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}")
        print("다른 관리자가 승인: POST /admin/changes/%d/approve (요청자 ≠ 승인자)" % r["id"])
        sys.exit(1 if r["blocking"] else 0)
    if cmd == "change-gate":
        from app.services import changes
        if not args:
            print("사용법: python run.py change-gate <설치본> [--by 아이디] [--emergency 사유]")
            sys.exit(2)
        db.init_db()
        try:
            g = changes.gate(Path(args[0]), _opt("--emergency") or "", _opt("--by") or "deploy")
        except changes.ChangeError as e:
            print("거부:", e)
            sys.exit(3)
        print(f"변경관리({g['mode']}): {'허용' if g['allow'] else '중단'} — {g['reason']}")
        for w in g.get("warnings") or ():
            print("  주의:", w)
        for c in g.get("prechecks") or ():
            if not c["ok"]:
                print(f"  ✗ {c['check']}: {c['detail']}")
        if g.get("change_id"):
            print(f"CHANGE_ID={g['change_id']}")
        sys.exit(0 if g["allow"] else 3)
    if cmd == "change-applied":
        from app.security import integrity
        from app.services import changes
        if not args or not args[0].isdigit():
            print("사용법: python run.py change-applied <번호> [--failed] [--by 아이디]")
            sys.exit(2)
        db.init_db()
        try:
            base = integrity.latest_baseline()
        except Exception:                                   # noqa: BLE001 — 기준선 표가 없는 구버전
            base = None
        r = changes.applied(int(args[0]), _opt("--by") or "deploy", base["id"] if base else None, ok="--failed" not in opts)
        print(f"변경 #{args[0]}: {r['status'] if r else '—'}" + (f" · 무결성 기준선 #{base['id']}" if base else ""))
        return True
    if cmd == "change-rolled-back":
        from app.services import changes
        db.init_db()
        r = changes.rolled_back(_opt("--by") or "deploy", int(args[0]) if args and args[0].isdigit() else None)
        print(f"되돌림 기록: 변경 #{r['id']}" if r else "적용된 변경 기록 없음")
        return True
    if cmd == "change-list":
        from app.services import changes
        db.init_db()
        ov = changes.overview()
        print(f"변경관리 모드 {ov['mode']} · 배포 금지 시간 {ov['freeze']}(지금 {'금지' if ov['freeze_now'] else '가능'}) · "
              f"승인 대기 {len(ov['pending'])} · 사후 검토 기한 초과 {len(ov['overdue_reviews'])}")
        for c in ov["recent"][:20]:
            at = clock.parse_utc(c["created_at"]).astimezone(clock.KST).strftime("%m-%d %H:%M")
            print(f"  #{c['id']} {at} KST {c['kind']:10} {c['status']:11} 위험 {c['risk']:6} {c['title']}")
        return True
    if cmd == "incident-report":
        from app.services import incidents
        if not args or not args[0].isdigit():
            print("사용법: python run.py incident-report <번호> [--stage 최초|중간|종결] [--out 파일]")
            sys.exit(2)
        db.init_db()
        try:
            md = incidents.report_md(int(args[0]), _opt("--stage") or "최초")
        except LookupError:
            print("없는 사건:", args[0])
            sys.exit(2)
        out = _opt("--out")
        if out:
            Path(out).write_text(md, encoding="utf-8")
            print("저장:", out)
        else:
            print(md)
        return True
    if cmd == "uptime-check":
        from app.services import incidents
        db.init_db()
        url = incidents.target_url()
        if not url:
            print("UPTIME_URL=off — 감시 꺼짐")
            return True
        res = incidents.probe(url)
        incidents.record_check(res)
        ev = incidents.evaluate()
        av = incidents.availability(30)
        print(f"{url} → {res['code']} · {res['ms']}ms · {'정상' if res['ok'] else '실패/지연'} {ev or ''}")
        print(f"30일 가용률 {av['availability']}% (목표 {av['target']}%) · 확인 {av['checks']}회 · 감시 범위 {av['coverage']}%")
        sys.exit(0 if res["ok"] else 1)
    if cmd == "siem-export":
        from app.security import siem
        db.init_db()
        fmt = _opt("--format") or "cef"
        try:
            r = siem.export(_opt("--hours", float) or 24, fmt, actor="cli")
        except ValueError as e:
            print("거부:", e)
            sys.exit(2)
        out = _opt("--out")
        text_ = "\n".join(r["lines"]) + ("\n" if r["lines"] else "")
        if out:
            Path(out).write_text(text_, encoding="utf-8")
            print(f"{r['count']}건({fmt}) → {out}")
        else:
            sys.stdout.write(text_)
        return True
    if cmd == "pentest":
        from app.security import pentest
        base = _opt("--base")
        if not base:
            print("사용법: python run.py pentest --base http://127.0.0.1:8000 [--register | --user 아이디 --password 비번] "
                  "[--authorized '승인 근거'] [--out 폴더]")
            sys.exit(2)
        db.init_db()
        try:
            rep = pentest.run(base, user=_opt("--user"), password=_opt("--password"), register="--register" in opts,
                              authorized=_opt("--authorized") or "", actor="cli",
                              out_dir=Path(_opt("--out") or pentest.OUT_DIR))
        except pentest.PentestError as e:
            print("거부:", e)
            sys.exit(2)
        c = rep["counts"]
        for f in rep["flow"]:
            print(f"  {f['name']}: 시도 {f['tried']} · " + (f"열림 {f['open']}" if f["open"] else "막힘"))
        print(f"취약점 자가 점검 — 막힘 {c['pass']} · 열림 {c['fail']} · 주의 {c['warn']} · 건너뜀 {c['skip']} · "
              f"해당 없음 {c['na']} · 오류 {c['error']} → {', '.join(rep.get('files') or [])}")
        print(json.dumps({"ok": rep["ok"]}, ensure_ascii=False))
        sys.exit(0 if rep["ok"] else 1)
    return False


def _feature_ops(cmd) -> bool:
    """화면 기능 운영 명령(양봉이 주간 추천·모의투자 시즌) — 처리했으면 True."""
    import json
    from pathlib import Path
    from app import db
    if cmd in ("yangbong", "yangbong-status"):
        from app.services import yangbong
        db.init_db()
        if cmd == "yangbong":
            print(json.dumps(yangbong.run(), ensure_ascii=False, indent=1, default=str))
        print(json.dumps(yangbong.status(), ensure_ascii=False, indent=1, default=str))
        return True
    if cmd in ("vmarket-import", "vmarket-status", "vmarket-settle"):
        from app.services import vmarket
        db.init_db()
        if cmd == "vmarket-import":
            files = [a for a in sys.argv[2:] if not a.startswith("--")]
            if not files:
                print("사용법: python run.py vmarket-import season_11.json [season_12.json …]")
                sys.exit(2)
            bad = 0
            for f in files:
                try:
                    print(f"{f}: {vmarket.import_season(vmarket.parse_file(Path(f)), Path(f).name)}")
                except (OSError, vmarket.VMarketError) as e:
                    bad += 1
                    print(f"{f}: 거부 — {e}")
            sys.exit(1 if bad else 0)
        if cmd == "vmarket-settle":
            print(json.dumps(vmarket.settle_all(), ensure_ascii=False))
        st = vmarket.status()
        print(f"이번 시즌 {st['season']} 데이터 {'있음' if st['current_ready'] else '없음'} · 다음 시즌 "
              f"{'있음' if st['next_ready'] else '없음(경보 대상)'} · 마지막 준비된 시즌 {st['last_ready']}(앞으로 {st['weeks_left']}주) · "
              f"열린 참가 {st['open_entries']} · 정산 대기 {st['unsettled']}")
        return True
    return False


def _db_ops(cmd) -> bool:
    """DB 운영 명령(ADR-0027) — 처리했으면 True."""
    import json
    from pathlib import Path
    from app import db
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if cmd == "db-tune-plan":                    # vm.sh update 가 읽는다 — DB 연결 없이 계산만
        from app.services import dbtune
        for line in dbtune.plan_lines(_opt("--ram-mb", int) or 4096, _opt("--cpus", int) or 2, _opt("--pg", int) or 16,
                                      ssd=(_opt("--ssd") or "1") != "0", preload=_opt("--preload") or ""):
            print(line)
        return True
    if cmd == "db-report":
        from app.services import dbhealth
        db.init_db()
        rep = dbhealth.report()
        if "--json" in sys.argv:
            print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
            return True
        md = dbhealth.render_md(rep)
        out = _opt("--md")
        if out:
            Path(out).write_text(md, encoding="utf-8")
            print("저장:", out)
        else:
            print(md)
        sys.exit(1 if any(f["level"] == "error" for f in rep["findings"]) else 0)
    if cmd == "db-snapshot":
        from app.services import dbhealth
        db.init_db()
        print(json.dumps(dbhealth.snapshot(notify="--quiet" not in sys.argv), ensure_ascii=False))
        return True
    if cmd == "ilm-status":
        from app.services import ilm
        db.init_db()
        st = ilm.status()
        print(f"보관 폴더 {st['archive_dir']}")
        for n, d in st["datasets"].items():
            print(f"  {n:7} {d['title']} — 보관 {d['retention_days']}일(기한 {d['cutoff']}) · 가장 오래된 행 {d['oldest']} · "
                  f"기한 지난 행 {d['rows_past_cutoff']:,} · 보관 파일 {d['files']}개 {d['bytes'] / 1048576:.1f}MB"
                  + (f"(원본 {d['raw_bytes'] / 1048576:.1f}MB)" if d["raw_bytes"] else "") +
                  (f" · {d['archived_through']} 까지 보관" if d["archived_through"] else ""))
        return True
    if cmd == "ilm-archive":
        from app.services import ilm
        db.init_db()
        r = ilm.archive(_opt("--dataset"), dry_run="--dry-run" in sys.argv, max_months=_opt("--max-months", int))
        if r.get("dry_run"):
            for m in r["months"]:
                print(f"  보관 예정 {m['dataset']} {m['start']}~{m['end']} {m['rows']:,}행")
            print(f"{len(r['months'])}개월(--dry-run — 아무것도 바꾸지 않음)")
            return True
        for m in r.get("months", []):
            print(f"  {m['dataset']} {m['start'][:7]} — {sum(m['rows'].values()):,}행 → {m['file']} "
                  f"({m['bytes'] / 1048576:.1f}MB, 압축 {m['ratio']}배) · DB 정리 {sum(m['deleted'].values()):,}행")
        print(json.dumps({k: v for k, v in r.items() if k != "months"}, ensure_ascii=False))
        return True
    if cmd == "ilm-restore":
        from app.services import ilm
        if not args:
            print("사용법: python run.py ilm-restore <manifest.json>   (보관 폴더 안의 파일)")
            sys.exit(2)
        db.init_db()
        try:
            r = ilm.restore(args[0], actor="cli")
        except (ValueError, FileNotFoundError) as e:
            print("복원 거부:", e)
            sys.exit(2)
        print(json.dumps(r, ensure_ascii=False))
        return True
    if cmd == "ilm-verify":
        from app.services import ilm
        db.init_db()
        r = ilm.verify()
        print(f"보관 파일 {r['files']}개 — 정상 {r['ok']} · 문제 {len(r['bad'])}")
        for b in r["bad"]:
            print(f"  #{b['id']} {b['path']} — {b['problem']}")
        sys.exit(1 if r["bad"] else 0)
    return False


def main():
    setup_logging()
    if len(sys.argv) < 2:                        # 인자 없이 실행 = 사용법만(예전엔 곧바로 demo 가 돌아 실데이터를 덮었다)
        print(__doc__)
        print(USAGE)
        return
    cmd = sys.argv[1]
    if cmd == "demo":
        if _refuse_demo_on_real_db():
            sys.exit(2)
        _seed_demo()
        print("데모 데이터 준비 완료(오프라인) — uvicorn app.api:app → / (웹 화면)")
    elif cmd == "recompute":
        from app import db
        from app.services import signals
        from app.services.themes import classify_news
        db.init_db()
        print("뉴스 감성:", classify_news())
        signals.clear_cache()
        from app.services import news_home
        news_home.clear_cache()
        print("다시 계산 완료(수집·시세 데이터는 그대로) — API 서버가 떠 있으면 몇 분 안에 화면에 반영")
    elif cmd == "real":
        r = real_pipeline("manual")
        if not r.get("skipped"):
            print(f"실데이터 준비 완료({r.get('seconds')}초) — uvicorn app.api:app → / (웹 화면)  "
                  "(실시간: python run.py realtime · 매일 23:00 자동 실행: python scheduler.py)")
    elif cmd == "initdb":
        from app import db
        db.init_db()
        print("DB 초기화/마이그레이션 완료 (기존 데이터 유지)")
        if db.get_meta("market_source") == "krx":
            from app.collectors import marketgen
            n = marketgen.purge_demo_artifacts()
            if n["financials"] or n["news"] or n.get("price_bars"):
                print(f"실데이터 모드: 예전 데모 데이터 정리 — 표본 재무 {n['financials']}건 · 데모 뉴스 {n['news']}건 · "
                      f"실데이터 구간 앞 생성 시세 봉 {n.get('price_bars', 0)}건 "
                      "(실제 재무는 'python run.py fetch-dart', 뉴스는 'python run.py fetch-news')")
    elif cmd == "fetch-market":
        from app import db
        from app.collectors import marketgen
        db.init_db()
        m = marketgen.seed_market(real=True)
        print("KRX 시세·지수·유니버스(top-N):", m)
        _warn_banner(m)
        from app.services import quality
        g = quality.gate_and_alert(m)
        print(f"적재 검증: {g['level']} — {g['summary']}")
    elif cmd == "platform-load":
        from app import db
        from app.collectors import krx_history
        db.init_db()
        if not krx_history.enabled():
            print("KRX_API_KEY 가 없어 실데이터 적재를 할 수 없습니다(표본: python run.py demo).")
            sys.exit(2)
        years = _opt("--years", int)
        budget = _opt("--budget", int) or 5000
        print(f"분석 계층 적재 — 최근 {years or 'ANALYTICS_YEARS'}년, KRX 호출 예산 {budget}회(하루 한도 10,000)")
        r = krx_history.backfill(years=years, budget=budget)
        print(r)
        if r.get("remaining"):
            print(f"남은 거래일 {r['remaining']}일 — 같은 명령을 다시 실행하면 이어서 받습니다")
    elif cmd == "platform-history":
        from app import db
        db.init_db()
        print("10년 이력(시장조치/주요사항 공시·업종·다년 재무·금리) — 원천별 예산 안에서 이어받기")
        r = _market_data_daily(history=True)
        for k in r["ok"]:
            print("  ✓", k)
        for k, v in r["errors"].items():
            print("  ✗", k, "—", v)
    elif cmd in ("credit-train", "return-validate"):
        import json
        from app import db
        db.init_db()
        src = "synthetic" if "--synthetic" in sys.argv else "real"
        if cmd == "credit-train":
            from app.models import credit_risk as model
        else:
            from app.models import return_model as model
        try:
            r = model.run(src)
        except ValueError as e:                    # 데이터 부족 — 무엇을 더 받아야 하는지 알려 준다
            print("학습 불가:", e)
            sys.exit(2)
        m = r["metrics"]
        print(f"[{src}] run_id={r.get('run_id')}")
        print(json.dumps({k: m[k] for k in m if k not in ("coefficients", "gbm_importance")}, ensure_ascii=False,
                         indent=1, default=str)[:4000])
    elif cmd == "platform-status":
        from app import db
        from app.services import platform
        db.init_db()
        st = platform.status()
        print(f"최신 거래일 {st['latest_trade_date']} · 첫 적재일 {st['first_loaded_date']} · 종목 {st['securities']} · "
              f"추정 행 {st['est_rows']:,} · 크기 {st['bytes'] / 1e6:,.0f}MB · 스냅샷 {st['snapshot_rows']}종목")
        print("파티션:", ", ".join(f"{p['name'][-4:]}={p['est_rows']:,}" for p in st["partitions"] if p["est_rows"] > 0))
        print("최신일 커버리지:", st["latest_coverage"])
        for x in st["loads"]:
            print(f"  적재 {x['dataset']} {x['status']}: {x['n']}")
        for mg in st["migrations"]:
            print(f"  마이그레이션 V{mg['version']} {mg['description']}: {mg['state']}")
    elif cmd == "platform-refresh":
        from app.collectors import krx_history
        print(krx_history.refresh_snapshot(full_adj=True))      # 권리 이벤트 전체 다시 찾기 + 스냅샷
    elif cmd == "model-report":
        import json
        from app import db
        from app.models import common
        db.init_db()
        which = (sys.argv[2] if len(sys.argv) > 2 else "credit").lower()
        model = {"credit": "credit_risk", "return": "return_5d"}.get(which)
        if not model:
            print("사용법: python run.py model-report credit|return")
            sys.exit(2)
        print(json.dumps(common.report(model), ensure_ascii=False, indent=1, default=str)[:8000])
    elif cmd == "analytics-query":
        import json
        from app.services import platform
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        if not args:
            print("쿼리:", ", ".join(platform.queries()))
            sys.exit(0)
        params = dict(a.split("=", 1) for a in args[1:] if "=" in a)
        try:
            print(json.dumps(platform.run_query(args[0], params), ensure_ascii=False, indent=1, default=str)[:12000])
        except platform.QueryError as e:
            print("쿼리 오류:", e)
            sys.exit(2)
    elif cmd == "security-status":
        import json
        from app import db
        from app.security import events, pdp, rbac
        db.init_db()
        active = db.query("SELECT COUNT(*) AS n, COUNT(DISTINCT username) AS users FROM auth_session "
                          "WHERE revoked_at IS NULL AND expires_at > now()")[0]
        print("접근 정책:", pdp.info())
        print(f"활성 세션 {active['n']}개 · 사용자 {active['users']}명")
        print(json.dumps(events.digest(24, notify=False), ensure_ascii=False, indent=1, default=str)[:4000])
        ov = rbac.overview()
        print(f"역할 자격 {len(ov['roles'])}건 · 대기 중 요청 {len(ov['pending'])}건")
    elif cmd == "crypto-rotate":
        from app import crypto, db
        from app.services import audit
        db.init_db()
        if not crypto.enabled():
            print("DATA_ENCRYPTION_KEY 가 없어 암호화가 꺼져 있어요")
            sys.exit(2)
        r = crypto.rotate_all()
        audit.record("crypto.rotate", "cli", detail=r)
        for k, v in r.items():
            print(f"  {k}: 검사 {v['scanned']} · 다시 포장 {v['rewrapped']}")
        print("완료 — 모든 값이 현재 KEK 로 포장됐으면 DATA_ENCRYPTION_KEYS_OLD 에서 예전 키를 지워도 된다")
    elif cmd == "fds-eval":
        import json
        from pathlib import Path
        from app.models import fds_eval
        from app.services import fds
        r = fds_eval.evaluate(n=_opt("--n", int) or 20000)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        rows = "\n".join(f"| {k} | {v['caught']:.1%} | {v['blocked']:.1%} |" for k, v in r["by_kind"].items())
        rules = "\n".join(f"| {x.key} | +{x.points} | {x.desc} |" for x in fds.RULES.values())
        Path("docs/models/fds_eval.md").write_text(
            "# FDS(이상거래탐지) 합성 평가\n\n"
            f"`python run.py fds-eval` 로 생성 · 표본 {r['n']:,}건(사기 {r['fraud_share']:.0%}) · seed {r['seed']}\n\n"
            "| 지표 | 값 | 뜻 |\n|---|---|---|\n"
            f"| 사기 탐지율(차단+추가 인증) | {r['fraud_caught_rate']:.1%} | 재현율 |\n"
            f"| 사기 차단율 | {r['fraud_blocked_rate']:.1%} | 추가 인증 없이 바로 막음 |\n"
            f"| 정상 거래 차단율 | {r['normal_block_rate']:.2%} | 가장 중요한 오탐(고객 피해) |\n"
            f"| 정상 거래 추가 인증률 | {r['normal_step_up_rate']:.1%} | 마찰(불편) |\n"
            f"| 플래그 정밀도 | {r['flag_precision']:.1%} | 걸린 것 중 실제 사기 |\n\n"
            "## 유형별\n\n| 유형 | 탐지 | 차단 |\n|---|---|---|\n" + rows + "\n\n"
            "## 규칙\n\n| 키 | 점수 | 설명 |\n|---|---|---|\n" + rules + "\n\n"
            f"결정: {r['thresholds']['block']}점 이상 차단 · {r['thresholds']['step_up']}점 이상 추가 인증.\n\n"
            "**한계** — 합성 분포에서의 수치는 실제 성능이 아니다. 알려진 수법(계정 탈취 후 인출·대포통장 집금·연속 인출·"
            "충전 후 즉시 이체·자전거래)을 잡는지, 정상 경계 사례(중고거래 큰 송금·모바일 IP 변경)를 막지 않는지를 "
            "회귀 시험하는 용도다. 운영에서는 fds_decision 기록과 /admin/fds 요약으로 오탐을 검토해 임계값을 조정한다.\n",
            encoding="utf-8")
        print("docs/models/fds_eval.md 갱신")
    elif cmd == "data-docs":
        from app.services import datadocs
        for p in datadocs.write():
            print("작성:", p)
    elif cmd == "assurance-docs":
        from app.security import assessment
        for p in assessment.write_docs():
            print("작성:", p)
    elif cmd == "self-assess":
        import json
        from app import db
        from app.security import assessment
        if "--static" in sys.argv:
            r = assessment.evaluate("static")
            doc = assessment.compact(r)
        else:
            db.init_db()
            r = assessment.run(actor="cli")
            doc = r
        ov = r["overall"]
        print(f"금융보안 수준진단 — 평균 {ov['avg']} / 목표 {ov['target_avg']} · 목표 미달 {ov['below_target']}개 · "
              f"운영 점검 실패 {ov['probe_failures']}개 · CSF 2.0 {r['csf']['covered']}/{r['csf']['total']}")
        for d in r["domains"]:
            print(f"  {d['id']}. {d['name']}: {d['avg']} (목표 {d['target_avg']})")
        for g in r["gaps"][:7]:
            print(f"  갭 {g['id']} {g['name']}: {g['level']} → {g['target']} — {g['next']}")
        out = _opt("--json", str)
        if out:
            from pathlib import Path
            Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
            print("저장:", out)
    elif cmd == "integrity-baseline":
        from app import db
        from app.security import integrity
        db.init_db()
        only = None
        listed = _opt("--only-listed", str)
        if listed:                                           # 설치본이 가져온 파일만(배포 전부터 있던 낯선 파일은 계속 경보)
            from pathlib import Path
            only = {ln.strip().removeprefix("./") for ln in Path(listed).read_text(encoding="utf-8").splitlines() if ln.strip()}
        r = integrity.record_baseline(actor="deploy", release=_opt("--release", str) or "", only=only)
        print(f"파일 무결성 기준선 #{r['id']} — 파일 {r['files']}개 · 다이제스트 {r['digest'][:16]}…(감사 체인 기록)")
        if r["excluded"]:
            print(f"  ⚠ 설치본에 없던 파일 {len(r['excluded'])}개는 기준선에서 뺐어요(계속 '새 파일'로 경보): "
                  + ", ".join(r["excluded"][:10]))
    elif cmd == "integrity-check":
        from app import db
        from app.security import integrity
        db.init_db()
        r = integrity.check()
        if r["state"] == "no_baseline":
            print("기준선 없음 — 배포 직후 python run.py integrity-baseline")
            sys.exit(2)
        c = r.get("counts") or {}
        print(f"파일 무결성: {'정상' if r['state'] == 'ok' else '위반'} — 기준선 #{r.get('baseline_id')} · "
              f"변경 {c.get('changed', 0)} · 삭제 {c.get('missing', 0)} · 새 파일 {c.get('unexpected', 0)}")
        for k in ("changed", "missing", "unexpected"):
            for path in r.get(k) or ():
                print(f"  {k}: {path}")
        sys.exit(0 if r["state"] == "ok" else 1)
    elif cmd == "incident-drill":
        import os
        from pathlib import Path
        from app import db
        from app.security import drill
        url = _opt("--target-db", str) or os.getenv("DRILL_DATABASE_URL", "")
        if not url:
            print("훈련용 DB 주소가 필요해요: --target-db postgresql+psycopg://…/outofstock_drill (이름에 drill/test)")
            sys.exit(2)
        db.init_db()
        try:
            rep = drill.run_isolated(url, out_dir=Path(_opt("--out", str) or "docs/incidents/drills"))
        except drill.DrillError as e:
            print("거부:", e)
            sys.exit(2)
        for x in rep["results"]:
            print(f"  {'✓' if x['ok'] else '✗'} {x['title']}: 탐지 {x['detect_ms']}ms · 차단 {x['contain_ms']}ms · "
                  f"복구 {x['recover_ms']}ms")
        print(f"사고 대응 모의훈련 {rep['passed']}/{rep['scenarios']} 통과 — 보고서 {', '.join(rep.get('files') or [])}")
        sys.exit(0 if rep["ok"] else 1)
    elif cmd == "export-pseudonymized":
        from app import db
        from app.services import pseudonymize
        db.init_db()
        k = _opt("--k", int) or 5
        purpose = _opt("--purpose", str) or ""
        args = [a for a in sys.argv[2:] if not a.startswith("--") and a not in (str(k), purpose)]
        path = args[0] if args else "exports/pseudonymized.csv"
        try:
            r = pseudonymize.export(path, k=k, purpose=purpose)
        except ValueError as e:
            print("거부:", e)
            sys.exit(2)
        print(f"가명정보 {r['records_out']}건(입력 {r['records_in']} · 억제 {r['suppressed']}) · k={r['k']} · "
              f"최소 그룹 {r['min_group']} · 일반화 단계 {r['generalization_level']} → {r['path']}")
    elif cmd == "dq":
        from app import db
        from app.services import contracts
        db.init_db()
        r = contracts.run()
        print(f"데이터 품질 점수 {r['score']:.0%}")
        for ds, v in r["datasets"].items():
            print(f"  {ds}: {v['pass']}/{v['total']}")
        for f in r["failed"]:
            print(f"  ✗ {f['dataset']}.{f['check']} = {f['value']} (기준 {f['op']} {f['threshold']}, {f['severity']})")
    elif cmd == "ledger-close":
        import json
        from app import db
        from app.services import ledger
        db.init_db()
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        try:   # 날짜 없이: 마감 안 한 날을 어제까지 차례로 · 날짜: 그날(까지 빠진 날 포함) 마감, 이미 마감했으면 다시 계산해 비교
            rs = [ledger.close_day(args[0])] if args else ledger.close_pending()
        except ledger.CloseError as e:
            print("거부:", e)
            sys.exit(2)
        if not rs:
            print("마감할 날이 없습니다(어제까지 모두 마감됨)")
        for r in rs:
            print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:4000])
        sys.exit(0 if all(r.get("status") == "ok" and r.get("unchanged", True) for r in rs) else 1)
    elif cmd == "ledger-verify":
        from app import db
        from app.services import ledger
        db.init_db()
        r = ledger.verify_closes()
        tb = ledger.trial_balance()
        print(f"마감 {r['days']}일 · 깨진 날짜 {r['broken'] or '없음'} · 빠진 날짜 {r['gaps'] or '없음'} · "
              f"감사 체인 앵커 일치 {r['anchored']}일 · 시산표 합 {tb['total']}")
        sys.exit(0 if r["ok"] and tb["total"] == 0 else 1)
    elif cmd == "security-purge":
        from app import db
        from app.services import privacy
        db.init_db()
        print("보관 기간 파기:", privacy.retention_purge())
    elif cmd == "audit-verify":
        from app import db
        from app.services import audit
        db.init_db()
        r = audit.verify()
        print("감사 로그 체인:", "정상" if r["ok"] else f"깨짐 — audit_id {r['broken_at']} 부터", f"({r['rows']}행)")
        w = audit.verify_witnesses()
        print("외부 증인 대조:", "정상" if w["ok"] else f"불일치 {w['mismatches']}", f"({w['checked']}건)", w.get("note", ""))
        sys.exit(0 if r["ok"] and w["ok"] else 1)
    elif cmd == "admin-bootstrap":
        from app import db
        from app.security import rbac
        db.init_db()
        print("관리 역할 자격 부여:", rbac.bootstrap(), "건 — 활성은 TOTP 등록 후 POST /admin/elevate")
    elif cmd == "user-unlock":
        from app import db
        from app.services import audit, auth
        db.init_db()
        if len(sys.argv) < 3:
            print("사용법: python run.py user-unlock <아이디>")
            sys.exit(2)
        n = auth.unlock(sys.argv[2])
        audit.record("admin.unlock", "cli", target=sys.argv[2])
        print("잠금 해제:" if n else "없는 계정:", sys.argv[2])
    elif cmd == "quality":
        from app import db
        from app.services import quality
        db.init_db()
        g = quality.gate_market_load()
        print(f"국내 시세 적재 검증: {g['level']}")
        for c in g["checks"]:
            print(f"  [{'OK ' if c['ok'] else c['level'].upper()}] {c['name']}: {c['detail']}")
    elif cmd == "alert-test":
        from app import db
        from app.services import alerts
        db.init_db()
        ch = alerts.channels()
        print("설정된 알림 채널:", ", ".join(ch) if ch else "없음(.env 의 ALERT_WEBHOOK_URL·TELEGRAM_*·SMTP_* 중 하나 이상)")
        r = alerts.test_alert()
        print("보냄:", r["sent"] or "-", "| 실패:", r["failed"] or "-")
        if r["failed"]:
            sys.exit(1)
    elif cmd == "fetch-index":
        from app import db
        from app.collectors import indexes
        db.init_db()
        r = indexes.refresh(real=True)
        for code in ("KOSPI", "KOSDAQ"):
            x = r.get(code) or {}
            print(f"  {code}: {x.get('value', '-')}  출처={x.get('source')}  기준일={x.get('as_of', '-')}")
        if r.get("errors"):
            print("  실패한 소스:", r["errors"])
    elif cmd == "fetch-overseas":
        from app import db
        from app.collectors import overseas_market
        db.init_db()
        o = overseas_market.seed_overseas(real=True)
        print("해외(미국) 일봉·지수·환율:", o)
        _warn_banner(o)
    elif cmd == "fetch-latest":
        from app import db
        from app.collectors import realtime
        db.init_db()
        print("KIS 최근 봉(오늘 종가 포함):", realtime.refresh_latest_bars())
    elif cmd in ("realtime", "realtime-kr", "realtime-us", "realtime-sim"):
        from app import db
        db.init_db()
        _run_realtime({"realtime": "all", "realtime-kr": "kr", "realtime-us": "us",
                       "realtime-sim": "sim"}[cmd])
    elif cmd == "fetch-practice":
        from app import db
        from app.services import practice
        from app.collectors import marketgen
        db.init_db()
        force = "--force" in sys.argv[2:]
        for sc in practice.SCENARIOS:
            print(f"  {sc['name']}: {marketgen.seed_scenario_ohlc(sc, real=True, force=force)}건")
        if not force:
            print("  (이미 실데이터로 받은 구간은 건너뜀 — 다시 받으려면 python run.py fetch-practice --force)")
    elif cmd == "fetch-dart":
        from app import db
        from app.collectors import dart
        db.init_db()
        print("DART 재무:", dart.collect_financials())
    elif cmd == "fetch-news":
        from app import db
        from app.collectors import news
        from app.services.themes import classify_news
        db.init_db()
        nw = news.collect_news()
        print("뉴스:", nw)
        if nw.get("stopped"):
            _warn_banner({"warning": f"뉴스 수집 중단: {nw['stopped']}"})
        classify_news()
    elif cmd == "snapshot-export":
        from app import db
        from app.services import snapshot
        db.init_db()
        r = snapshot.export_snapshot(sys.argv[2] if len(sys.argv) > 2 else None)
        print(f"스냅샷 저장: {r['path']} ({r['bytes'] / 1e6:.1f}MB) — 계정·토큰·지갑·채팅·로그는 들어 있지 않음")
        for t, n in r["tables"].items():
            print(f"  {t}: {n:,}행")
        print("팀원 PC: python run.py initdb → python run.py snapshot-import <파일> --yes")
    elif cmd == "snapshot-import":
        from app import db
        from app.services import snapshot
        files = [a for a in sys.argv[2:] if not a.startswith("--")]
        if not files:
            print("사용법: python run.py snapshot-import <파일> [--yes]")
            sys.exit(2)
        if "--yes" not in sys.argv[2:]:
            ans = input("이 DB 의 시세·지수·재무·뉴스·해외 데이터를 스냅샷으로 바꿉니다"
                        "(계정·지갑·채팅·로그는 그대로). 계속할까요? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("취소했어요")
                return
        db.init_db()
        try:
            r = snapshot.import_snapshot(files[0])
        except snapshot.SnapshotError as e:
            print("스냅샷 들이기 실패(데이터는 그대로):", e)
            sys.exit(1)
        print(f"스냅샷 들이기 완료(만든 시각 {r['created_at']}):")
        for t, n in r["tables"].items():
            print(f"  {t}: {n:,}행")
    elif _internal_control(cmd):
        pass
    elif _feature_ops(cmd):
        pass
    elif _db_ops(cmd):
        pass
    else:
        print(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
