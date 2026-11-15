"""운영 보안 설정 점검(읽기만) — 기준선(baseline)과 다른 설정을 근거 조항과 함께 보여 준다.

    python tools/security_check.py                          # 지금 설정(.env + 환경변수) + DB 계정 권한
    python tools/security_check.py --no-db                  # 설정만
    python tools/security_check.py --template env.prod.example   # 견본 점검(CI): 안전한 기본값인지 + 비밀값이 비어 있는지
    python tools/security_check.py --json                   # 기계가 읽는 결과(CI 산출물)

판정: FAIL(기준 위반 — 종료 코드 1) · WARN(운영 판단 필요 — 예: HTTPS 여부) · PASS · INFO · SKIP
근거 약어: 안전성고시 = 개인정보의 안전성 확보조치 기준(개인정보보호위원회 고시 제2023-6호)
          전금규정 = 전자금융감독규정 · ZT = KISA 제로트러스트 가이드라인 2.0
같은 점검을 DB 계정 쪽은 tools/db_roles_check.py, 서버 쪽은 bash deploy/vm.sh status(샌드박스 점수)가 맡는다.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAIL, WARN, PASS, INFO, SKIP = "FAIL", "WARN", "PASS", "INFO", "SKIP"
MARK = {FAIL: "✗", WARN: "!", PASS: "✓", INFO: "·", SKIP: "-"}

# 견본에 값이 들어 있으면 안 되는 비밀값(운영 키가 저장소·설치본에 섞이는 사고 방지)
SECRET_NAMES = ("APP_SECRET_KEY", "APP_SECRET_KEYS_OLD", "DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_KEYS_OLD",
                "KRX_API_KEY", "DART_API_KEY", "KIS_APP_KEY", "KIS_APP_SECRET", "FINNHUB_API_KEY", "EXIM_API_KEY",
                "ECOS_API_KEY", "TOSS_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "ALERT_WEBHOOK_URL")
PLACEHOLDER = re.compile(r"^(|CHANGE_ME.*|changeme.*|xxx+|<.*>)$")


def _key32(v: str) -> bool:
    try:
        return len(base64.b64decode(v.strip(), validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def _env_file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def checks(S, *, template: bool, env_text: str, env_path: Path | None) -> list[dict]:
    """S = app.config.settings(점검 대상 설정으로 읽힌 것). 결과 목록."""
    out: list[dict] = []

    def add(cid, title, status, detail, basis=""):
        out.append({"id": cid, "title": title, "status": status, "detail": detail, "basis": basis})

    # ── 키 ────────────────────────────────────────────────────
    basis_key = "안전성고시 제7조②·⑥ · 전금규정 제13조"
    if template:
        add("KEY-1", "마스터 키(APP_SECRET_KEY)", SKIP, "견본 — 비밀값은 비어 있어야 정상", basis_key)
        add("KEY-2", "데이터 암호화 키(DATA_ENCRYPTION_KEY)", SKIP, "견본 — 비밀값은 비어 있어야 정상", basis_key)
    else:
        for cid, name in (("KEY-1", "APP_SECRET_KEY"), ("KEY-2", "DATA_ENCRYPTION_KEY")):
            v = getattr(S, name) or ""
            if not v.strip():
                add(cid, f"{name}", FAIL, "없음 — python3 -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"",
                    basis_key)
            elif not _key32(v):
                add(cid, f"{name}", FAIL, "형식 오류 — base64 로 인코딩한 32바이트여야 한다", basis_key)
            else:
                add(cid, f"{name}", PASS, "32바이트 키 설정됨", basis_key)
        a, d = (S.APP_SECRET_KEY or "").strip(), (S.DATA_ENCRYPTION_KEY or "").strip()
        if a and a == d:
            add("KEY-3", "키 분리(용도별 다른 키)", FAIL, "APP_SECRET_KEY 와 DATA_ENCRYPTION_KEY 가 같다", basis_key)
        else:
            add("KEY-3", "키 분리(용도별 다른 키)", PASS, "마스터 키 ≠ 데이터 키(하위 키는 HKDF 로 용도별 파생)", basis_key)

    # ── 인증·세션 ──────────────────────────────────────────────
    it = S.PASSWORD_HASH_ITERATIONS
    add("AUTH-1", "비밀번호 일방향 해시 강도", PASS if it >= 600_000 else FAIL,
        f"PBKDF2-HMAC-SHA256 {it:,}회(기준 600,000 — OWASP 2023)", "안전성고시 제7조① · 전금규정 제33조①")
    lock, fails = S.ACCOUNT_LOCK_THRESHOLD, S.LOGIN_MAX_FAILS
    add("AUTH-2", "연속 인증 실패 제한", PASS if max(lock, fails) <= 5 else FAIL,
        f"계정 잠금 {lock}회 · 시도 제한 {fails}회(기준 5회 이내)", "안전성고시 제5조⑥ · 전금규정 제33조②3호")
    idle, ttl, absl = S.SESSION_IDLE_SECONDS, S.ACCESS_TOKEN_TTL_SECONDS, S.SESSION_ABSOLUTE_SECONDS
    st = FAIL if idle > 3600 else WARN if (idle > 1800 or ttl > 900 or absl > 43200) else PASS
    add("AUTH-3", "미사용 세션 자동 차단", st,
        f"유휴 {idle // 60}분 · 접근 토큰 {ttl // 60}분 · 절대 만료 {absl // 3600}시간(기준 30분·15분·12시간)",
        "안전성고시 제6조④ · ZT 신원(세션 지속 검증)")
    su = S.STEP_UP_MAX_AGE_SECONDS
    add("AUTH-4", "고위험 동작 추가 인증 유효 시간", PASS if su <= 600 else WARN, f"{su // 60}분(기준 10분 이내)",
        "ZT 정책(위험 기반 재인증)")
    add("AUTH-5", "시도 제한 저장소 공유", PASS if S.RATE_LIMIT_BACKEND == "postgres" else WARN,
        f"{S.RATE_LIMIT_BACKEND}" + ("" if S.RATE_LIMIT_BACKEND == "postgres" else
                                       " — 워커·서버가 여럿이면 한도가 그 수만큼 늘어난다(postgres 권장)"),
        "안전성고시 제5조⑥")

    # ── 경계 ─────────────────────────────────────────────────
    add("EDGE-1", "API 문서 숨김", PASS if not S.DOCS_ENABLED else WARN,
        "DOCS_ENABLED=" + str(S.DOCS_ENABLED).lower() + ("" if not S.DOCS_ENABLED else " — 공격면(엔드포인트 목록) 노출"),
        "전금규정 제15조(최소 기능)")
    add("EDGE-2", "CORS 와일드카드 없음", FAIL if "*" in S.CORS_ORIGINS else PASS,
        ",".join(S.CORS_ORIGINS) or "비움(같은 출처만)", "ZT 애플리케이션(출처 검증)")
    csp = (S.SECURITY_CSP or "").strip().lower()
    add("EDGE-3", "Content-Security-Policy", FAIL if csp == "off" else PASS,
        "꺼짐" if csp == "off" else ("기본(nonce·Trusted Types·인라인 금지)" if not csp else "사용자 지정"),
        "OWASP ASVS V14.4 · ZT 애플리케이션")
    add("EDGE-4", "쿠키 Secure", WARN if S.COOKIE_SECURE == "false" else PASS, f"COOKIE_SECURE={S.COOKIE_SECURE}",
        "안전성고시 제7조(전송 구간 보호)")
    add("EDGE-5", "HSTS", PASS if S.SECURITY_HSTS else INFO,
        "켬" if S.SECURITY_HSTS else "꺼짐 — HTTPS 경계(nginx TLS)로 서비스하면 true", "안전성고시 제7조(전송 구간 보호)")
    insecure = [o for o in S.TRUSTED_ORIGINS if o.startswith("http://") and not re.match(r"http://(localhost|127\.)", o)]
    add("EDGE-6", "신뢰 출처는 HTTPS", WARN if insecure else PASS, ", ".join(insecure) or "문제 없음",
        "ZT 애플리케이션(출처 검증)")
    add("EDGE-7", "요청 본문 상한", PASS if S.MAX_BODY_BYTES <= 10_000_000 else WARN, f"{S.MAX_BODY_BYTES:,} 바이트",
        "복원력(자원 고갈 방지)")
    add("EDGE-8", "관리 기능 기기 인증(mTLS)", PASS if S.ADMIN_REQUIRE_CLIENT_CERT else INFO,
        "켬(nginx 뒤 127.0.0.1 에서만 의미)" if S.ADMIN_REQUIRE_CLIENT_CERT
        else "꺼짐 — 관리 기능은 TOTP + JIT 활성로만 보호(기기 인증서는 nginx 경계와 함께 켠다)",
        "안전성고시 제6조② · ZT 기기")

    # ── 네트워크 ─────────────────────────────────────────────
    em = S.EGRESS_MODE
    add("NET-1", "나가는 연결 허용 목록", PASS if em == "enforce" else WARN if em == "monitor" else FAIL,
        f"EGRESS_MODE={em}" + (" — 기록만(차단 안 함)" if em == "monitor" else " — 통제 꺼짐" if em == "off" else ""),
        "전금규정 제15조①1호 · ZT 네트워크(마이크로 세그멘테이션)")

    # ── 결제 검증 ─────────────────────────────────────────────
    stub = [k for k, v in (("IAP_VERIFY_MODE", S.IAP_VERIFY_MODE), ("TOSS_MODE", S.TOSS_MODE))
            if str(v).lower() == "stub"]
    add("PAY-1", "결제 검증(영수증·승인)", INFO if stub else PASS,
        (f"{'·'.join(stub)}=stub — 데모 모드: 영수증·결제를 형식만 확인해 가상머니가 검증 없이 충전된다(현금화 불가 가상머니라 "
         "의도된 기본값). 실제 결제를 붙이면 스토어·PG 서버 검증(strict/real)으로") if stub else "스토어·PG 서버 검증",
        "전금규정 제13조(거래 무결성) · 결제 위·변조")

    # ── 기록·보관 ────────────────────────────────────────────
    acc, aud, pii = S.ACCESS_LOG_RETENTION_DAYS, S.AUDIT_RETENTION_DAYS, S.PII_LOG_RETENTION_DAYS
    st = FAIL if (acc < 365 or aud < 1095) else WARN if pii < 730 else PASS
    add("LOG-1", "접속기록·감사 기록 보관", st,
        f"접속 {acc}일 · 감사 {aud}일 · 개인정보 열람 {pii}일(기준: 접속기록 1년↑ — 5만명 이상·민감정보면 2년, "
        f"권한 부여·변경·말소 내역(감사) 3년↑)", "안전성고시 제8조① · 제5조③")
    add("LOG-2", "구조화 로그(SIEM 연동)", PASS if S.LOG_FORMAT == "json" else WARN, f"LOG_FORMAT={S.LOG_FORMAT}",
        "안전성고시 제8조② · ZT 가시성")
    add("LOG-3", "감사 체인 외부 증인", PASS if S.AUDIT_WITNESS_FILE else WARN,
        S.AUDIT_WITNESS_FILE or "없음 — DB 를 통째로 바꿔치기하면 체인 위조를 못 잡는다", "ZT 가시성 · 무결성")

    # ── 백업 ─────────────────────────────────────────────────
    rec = S.BACKUP_AGE_RECIPIENTS
    if template:
        add("DR-1", "백업 암호화 공개키", SKIP, "견본 — 서버마다 경로를 정한다", "전금규정 제23조 · 안전성고시 제11조")
    elif not rec:
        add("DR-1", "백업 암호화 공개키", FAIL, "BACKUP_AGE_RECIPIENTS 없음 — 백업이 평문(개인정보 포함)으로 남는다",
            "전금규정 제23조 · 안전성고시 제7조·제11조")
    else:
        keys = [ln for ln in _env_file_text(Path(rec)).splitlines() if ln.strip().startswith("age1")]
        st = FAIL if not keys else WARN if len(keys) < 2 else PASS
        add("DR-1", "백업 암호화 공개키", st,
            f"{rec}: 공개키 {len(keys)}개" + (" — 두 개(운영·금고) 권장: 한쪽 키를 잃어도 복구" if len(keys) == 1 else ""),
            "전금규정 제23조 · 안전성고시 제7조·제11조")

    # ── 운영 파일 ─────────────────────────────────────────────
    if re.search(r"^\s*DB_STATEMENT_TIMEOUT_MS\s*=\s*[1-9]", env_text, re.M):
        add("OPS-1", "쿼리 시간 제한 범위", WARN, ".env 의 DB_STATEMENT_TIMEOUT_MS 는 배치까지 끊는다 — API_STATEMENT_TIMEOUT_MS 를 쓸 것",
            "복원력")
    else:
        add("OPS-1", "쿼리 시간 제한 범위", PASS, "API 에만(API_STATEMENT_TIMEOUT_MS)", "복원력")
    if template or env_path is None:
        add("OPS-2", ".env 권한", SKIP, "견본·파일 없음", "안전성고시 제7조")
    elif not env_path.exists():
        add("OPS-2", ".env 권한", INFO, f"{env_path} 없음(환경변수로만 설정)", "안전성고시 제7조")
    else:
        mode = stat.S_IMODE(env_path.stat().st_mode)
        add("OPS-2", ".env 권한", FAIL if mode & 0o077 else PASS, f"{oct(mode)}(기준 600 — 같은 서버 다른 계정이 키를 못 읽게)",
            "안전성고시 제7조 · 전금규정 제13조")

    # ── 견본 전용: 비밀값이 비어 있는가 ─────────────────────────
    if template:
        filled = []
        for name in SECRET_NAMES:
            m = re.search(rf"^\s*{name}\s*=(.*)$", env_text, re.M)
            if m and not PLACEHOLDER.match(m.group(1).strip()):
                filled.append(name)
        m = re.search(r"^\s*DATABASE_URL\s*=\s*\S+://[^:]+:([^@]*)@", env_text, re.M)
        if m and not PLACEHOLDER.match(m.group(1)):
            filled.append("DATABASE_URL(비밀번호)")
        add("TPL-1", "견본에 비밀값 없음", FAIL if filled else PASS,
            ", ".join(filled) or "키·토큰·비밀번호 모두 비어 있음(자리표시자)", "안전성고시 제7조⑥(키 관리)")
    return out


def db_checks() -> list[dict]:
    """DB 계정 권한 — tools/db_roles_check.py 의 점검을 판정으로 바꾼다."""
    from tools import db_roles_check as d
    from app.config import settings
    out = []
    try:
        rows = d.check(settings.DATABASE_URL, "서비스")
    except Exception as e:                                   # noqa: BLE001 — 접속 실패도 결과로
        return [{"id": "DB-0", "title": "DB 접속", "status": WARN, "detail": f"접속 실패: {type(e).__name__}", "basis": ""}]
    severe = {"슈퍼유저", "RLS 우회(BYPASSRLS)", "감사 로그 UPDATE 권한", "감사 로그 DELETE 권한", "열람 기록 DELETE 권한"}
    n = 0
    for name, val, want in rows:
        if want is None:
            continue
        n += 1
        bad = bool(val) != want
        status = PASS if not bad else FAIL if (name in severe or name.startswith("RLS 강제")) else WARN
        out.append({"id": f"DB-{n}", "title": f"서비스 계정 — {name}", "status": status,
                    "detail": f"{val}" + ("" if not bad else " — deploy/sql/roles.sql 로 권한 분리"),
                    "basis": "안전성고시 제5조 · 전금규정 제26조(직무분리) · ZT 데이터"})
    return out


def _run_template(path: Path, as_json: bool) -> int:
    """견본 파일만으로 설정을 읽힌 새 프로세스에서 점검 — 지금 서버의 .env·환경변수가 섞이지 않게."""
    text = _env_file_text(path)
    if not text:
        print(f"견본 파일을 읽을 수 없습니다: {path}", file=sys.stderr)
        return 2
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH") if k in os.environ}
    env["PYTHON_DOTENV_DISABLED"] = "1"                      # 저장소의 .env 를 읽지 않게(python-dotenv 1.1+)
    for ln in text.splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$", ln)
        if m:
            env[m.group(1)] = m.group(2).strip()
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_template-child", str(path.resolve())]
    if as_json:
        cmd.append("--json")
    return subprocess.run(cmd, env=env, cwd=str(ROOT), check=False).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", type=Path, help="견본 .env 파일 점검(CI)")
    ap.add_argument("--no-db", action="store_true", help="DB 계정 점검 건너뛰기")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--_template-child", dest="child", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.template:
        return _run_template(a.template, a.json)

    from app.config import settings
    template = a.child is not None
    env_path = None if template else ROOT / ".env"
    env_text = _env_file_text(a.child if template else ROOT / ".env")
    results = checks(settings, template=template, env_text=env_text, env_path=env_path)
    if not template and not a.no_db:
        results += db_checks()
    fails = sum(r["status"] == FAIL for r in results)
    if a.json:
        print(json.dumps({"target": str(a.child) if template else "runtime", "fail": fails,
                          "warn": sum(r["status"] == WARN for r in results), "results": results}, ensure_ascii=False, indent=1))
    else:
        print(f"보안 설정 점검 — {'견본 ' + str(a.child.name) if template else '지금 설정(.env + 환경변수)'}")
        for r in results:
            basis = f"  [{r['basis']}]" if r["basis"] else ""
            print(f" {MARK[r['status']]} {r['status']:4s} {r['id']:7s} {r['title']} — {r['detail']}{basis}")
        counts = {s: sum(r["status"] == s for r in results) for s in (FAIL, WARN, PASS, INFO, SKIP)}
        print(" · ".join(f"{k} {v}" for k, v in counts.items()))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
