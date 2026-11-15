"""VM 운영 스크립트 — 문법·안전장치(손상/엉뚱한 설치 파일 거부, sudo 통째 실행 거부) 회귀 검사.
(전체 흐름 install→backup→restore→update→rollback 은 실제 PostgreSQL 로 따로 검증했다 — MERGE_SUMMARY 5차)"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / "deploy" / "vm.sh"


def _bash(*args, env=None):
    return subprocess.run(["bash", str(VM), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


@pytest.mark.parametrize("script", ["deploy/vm.sh", "deploy/deploy.sh"])
def test_shell_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["deploy/vm.sh", "deploy/deploy.sh", "deploy/backup/pitr_setup.sh"])
def test_inline_python_snippets_compile(script):
    """셸 안의 `python -c '…'` 한 줄 코드도 문법 검사 — vm.sh status 의 '보안 설정 점검' 요약 줄이 f-string 안의 \\" 때문에
    SyntaxError 로 조용히 사라져 있었다(2026-10-23 VM 실측에서 발견, 오류는 2>/dev/null 로 숨겨져 있었다)."""
    import re
    text = (ROOT / script).read_text(encoding="utf-8")
    snippets = re.findall(r"(?:python3?|\$PY\"?|\"\$py\"|\"\$c\")\s+-c\s+'([^']*)'", text)
    for code in snippets:
        compile(code, f"{script}:<python -c>", "exec")
    if script == "deploy/vm.sh":
        assert len(snippets) >= 6


def test_vm_script_usage_lists_operations():
    r = _bash()
    assert r.returncode != 0
    for op in ("install", "update", "status", "backup", "restore", "rollback", "logs"):
        assert op in r.stdout


def test_update_rejects_files_that_are_not_our_installer(tmp_path):
    fake = tmp_path / "install_outofstock_full.sh"
    fake.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    r = _bash("update", str(fake), env={"HOME": str(tmp_path)})
    assert r.returncode != 0 and "설치 파일이 아닙니다" in r.stderr
    assert not (tmp_path / "outofstock-releases").exists()          # 아무것도 건드리지 않음


def test_running_whole_script_with_sudo_is_refused(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("root 일 때만 재현 가능")
    r = _bash("status", env={"SUDO_USER": "someone", "HOME": str(tmp_path)})
    assert r.returncode != 0 and "sudo 없이" in r.stderr


def _prune(app_dir, keep_lines):
    """vm.sh 의 prune_stale 만 떼어 실행(서비스·DB 없이) — update/rollback 의 '예전 파일 정리' 규칙 검사."""
    src = VM.read_text(encoding="utf-8")
    start = src.index('MANIFEST_NAME=".release-files"')
    end = src.index("\ncmd_update() {")
    keep = app_dir.parent / "keep.txt"
    keep.write_text("".join(f"{k}\n" for k in keep_lines), encoding="utf-8")
    script = ("set -euo pipefail\nok(){ echo \"OK $*\"; }\n" + src[start:end]
              + f'\nAPP_DIR="{app_dir}"\nprune_stale "{keep}"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def _touch(root, *paths):
    for p in paths:
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")


def test_update_removes_files_deleted_in_new_release_but_keeps_user_files(tmp_path):
    """덮어쓰기만 하면 7차에서 지운 legacy.html·pipeline.py 등이 VM 에 남아 계속 서비스된다 → 지운다.
    .env·deploy/generated(서비스 파일)·사용자가 만든 파일은 그대로."""
    app = tmp_path / "stock-backend"
    _touch(app, "app/api.py", "app/static/legacy.html", "app/pipeline.py", "setup_deploy.sh", ".env",
           "deploy/generated/outofstock-api.service", "my_notes.txt")
    r = _prune(app, ["app/api.py"])
    assert r.returncode == 0, r.stderr
    assert not (app / "app/static/legacy.html").exists() and not (app / "app/pipeline.py").exists()
    assert not (app / "setup_deploy.sh").exists()
    for kept in ("app/api.py", ".env", "deploy/generated/outofstock-api.service", "my_notes.txt"):
        assert (app / kept).exists(), kept


def test_prune_follows_previous_release_manifest_and_ignores_unsafe_paths(tmp_path):
    app = tmp_path / "stock-backend"
    _touch(app, "app/api.py", "app/old_module.py", "tests/test_old.py", "keep_me.txt")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    (app / ".release-files").write_text("app/api.py\napp/old_module.py\ntests/test_old.py\n../outside.txt\n"
                                        "/etc/hostname\n.env\n", encoding="utf-8")
    r = _prune(app, ["app/api.py"])
    assert r.returncode == 0, r.stderr
    assert not (app / "app/old_module.py").exists() and not (app / "tests/test_old.py").exists()
    assert (app / "app/api.py").exists() and (app / "keep_me.txt").exists()
    assert (tmp_path / "outside.txt").exists()                     # 폴더 밖 경로는 무시


def test_dispatch_is_read_whole_so_self_update_cannot_cut_the_script():
    """update·rollback 은 실행 중인 vm.sh 를 다른 버전으로 덮는다. bash 는 스크립트를 읽으면서 실행하므로, 분기(case) 뒤에
    더 읽을 게 남아 있으면 바뀐 파일의 엉뚱한 위치부터 읽어 'syntax error'(종료 코드 2)로 끝났다 → 분기를 { …; exit; } 로 묶음."""
    tail = [ln for ln in VM.read_text(encoding="utf-8").rstrip().splitlines()[-30:] if ln.strip()]
    assert tail[-1] == "}" and tail[-2].strip() == "exit $?" and tail[-3].strip() == "esac"
    assert any(ln.strip() == "{" for ln in tail) and 'case "${1:-}" in' in "\n".join(tail)


def _self_swapping_script(tmp_path, wrapped):
    longer = tmp_path / "longer.sh"
    longer.write_text("#!/usr/bin/env bash\n" + "x=1  # 다음 버전에서 늘어난 줄\n" * 40 + "echo LONGER-VERSION-RAN\n"
                      "if then fi\n", encoding="utf-8")
    dispatch = 'case "${1:-}" in\n    go) swap; echo swapped ;;\nesac\n'
    body = ("#!/usr/bin/env bash\nset -euo pipefail\n"
            f'swap() {{ cat "{longer}" > "$0"; }}\n'
            + ("{\n" + dispatch + "exit $?\n}\n" if wrapped else dispatch))
    script = tmp_path / ("wrapped.sh" if wrapped else "plain.sh")
    script.write_text(body, encoding="utf-8")
    return subprocess.run(["bash", str(script), "go"], capture_output=True, text=True, timeout=30)


def test_self_overwrite_during_run_does_not_end_in_syntax_error(tmp_path):
    """실제 재현: 실행 중에 자기 파일을 더 긴 버전으로 바꾸는 스크립트. 묶지 않으면 바뀐 파일의 중간부터 읽어 실행·문법 오류,
    { case …; exit; } 로 묶으면 깨끗이 끝난다(vm.sh 와 같은 구조)."""
    plain = _self_swapping_script(tmp_path, wrapped=False)
    assert plain.returncode != 0 or "LONGER" in plain.stdout                  # 대조군: 예전 구조는 깨진다
    r = _self_swapping_script(tmp_path, wrapped=True)
    assert r.returncode == 0 and "swapped" in r.stdout and "LONGER" not in r.stdout, (r.stdout, r.stderr)


def _gate_fns(app_dir, body, run_app_stub):
    """vm.sh 의 변경관리 함수(change_gate·change_record·record_integrity_baseline)만 떼어 실행 — run_app 은 가짜."""
    src = VM.read_text(encoding="utf-8")
    start = src.index("# $2 = 설치본·스냅샷이 가져온 파일 목록")
    end = src.index("\ncmd_install() {")
    script = ("set -euo pipefail\nsay(){ echo \"SAY $*\"; }\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              "die(){ echo \"DIE $*\" >&2; exit 1; }\n" + run_app_stub + "\n" + src[start:end]
              + f'\nAPP_DIR="{app_dir}"\n' + body)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_update_runs_the_change_gate_of_the_deployed_code_and_keeps_the_change_id(tmp_path):
    app = tmp_path / "stock-backend"
    app.mkdir()
    (app / "run.py").write_text("# old release\n", encoding="utf-8")
    calls = tmp_path / "calls.txt"
    stub = f'run_app(){{ printf "%s\\n" "$*" >> "{calls}"; printf "변경관리(record): 허용 — 기록 모드\\nCHANGE_ID=12\\n"; return ${{GATE_RC:-0}}; }}'
    r = _gate_fns(app, 'change_gate /tmp/inst.sh; echo "ID=[$CHANGE_ID]"', stub)
    assert r.returncode == 0 and "ID=[]" in r.stdout and not calls.exists()      # 변경관리 이전 버전에서 올라오는 첫 업데이트
    (app / "run.py").write_text('elif cmd == "change-gate":\n"change-applied"\n"--only-listed"\n', encoding="utf-8")
    r = _gate_fns(app, 'change_gate /tmp/inst.sh; echo "ID=[$CHANGE_ID]"; change_record applied; change_record failed',
                  stub)
    assert r.returncode == 0, r.stderr
    gate_out = r.stdout.split("ID=[12]")[0]                                       # 그 뒤는 가짜 run_app 이 change-applied 에 찍은 것
    assert "ID=[12]" in r.stdout and "    변경관리(record): 허용" in gate_out and "CHANGE_ID=12" not in gate_out
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("run.py change-gate /tmp/inst.sh --by ")
    assert lines[1].startswith("run.py change-applied 12 --by ") and lines[1].endswith(lines[1].split()[-1])
    assert lines[2].endswith("--failed")
    calls.unlink()
    argv_stub = (f'run_app(){{ {{ printf "%s|" "$@"; echo; }} >> "{calls}"; '
                 'printf "변경관리(record): 허용\\nCHANGE_ID=13\\n"; }')
    r = _gate_fns(app, 'CHANGE_REASON="로그인 장애 긴급 수정 배포"; change_gate /tmp/inst.sh', argv_stub)
    assert r.returncode == 0, r.stderr
    assert "|--emergency|로그인 장애 긴급 수정 배포|" in calls.read_text(encoding="utf-8")   # 사유는 한 인자 그대로
    r = _gate_fns(app, "change_gate /tmp/inst.sh", stub.replace("${GATE_RC:-0}", "3"))
    assert r.returncode != 0 and "변경관리 확인에서 멈췄습니다" in r.stderr                      # 막히면 서비스를 멈추기 전에 끝


def test_baseline_after_update_or_rollback_takes_only_listed_files(tmp_path):
    app = tmp_path / "stock-backend"
    app.mkdir()
    (app / "run.py").write_text('"integrity-baseline" "--only-listed"\n', encoding="utf-8")
    calls = tmp_path / "calls.txt"
    lst = tmp_path / "list.txt"
    lst.write_text("app/api.py\n", encoding="utf-8")
    stub = f'run_app(){{ printf "%s\\n" "$*" >> "{calls}"; }}'
    r = _gate_fns(app, f'record_integrity_baseline rollback "{lst}"; record_integrity_baseline update /nonexistent', stub)
    assert r.returncode == 0, r.stderr
    a, b = calls.read_text(encoding="utf-8").splitlines()
    assert a == f"run.py integrity-baseline --release rollback --only-listed {lst}"
    assert b == "run.py integrity-baseline --release update"                      # 목록이 없으면 예전처럼 전체
