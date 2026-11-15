"""팀원 공유용 DB 스냅샷 — 시세·지수·재무·뉴스·해외 같은 '공개 데이터'만 내보내고 들여온다.

  python run.py snapshot-export [파일]          # 기본: snapshots/outofstock-snapshot-YYYYMMDD-HHMM.jsonl.gz
  python run.py snapshot-import <파일> --yes    # 아래 표만 비우고 채움 — 계정·지갑·채팅·로그는 그대로

API 키 없는 팀원 PC 에서도 VM 과 같은 실데이터 화면을 띄우는 용도.

왜 pg_dump 가 아니라 이 형식인가
  · 넣을 표와 app_meta 키를 '허용 목록'으로만 고른다 — 계정·비밀번호 해시·로그인 토큰·지갑·채팅·학습/접속 로그,
    그리고 app_meta 에 저장되는 KIS 접근 토큰(kis_token)은 파일에 절대 들어가지 않는다.
  · 들일 때도 허용 목록 밖의 표는 거부한다(파일을 손봐도 계정 표를 덮어쓸 수 없음).
  · PostgreSQL 버전·pg_dump 도구와 무관하게 파이썬만으로 된다(팀원 PC 의 Docker DB 도 그대로).
  · 들이기는 한 트랜잭션 — 중간에 실패하면 원래 데이터 그대로.

형식: gzip JSON Lines — 1줄 헤더 {format, version, created_at, tables},
      표마다 {"table", "columns"} 한 줄 + 행 배열 줄들, 마지막 {"end": {표: 행 수}}(잘린 파일 검출)."""
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, text

from app import db
from app.services import practice as _practice

FORMAT = "outofstock-snapshot"
VERSION = 1
# 공개 데이터 표(수집·가공 결과). 순서는 의미 없음(외래키 없음).
SHARE_TABLES = ("stock", "daily_price", "ohlc", "market_index", "index_daily", "realtime_price",
                "financial_statement", "ranking", "recommendation", "reco_snapshot",
                "news", "news_stock", "news_sentiment",
                "overseas_stock", "overseas_ohlc", "overseas_quote", "overseas_financial",
                "app_meta")
# app_meta 중 화면 표시에 필요한 출처·기준시각만(토큰·배치 상태·오류 메시지는 제외)
META_KEYS = ("market_source", "market_asof", "overseas_source", "us_index_source", "fx_source",
             "overseas_asof", "news_collected_at", "kr_index_source", "kr_close_final",
             # 시세 출처 기록 — 받은 쪽에서 run.py real 을 돌려도 실데이터를 데모 잔여물로 오인해 지우지 않게
             "krx_since") + tuple(f"{k}:{sc['scenario_id']}" for sc in _practice.SCENARIOS
                                  for k in ("scenario_src", "scenario_from"))
_BATCH = 2000


class SnapshotError(Exception):
    pass


