"""운영 관측성(observability) + 파이프라인 실행 이력(lineage).

배치가 언제 돌았는지, 소스별로 몇 건 수집했는지, 품질 검사는 통과했는지, 성공/실패했는지를
pipeline_run 에 남긴다. 운영에서 '데이터가 왜 안 도나', '언제부터 멈췄나'를 추적하는 근거가 된다.
  - start_run()/finish_run() : 배치 실행을 이력으로 감쌈
  - health()                 : 라이브니스 + 데이터 신선도 + 마지막 배치 상태
  - recent_runs()            : 최근 실행 이력
"""
import logging
from datetime import datetime
from app import clock
from app import db
from app.redact import redact

log = logging.getLogger(__name__)


def _now():
    return clock.iso_now()


def start_run():
    """배치 실행 시작 기록 → run_id 반환(RUNNING 상태로 삽입)."""
    with db.transaction() as conn:
        r = conn.execute(db.pipeline_run.insert().values(started_at=_now(), status="RUNNING"))
        return r.inserted_primary_key[0]


def finish_run(run_id, status="SUCCESS", counts=None, quality=None, note=None):
    """배치 종료 기록 — 소스별 건수·품질결과·상태를 채운다."""
    counts = counts or {}
    q_ok = None if quality is None else (1 if quality.get("ok") else 0)
    q_issues = None if quality is None else quality.get("total_issues")
    db.execute("""UPDATE pipeline_run SET finished_at=:f, status=:s,
                    price_rows=:pr, dart_rows=:dr, news_rows=:nr,
                    quality_ok=:qo, quality_issues=:qi, note=:note
                  WHERE run_id=:id""",
               {"f": _now(), "s": status,
                "pr": counts.get("prices"), "dr": counts.get("dart"), "nr": counts.get("news"),
                "qo": q_ok, "qi": q_issues, "note": redact(note)[:200] if note else note, "id": run_id})


def close_stale_runs(note="중단됨 — 실행 중이던 프로세스가 끝났거나(재시작·종료) 기록 없이 멈춤",
                     holding_real_lock=False):
    """끝나지 않은 채 남은 실행(RUNNING)을 FAILED 로 정리. 예전엔 업데이트·재시작으로 죽은 23:00 실행이
    영원히 RUNNING 으로 남았다. run.py real 잠금을 잡은 상태(holding_real_lock)거나 잠금이 비어 있을 때만 —
    다른 프로세스에서 실제로 도는 실행을 잘못 닫지 않게."""
    from sqlalchemy import text
    from app.redact import redact

    def close():
        with db.transaction() as conn:
            n = conn.execute(text("UPDATE pipeline_run SET status='FAILED', finished_at=:f, note=:n "
                                  "WHERE status='RUNNING'"), {"f": _now(), "n": redact(note)[:200]}).rowcount or 0
        if n:
            log.warning("끝나지 않은 배치 실행 %d건을 FAILED 로 정리", n)
        return n
    if holding_real_lock:
        return close()
    with db.advisory_lock("real") as got:
        return close() if got else 0


def last_run():
    from app.redact import redact_obj
    rows = db.query("SELECT run_id, started_at, finished_at, status, price_rows, dart_rows, "
                    "news_rows, quality_ok, quality_issues, note "
                    "FROM pipeline_run ORDER BY run_id DESC LIMIT 1")
    return redact_obj(rows[0]) if rows else None              # 예전에 저장된 메모에 키가 있어도 응답에선 가림


def recent_runs(limit=10):
    return db.query("SELECT run_id, started_at, finished_at, status, price_rows, dart_rows, "
                    "news_rows, quality_ok, quality_issues "
                    "FROM pipeline_run ORDER BY run_id DESC LIMIT :n", {"n": limit})


