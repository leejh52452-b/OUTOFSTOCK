"""배포용 스케줄러 — 정해진 시각에 데이터를 자동 갱신(한국시간 기준, 서버 시간대와 무관).

  매일  23:00  python run.py real 전체 파이프라인                                   [REAL_PIPELINE_TIME]
               (KRX 시세·지수 + KIS 오늘 종가 + 실습 구간 + DART 재무 + 뉴스 + 해외 일봉·지수·환율)
  평일  08:40  국내 KRX 일봉·지수(전 영업일 확정치 — KRX 는 D+1 아침에 제공)          [KRX_API_KEY]
  평일  15:45  국내 KIS 최근 봉 → '오늘 종가' 즉시 반영 + 코스피·코스닥 종가 + 추천 Top5 확정 스냅샷  [KIS 키]
  화~토 07:30  해외(미국) 일봉·지수·환율 확정치(미국 장 마감 = 한국 05:00~06:00 이후)   [키 불필요]
  매 1분      가용성 감시(합성 모니터링 → 장애 자동 판정·보고 대상 표시)               [UPTIME_URL]
  매 5분      관제 — 확인 안 된 오류 경보 상향(15분) · 사고 최초 보고 기한 알림
  매일 09:10  긴급 변경 사후 검토 기한(3일) 초과 알림
  매일 04:50  DB 크기 기록·진단(증가 추세·디스크 소진 예측·죽은 행·인덱스·XID — 기준 넘으면 경보)   [ADR-0027]
  매월 2일 04:10  데이터 수명주기 — 보관 기한 지난 달을 압축 보관·검증 → DB 정리 + 보관 파일 재해시  [ILM_ENABLED]
  매일 08:55·23:55  양봉이 주간 추천 — 데이터가 들어온 끝난 주의 5종목 선정 + 30일 지난 바구니 채점(따라잡기·멱등)
               (금요일 확정치는 KRX D+1 — 토요일 23:00 파이프라인 또는 월요일 08:40 적재 뒤, 월요일 장 시작 전에 나온다)
  매일 00:05  모의투자 정산 — 끝난 시즌(토요일 00:00 이후)의 열린 참가를 마지막 종가로 정산 → 지갑(멱등)
  금    18:00  모의투자 다음 시즌 데이터 확인 — 없으면 경보(월요일 09:00 에 참가가 막히지 않게)
  뉴스는 23:00 전체 파이프라인에서만(하루 1번) — 구글 RSS 요청을 늘리지 않기 위해 별도 작업을 두지 않는다.
안전장치
  · 23:00 작업은 advisory lock 으로 보호 — 수동 'python run.py real' 과 겹치면 늦게 온 쪽이 건너뜀.
  · misfire_grace_time 1시간 + coalesce — 잠깐 멈췄다 돌아오면 한 번만 실행.
  · 스케줄러가 꺼져 있어 23:00 을 놓쳤으면(마지막 성공 26시간 초과) 켜질 때 1분 뒤 1회 보충(REAL_PIPELINE_CATCHUP).
실행:  python scheduler.py   (tmux 창 하나 / systemd: deploy/outofstock-scheduler.service)
실시간 체결가는 별도 프로세스:  python run.py realtime
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import settings
from app.logging_conf import setup_logging

log = logging.getLogger(__name__)
KST_NAME = "Asia/Seoul"
CATCHUP_AFTER = timedelta(hours=26)


def _safe(name, fn, quiet=False):
    """작업 하나가 실패해도 스케줄러 전체가 죽지 않게. (작업 이름 = 로그·다음 실행 표시에 쓰는 한글 이름)
    quiet: 자주 도는 작업(아웃박스 30초) — 한 일이 없으면 로그를 남기지 않는다."""
    def run():
        try:
            if not quiet:
                log.info("[%s] 시작", name)
            res = fn()
            if not quiet or (isinstance(res, dict) and any(res.values())):
                log.info("[%s] 완료: %s", name, res)
        except Exception as e:                           # noqa: BLE001
            log.exception("[%s] 실패: %s", name, e)
            try:                                         # 예약 작업 실패를 사람이 알게(같은 작업은 6시간에 1번)
                from app.services import alerts
                alerts.notify(f"job:{name}", f"예약 작업 실패: {name}", f"{type(e).__name__}: {e}", level="error")
            except Exception:                            # noqa: BLE001 — 알림 실패가 스케줄러를 죽이지 않게
                pass
    run.__name__ = run.__qualname__ = name
    return run


def job_real():
    from run import real_pipeline                        # 'python run.py real' 과 같은 함수
    return real_pipeline(trigger="scheduler")


def job_krx():
    from app.collectors import krx, marketgen
    if not krx.enabled():
        return "KRX_API_KEY 없음 — 건너뜀"
    from app.services import quality
    m = marketgen.seed_market(real=True)
    g = quality.gate_and_alert(m)                         # 적재 직후 검증 — 실패면 알림
    return {**m, "quality": g["level"], "quality_summary": g["summary"]}


def job_kis_close():
    """장 마감 직후: KIS 오늘 종가 → 코스피·코스닥 종가 → 오늘의 추천 Top5 확정 스냅샷(감사 기록).
    세 단계는 서로 독립 — 예전엔 KIS 토큰 발급이 실패하면 지수·추천 스냅샷까지 건너뛰었다."""
    from app.collectors import realtime, indexes
    try:
        bars = realtime.refresh_latest_bars() if realtime.enabled() else "KIS 키 없음 — 건너뜀"
    except Exception as e:                                # noqa: BLE001
        log.warning("KIS 오늘 종가 갱신 실패(지수·추천은 계속): %s", e)
        bars = f"실패: {str(e)[:120]}"
    try:
        idx = indexes.refresh(real=True, sources=("kis", "yahoo", "krx"), history=False)
        idx = {c: (idx.get(c) or {}).get("source") for c in ("KOSPI", "KOSDAQ")}
    except Exception as e:                                # noqa: BLE001
        idx = f"지수 갱신 실패: {e}"
    from run import _publish_reco
    try:                                               # 오늘 종가가 들어왔으니 권리 이벤트(액면분할)를 모의투자 보유 수량에 반영
        from app.services import invest
        ca = invest.apply_corporate_actions()
    except Exception as e:                                # noqa: BLE001
        ca = f"권리 반영 실패: {str(e)[:120]}"
    return {"bars": bars, "indices": idx, "reco": _publish_reco(), "corporate_actions": ca}


def job_security_daily():
    """보안 일일 작업 — 보관 기간 파기(접속 기록·보안 이벤트·세션·알림·감사) + 일일 보안 요약(심각 이벤트·역할 재검토 알림)
    + 감사 체인 외부 증인. 단계마다 따로 실행한다 — 예전엔 파기 한 단계가 예외를 내면 요약·외부 증인까지 그날 통째로 빠졌다
    (결함 30: 무결성의 기준점인 증인이 무관한 정리 작업 실패에 묶여 있었다). 실패가 있으면 모두 돌린 뒤 한 번에 알린다."""
    from app.security import events, rbac
    from app.services import audit, privacy
    out, failed = {}, []
    for name, fn in (("purge", privacy.retention_purge), ("dormant_roles", rbac.sweep_dormant),
                     ("digest", lambda: events.digest(24)), ("witness", lambda: audit.witness("daily"))):
        try:
            out[name] = fn()
        except Exception as e:                            # noqa: BLE001 — 다음 단계는 계속
            log.exception("보안 일일 작업 %s 실패", name)
            out[name] = f"실패: {type(e).__name__}"
            failed.append(name)
    if failed:
        raise RuntimeError(f"보안 일일 작업 일부 실패 {failed} — 나머지는 완료: {out}")
    return out


def job_integrity():
    """파일 무결성(10분) — 배포 기준선 대비 바뀐·사라진·새 파일. 새 위반이면 보안 이벤트 → 경보(같은 위반은 한 번)."""
    from app.security import integrity
    r = integrity.check()
    return {} if r["state"] in ("ok", "no_baseline") else {"state": r["state"], **(r.get("counts") or {})}


def job_assessment():
    """금융보안 수준진단(매월 1일) — 근거 확인 + 운영 점검, 결과는 추가만 저장. 직전보다 내려간 항목·실패한 점검은 경보."""
    from app.security import assessment
    r = assessment.run(actor="system")
    return {"id": r["id"], "overall": r["overall"]["avg"], "regressions": len(r["regressions"]),
            "probe_failures": len(r["probe_failures"])}


def job_uptime():
    """가용성 감시(1분) — 헬스 확인 → 기록 → 장애 판정(10분 실패·지연이면 사건 · 30분이면 보고 대상 · 정상 3번이면 닫기)."""
    from app.services import incidents
    return incidents.run_check()


def job_watch():
    """관제(5분) — 15분 넘게 아무도 확인하지 않은 오류 경보 상향 · 사고 최초 보고 기한 임박·초과 알림. 단계마다 따로."""
    from app.services import alerts, incidents
    out = {}
    for name, fn in (("escalate", alerts.escalate_unacked), ("deadlines", incidents.remind_deadlines)):
        try:
            r = fn()
            if r:
                out[name] = r
        except Exception as e:                            # noqa: BLE001 — 한 단계 실패가 다른 단계를 막지 않게
            log.exception("관제 작업 %s 실패", name)
            out[name] = f"실패: {type(e).__name__}"
    return out


def job_change_reviews():
    """긴급 변경 사후 검토 기한(3일) 초과 알림 — 매일 09:10."""
    from app.services import changes
    return changes.remind_reviews()


def job_db_daily():
    """DB 크기 기록·진단(매일 04:50 — 04:30 백업 뒤) — 하루 1행 크기 기록, 디스크 소진 예측·죽은 행·겹치는 인덱스·XID 경보."""
    from app.services import dbhealth
    return dbhealth.snapshot()


def job_ilm():
    """데이터 수명주기(매월 2일 04:10) — 보관 기한 지난 달 → 압축 보관·검증 → DB 정리, 이어서 보관 파일 전부 재해시.
    파일이 없어졌거나 바뀌었으면 실패로 올려 경보(_safe)."""
    from app.services import ilm
    if not settings.ILM_ENABLED:
        return {"skipped": "ILM_ENABLED=0"}
    r = ilm.archive()
    v = ilm.verify()
    if v["bad"]:
        raise RuntimeError(f"보관 파일 이상 {len(v['bad'])}건: {v['bad'][:3]}")
    if r.get("offsite_failed"):                          # 외부 복사 실패한 달은 DB 에 남겨 두었다 — 사람이 알게
        raise RuntimeError(f"보관 파일 외부 복사 실패 {r['offsite_failed']} — 그 달은 DB 에서 지우지 않았다")
    return {"archived_months": len(r.get("months", [])), "rows_deleted": r.get("rows_deleted", 0),
            "archive_files": v["files"]}


def job_yangbong():
    """양봉이 주간 추천 — 끝난 주 선정 + 30일 지난 바구니 채점(app/services/yangbong.py, 팀 알고리즘 그대로)."""
    from app.services import yangbong
    r = yangbong.run()
    return {"picked": [p["signal_date"] for p in r.get("picked", [])], "scored": len(r.get("scored", [])),
            "skipped": r.get("skipped", [])[:3]}


def job_vmarket_settle():
    """모의투자 — 끝난 시즌 정산(남은 주식 마지막 종가 매도 → 지갑). 실패는 vmarket.settle_all 이 경보."""
    from app.services import vmarket
    return vmarket.settle_all()


def job_vmarket_check():
    """모의투자 — 다음 시즌 데이터가 있는지(없으면 경보)."""
    from app.services import vmarket
    st = vmarket.check_upcoming()
    return {"season": st["season"], "next_ready": st["next_ready"], "weeks_left": st["weeks_left"]}


def job_data_quality():
    """데이터 계약 측정(매일) — 품질 차원별 값·통과 여부를 쌓고 핵심 계약 위반은 알림."""
    from app.services import contracts
    r = contracts.run()
    return {"score": r["score"], "failed": [f"{x['dataset']}.{x['check']}" for x in r["failed"]]}


def job_ledger_close():
    """원장 일 마감(KST) — 마감하지 않은 날을 어제까지 날짜순으로 차례로(서버가 꺼져 있던 날도 메운다).
    분개 균형·시산표·잔액 대사·고아 계좌·가계정 0 확인, 실패하면 알림(ledger.close_pending)."""
    from app.services import ledger
    return [f"{r['date']} {r['status']}" for r in ledger.close_pending()]


def job_outbox():
    """아웃박스 중계 — 커밋된 사건을 처리기로(실패는 백오프 재시도, 8회 실패 dead)."""
    from app.services import outbox
    return outbox.dispatch()


def job_overseas():
    from app.collectors import overseas_market
    return overseas_market.seed_overseas(real=True)


def parse_hhmm(value, default=(23, 0)):
    """'23:00' → (23, 0). 형식이 틀리면 기본값(경고 로그)."""
    try:
        h, m = (int(x) for x in str(value).strip().split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    log.warning("REAL_PIPELINE_TIME=%r 형식 오류 → 기본 %02d:%02d 사용", value, *default)
    return default


_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _allowed_days(spec):
    """'mon-sun' · 'mon-fri' · 'mon,wed,fri' → {0..6}. 모르는 형식이면 매일."""
    days = set()
    for part in str(spec or "mon-sun").lower().replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a in _DOW and b in _DOW:
                i, j = _DOW[a], _DOW[b]
                days |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
        elif part in _DOW:
            days.add(_DOW[part])
    return days or set(range(7))


def last_scheduled_real(now=None):
    """지금 이전의 가장 최근 정기 실행 시각(KST) — REAL_PIPELINE_TIME·REAL_PIPELINE_DAYS 기준."""
    from app.services import quotes
    now = now or quotes.now_kst()
    h, m = parse_hhmm(settings.REAL_PIPELINE_TIME)
    days = _allowed_days(settings.REAL_PIPELINE_DAYS)
    for back in range(0, 8):
        d = (now - timedelta(days=back)).date()
        cand = now.replace(year=d.year, month=d.month, day=d.day, hour=h, minute=m, second=0, microsecond=0)
        if cand <= now and cand.weekday() in days:
            return cand
    return None


def needs_catchup(now=None):
    """보충 실행이 필요한가 — 마지막 성공이 없거나, '가장 최근 정기 실행 시각'보다 이전이면(그 실행을 놓쳤거나
    도중에 재시작돼 죽었으면) True. 예전 기준(마지막 성공 후 26시간)은 23:10 에 재시작해 23:00 실행이 죽어도
    보충하지 않아 다음 날 23:00 까지(약 48시간) 갱신이 비었다."""
    from app import db
    from app.services import quotes
    last = quotes.parse_ts(db.get_meta("pipeline_real_finished"))
    now = now or quotes.now_kst()
    if last is None:
        return True
    due = last_scheduled_real(now)
    if due is None:                            # (요일 설정이 비정상일 때만) 예전 기준
        return (now - last) > CATCHUP_AFTER
    return last < due


def build_scheduler():
    s = BlockingScheduler(timezone=KST_NAME)
    common = {"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1}
    h, m = parse_hhmm(settings.REAL_PIPELINE_TIME)
    s.add_job(_safe("run.py real 전체 파이프라인", job_real), "cron",
              day_of_week=settings.REAL_PIPELINE_DAYS or "mon-sun", hour=h, minute=m, id="real", **common)
    s.add_job(_safe("KRX 일봉", job_krx), "cron", day_of_week="mon-fri", hour=8, minute=40, id="krx", **common)
    s.add_job(_safe("KIS 오늘 종가·지수·추천 확정", job_kis_close), "cron", day_of_week="mon-fri", hour=15, minute=45,
              id="kis", **common)
    s.add_job(_safe("해외 일봉", job_overseas), "cron", day_of_week="tue-sat", hour=7, minute=30, id="us", **common)
    s.add_job(_safe("보안 일일 작업(파기·요약)", job_security_daily), "cron", hour=6, minute=10, id="security",
              **common)
    s.add_job(_safe("원장 일 마감(전날)", job_ledger_close), "cron", hour=0, minute=20, id="ledger", **common)
    s.add_job(_safe("아웃박스 중계", job_outbox, quiet=True), "interval", seconds=30, id="outbox", **common)
    s.add_job(_safe("데이터 계약 측정", job_data_quality), "cron", hour=7, minute=50, id="dq", **common)
    s.add_job(_safe("파일 무결성 검사", job_integrity, quiet=True), "interval", minutes=10, id="integrity", **common)
    s.add_job(_safe("금융보안 수준진단(월간)", job_assessment), "cron", day=1, hour=6, minute=40, id="assessment", **common)
    s.add_job(_safe("가용성 감시", job_uptime, quiet=True), "interval", minutes=1, id="uptime", **common)
    s.add_job(_safe("관제(경보 상향·보고 기한)", job_watch, quiet=True), "interval", minutes=5, id="watch", **common)
    s.add_job(_safe("긴급 변경 사후 검토 기한", job_change_reviews), "cron", hour=9, minute=10, id="change_reviews", **common)
    s.add_job(_safe("DB 크기 기록·진단", job_db_daily), "cron", hour=4, minute=50, id="db_daily", **common)
    s.add_job(_safe("데이터 수명주기(보관·정리)", job_ilm), "cron", day=2, hour=4, minute=10, id="ilm", **common)
    s.add_job(_safe("양봉이 주간 추천(선정·채점)", job_yangbong), "cron", hour="8,23", minute=55, id="yangbong", **common)
    s.add_job(_safe("모의투자 시즌 정산", job_vmarket_settle), "cron", hour=0, minute=5, id="vmarket", **common)
    s.add_job(_safe("모의투자 다음 시즌 데이터 확인", job_vmarket_check), "cron", day_of_week="fri", hour=18, minute=0,
              id="vmarket_check", **common)
    return s                                   # 뉴스는 'real'(23:00) 안에서만 — 하루 1번


def describe(s, now=None):
    """등록된 작업과 다음 실행 시각(KST) — 시작 로그용."""
    from app.services import quotes
    now = now or quotes.now_kst()
    out = []
    for j in s.get_jobs():
        nxt = j.trigger.get_next_fire_time(None, now)
        out.append((j.id, j.name, nxt.strftime("%m/%d(%a) %H:%M") if nxt else "-"))
    return out


def main():
    setup_logging()
    from app import db
    db.init_db()
    from app.services.monitoring import close_stale_runs
    close_stale_runs()                         # 재시작으로 죽은 실행(RUNNING) 정리 — 다른 real 이 돌고 있으면 건드리지 않음
    s = build_scheduler()
    for jid, name, nxt in describe(s):
        log.info("  · %-6s 다음 실행 %s  %s", jid, nxt, name)
    if settings.REAL_PIPELINE_CATCHUP and needs_catchup():
        when = datetime.now(s.timezone) + timedelta(minutes=1)
        s.add_job(_safe("run.py real 보충 실행(놓친 정기 실행)", job_real), "date", run_date=when,
                  id="real-catchup", misfire_grace_time=3600)
        log.info("가장 최근 정기 실행(%s)이 성공 기록보다 뒤 — 놓쳤거나 도중에 끊김 → 1분 뒤 1회 보충 실행",
                 last_scheduled_real())
    # 꺼져 있던 동안 끝난 시즌이 있으면 1분 뒤 정산(멱등 — 없으면 아무 일도 안 함)
    s.add_job(_safe("모의투자 시즌 정산(시작 보충)", job_vmarket_settle), "date",
              run_date=datetime.now(s.timezone) + timedelta(minutes=1), id="vmarket-catchup", misfire_grace_time=3600)
    log.info("스케줄러 시작 (한국시간 기준, Ctrl+C 종료)")
    try:
        s.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("스케줄러 종료")


if __name__ == "__main__":
    main()
