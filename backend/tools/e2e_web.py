"""브라우저 E2E — 웹 화면(web/ 다섯 장)을 실제 크로미움으로: 쿠키 세션 · CSRF · 토큰 갱신 · 추가 인증 · 실시간 알림(SSE) ·
CSP/신뢰 타입 · 서버 연동(가입·출석·설문·학습·퀴즈·실습·마이페이지·친구·채팅·송금·탈퇴) · 접근성(axe, 선택).

    python tools/e2e_web.py --base http://127.0.0.1:8000 [--axe axe.min.js] [--shots 폴더] [--chromium 실행 파일]

전제: 서버가 떠 있고, 이 도구가 같은 DB(DATABASE_URL)에 붙을 수 있을 것(시험 계정·토큰 만료 흉내·탈퇴 확인).
확인하는 것
  ① 비로그인: 다섯 화면 콘솔 오류 0 · CSP 위반 0 · 인라인 핸들러 0 · 헤더 '로그인'(잔액 숨김) · 마이페이지 → 로그인 안내
  ② 선언형 동작(data-on): 설문 창 열기·닫기 · 회원가입 창 바깥 클릭으로 닫기(data-on-self)
  ③ 가입(화면) → 바로 로그인: HttpOnly 접근·갱신 쿠키(갱신은 /auth 경로만) · CSRF 쿠키 · SameSite=Strict ·
     '로그인 유지' 해제 = 세션 쿠키 · localStorage 에는 아이디 힌트만(토큰 없음)
  ④ 출석(+보상) · 설문(서버 채점·이력, 브라우저에 성향 저장 안 함) · 용어 '이해했어요'(계정) · 퀴즈(서버 채점 = 화면 채점) · 실습 기록
  ⑤ SSE: 다른 사람이 보낸 송금이 새로 고침 없이 헤더 잔액에 반영
  ⑥ 접근 토큰 만료 → /auth/refresh 로 갱신하고 원래 요청을 다시 보내 로그인 유지
  ⑦ 마이페이지: 활동 현황 · 관심 종목 담기/빼기 · 송금 · 100만원 송금 → 추가 인증 창(틀린 비밀번호는 창 안에서 안내) → 완료
  ⑧ 친구 요청 → 상대 수락 → 1:1 대화(내 메시지는 이스케이프, 상대 메시지는 SSE 로 바로)
     · 양봉이 꾸미기: 화면에서 사기 → 서버 가격 차감·착용 → 마이페이지 프로필 그림
     · 기기 결속 키(DPoP): 세션 ↔ 브라우저 비추출 키 지문, 쿠키만 복사한 다른 기기는 송금·갱신 실패(세션 종료)
  ⑨ 신뢰 타입: 이벤트 핸들러가 든 HTML 주입은 막히고 서버에 위반 보고가 남는다
  ⑩ 로그아웃: 쿠키 삭제 + 서버 세션 폐기 + SSE 닫힘 · ⑪ 탈퇴: 추가 인증 후 계정 삭제
  ⑫ 첫 접속 투자 유의 안내(확인 = 이번 방문 · 30일 동안 안 보기) · 홈 '양봉이 주간 추천'(서버 /yangbong/weekly 값 그대로)
  ⑬ 모의투자(가상 시장 시즌): 비로그인 차트 = 공개된 봉까지만(사전 120 + 서버 시계의 봉 수)·시즌 파일 요청 없음 ·
     로그인 → 화면에서 참가(지갑 −참가금·원장) → 장중이면 화면에서 매수(체결가 = 서버의 그 봉 종가)
  (선택) axe-core: 비로그인 네 화면 + 추가 인증 창 + 양봉이 꾸미기 + 마이페이지 + 첫 접속 안내 + 모의투자 WCAG 2.1 AA
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

PW = "E2e-pass-2026"
PAGES = ("/", "/learn.html", "/news.html", "/practice.html", "/mypage.html", "/stocks.html", "/avatar.html", "/invest.html")
RESULTS: list[tuple[str, bool, str]] = []
AXE_TAGS = "['wcag2a','wcag2aa','wcag21a','wcag21aa']"


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond), str(extra)[:300]))
    print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""), flush=True)


async def main(base: str, axe: str | None, shots: str | None, chromium: str | None):
    from playwright.async_api import async_playwright
    from app import db
    from app.services import auth, quiz, vmarket, wallet
    tag = secrets.token_hex(3)
    ua, ub, uc, ud = f"e2ea{tag}", f"e2eb{tag}", f"e2ec{tag}", f"e2ed{tag}"
    for u in (ub, uc):
        auth.register(u, PW)
    events0 = db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='csp_violation'")[0]["n"]
    axe_src = Path(axe).read_text(encoding="utf-8") if axe else None

    async def bearer(u):
        async with httpx.AsyncClient(base_url=base) as c:
            return (await c.post("/auth/login", json={"username": u, "password": PW})).json()["token"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(**({"executable_path": chromium} if chromium else {}))

        async def new_page(notice_seen=True):
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul")
            if notice_seen:                                 # 첫 접속 투자 유의 안내는 ⑫ 에서 따로 — 나머지 흐름은 확인한 상태로
                await ctx.add_init_script("try { sessionStorage.setItem('outofstock_notice_seen', '1'); } catch (e) {}")
            await ctx.add_init_script("""window.__csp=[]; document.addEventListener('securitypolicyviolation',
                e=>window.__csp.push(e.violatedDirective+' '+(e.blockedURI||'')+' '+(e.sample||'')));""")
            pg = await ctx.new_page()
            pg.errors, pg.dialogs, pg.net = [], [], []
            pg.on("console", lambda m: pg.errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
            pg.on("response", lambda r: pg.net.append((r.request.method, r.url.replace(base, ""), r.status)))

            async def on_dialog(d):
                pg.dialogs.append(d.message)
                await d.accept()
            pg.on("dialog", lambda d: asyncio.ensure_future(on_dialog(d)))
            return ctx, pg

        async def goto(pg, path, wait=900):
            await pg.goto(base + path, wait_until="load")               # SSE 가 열려 있어 networkidle 은 오지 않는다
            await pg.wait_for_timeout(wait)

        async def axe_check(pg, name, selector="document"):
            if not axe_src:
                return
            # add_script_tag(인라인 <script>)는 이 화면의 CSP·신뢰 타입이 막는다(그게 정상) — 개발자 도구 평가로 넣는다
            if not await pg.evaluate("!!window.axe"):
                await pg.evaluate(axe_src)
            for _ in range(50):                                             # 알림(토스트)이 사라지는 중이면 반투명 글자로 잡힌다 — 다 사라진 뒤
                if not await pg.evaluate("!!document.querySelector('.oos-toast.show')"):
                    break
                await pg.wait_for_timeout(100)
            await pg.wait_for_timeout(300)
            v = await pg.evaluate(f"""async()=>{{ const r=await axe.run({selector}, {{runOnly:{{type:'tag',values:{AXE_TAGS}}}}});
                return r.violations.map(x=>x.id+':'+x.nodes.length+' '+x.nodes.slice(0,2).map(n=>n.target.join(' ')).join(' | ')); }}""")
            ok(f"접근성(axe, WCAG 2.1 AA) 위반 0 — {name}", not v, v)

        def real_errors(pg):     # 401 = 위에서 확인하는 만료·추가 인증 응답(브라우저가 '리소스 로드 실패'로 찍는다)
            return [e for e in pg.errors if "status of 401" not in e and "차단된 HTML" not in e and "trusted" not in e.lower()]

        # ① 비로그인 둘러보기 ------------------------------------------------------
        ctx, pg = await new_page()
        for path in PAGES[:4]:
            await goto(pg, path, 700)
            await axe_check(pg, f"비로그인 {path}")
        inline = await pg.evaluate("document.querySelectorAll('[onclick],[onsubmit],[onerror],[onload]').length")
        ok("비로그인: 화면 4개 콘솔 오류 0", not pg.errors, pg.errors[:3])
        ok("비로그인: CSP 위반 0 · 인라인 핸들러 0", not await pg.evaluate("window.__csp") and inline == 0,
           await pg.evaluate("window.__csp"))
        ok("비로그인 헤더: '로그인' · 잔액 숨김", await pg.text_content(".header-btn-logout .header-btn-label") == "로그인"
           and not await pg.locator(".header-balance").is_visible())
        await goto(pg, "/mypage.html", 900)
        ok("비로그인 마이페이지 → 홈 로그인 카드", pg.url.endswith("/index.html#login")
           and await pg.evaluate("document.activeElement && document.activeElement.id") == "loginId", pg.url)

        # ② 선언형 동작 ---------------------------------------------------------------
        await goto(pg, "/")
        await pg.click("#profileGuideCard")
        opened = await pg.locator("#surveyOverlay").is_visible()
        await pg.click("#surveyOverlay .survey-close")
        ok("data-on: 설문 창 열기·닫기", opened and not await pg.locator("#surveyOverlay").is_visible())
        await pg.click("text=회원가입")
        await pg.mouse.click(5, 5)                                          # 창 바깥(배경) — data-on-self
        ok("data-on-self: 회원가입 창 바깥 클릭으로 닫기", not await pg.locator("#signupOverlay").is_visible())
        await ctx.close()

        # ⑫ 첫 접속 투자 유의 안내 · 홈 양봉이 주간 추천 ------------------------------------------------
        ctx, pg = await new_page(notice_seen=False)
        await goto(pg, "/")
        shown = await pg.locator("#noticeOverlay").is_visible()
        focus = await pg.evaluate("document.activeElement && document.activeElement.id")
        await axe_check(pg, "첫 접속 투자 유의 안내", "document.getElementById('noticeOverlay')")
        await pg.click("#noticeConfirm")
        closed = not await pg.locator("#noticeOverlay").is_visible()
        await goto(pg, "/")
        ok("첫 접속 안내: 처음엔 뜨고(확인 버튼에 초점) · '확인했습니다' → 이번 방문 동안 다시 안 뜸",
           shown and focus == "noticeConfirm" and closed and not await pg.locator("#noticeOverlay").is_visible(), (shown, focus, closed))
        await ctx.close()
        ctx, pg = await new_page(notice_seen=False)
        await goto(pg, "/")
        await pg.click("#noticeOverlay .notice-btn.ghost")
        until = int(await pg.evaluate("localStorage.getItem('outofstock_notice_hide_until') || 0"))
        await pg.evaluate("sessionStorage.clear()")                       # 탭을 새로 연 것처럼
        await goto(pg, "/")
        ok("첫 접속 안내: '30일 동안 안 보기' → 약 30일 뒤까지 안 뜸",
           not await pg.locator("#noticeOverlay").is_visible() and 29 * 86400e3 < until - time.time() * 1000 < 31 * 86400e3, until)
        async with httpx.AsyncClient(base_url=base) as c:
            wk = (await c.get("/yangbong/weekly")).json()
        names = await pg.locator("#weeklyPicks .stock-name").all_text_contents()
        btns = await pg.locator(".sim-btn-group").is_visible()
        ok("홈 양봉이 주간 추천 = 서버 /yangbong/weekly(5종목·기준일·승률·최근 바구니)",
           names == [x["name"] for x in wk["picks"]] and len(names) == 5
           and wk["as_of"].replace("-", ".") in await pg.text_content("#picksAsOf")
           and await pg.text_content("#statWin") == f"{round(wk['stats']['wins'] / wk['stats']['weeks'] * 100)}%"
           and (btns is False) == bool(wk["last_basket"]), (names, wk["as_of"], btns))
        if shots:
            await pg.screenshot(path=str(Path(shots) / "e2e_home_yangbong.png"), full_page=True)
        await ctx.close()

        # ⑬ 모의투자(비로그인) — 차트는 공개된 봉까지만, 시즌 파일은 요청하지 않는다 -------------------------------------
        ctx, pg = await new_page()
        await goto(pg, "/invest.html", 1500)
        async with httpx.AsyncClient(base_url=base) as c:
            vs = (await c.get("/vmarket/season")).json()
        lens = await pg.evaluate("(() => { const d = window.__VM.DATA[window.__VM.CL().id]; return d ? d.stocks.map(s => s.c.length) : []; })()")
        files = [u for _, u, _ in pg.net if "season_" in u or u.startswith("/data/")]
        ok("모의투자(비로그인): 종목 5개 · 봉 = 사전 120 + 서버가 공개한 봉 · 시즌 파일 요청 없음 · 참가는 로그인 안내",
           vs["available"] and lens == [120 + vs["revealed"]] * 5 and not files
           and await pg.locator(".js-stocks .stock-tab").count() >= 5
           and (vs["clock"]["phase"] == "weekend" or await pg.locator("#joinLogin").is_visible()), (lens, vs["revealed"], files))
        ok("모의투자: 콘솔 오류 0 · CSP 위반 0", not real_errors(pg) and not await pg.evaluate("window.__csp"),
           (real_errors(pg)[:3], await pg.evaluate("window.__csp")))
        await axe_check(pg, "모의투자(비로그인)")
        if shots:
            await pg.screenshot(path=str(Path(shots) / "e2e_invest.png"), full_page=True)
        await ctx.close()

        # ③ 가입(화면) → 바로 로그인 ---------------------------------------------------------
        ctx, pg = await new_page()
        await goto(pg, "/")
        await pg.click("text=회원가입")
        for sel, v in (("#suId", ua), ("#suPw", PW), ("#suPw2", PW)):
            await pg.fill(sel, v)
        await pg.click("#signupForm .auth-submit")
        await pg.wait_for_selector("#loginDoneView", state="visible", timeout=10000)
        await pg.wait_for_timeout(800)
        cookies = {c["name"]: c for c in await ctx.cookies()}
        store = await pg.evaluate("Object.keys(localStorage)")
        ok("가입 → 로그인 카드(내 아이디)", await pg.text_content("#loginDoneId") == ua)
        ok("로그인: 접근·갱신 쿠키 HttpOnly, 갱신 쿠키는 /auth 경로만",
           cookies.get("oos_at", {}).get("httpOnly") and cookies.get("oos_rt", {}).get("httpOnly")
           and cookies.get("oos_rt", {}).get("path") == "/auth", {k: (v["httpOnly"], v["path"]) for k, v in cookies.items()})
        ok("로그인: CSRF 쿠키는 스크립트가 읽을 수 있음 · SameSite=Strict",
           "oos_csrf" in cookies and not cookies["oos_csrf"]["httpOnly"]
           and all(c["sameSite"] == "Strict" for c in cookies.values() if c["name"].startswith("oos_") and c["name"] != "oos_gid"))
        ok("'로그인 유지' 해제(기본) = 세션 쿠키", cookies["oos_at"]["expires"] == -1 and cookies["oos_rt"]["expires"] == -1)
        ok("localStorage 에 토큰 없음(아이디 힌트만)", store == ["oos_user"], store)

        # ④ 출석 · 설문 · 학습 · 퀴즈 · 실습 ---------------------------------------------------
        bal0 = await pg.text_content("#headerBalanceAmt")
        await pg.click("#btnAttendance")
        await pg.wait_for_selector("#btnAttendance:disabled", timeout=5000)   # wait_for_function(문자열)은 CSP 가 막는다(eval)
        await pg.wait_for_timeout(500)
        bal1 = await pg.text_content("#headerBalanceAmt")
        ok("출석 → 헤더·카드 잔액 증가", bal1 != bal0 and await pg.text_content("#loginDoneBalance") == bal1, (bal0, bal1))
        await pg.click("#profileGuideCard")
        for _ in range(10):
            await pg.click("#surveyQuestionView .survey-option >> nth=0")
            await pg.click("#surveyQuestionView .survey-btn.primary")
        await pg.wait_for_selector("#surveyResultView", state="visible")
        sv = await pg.evaluate("OOS.api('/me/survey')")
        ok("설문: 서버 채점·이력(안정형) · 브라우저에 성향 저장 안 함",
           sv["survey"]["type"] == "안정형" and await pg.evaluate("localStorage.getItem('outofstock_risk_profile')") is None, sv)
        await pg.click("#surveyResultView .survey-btn.primary")
        await goto(pg, "/learn.html?term=" + "골든크로스")
        await pg.click("#termDetail .learn-btn")
        await pg.wait_for_timeout(600)
        prog = await pg.evaluate("OOS.api('/learn/progress')")
        ok("용어 '이해했어요' → 계정 저장", "골든크로스" in prog["done"], prog["done"])
        bal0 = await pg.evaluate("OOS.balance")
        await pg.click("[data-act=quiz-start]")
        key = [q["answer"] for q in quiz.build(await pg.evaluate("quiz.date"))]
        for i in range(5):                                                # 앞 셋은 정답, 뒤 둘은 오답
            k = key[i] if i < 3 else (key[i] + 1) % 4
            await pg.click(f".quiz-opt[data-k='{k}']")
            await pg.click("[data-act=quiz-next], [data-act=quiz-submit]")
        await pg.wait_for_timeout(1000)
        qz = await pg.evaluate("OOS.api('/learn/quiz')")
        ok("퀴즈: 서버가 다시 만들어 채점 = 화면 채점(3/5)", qz["done"] and qz["score"] == 3 == await pg.evaluate("quizScore()"), qz)
        want = 3 * qz["reward_per_correct"]
        res = await pg.inner_text("#quizBody")
        ok("퀴즈 보상: 서버가 지갑에 지급 → 결과·헤더 잔액 반영(정답 3 × 단가)",
           qz["reward"] == want > 0 and f"+{want:,}원" in res and await pg.evaluate("OOS.balance") == (bal0 or 0) + want,
           {"reward": qz["reward"], "before": bal0, "after": await pg.evaluate("OOS.balance"), "text": res[-80:]})
        await pg.keyboard.press("Escape")
        await goto(pg, "/practice.html")
        await pg.evaluate("__P.startPlay('golden', {skipTutorial: true}); __P.finish('end')")
        await pg.wait_for_timeout(1500)
        pr = await pg.evaluate("OOS.api('/practice/records')")
        ok("실습 한 판 → 계정 기록", pr["summary"].get("golden", {}).get("tries") == 1, pr["summary"])

        # ⑤ SSE — 다른 사람의 송금이 새로 고침 없이 반영 ---------------------------------------------
        for _ in range(30):
            if await pg.evaluate("OOS.balance") is not None:
                break
            await pg.wait_for_timeout(200)
        before = await pg.evaluate("OOS.balance")
        async with httpx.AsyncClient(base_url=base) as c:
            r = await c.post("/transfer", json={"to": ua, "amount": 1234, "memo": "e2e"},
                             headers={"Authorization": f"Bearer {await bearer(uc)}"})
        t0 = time.time()
        while time.time() - t0 < 6 and await pg.evaluate("OOS.balance") == before:
            await pg.wait_for_timeout(100)
        after = await pg.evaluate("OOS.balance")
        ok("SSE: 받은 송금이 헤더 잔액에 바로 반영", r.status_code == 200 and after == before + 1234
           and f"{after:,}" in (await pg.text_content("#headerBalanceAmt")), (r.status_code, before, after))

        # ⑥ 접근 토큰 만료 → 갱신 후 재시도 ---------------------------------------------------------
        db.execute("UPDATE auth_token SET expires_at = now() - interval '1 minute' WHERE session_id IN "
                   "(SELECT session_id FROM auth_session WHERE username=:u)", {"u": ua})
        pg.net.clear()
        await goto(pg, "/mypage.html", 1800)
        calls = [(m, u.split("?")[0], s) for m, u, s in pg.net if u.startswith(("/auth/", "/me"))]
        ok("토큰 만료: 401 → /auth/refresh 200 → 다시 200(로그인 유지)",
           ("POST", "/auth/refresh", 200) in calls and ("GET", "/me", 200) in calls
           and any(s == 401 for _, _, s in calls) and await pg.evaluate("OOS.user") == ua, calls[:8])

        # ⑦ 마이페이지 -----------------------------------------------------------------------
        stats = [re.sub(r"\D", "", s.split("/")[0]) for s in await pg.locator(".stat-tile-val").all_text_contents()]
        ok("활동 현황(학습1·실습0·관심0·퀴즈1·출석1)", stats == ["1", "0", "0", "1", "1"], stats)
        await pg.click("[data-tab=secWatch]")
        await pg.fill("#watchQ", "삼성")
        await pg.wait_for_selector("#watchSuggest .watch-item", timeout=6000)
        await pg.click("#watchSuggest .watch-item >> nth=0")
        await pg.wait_for_selector("#watchList .watch-item", timeout=6000)
        added = await pg.locator("#watchList .watch-item").count()
        await pg.click("#watchList .wi-remove")
        await pg.wait_for_timeout(800)
        ok("관심 종목 담기·빼기", added == 1 and await pg.locator("#watchList .watch-item").count() == 0)
        await pg.click("[data-act=transfer]")
        await pg.fill(".oos-dlg input[name=to]", ub)
        await pg.fill(".oos-dlg input[name=amount]", "1,000")
        await pg.click(".oos-dlg .oos-dlg-ok")
        await pg.wait_for_selector(".oos-dlg-overlay", state="detached", timeout=8000)
        await pg.wait_for_timeout(700)
        ok("송금 → 내 활동에 −1,000원", "−1,000원" in await pg.text_content("#secActivity"))
        await pg.click("[data-act=transfer]")
        await pg.fill(".oos-dlg input[name=to]", ub)
        await pg.fill(".oos-dlg input[name=amount]", "1000000")
        await pg.click(".oos-dlg .oos-dlg-ok")
        await pg.wait_for_selector(".oos-dlg input[name=password]", timeout=8000)
        await axe_check(pg, "추가 인증 창", "[...document.querySelectorAll('.oos-dlg-overlay')].pop()")
        if shots:
            await pg.screenshot(path=str(Path(shots) / "e2e_stepup.png"))
        top = pg.locator(".oos-dlg-overlay").last
        await top.locator("input[name=password]").fill("wrong-pass-1")
        await top.locator(".oos-dlg-ok").click()
        try:                                                              # 비밀번호 확인(해시)은 수백 ms — 고정 대기 대신 안내가 뜰 때까지
            await top.locator(".oos-dlg-msg").filter(has_text=re.compile(r"\S")).wait_for(timeout=8000)
        except Exception:                                                 # noqa: BLE001 — 아래 ok() 가 실패로 보고
            pass
        wrong = await top.locator(".oos-dlg-msg").text_content()
        await top.locator("input[name=password]").fill(PW)
        await top.locator(".oos-dlg-ok").click()
        await pg.wait_for_selector(".oos-dlg-overlay", state="detached", timeout=10000)
        await pg.wait_for_timeout(700)
        sent = db.query("SELECT COUNT(*) AS n FROM journal_entry WHERE amount = -1000000 AND account = "
                        "(SELECT account_no FROM wallet WHERE username=:u)", {"u": ua})[0]["n"]
        ok("100만원 송금: 추가 인증 창 — 틀린 비밀번호는 창 안에서 안내, 맞으면 원래 송금 완료",
           wrong and sent == 1 and "−1,000,000원" in await pg.text_content("#secActivity"), (wrong, sent))

        # ⑧ 친구 → 대화 ----------------------------------------------------------------------
        await pg.click("#friendTeaser")
        await pg.click("[data-social-tab=find]")
        await pg.fill("#findQ", ub)
        await pg.click(f"#findList [data-user='{ub}']")
        await pg.wait_for_timeout(600)
        tb = await bearer(ub)
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {tb}"}) as c:
            acc = await c.post(f"/friends/{ua}/accept")
            await pg.wait_for_timeout(1000)
            await pg.click("[data-social-tab=friends]")
            await pg.click(f"#socialBody [data-act=chat-open][data-user='{ub}']")
            await pg.fill("#chatInput", "<img src=x onerror=alert(1)>안녕")
            await pg.press("#chatInput", "Enter")
            await pg.wait_for_selector(".chat-msg.mine", timeout=6000)
            await c.post(f"/chat/{ua}/messages", json={"body": "반가워요"})
        try:
            await pg.wait_for_selector(".chat-msg:not(.mine)", timeout=8000)
            got = True
        except Exception:                                                 # noqa: BLE001
            got = False
        ok("친구 요청 → 수락 → 대화: 내 메시지는 글자 그대로(이스케이프), 상대 메시지는 SSE 로 바로",
           acc.status_code == 200 and "<img" in await pg.text_content(".chat-msg.mine")
           and await pg.locator(".chat-msg img").count() == 0 and got)
        await pg.click(".social-close")

        # ⑧-2 양봉이 꾸미기 — 화면에서 사기(확인 창) → 서버 가격으로 차감·착용 → 마이페이지 프로필 그림 ------------------
        await goto(pg, "/avatar.html", 1200)
        await axe_check(pg, "양봉이 꾸미기(로그인)")
        bal_a = await pg.evaluate("OOS.balance")
        await pg.click('.av-item:nth-child(1) .av-act [data-act="buy"]')                 # 모자 탭 서민 등급(1만)
        await pg.click(".oos-dlg .oos-dlg-ok")
        await pg.wait_for_selector('.av-item:nth-child(1).on', timeout=8000)
        row = db.query("SELECT price, equipped FROM avatar_closet WHERE username=:u AND item_id='hat-1'", {"u": ua})
        led = db.query("SELECT amount FROM wallet_ledger WHERE username=:u AND reason='avatar_buy'", {"u": ua})
        ok("양봉이 꾸미기: 화면에서 사기 → 서버 가격(1만) 차감 · 바로 착용 · 헤더 잔액 반영",
           row and row[0]["price"] == 10_000 and row[0]["equipped"] and [r["amount"] for r in led] == [-10_000]
           and await pg.evaluate("OOS.balance") == bal_a - 10_000, (row, led, bal_a, await pg.evaluate("OOS.balance")))
        if shots:
            await pg.screenshot(path=str(Path(shots) / "e2e_avatar.png"), full_page=True)
        await goto(pg, "/mypage.html", 1500)
        ok("마이페이지 프로필 그림 = 꾸민 양봉이(산 옷을 입은 모습)",
           (await pg.get_attribute("#profileAvatarImg", "src") or "").startswith("data:image/svg+xml"))

        # ⑬-2 모의투자(로그인) — 화면에서 참가(지갑 출금) → 장중이면 매수(체결가 = 서버의 그 봉 종가) ----------------------
        wallet.credit_once(ua, 2_000_000, "iap", memo="E2E 모의투자 참가금")
        await goto(pg, "/invest.html", 1800)
        vcl = vmarket.clock_of()
        if vcl["phase"] == "weekend" or not vmarket.payload(vcl["id"]):
            ok("모의투자(로그인): 주말·데이터 없음 — 참가 확인 건너뜀(시험은 tests/test_vmarket.py)", True)
        else:
            bal_v = await pg.evaluate("OOS.balance")
            await pg.fill("#entryInput", "1000000")
            await pg.click("#joinBtn")
            await pg.wait_for_selector("#orderCard", state="visible", timeout=8000)
            ent = db.query("SELECT entry, cash FROM vmarket_entry WHERE username=:u AND season_id=:s", {"u": ua, "s": vcl["id"]})
            led = db.query("SELECT amount FROM wallet_ledger WHERE username=:u AND reason='vmarket_entry'", {"u": ua})
            ok("모의투자: 화면에서 참가 → 참가금 100만 원 지갑 출금(원장) · 헤더 잔액 반영",
               ent and ent[0]["entry"] == 1_000_000 and [r["amount"] for r in led] == [-1_000_000]
               and await pg.evaluate("OOS.balance") == bal_v - 1_000_000, (ent, led, bal_v))
            if vmarket.clock_of()["phase"] == "open":
                await pg.fill("#qtyInput", "1")
                await pg.click("#orderBtn")
                await pg.wait_for_timeout(1200)
                tr = db.query("SELECT stock_idx, price, bar FROM vmarket_trade WHERE username=:u", {"u": ua})
                want = tr and vmarket.payload(vcl["id"])["stocks"][tr[0]["stock_idx"]]["candles"][119 + tr[0]["bar"]][3]
                ok("모의투자: 화면에서 매수 → 체결가 = 서버가 정한 그 봉 종가", tr and tr[0]["price"] == want, (tr, want))
            else:
                ok("모의투자: 장 시간이 아니라 주문 버튼 비활성(체결 확인은 tests/test_vmarket.py)",
                   await pg.locator("#orderBtn").is_disabled())

        # ⑧-3 기기 결속 키(DPoP) — 로그인한 브라우저의 비추출 키로 서명해야 돈·갱신이 된다. 쿠키만 훔친 '다른 기기'는 실패 ----------
        uk = f"e2ek{tag}"
        kctx, kpg = await new_page()
        await goto(kpg, "/")
        await kpg.evaluate("([u, p]) => OOS.register(u, p)", [uk, PW])
        jk = db.query("SELECT dpop_jkt FROM auth_session WHERE username=:u AND revoked_at IS NULL", {"u": uk})
        ok("기기 결속: 로그인 세션에 브라우저 키 지문(비추출 ECDSA P-256)", jk and len(jk[0]["dpop_jkt"] or "") == 43, jk)
        sent = await kpg.evaluate("u => OOS.api('/transfer', {method: 'POST', headers: {'Idempotency-Key': OOS.idemKey()},"
                                  " body: {to: u, amount: 1000}}).then(() => 'ok').catch((e) => e.code)", ua)
        thief = await browser.new_context(base_url=base)
        await thief.add_cookies(await kctx.cookies())
        tpg = await thief.new_page()
        await tpg.goto(base + "/index.html")
        stolen = await tpg.evaluate('''async (u) => {
            const csrf = (document.cookie.match(/(?:^|; )(?:__Host-)?oos_csrf=([^;]+)/) || [])[1] || '';
            const h = {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf), 'X-Client': 'web'};
            const t = await fetch('/transfer', {method: 'POST', credentials: 'same-origin', headers: {...h, 'Idempotency-Key': 'thief-1'},
                                               body: JSON.stringify({to: u, amount: 1000})});
            const r = await fetch('/auth/refresh', {method: 'POST', credentials: 'same-origin', headers: h});
            return [t.status, (await t.json()).code, r.status, (await r.json()).code];
        }''', ua)
        alive = db.query("SELECT COUNT(*) AS n FROM auth_session WHERE username=:u AND revoked_at IS NULL", {"u": uk})[0]["n"]
        ok("쿠키 탈취(다른 기기): 송금 401 dpop_required · 갱신 401 → 세션 종료 / 같은 기기는 송금 성공",
           sent == "ok" and stolen[:2] == [401, "dpop_required"] and stolen[2] == 401 and alive == 0, (sent, stolen, alive))
        await thief.close()
        await kctx.close()

        # ⑨ 신뢰 타입 — 핸들러가 든 HTML 주입은 막히고 보고된다 ------------------------------------------
        blocked = await pg.evaluate("""()=>{ try{ document.body.insertAdjacentHTML('beforeend','<img src=x onerror=alert(1)>'); return false; }
                                            catch(e){ return e instanceof TypeError; } }""")
        await pg.wait_for_timeout(900)
        events1 = db.query("SELECT COUNT(*) AS n FROM security_event WHERE kind='csp_violation'")[0]["n"]
        ok("신뢰 타입: on* 핸들러 주입 차단 + 서버에 위반 보고", blocked and events1 > events0, (blocked, events0, events1))
        await axe_check(pg, "마이페이지(로그인)")
        if shots:
            await pg.screenshot(path=str(Path(shots) / "e2e_mypage.png"), full_page=True)
        ok("로그인 상태 화면들: 콘솔 오류 0 · CSP 위반 0(주입 시험 제외)",
           not real_errors(pg) and not [v for v in await pg.evaluate("window.__csp") if "trusted-types" not in v],
           real_errors(pg)[:3])

        # ⑩ 로그아웃(헤더) -------------------------------------------------------------------
        await pg.click(".header-btn-logout")
        await pg.wait_for_url(re.compile(r"/(index\.html)?(#.*)?$"), timeout=8000)
        await pg.wait_for_timeout(800)
        names = {c["name"] for c in await ctx.cookies()}
        alive = db.query("SELECT COUNT(*) AS n FROM auth_session WHERE username=:u AND revoked_at IS NULL", {"u": ua})[0]["n"]
        ok("로그아웃: 쿠키 삭제 + 서버 세션 폐기 + 헤더 '로그인' + '세션 종료' 안내 없음",
           not ({"oos_at", "oos_rt", "oos_csrf"} & names) and alive == 0
           and await pg.text_content(".header-btn-logout .header-btn-label") == "로그인"
           and not any("세션이 종료" in d for d in pg.dialogs), (names, alive, pg.dialogs[-2:]))
        await ctx.close()

        # ⑪ 탈퇴 — 항상 추가 인증 ----------------------------------------------------------------
        ctx, pg = await new_page()
        await goto(pg, "/")
        await pg.click("text=회원가입")
        for sel, v in (("#suId", ud), ("#suPw", PW), ("#suPw2", PW)):
            await pg.fill(sel, v)
        await pg.click("#signupForm .auth-submit")
        await pg.wait_for_selector("#loginDoneView", state="visible", timeout=10000)
        await goto(pg, "/mypage.html", 1200)
        await pg.click("[data-act=delete-account]")
        await pg.click(".oos-dlg .oos-dlg-ok")                              # 정말 탈퇴?
        await pg.wait_for_selector(".oos-dlg input[name=password]", timeout=8000)
        await pg.fill(".oos-dlg input[name=password]", PW)
        await pg.click(".oos-dlg .oos-dlg-ok")
        await pg.wait_for_url(re.compile(r"index\.html"), timeout=10000)
        gone = not db.query("SELECT 1 FROM app_user WHERE username=:u", {"u": ud})
        ok("탈퇴: 추가 인증 후 계정 삭제 · 홈으로", gone and await pg.evaluate("OOS.user") is None, pg.dialogs[-2:])
        await ctx.close()
        await browser.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--axe")
    ap.add_argument("--shots")
    ap.add_argument("--chromium", default=os.getenv("PW_CHROMIUM"), help="크로미움 실행 파일(설치된 playwright 와 판이 다를 때)")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.base, a.axe, a.shots, a.chromium)))
