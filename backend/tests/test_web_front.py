"""웹 화면(web/ — 팀 확정 디자인)과 서버가 맞물리는지 — 화면이 보내는 값이 서버 규칙과 어긋나면 기능이 조용히 깨진다.

  · 설문: 화면의 문항 은행(문구·순서) = 서버 은행(Q1~Q20) — 화면은 '문항 번호·보기 번호'만 보내고 채점은 서버
  · 용어사전: 화면 용어 이름 전부 ⊂ 서버 사전(learn_terms.json) — '이해했어요' 저장이 404 로 깨지지 않게
  · 오늘의 용어: 서버가 고른 용어가 화면 사전에 있다(홈 카드 → learn.html?term= 이 열리게)
  · 퀴즈: 서버 문제은행 사본의 원본 해시 = 화면 파일(tools/build_daily_quiz.py 를 잊지 않게)
  · 실습: 화면 기법 이름 = 서버 허용 목록
  · 공용 헤더 마크업(잔액·마이페이지·로그인/로그아웃)이 모든 화면에 같은 모양으로 있다(런타임이 찾는다)
"""
import hashlib
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import learn, practice_records, quiz, survey

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = {p.stem: p.read_text(encoding="utf-8") for p in sorted(WEB.glob("*.html"))}
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node 없음")


