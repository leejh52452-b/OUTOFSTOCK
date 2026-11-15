"""① 뉴스 수집기 — 종목별 뉴스(Google News RSS) → news + news_stock(종목 태깅).
파싱/저장 로직은 테스트로 검증. 실제 RSS 요청은 사용자 머신에서 동작.

저장 필드: title(언론사 꼬리 ' - 매체명' 제거) · url · source · published_at(YYYY-MM-DD) ·
          published_ts(발행 시각 ISO, 한국시간) — 뉴스 화면의 'N분 전'·오늘 기준 집계에 쓴다.
본문은 저장하지 않는다(제목·링크·시각·언론사만).

수집 주기: 하루 한 번(run.py real, 매일 23:00) — 종목당 요청 1번, 하루 약 150번.
  구글 뉴스 RSS 는 robots.txt 가 자동 수집을 허용하지 않는 경로라, 요청을 적게·천천히 보내고
  막히면(429/503/403·확인 페이지) 그 회차를 바로 멈춘다(재시도하면 차단만 길어진다)."""
import json
import logging
import time
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from sqlalchemy import text

from app.config import settings
from app.retry import retry
from app.services import catalog
from app import db

log = logging.getLogger(__name__)

def _xml(data):
    """외부 XML 파싱 — defusedxml 이 있으면 그것으로(엔티티 확장·외부 엔티티 공격 차단, bandit B314).
    없으면 표준 라이브러리(파이썬 3.7.1+ 의 expat 은 '억만 개의 웃음' 확장 공격을 막는다)."""
    try:
        from defusedxml.ElementTree import fromstring
    except ImportError:
        from xml.etree.ElementTree import fromstring       # nosec B405
    # 아래 nosec: defusedxml 이 없을 때만 표준 파서를 쓴다(위 설명)
    return fromstring(data)  # nosec B314

KST = timezone(timedelta(hours=9))


class Blocked(Exception):
    """요청이 막혔다(429/503/403 또는 구글 확인 페이지) — 이번 회차를 멈추는 신호."""


# 연결 끊김·시간 초과만 짧게 한 번 더. 차단(Blocked)·그 밖의 HTTP 오류는 재시도하지 않는다.
@retry(times=2, delay=2.0, exceptions=(requests.ConnectionError, requests.Timeout))
def _fetch_rss(query):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code in (403, 429, 503) or "/sorry/" in (r.url or ""):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.text


def _parse_pub(pub):
    """RSS pubDate(RFC822 'Mon, 08 Sep 2026 …') → aware datetime(KST). 실패 시 None."""
    if not pub:
        return None
    try:
        dt = parsedate_to_datetime(pub)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


def clean_title(title, source=""):
    """구글 뉴스 제목 끝의 ' - 매체명' 꼬리를 뗀다(출처는 따로 저장 — 중복 표시·키워드 오인 방지)."""
    t = (title or "").strip()
    src = (source or "").strip()
    if src and t.endswith(" - " + src) and len(t) > len(src) + 3:
        t = t[: -(len(src) + 3)].rstrip()
    return t


def parse_rss(xml_text, limit=5):
    """RSS XML → [{title, url, source, published_at, published_ts}] (최대 limit개)."""
    root = _xml(xml_text)
    items = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        dt = _parse_pub(pub_raw)
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        title = clean_title(item.findtext("title") or "", source)
        if title and link:
            items.append({"title": title, "url": link, "source": source,
                          "published_at": dt.date().isoformat() if dt else pub_raw[:10],
                          "published_ts": dt.isoformat(timespec="seconds") if dt else None})
        if len(items) >= limit:
            break
    return items


