# ADR-0018 화면은 그대로, 스크립트 실행 경로만 바꾼다 — CSP nonce · Trusted Types · 선언형 동작(data-on)

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-21 (ADR-0006 의 CSP 를 강화) · **2026-10-27 개정 — 팀 화면 다섯 장으로 확장(ADR-0021)** |
| 범위 | 애플리케이션 — XSS 방어 |
| 코드 | `app/webui.py`(화면 nonce·`PAGE_CSP`) · `app/api.py`(`/csp-report`) · `web/assets/oos/runtime.js`(신뢰 타입 정책·`data-on` 해석기·등록 함수) · `policy.json`(`security.report`) |
| 시험 | `test_web_security.py`(문법·등록 함수·nonce·보고) · `test_accessibility.py` · `tools/e2e_web.py` ①⑨ |

## 맥락
한 화면 앱(팀 UI 확정본)에 인라인 이벤트 핸들러가 약 136곳 있어 CSP 에 `'unsafe-inline'` 이 남아 있었다. 이 상태의 CSP 는
주입된 `<img onerror=…>` 를 막지 못한다. 화면 모양은 팀 결정으로 바꿀 수 없다.

## 결정
- `/app` 응답마다 새 nonce: `script-src 'nonce-…'` · **`script-src-attr 'none'`**(인라인 핸들러 전면 금지) ·
  **`require-trusted-types-for 'script'`** · `object-src 'none'` · `frame-ancestors 'self'` · `report-uri /csp-report`. HTML 은 `no-store`.
- 인라인 핸들러 → `data-on="함수(인자)"` 속성. 작은 해석기(eval·new Function 없음)가 문법을 읽고 **허용 목록(51개)** 에 있는 함수만
  부른다. 캡처 단계 위임 리스너가 클릭·제출을 받아 처리 — 화면 모양·동작은 같다.
- 신뢰 타입 기본 정책: `innerHTML` 로 들어가는 문자열에 `<script`·`on…=`·`javascript:` 가 있으면 거부 + 보고. 사용자 값은 `esc()`.
- `/csp-report` — 브라우저가 자동으로 보내 헤더를 붙일 수 없으므로 정책 동작 `security.report` 만 CSRF 면제(상태 변경 없음),
  IP 당 분당 30건 상한, 쿼리스트링 제거 후 보안 이벤트(`csp_violation`, 20건/10분 → 경보).

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 해시 기반 CSP | 인라인 스크립트가 바뀔 때마다 해시 갱신 — 핸들러 136곳은 해시로 못 막는다 |
| 프레임워크(React 등) 재작성 | 팀 UI 확정본을 바꾸게 된다 — 범위 밖 |
| `'unsafe-inline'` 유지 + 입력 이스케이프만 | 이스케이프 누락 한 곳이 곧 실행 — 실행 경로 자체를 막는 쪽이 견고 |

## 결과
- 인라인 핸들러 0, 화면 48곳 JS 오류 0, CSP 위반 0(비로그인 전체 순회), 추가 인증 창 접근성(axe, WCAG 2.1 AA) 위반 0.
- 새 버튼을 만들 때 함수를 허용 목록에 넣어야 한다(시험이 '목록에 없는 함수'를 잡는다) — 의도된 마찰.

## 검증
`python -m pytest -q tests/test_web_security.py` · `python tools/e2e_web.py --base … --axe axe.min.js`

## 2026-10-27 개정 — 여러 화면(ADR-0021)
- 화면이 한 장(`/app`)에서 팀 확정 다섯 장(`/`·`/learn.html`·`/news.html`·`/practice.html`·`/mypage.html`)으로 바뀌었다. 해석기·신뢰 타입 정책은
  화면마다 복사하지 않고 **공용 런타임 한 파일**(`web/assets/oos/runtime.js`)로 — 모든 화면이 가장 먼저 불러온다.
- 허용 목록 51개(전역 함수 이름) → **화면이 `OOS.actions({...})` 로 등록한 함수만**(전역 `window[이름]` 조회 없음). 시험이 화면별로
  '쓰인 동작 ⊆ 그 화면이 등록한 함수'를 확인한다.
- nonce 는 **인라인 스크립트에만** — 파일 스크립트는 `'self'` 로만 허용해, 외부 `<script src>` 가 끼어들어도 nonce 를 얻지 못한다.
  `trusted-types default`(다른 이름의 정책 생성 금지)·`connect-src 'self'`·`frame-src 'none'` 추가, 결제창(토스) 출처 제거(화면에 결제 없음).
- 바깥 배경 클릭으로 닫는 모달(`if (event.target === this) …`)은 `data-on-self` 로 — 문법에 조건문을 넣지 않는다.
