"""배포 설정 정책 시험(policy as code) — 사람이 고치다 보안 기준을 깨면 CI 가 잡는다.

  · egress 프록시 설정 = 코드의 허용 목록(손으로 고친 설정 금지) · 내부 주소 거부(DNS rebinding)가 허용 규칙보다 먼저
  · compose 운영 오버레이(실제로 합쳐진 설정): 공개 포트는 edge 하나 · 내부망 · 컨테이너 최소 권한 · 앱에 남의 비밀번호 없음
  · Dockerfile: 루트 아님 · 해시 고정 설치 · 실행 이미지에 pip 없음
  · VM(systemd) 서비스: 샌드박스 지시어 · 루프백 바인딩 · egress 차단 · 배포 선택값 유지 표식
  · nginx 경계: 전달 헤더·인증서 헤더 덮어쓰기 · 문서 숨김 · TLS 1.2+ · /admin 기기 인증 · SSE 버퍼링 끔
  · DB: TLS 판 pg_hba 는 평문 TCP 거부 · 역할 분리(roles.sql)
  · 운영 견본(env.prod.example)은 보안 기준선 통과 + 비밀값 없음 · 잠금 파일 일치 · 설치본에 비밀이 섞이지 않음
"""
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import egress  # noqa: E402

SERVICES = ("outofstock-api", "outofstock-scheduler", "outofstock-realtime")
SANDBOX = ("NoNewPrivileges=yes", "PrivateTmp=yes", "PrivateDevices=yes", "ProtectSystem=strict",
           "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes", "ProtectControlGroups=yes",
           "ProtectClock=yes", "ProtectHostname=yes", "ProtectProc=invisible", "RestrictNamespaces=yes",
           "RestrictRealtime=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
           "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "SystemCallArchitectures=native",
           "SystemCallFilter=@system-service", "CapabilityBoundingSet=", "AmbientCapabilities=", "UMask=0077",
           "LimitCORE=0")


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _lines(text):
    return [ln.strip() for ln in text.splitlines()]


# ── egress 프록시 ─────────────────────────────────────────────
def test_squid_conf_is_generated_from_the_allowlist():
    assert _read("deploy/egress-proxy/squid.conf") == egress.squid_conf(""), \
        "python tools/egress.py --squid > deploy/egress-proxy/squid.conf 로 다시 만들 것"


@pytest.mark.parametrize("vm", [False, True])
def test_squid_denies_internal_destinations_before_any_allow(vm):
    conf = egress.squid_conf("", "127.0.0.1:3128" if vm else "3128", vm=vm)
    rules = [ln for ln in conf.splitlines() if ln.startswith("http_access")]
    assert rules[0] == "http_access deny !oos_net" and rules[1] == "http_access deny to_internal"
    assert rules[-1] == "http_access deny all"
    assert all("allow" not in r for r in rules[:2])
    acl = next(ln for ln in conf.splitlines() if ln.startswith("acl to_internal dst"))
    for net in ("169.254.0.0/16", "10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"):
        assert net in acl
    assert "cache deny all" in conf and "forwarded_for delete" in conf
    assert ("pid_filename /run/squid.pid" in conf) == vm
    if vm:
        assert "http_port 127.0.0.1:3128" in conf and "acl oos_net src 127.0.0.1/32 ::1" in conf


def test_squid_extra_hosts_are_the_only_internal_exemption():
    conf = egress.squid_conf("alerts.corp.local")
    assert "acl oos_extra dstdomain alerts.corp.local" in conf
    assert "http_access deny to_internal !oos_extra" in conf
    assert "alerts.corp.local" in next(ln for ln in conf.splitlines() if ln.startswith("acl oos_hosts"))