def _store(items, ticker):
    """뉴스 저장(url 중복 방지 — 예전에 시각 없이 저장된 기사는 시각만 채움) + 종목 태깅.
    이미 시각이 있는 기사는 다시 쓰지 않는다(매일 같은 기사를 받아도 행을 새로 만들지 않음)."""
    with db.transaction() as conn:
        for it in items:
            conn.execute(text(
                "INSERT INTO news(title, url, source, published_at, published_ts) "
                "VALUES(:t, :u, :s, :p, :ts) "
                f"{db.NEWS_URL_CONFLICT} DO UPDATE SET published_ts = EXCLUDED.published_ts "
                "WHERE news.published_ts IS NULL AND EXCLUDED.published_ts IS NOT NULL"),
                {"t": it["title"], "u": it["url"], "s": it["source"], "p": it["published_at"],
                 "ts": it.get("published_ts")})
            nid = conn.execute(text(db.NEWS_ID_BY_URL), {"u": it["url"]}).scalar()
            if nid:
                conn.execute(text("INSERT INTO news_stock(news_id, ticker) VALUES(:n, :t) "
                                  "ON CONFLICT(news_id, ticker) DO NOTHING"),
                             {"n": nid, "t": ticker})


def collect_news(tickers=None):
    """종목별 뉴스 수집(하루 한 번 — run.py real). 반환 {total, ok, failed, tickers, stopped}.
    · 요청 사이 NEWS_REQUEST_GAP 초(기본 0.5) — 한꺼번에 몰아 보내지 않는다.
    · 차단 응답이면 즉시, 연속 NEWS_MAX_FAILS(기본 5)번 실패하면 그 회차를 멈춘다(stopped 에 이유).
    · 저장 오류는 그 종목만 건너뛴다 — 뉴스 때문에 23:00 전체 파이프라인이 멈추지 않게.
    · 한 종목이라도 받았을 때만 news_collected_at(뉴스 화면 '○○ 기준')을 갱신한다.
    · 동시 실행 방지 — 수동 fetch-news 와 23:00 이 겹치면 늦게 온 쪽은 건너뛴다."""
    from app.services import quotes
    with db.advisory_lock("news") as got:
        if not got:
            log.warning("뉴스 수집이 이미 실행 중 — 이번 실행은 건너뜀")
            return {"total": 0, "ok": 0, "failed": 0, "tickers": 0, "skipped": True,
                    "stopped": "다른 뉴스 수집이 실행 중 — 건너뜀"}
        tickers = list(tickers or catalog.universe())        # KRX 유니버스(top-N)
        res = {"total": 0, "ok": 0, "failed": 0, "tickers": len(tickers), "stopped": None}
        streak = 0
        for i, ticker in enumerate(tickers):
            if i and settings.NEWS_REQUEST_GAP > 0:
                time.sleep(settings.NEWS_REQUEST_GAP)
            name = catalog.meta(ticker)["name"]
            try:
                items = parse_rss(_fetch_rss(f"{name} 주식"), settings.NEWS_PER_STOCK)
            except Blocked as e:
                res["stopped"] = f"차단 응답({e}) — {i + 1}/{len(tickers)}번째 종목에서 중단"
                break
            except Exception as e:                         # noqa: BLE001 — 네트워크·파싱 오류는 이 종목만
                res["failed"] += 1
                streak += 1
                log.error("뉴스 실패 %s: %s", name, e)
                if streak >= settings.NEWS_MAX_FAILS:
                    res["stopped"] = f"연속 {streak}번 실패 — {i + 1}/{len(tickers)}번째 종목에서 중단"
                    break
                continue
            streak = 0
            try:
                _store(items, ticker)
            except Exception:                              # noqa: BLE001
                res["failed"] += 1
                log.exception("뉴스 저장 실패 %s", name)
                continue
            res["ok"] += 1
            res["total"] += len(items)
            log.info("뉴스: %s %d건", name, len(items))
        now = quotes.iso_now()
        if res["ok"]:
            db.set_meta("news_collected_at", now)          # 뉴스 화면 '○○:○○ 기준'
        db.set_meta("news_last_run", json.dumps({**res, "at": now}, ensure_ascii=False))
        if res["stopped"]:
            log.error("뉴스 수집 중단: %s (받은 종목 %d)", res["stopped"], res["ok"])
        log.info("뉴스 수집 완료: %d/%d종목 · %d건", res["ok"], len(tickers), res["total"])
        return res