def health(max_age_days=4):
    """헬스체크 — DB 연결(readiness) + 데이터 신선도 + 마지막 배치 상태를 종합해 status 산정.
    ok / degraded(신선도·배치 문제) / error(DB 불가). max_age_days 기본 4(주말 휴장 감안)."""
    db_ok, latest, age = True, None, None
    try:
        row = db.query("SELECT MAX(trade_date) AS d FROM daily_price")
        latest = row[0]["d"] if row else None
        if latest:
            age = (clock.now() - datetime.strptime(latest, "%Y-%m-%d")).days
    except Exception as e:                          # noqa: BLE001 — 헬스체크는 예외를 삼키고 상태로 보고
        db_ok = False
        log.error("health db error: %s", e)

    lr = last_run() if db_ok else None
    news = news_status() if db_ok else None
    issues = []
    if not db_ok:
        status = "error"
        issues.append("db_unreachable")
    else:
        if age is None:
            issues.append("no_price_data")
        elif age > max_age_days:
            issues.append("stale_data")
        if lr and lr.get("status") == "FAILED":
            issues.append("last_pipeline_failed")
        elif lr and lr.get("status") == "PARTIAL":
            issues.append("last_pipeline_partial")           # 일부 단계만 실패(어느 단계인지는 last_run.note)
        if news and (news.get("last_run") or {}).get("stopped"):
            issues.append("news_collection_stopped")     # 차단·연속 실패로 뉴스 수집이 중간에 멈춤
        idx = index_status()
        issues += idx.pop("issues")
        gates = _gates()
        if any(g.get("level") == "fail" for g in gates.values()):
            issues.append("quality_gate_failed")     # 적재 직후 검증(종목 수·이력·기준일·출처) 실패 — 2026-10-10 장애 재발 방지
        dr = dr_status()
        issues += dr["issues"]                       # 백업 실패·오래됨, 복구 훈련 실패·기한 초과(관리자 화면에만)
        feats = feature_status()
        issues += feats.pop("issues")
        status = "degraded" if issues else "ok"      # 신선도/배치 신호를 status 에 반영

    from app.redact import redact_obj
    return redact_obj({"status": status, "db_ok": db_ok, "issues": issues,          # 공개 응답 — 비밀값 가림
                       "latest_price_date": latest, "data_age_days": age, "max_age_days": max_age_days,
                       "last_run": lr, "news": news, "indices": idx if db_ok else None,
                       "reco": reco_status() if db_ok else None,
                       "quality_gates": gates if db_ok else None,
                       "dr": dr if db_ok else None,
                       "features": feats if db_ok else None,
                       "alerts": _recent_alerts() if db_ok else None, "checked_at": _now()})


def feature_status():
    """화면 기능의 운영 상태(2026-11-09) — 양봉이 주간 추천 배치 · 모의투자 시즌 데이터·정산.
    issues: vmarket_next_season_missing(시즌 데이터가 있는데 다음 주 파일이 없음 — 월요일 09:00 에 참가 불가) ·
            vmarket_unsettled(끝난 시즌의 열린 참가 — 00:05 정산 작업 실패 의심)."""
    out = {"issues": []}
    try:
        from app.services import vmarket, yangbong
        out["yangbong"] = yangbong.status()
        vm = vmarket.status()
        out["vmarket"] = vm
        if vm["seasons"] and not vm["next_ready"]:                # 시즌 데이터를 쓰고 있는데 다음 주 파일이 없음
            out["issues"].append("vmarket_next_season_missing")
        if vm["unsettled"] and (vm["clock"]["wd"] != 5 or vm["clock"]["phase"] != "weekend" or clock.now().hour >= 1):
            out["issues"].append("vmarket_unsettled")          # 토요일 00:05 정산 전(00시대)은 정상 대기
    except Exception as e:                                      # noqa: BLE001 — 헬스체크는 예외를 상태로
        log.warning("feature status: %s", e)
        out["error"] = type(e).__name__
    return out


DR_ISSUES = ("backup_failed", "backup_stale", "backup_unencrypted", "dr_drill_failed", "dr_drill_overdue")


def dr_status():
    """백업·복구 훈련 상태 — 마지막 성공 백업의 경과 시간(= 지금 사고가 나면 잃을 최대 데이터, RPO 실측)과
    마지막 복구 훈련 결과(tools/dr.py 가 app_meta 에 남긴다). 기록이 아예 없으면(개발 환경) 경고하지 않는다."""
    import json
    from datetime import timezone
    from app.config import settings

    def meta(k):
        try:
            v = db.get_meta(k)
            return json.loads(v) if v else None
        except (ValueError, TypeError):
            return None
    b, d, issues = meta("backup_last"), meta("dr_drill_last"), []
    now = datetime.now(timezone.utc)

    def age(ts, unit):
        try:
            sec = (now - datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError):
            return None
        return round(sec / unit, 1)
    out = {"backup": b, "drill": d, "backup_age_hours": None, "drill_age_days": None,
           "backup_max_age_hours": settings.BACKUP_MAX_AGE_HOURS, "drill_max_age_days": settings.DR_DRILL_MAX_AGE_DAYS,
           "rto_target_min": settings.DR_RTO_TARGET_MIN}
    if b:
        out["backup_age_hours"] = age(b.get("last_ok_at"), 3600)
        if b.get("status") == "fail":
            issues.append("backup_failed")
        if out["backup_age_hours"] is None or out["backup_age_hours"] > settings.BACKUP_MAX_AGE_HOURS:
            issues.append("backup_stale")
        if b.get("status") == "ok" and not b.get("encrypted"):
            issues.append("backup_unencrypted")
    if d:
        out["drill_age_days"] = age(d.get("at"), 86400)
        if not d.get("ok"):
            issues.append("dr_drill_failed")
        if out["drill_age_days"] is None or out["drill_age_days"] > settings.DR_DRILL_MAX_AGE_DAYS:
            issues.append("dr_drill_overdue")
        rto = d.get("rto_seconds")                  # 실측 복구 시간 > 목표(감독규정 제23조 핵심업무 3시간)면 훈련 성공이어도 미달
        if isinstance(rto, (int, float)) and rto > settings.DR_RTO_TARGET_MIN * 60:
            issues.append("dr_rto_exceeded")
    elif b:
        issues.append("dr_drill_overdue")          # 백업은 하는데 복구해 본 적이 없다 — 가장 흔한 DR 실패
    out["issues"] = issues
    return out