def _iso(v):
    """JSON 에 못 담는 값 — timestamptz(시세·뉴스 시각)는 ISO 문자열(+09:00)로. 들일 때 DB 가 다시 시각으로 바꾼다."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    raise TypeError(f"JSON 으로 바꿀 수 없는 값: {type(v).__name__}")


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def default_path(now=None):
    now = now or datetime.now(timezone(timedelta(hours=9)))
    return os.path.join("snapshots", f"outofstock-snapshot-{now:%Y%m%d-%H%M}.jsonl.gz")


def export_snapshot(path=None):
    """공개 데이터 표를 gzip JSON Lines 로 — 큰 표도 조금씩 읽어 메모리를 적게 쓴다. 반환 {path, bytes, tables}."""
    path = path or default_path()
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    counts = {}
    # 한 시점의 일관된 사본 — REPEATABLE READ 는 첫 조회 시점의 스냅샷을 끝까지 본다. 예전엔 표마다 그 순간의 최신 커밋을
    # 읽어(READ COMMITTED), 23:00 뉴스 수집과 겹치면 기사 없는 감성·종목 행(또는 그 반대)이 파일에 섞였다.
    with gzip.open(tmp, "wt", encoding="utf-8") as f, \
            db.get_engine().connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        f.write(json.dumps({"format": FORMAT, "version": VERSION, "tables": list(SHARE_TABLES),
                            "created_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")},
                           ensure_ascii=False) + "\n")
        for name in SHARE_TABLES:
            cols = [c.name for c in db.metadata.tables[name].columns]
            sql = f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(name)}"
            params = {}
            if name == "app_meta":
                sql += " WHERE key = ANY(:keys)"
                params = {"keys": list(META_KEYS)}
            f.write(json.dumps({"table": name, "columns": cols}) + "\n")
            res = conn.execution_options(stream_results=True).execute(text(sql), params)
            n = 0
            while True:
                rows = res.fetchmany(_BATCH)
                if not rows:
                    break
                for r in rows:
                    f.write(json.dumps(list(r), ensure_ascii=False, separators=(",", ":"), default=_iso) + "\n")
                n += len(rows)
            counts[name] = n
        f.write(json.dumps({"end": counts}) + "\n")
    os.replace(tmp, path)
    return {"path": path, "bytes": os.path.getsize(path), "tables": counts}


def _reset_sequences(conn, names):
    """자동 증가 id 를 들인 데이터의 최댓값 뒤로 — 이후 수집이 id 충돌 없이 이어지게."""
    for name in names:
        table = db.metadata.tables[name]
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not isinstance(pk[0].type, Integer):
            continue
        col = pk[0].name
        seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": name, "c": col}).scalar()
        if seq:
            conn.execute(text(f"SELECT setval(:s, COALESCE((SELECT MAX({_q(col)}) FROM {_q(name)}), 1), "
                              f"(SELECT MAX({_q(col)}) FROM {_q(name)}) IS NOT NULL)"), {"s": seq})


def import_snapshot(path):
    """스냅샷 들이기 — 파일에 든 공개 데이터 표만 비우고 채운다(한 트랜잭션). 반환 {tables, created_at}."""
    if not os.path.exists(path):
        raise SnapshotError(f"파일이 없어요: {path}")
    counts, done = {}, []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        try:
            header = json.loads(f.readline() or "{}")
        except (OSError, ValueError) as e:
            raise SnapshotError(f"스냅샷 파일을 읽을 수 없어요: {e}") from e
        if header.get("format") != FORMAT:
            raise SnapshotError("OUTOFSTOCK 스냅샷 파일이 아니에요")
        if header.get("version") != VERSION:
            raise SnapshotError(f"지원하지 않는 스냅샷 버전: {header.get('version')}")
        end = None
        with db.transaction() as conn:
            table = keep = cols = None
            buf = []

            def flush():
                if buf:
                    conn.execute(table.insert(), buf)
                    counts[table.name] = counts.get(table.name, 0) + len(buf)
                    buf.clear()

            for line in f:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    if table is not None:
                        flush()
                    if "end" in obj:
                        end = obj["end"]
                        break
                    name = obj.get("table")
                    if name not in SHARE_TABLES:
                        raise SnapshotError(f"허용되지 않은 표가 들어 있어요: {name}")
                    table = db.metadata.tables[name]
                    have = {c.name for c in table.columns}
                    keep = [i for i, c in enumerate(obj["columns"]) if c in have]   # 스키마가 달라도 공통 열만
                    cols = [obj["columns"][i] for i in keep]
                    if name == "app_meta":
                        conn.execute(text("DELETE FROM app_meta WHERE key = ANY(:k)"), {"k": list(META_KEYS)})
                    else:
                        conn.execute(text(f"TRUNCATE {_q(name)}"))
                    counts.setdefault(name, 0)
                    done.append(name)
                    continue
                if table is None:
                    raise SnapshotError("표 정보 없이 행이 나왔어요(손상된 파일)")
                row = dict(zip(cols, (obj[i] for i in keep)))
                if table.name == "app_meta" and row.get("key") not in META_KEYS:
                    continue                                  # 허용 키 밖은 버림(토큰 등)
                buf.append(row)
                if len(buf) >= _BATCH:
                    flush()
            if end is None:
                raise SnapshotError("파일이 중간에 끊겼어요(끝 표시 없음) — 다시 받아 주세요")
            for name, n in end.items():
                if name in counts and name != "app_meta" and counts[name] != n:
                    raise SnapshotError(f"{name} 행 수가 맞지 않아요({counts[name]} ≠ {n})")
            _reset_sequences(conn, done)
    return {"tables": counts, "created_at": header.get("created_at")}
