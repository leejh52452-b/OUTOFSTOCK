"""API 서버 로그 — uvicorn 은 자기 로거만 설정해서, 예전엔 앱 로그(app.*)의 INFO 가 버려지고
WARNING 도 시각·레벨·모듈 없이 찍혔다(VM journald 에서 '언제·어디서'를 알 수 없음). 시각은 서버 시간대와 무관하게 KST."""
import os
import subprocess
import sys
import time
from pathlib import Path

from app import logging_conf

ROOT = Path(__file__).resolve().parents[1]

_CHILD = r"""
import logging
from fastapi.testclient import TestClient
from app.api import app
with TestClient(app):                                   # lifespan 실행(uvicorn 이 띄울 때와 같은 경로)
    logging.getLogger("app.probe").info("probe-info crtfc_key=abcdef123456")
"""


def test_api_process_emits_app_info_logs_with_level_module_and_redaction():
    from conftest import TEST_DATABASE_URL
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "WARMUP_ON_START": "false", "TZ": "UTC",
           "LOG_LEVEL": "INFO"}
    r = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    line = next((ln for ln in r.stderr.splitlines() if "probe-info" in ln), None)
    assert line, r.stderr[-2000:]                          # 예전: INFO 가 아예 안 찍힘
    assert "| INFO    | app.probe |" in line
    assert "abcdef123456" not in line and "crtfc_key=***" in line   # 키 가리기도 그대로


def test_log_time_is_kst_regardless_of_server_timezone():
    assert time.strftime("%Y-%m-%d %H:%M", logging_conf._kst(0)) == "1970-01-01 09:00"