def _node(code: str):
    out = subprocess.run([NODE, "-"], input=code, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_survey_bank_on_screen_is_the_server_bank():
    m = re.search(r"const SURVEY_BANK = (\[.*?\n        \]);", PAGES["index"], re.S)
    bank = _node(f"process.stdout.write(JSON.stringify({m.group(1)}));")
    assert len(bank) == len(survey.BANK) == 20
    for i, (front, server) in enumerate(zip(bank, survey.BANK)):
        assert server["id"] == f"Q{i + 1}"                            # 화면은 순서로 번호를 붙인다(SURVEY_BANK.forEach)
        assert (front["domain"], front["q"], front["options"]) == (server["domain"], server["q"], server["options"]), i
    assert "SURVEY_BANK.forEach((item, i) => { item.id = 'Q' + (i + 1); });" in PAGES["index"]
    assert "id: q.id, answer: surveyAnswers[i]" in PAGES["index"]   # 점수 대신 답만 보낸다


@needs_node
def test_every_screen_term_can_be_saved_on_the_server():
    names = _node((WEB / "assets" / "terms.js").read_text(encoding="utf-8")
                  + "\nprocess.stdout.write(JSON.stringify(TERMS.map((t) => t.name)));")
    assert len(names) == len(set(names)) >= 169
    missing = sorted(set(names) - learn.term_names())
    assert not missing, f"서버 사전(learn_terms.json)에 없는 화면 용어 — tools/build_learn_terms.py 다시: {missing[:10]}"
    team = {t["name"] for t in learn.dictionary()["items"] if t["src"] == "team"}
    assert team == set(names)                                          # 서버 사전의 팀 카드 = 화면 사전
    for d in range(0, 400, 37):                                       # 오늘의 용어는 화면 사전에서만
        assert learn.today_learn(date(2026, 1, 1) + timedelta(days=d))["term"] in team


def test_quiz_bank_copy_matches_screen_file():
    data = json.loads((ROOT / "app" / "data" / "daily_quiz.json").read_text(encoding="utf-8"))
    src = (WEB / "assets" / "daily-quiz.js").read_bytes()
    assert data["source_sha256"] == hashlib.sha256(src).hexdigest(), "화면 문제은행이 바뀜 — python tools/build_daily_quiz.py"
    assert data["count"] == len(quiz.bank()) and data["case"] >= 3 and data["deep"] >= 2


def test_practice_techniques_match_server_allowlist():
    techs = re.findall(r"^    ([a-z]+): \{\n        id: '\1'", PAGES["practice"], re.M)
    assert tuple(techs) == practice_records.TECHS
    names = dict(re.findall(r"(golden|rsi|pullback|support): '([^']+실습)'", PAGES["mypage"]))
    assert set(names) == set(practice_records.TECHS)                  # 마이페이지 실습 이력 이름표도 같은 4개


def test_shared_header_markup_on_every_page():
    for name, src in PAGES.items():
        for needle in ('class="header-balance"', 'id="headerBalanceAmt"', 'class="header-btn header-btn-mypage"',
                       'class="header-btn header-btn-logout" data-on="headerLogout()"', 'class="header-btn-label">로그아웃<'):
            assert needle in src, (name, needle)


def test_pages_keep_guest_data_local_and_account_data_on_server():
    # 로그인 상태면 브라우저에 쓰지 않는다(공용 PC) — 쓰는 곳은 '로그인 전' 조건 안에서만
    assert "if (!OOS.loggedIn()) OOS.writeJSON(LKEY" in PAGES["learn"]
    assert "else OOS.writeJSON('outofstock_risk_profile'" in PAGES["index"]
    assert "if (!OOS.loggedIn()) { OOS.writeJSON(REC_KEY, r); return; }" in PAGES["practice"]
    for name, src in PAGES.items():                                   # 시안 전용 가짜 값(잔액·연속 출석)은 어디서도 안 쓴다
        assert "outofstock_balance" not in src and "outofstock_streak" not in src, name


# ── 팀 저장소 → web/ 가져오기(tools/sync_web.py) ─────────────────────────────
def _sync_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_web", ROOT / "tools" / "sync_web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sync_web_takes_screens_only_and_refuses_the_old_design(tmp_path, capsys):
    sw = _sync_tool()
    team, web = tmp_path / "OUTOFSTOCK", tmp_path / "web"
    for rel, body in {"index.html": '<button data-on="go()">x</button><script src="assets/oos/runtime.js"></script>',
                      "README.md": "# 팀", "자료조사/설문.docx": "doc", "assets/terms.js": "var TERMS=[]",
                      "assets/.DS_Store": "x", "images/a.png": "png"}.items():
        (team / rel).parent.mkdir(parents=True, exist_ok=True)
        (team / rel).write_text(body, encoding="utf-8")
    (web / "assets").mkdir(parents=True)
    (web / "README.md").write_text("서버 안내", encoding="utf-8")
    (web / "assets" / "old.js").write_text("old", encoding="utf-8")

    assert sw.main([str(team), "--web", str(web), "--apply"]) == 2                  # 연동판(runtime.js)이 없는 옛 시안 → 거부
    assert not (web / "index.html").exists()

    (team / "assets/oos").mkdir()
    (team / "assets/oos/runtime.js").write_text("/* runtime */", encoding="utf-8")
    assert sw.main([str(team), "--web", str(web)]) == 0                              # 미리 보기 — 아무것도 안 바뀜
    assert not (web / "index.html").exists()
    assert sw.main([str(team), "--web", str(web), "--apply", "--prune"]) == 0
    got = sorted(p.relative_to(web).as_posix() for p in web.rglob("*") if p.is_file())
    assert got == ["README.md", "assets/oos/runtime.js", "assets/terms.js", "images/a.png", "index.html"]
    assert (web / "README.md").read_text(encoding="utf-8") == "서버 안내"             # 서버 쪽 안내 문서는 그대로
    out = capsys.readouterr().out
    assert "build_learn_terms.py" in out                                           # 용어가 바뀌면 서버 사본 재생성 안내

    (team / "index.html").write_text('<a onclick="x()">x</a><script src="https://cdn.example/x.js"></script>', encoding="utf-8")
    assert sw.main([str(team), "--web", str(web)]) == 0
    out = capsys.readouterr().out
    assert "인라인 이벤트" in out and "외부 스크립트" in out and "runtime.js 를 불러오지 않는다" in out