def _gates():
    try:
        from app.services import quality
        return quality.gates()
    except Exception:                                # noqa: BLE001
        return {}


def _recent_alerts(limit=5):
    """최근 알림 제목·수준·시각만(본문은 공개 응답에 넣지 않는다)."""
    try:
        from app.services import alerts
        return {"channels": alerts.channels(),
                "recent": [{k: a.get(k) for k in ("level", "title", "created_at", "delivered")}
                           for a in alerts.recent(limit)]}
    except Exception:                                # noqa: BLE001
        return None


def index_status():
    """코스피·코스닥 지수의 출처·기준일·오래됨 + 문제 신호(데이터 계보 관측성).
      index_sample      실데이터 모드인데 지수가 표본(상수)
      index_stale       기준일이 4일 넘게 지남(수집 중단)
      krx_index_denied  KRX 지수 API 가 401/403 — 이용신청 필요(다른 소스로 대체 중)"""
    import json
    from app.collectors import indexes
    rows = {r["code"]: r for r in db.query(
        "SELECT code, value, source, as_of, ts FROM market_index WHERE code IN ('KOSPI','KOSDAQ')")}
    real = db.get_meta("market_source") == "krx"
    issues = []
    for code in ("KOSPI", "KOSDAQ"):
        r = rows.get(code)
        if real and (not r or r.get("source") in (None, "gen")):
            issues.append("index_sample")
            break
    if any(r.get("source") not in (None, "gen") and indexes.is_stale(r.get("as_of")) for r in rows.values()):
        issues.append("index_stale")
    last = None
    try:
        last = json.loads(db.get_meta("kr_index_status") or "null")
    except ValueError:
        pass
    errs = json.dumps((last or {}).get("errors") or {}, ensure_ascii=False)
    if "KrxAuth" in errs or " 401" in errs or " 403" in errs:
        issues.append("krx_index_denied")
    return {"items": [{"code": c, **({k: rows[c][k] for k in ("value", "source", "as_of", "ts")} if c in rows else {})}
                      for c in ("KOSPI", "KOSDAQ")],
            "last_refresh": (last or {}).get("at"), "errors": (last or {}).get("errors"), "issues": issues}


def reco_status():
    """추천 스냅샷(감사 추적) — 마지막으로 기록된 기준일·모델 버전."""
    try:
        r = db.query("SELECT as_of, model_version, MAX(created_at) AS at FROM reco_snapshot "
                     "GROUP BY as_of, model_version ORDER BY as_of DESC LIMIT 1")
    except Exception:                                # noqa: BLE001
        return None
    return r[0] if r else None


def news_status():
    """마지막 뉴스 수집 결과(collect_news 가 남김) — 받은 종목 수·실패·중단 이유·시각."""
    import json
    raw = db.get_meta("news_last_run")
    try:
        last = json.loads(raw) if raw else None
    except ValueError:
        last = None
    return {"collected_at": db.get_meta("news_collected_at"), "last_run": last}


def public_health(max_age_days=4):
    """공개 헬스체크 — 상태 판단에 필요한 최소 항목만(내부 오류 문구·알림·세부 출처·백업 상태는 제외)."""
    h = health(max_age_days)
    lr = h.get("last_run") or {}
    issues = [i for i in h["issues"] if i not in DR_ISSUES]          # 백업·복구 상태는 공격자에게 줄 정보가 아니다
    status = h["status"] if h["status"] == "error" else ("degraded" if issues else "ok")
    return {"status": status, "db_ok": h["db_ok"], "issues": issues,
            "latest_price_date": h.get("latest_price_date"), "data_age_days": h.get("data_age_days"),
            "last_run": {"status": lr.get("status"), "finished_at": lr.get("finished_at")} if lr else None,
            "checked_at": h.get("checked_at")}