# ── compose 운영 오버레이(합쳐진 실제 설정) ─────────────────────────
def _compose_config(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("docker 없음")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("docker compose 없음")
    env = tmp_path / ".env"
    env.write_text(_read("env.prod.example"), encoding="utf-8")
    envdb = tmp_path / ".env.db"
    envdb.write_text("POSTGRES_USER=oos\nPOSTGRES_DB=outofstock\nPOSTGRES_PASSWORD=pgsuper1\nOOS_OWNER_PASSWORD=owner1\n"
                     "OOS_APP_PASSWORD=app1\nOOS_RO_PASSWORD=ro1\nOOS_BACKUP_PASSWORD=backup1\n", encoding="utf-8")
    r = subprocess.run(["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env), "--env-file",
                        str(envdb), "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.prod.yml"),
                        "--profile", "full", "--profile", "ops", "config", "--format", "json"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_compose_prod_least_privilege_and_segmentation(tmp_path):
    cfg = _compose_config(tmp_path)
    svc, nets = cfg["services"], cfg["networks"]
    published = {name for name, s in svc.items() if s.get("ports")}
    assert published == {"edge"}, f"공개 포트는 edge 만: {published}"
    for n in ("web", "data", "egress"):
        assert nets[n].get("internal") is True, f"{n} 망은 내부망이어야"
    assert not nets["outside"].get("internal")
    for name in ("app", "scheduler", "realtime", "migrate", "backup"):
        s = svc[name]
        assert s.get("user") == "10001:10001", name
        assert s.get("read_only") is True, name
        assert s.get("cap_drop") == ["ALL"], name
        assert any("no-new-privileges" in o for o in s.get("security_opt", [])), name
        assert "outside" not in s.get("networks", {}), f"{name} 는 인터넷에 직접 닿지 않는다"
    for name in ("app", "scheduler", "realtime"):
        env = svc[name]["environment"]
        assert env["HTTPS_PROXY"] == "http://egress-proxy:3128" and env["https_proxy"] == env["HTTPS_PROXY"]
        assert "egress" in svc[name]["networks"]
        assert env["DATABASE_URL"].startswith("postgresql+psycopg://oos_app:"), "앱은 최소 권한 계정으로만"
        leaked = [k for k, v in env.items() if any(p in str(v) for p in ("pgsuper1", "owner1", "backup1"))]
        assert not leaked, f"{name} 환경에 다른 계정 비밀번호: {leaked}"
        assert not env.get("MIGRATION_DATABASE_URL"), "서비스에는 소유자 계정 없음"
    assert svc["app"]["environment"]["DB_STATEMENT_TIMEOUT_MS"] == "5000"
    assert "DB_STATEMENT_TIMEOUT_MS" not in svc["scheduler"]["environment"]
    assert set(svc["egress-proxy"]["networks"]) == {"outside", "egress"}
    assert set(svc["edge"]["networks"]) == {"outside", "web"}
    assert not svc["db"].get("ports")
    # 앱은 web 망 주소(별칭)에서만 듣는다 — data·egress 망의 컨테이너는 edge 를 건너뛰어 전달 헤더를 위조할 수 없다
    app = svc["app"]
    assert app["networks"]["web"]["aliases"] == ["app-web"]
    assert not any((app["networks"][n] or {}).get("aliases") for n in ("data", "egress"))
    cmd = app["command"]
    assert cmd[cmd.index("--host") + 1] == "app-web" and "0.0.0.0" not in cmd
    assert "--forwarded-allow-ips" not in cmd and app["environment"]["APP_HEALTH_URL"] == "http://app-web:8000/health"
    assert {n for n, s in svc.items() if "web" in (s.get("networks") or {})} == {"app", "edge"}
    assert "oos_backup:backup1@" in svc["backup"]["environment"]["BACKUP_DATABASE_URL"]


# ── 이미지 ───────────────────────────────────────────────────
def test_dockerfile_runs_unprivileged_with_hash_pinned_deps():
    text = _read("Dockerfile")
    assert "--require-hashes" in text and "pip uninstall -y pip" in text
    stages = re.split(r"^FROM ", text, flags=re.M)[1:]
    assert len(stages) == 3
    for st in stages[1:]:                                       # runtime · backup
        users = re.findall(r"^USER (\S+)", st, flags=re.M)
        assert users and users[-1] == "10001:10001", "마지막 USER 는 루트가 아닌 계정"
    assert not re.search(r"^ADD\s+https?://", text, flags=re.M), "원격 ADD 금지(무결성 확인 불가)"
    assert "HEALTHCHECK" in stages[1]
    cmd = re.search(r'^CMD \[("uvicorn".*?)\]', stages[1], flags=re.M | re.S).group(1)
    assert '"--proxy-headers"' in cmd and '"*"' not in cmd, "이미지 기본값은 같은 컨테이너의 전달 헤더만 믿는다"


# ── VM(systemd) 서비스 ─────────────────────────────────────────
def _units(tmp_path, **extra):
    if not shutil.which("bash"):
        pytest.skip("bash 없음")
    out = tmp_path / "units"
    env = {**os.environ, "DRY_UNITS_DIR": str(out), "UNIT_DIR": str(tmp_path / "etc"), "OOS_PY": sys.executable,
           "BACKUP_DIR": str(tmp_path / "bk"), **extra}
    r = subprocess.run(["bash", str(ROOT / "deploy" / "vm.sh"), "_units-dry"], capture_output=True, text=True, env=env,
                       timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}


def test_vm_units_are_sandboxed_and_egress_locked(tmp_path):
    u = _units(tmp_path, HARDEN="1", EGRESS_PROXY="1", BIND_HOST="127.0.0.1", PORT="8123", WORKERS="2")
    assert set(u) == {f"{s}.service" for s in SERVICES} | {"outofstock-backup.service", "outofstock-backup.timer"}
    api = _lines(u["outofstock-api.service"])
    assert any(ln.startswith("ExecStart=") and "--host 127.0.0.1 --port 8123 --workers 2" in ln for ln in api)
    for name in [f"{s}.service" for s in SERVICES] + ["outofstock-backup.service"]:
        lines = _lines(u[name])
        missing = [d for d in SANDBOX if d not in lines]
        assert not missing, f"{name}: {missing}"
        assert "StateDirectory=outofstock" in lines
    for s in SERVICES:
        lines = _lines(u[f"{s}.service"])
        assert "ProtectHome=tmpfs" in lines, "홈 폴더 숨김(코드 폴더만 읽기 전용으로)"
        assert any(ln.startswith("BindReadOnlyPaths=") for ln in lines)
        assert "IPAddressDeny=any" in lines and "IPAddressAllow=localhost" in lines
        assert "SystemCallFilter=~@privileged @resources" in lines
        assert any(ln.startswith("Environment=HTTP_PROXY=http://127.0.0.1:3128") for ln in lines)
    bk = _lines(u["outofstock-backup.service"])
    assert "ProtectHome=read-only" in bk and "IPAddressDeny=any" not in bk     # 외부 보관 도구 대비
    assert any(ln.startswith("ReadWritePaths=") and str(tmp_path / "bk") in ln for ln in bk)
    assert "Environment=DB_STATEMENT_TIMEOUT_MS=5000" in api                       # 쿼리 시간 제한은 API 에만
    assert not any("DB_STATEMENT_TIMEOUT_MS" in ln for ln in _lines(u["outofstock-scheduler.service"]))
    # 가용성 감시는 API 가 실제로 듣는 주소·포트로(합성 모니터링 — 포트를 바꿔도 가짜 장애가 나지 않게)
    assert "Environment=UPTIME_URL=http://127.0.0.1:8123/health" in _lines(u["outofstock-scheduler.service"])
    for marker in ("PORT=8123", "WORKERS=2", "BIND_HOST=127.0.0.1", "HARDEN=1", "EGRESS_PROXY=1"):
        assert f"# oos: {marker}" in api, "update 때 다시 읽는 배포 선택값"


def test_vm_units_without_hardening_still_keep_state_dir(tmp_path):
    u = _units(tmp_path, HARDEN="0", EGRESS_PROXY="0", BIND_HOST="0.0.0.0")
    api = _lines(u["outofstock-api.service"])
    assert "ProtectSystem=strict" not in api and "IPAddressDeny=any" not in api
    assert "StateDirectory=outofstock" in api                                    # 감사 증인 파일 기본 위치
    assert any("--host 0.0.0.0" in ln for ln in api)


def test_api_exposed_directly_gets_no_ip_deny_but_workers_do(tmp_path):
    u = _units(tmp_path, HARDEN="1", EGRESS_PROXY="1", BIND_HOST="0.0.0.0")
    assert "IPAddressDeny=any" not in _lines(u["outofstock-api.service"])        # 들어오는 연결까지 막히므로
    assert "IPAddressDeny=any" in _lines(u["outofstock-scheduler.service"])
    assert "Environment=UPTIME_URL=http://127.0.0.1:8000/health" in _lines(u["outofstock-scheduler.service"])   # 0.0.0.0 → 루프백


def test_static_unit_templates_follow_the_same_sandbox():
    for s in SERVICES:
        lines = _lines(_read(f"deploy/{s}.service"))
        missing = [d for d in SANDBOX if d not in lines]
        assert not missing, f"deploy/{s}.service: {missing}"
        assert "ProtectHome=yes" in lines
    assert any("--host 127.0.0.1" in ln for ln in _lines(_read("deploy/outofstock-api.service")))


# ── nginx 경계 ────────────────────────────────────────────────
EDGES = ("deploy/nginx.conf", "deploy/nginx-http-only.conf", "deploy/nginx/compose-http.conf",
         "deploy/nginx/compose-tls.conf")


@pytest.mark.parametrize("path", EDGES)
def test_edge_overwrites_forwarding_headers_and_hides_docs(path):
    text = "\n".join(ln.split("#", 1)[0] if not ln.lstrip().startswith("location ~ /\\.") else ln
                     for ln in _read(path).splitlines())                     # 주석 제외
    assert "server_tokens off;" in text
    assert re.search(r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr;", text)
    assert "$proxy_add_x_forwarded_for" not in text, "덧붙이면 IP 를 속여 시도 제한을 피한다"
    assert re.search(r"proxy_set_header\s+X-Client-Cert-Verified\s+(\$ssl_client_verify|\"NONE\");", text), \
        "인증서 확인 헤더는 항상 덮어쓴다(위조 차단)"
    assert re.search(r"location ~ \^/\(docs\|redoc\|openapi\\\.json\) \{ return 404; \}", text)
    assert "location ~ /\\." in text and "deny all" in text
    ev = re.search(r"location = /events \{(.*?)\}", text, flags=re.S)
    assert ev and "proxy_buffering off;" in ev.group(1) and "proxy_read_timeout 1h;" in ev.group(1)
    assert "limit_req zone=oos_auth" in text and "client_max_body_size" in text


@pytest.mark.parametrize("path", EDGES)
def test_edge_forwards_host_with_port_so_same_origin_check_matches(path):
    """$host 는 포트를 뗀다 → 80·443 이 아닌 포트(compose HTTP_PORT=8000 등)에서 웹 로그인·변경 요청이 전부 403(csrf_failed)이었다
    (2026-10-22 실측: nginx 1.24 + 8088 포트에서 재현, $http_host 로 고친 뒤 200 · 다른 포트 출처는 계속 403 · HTTP/2 도 확인)."""
    text = "\n".join(ln.split("#", 1)[0] for ln in _read(path).splitlines())
    assert re.search(r"proxy_set_header\s+Host\s+\$http_host;", text)
    assert not re.search(r"proxy_set_header\s+Host\s+\$host;", text)


@pytest.mark.parametrize("path", ("deploy/nginx.conf", "deploy/nginx/compose-tls.conf"))
def test_tls_edges_require_modern_tls_and_device_certs_for_admin(path):
    text = _read(path)
    assert re.search(r"ssl_protocols\s+TLSv1\.2 TLSv1\.3;", text)
    assert not re.search(r"TLSv1(\.0|\.1)?[ ;]", text.replace("TLSv1.2", "").replace("TLSv1.3", ""))
    assert "ssl_session_tickets     off;" in text and "Strict-Transport-Security" in text
    admin = re.search(r"location /admin/ \{(.*?)\n    \}", text, flags=re.S)
    assert admin and "if ($ssl_client_verify != SUCCESS) { return 403; }" in admin.group(1)


def test_host_nginx_confs_do_not_require_ipv6():
    for path in ("deploy/nginx.conf", "deploy/nginx-http-only.conf"):
        assert not re.search(r"^\s*listen \[::\]", _read(path), flags=re.M), "IPv6 를 끈 VM 에서 nginx 가 못 뜬다"


# ── DB ───────────────────────────────────────────────────────
def test_tls_pg_hba_rejects_plaintext_tcp_before_any_accept():
    rules = [ln.split() for ln in _read("deploy/sql/pg_hba_tls.conf").splitlines() if ln.strip() and not ln.startswith("#")]
    tcp = [r for r in rules if r[0] != "local"]
    assert tcp[0] == ["hostnossl", "all", "all", "all", "reject"]
    assert all(r[0] == "hostssl" and r[-1] == "scram-sha-256" for r in tcp[1:])
    assert not any(r[-1] in ("trust", "md5", "password") for r in tcp)


def test_roles_sql_keeps_app_and_backup_accounts_minimal():
    sql = _read("deploy/sql/roles.sql")
    for stmt in ("ALTER ROLE oos_app NOSUPERUSER NOCREATEDB NOCREATEROLE;", "ALTER ROLE oos_app NOBYPASSRLS;",
                 "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
                 "ALTER ROLE oos_backup NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS CONNECTION LIMIT 2;",
                 "ALTER ROLE oos_backup SET default_transaction_read_only = on;",
                 "ALTER ROLE oos_ro SET default_transaction_read_only = on;"):
        assert stmt in sql, stmt
    assert "PASSWORD '" not in sql, "비밀번호는 파일이 아니라 실행할 때 -v 로"


# ── 견본·잠금·설치본 ───────────────────────────────────────────
def test_prod_env_example_passes_the_security_baseline_and_holds_no_secrets():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "security_check.py"), "--template",
                        str(ROOT / "env.prod.example"), "--json"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["fail"] == 0 and res["warn"] == 0
    status = {x["id"]: x["status"] for x in res["results"]}
    assert status["TPL-1"] == "PASS" and status["NET-1"] == "PASS" and status["LOG-1"] == "PASS"


def test_template_check_catches_a_committed_secret(tmp_path):
    bad = tmp_path / "leak.env"
    bad.write_text(_read("env.prod.example").replace("APP_SECRET_KEY=\n", "APP_SECRET_KEY=" +
                                                     base64.b64encode(b"k" * 32).decode() + "\n"), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "security_check.py"), "--template", str(bad), "--json"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert next(x for x in json.loads(r.stdout)["results"] if x["id"] == "TPL-1")["status"] == "FAIL"


def test_requirements_lock_matches_requirements():
    from tools import check_lock
    assert check_lock.problems() == []


def test_installer_ships_code_but_never_secrets(tmp_path):
    from tools import make_installer
    out = tmp_path / "install_outofstock_full.sh"
    info = make_installer.build(out)
    text = out.read_text(encoding="utf-8")
    assert make_installer.MARK in text[:4000]
    assert re.search(rf"OUTOFSTOCK 전체 {info['files']}개 파일", text)               # vm.sh update 가 세는 표식
    b64 = text.split("__OOS_PAYLOAD_BEGIN__\n", 1)[1].split("__OOS_PAYLOAD_END__", 1)[0]
    blob = base64.b64decode(b64)
    assert hashlib.sha256(blob).hexdigest() == info["sha256"] and info["sha256"] in text
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as tar:
        names = tar.getnames()
    assert len(names) == info["files"]
    forbidden = [n for n in names if n in (".env", ".env.db", "deploy/backup/recipients.txt")
                 or n.startswith(("var/", "deploy/generated/", ".venv/")) or "__pycache__" in n
                 or n.endswith((".key", ".p12", ".age", ".pem", ".crt"))]
    assert not forbidden, forbidden
    for must in ("app/api.py", "deploy/vm.sh", "requirements.lock", ".env.example", "env.prod.example",
                 "deploy/backup/recipients.example.txt", "tools/egress.py"):
        assert must in names
    if shutil.which("bash") and shutil.which("sha256sum") and shutil.which("base64"):
        dest = tmp_path / "x"
        dest.mkdir()
        r = subprocess.run(["bash", str(out)], cwd=dest, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert sum(1 for p in dest.rglob("*") if p.is_file()) == info["files"]
        tampered = tmp_path / "bad.sh"
        i = text.index("__OOS_PAYLOAD_BEGIN__\n") + 500
        tampered.write_text(text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1:], encoding="utf-8")
        (tmp_path / "y").mkdir()
        r = subprocess.run(["bash", str(tampered)], cwd=tmp_path / "y", capture_output=True, text=True, timeout=120)
        assert r.returncode != 0 and not any((tmp_path / "y").iterdir()), "변조본은 아무것도 풀지 않는다"
