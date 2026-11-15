"""웹 접근성 — 공공기관 웹 접근성 기준(KWCAG 2.2, WCAG 2.1 AA 대응)의 정적 회귀 검사(web/ 다섯 화면 + 공용 런타임).
실측(axe-core, tools/e2e_web.py)과 별개로 같은 문제가 다시 들어오지 않도록 핵심 규칙만 소스에서 확인한다."""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = {p.stem: p.read_text(encoding="utf-8") for p in sorted(WEB.glob("*.html"))}
RUNTIME = (WEB / "assets" / "oos" / "runtime.js").read_text(encoding="utf-8")


def _lum(hex_color):
    h = hex_color.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    hi, lo = sorted([_lum(a), _lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _token(src, name):
    m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", src)
    assert m, name
    return m.group(1)


@pytest.mark.parametrize("page", sorted(PAGES))
def test_text_tokens_meet_wcag_aa_contrast(page):
    # 1.4.3 텍스트 명도 대비 4.5:1 — 시안의 --text-muted #94a3b8 은 흰 배경 2.56:1 → 같은 계열 #64748b
    src = PAGES[page]
    for fg in ("text-primary", "text-secondary", "text-muted"):
        for bg in ("card-bg", "bg-color", "item-bg"):
            ratio = _contrast(_token(src, fg), _token(src, bg))
            assert ratio >= 4.5, f"{page}: --{fg} on --{bg}: {ratio:.2f}:1"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_form_controls_have_accessible_names(page):
    # 1.3.1/4.1.2 — placeholder 만으로는 레이블이 아니다(입력 시작하면 사라짐)
    src, bad = PAGES[page], []
    for tag in re.findall(r"<(?:input|textarea|select)\b[^>]*>", src):
        if 'type="hidden"' in tag:
            continue
        m = re.search(r'id="([^"]+)"', tag)
        before = src[max(0, src.find(tag) - 200):src.find(tag)]
        labelled = ("aria-label=" in tag or "aria-labelledby=" in tag or (m and f'for="{m.group(1)}"' in src)
                    or re.search(r"<label[^>]*>[^<]*$", before))
        if not labelled:
            bad.append(tag[:90])
    assert not bad, bad


def test_pages_declare_korean_and_viewport():
    for name, src in PAGES.items():
        assert '<html lang="ko">' in src and 'name="viewport"' in src, name


def test_click_only_elements_are_keyboard_operable():
    # 2.1.1 키보드 — 클릭 동작(data-on)만 붙은 div·span·href 없는 a 는 Tab 이 닿지 않는다 → 런타임이 tabindex·role 보강 +
    # Enter/Space 를 클릭으로(화면 자체 키 처리와 이중 실행 방지)
    assert "const KBD_SEL = 'div[data-on],span[data-on],li[data-on],img[data-on],b[data-on],a[data-on]:not([href])'" in RUNTIME
    assert "el.tabIndex = 0" in RUNTIME and "setAttribute('role', 'button')" in RUNTIME
    assert "!e.defaultPrevented && (e.key === 'Enter' || e.key === ' ')" in RUNTIME
    assert "new MutationObserver" in RUNTIME                              # 나중에 그려지는 화면 조각도


def test_dialogs_are_announced_and_escape_closes():
    # 런타임 대화 상자(비밀번호 재확인·2단계 인증·송금): role=dialog + aria-modal + 제목 연결 · 오류는 role=alert · Esc 로 닫힘
    assert "role: 'dialog', 'aria-modal': 'true', 'aria-labelledby'" in RUNTIME
    assert "className: 'oos-dlg-msg', role: 'alert'" in RUNTIME and "e.key === 'Escape'" in RUNTIME
    assert "back.focus()" in RUNTIME                                        # 닫으면 원래 자리로 초점 복귀
    for name in ("index", "mypage"):                                       # 팀 모달도 role=dialog
        assert 'role="dialog" aria-modal="true"' in PAGES[name], name
