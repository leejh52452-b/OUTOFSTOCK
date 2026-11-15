# OUTOFSTOCK — 프론트엔드 연동 가이드 (2026-10-27 판)

> 백엔드(주원) → 프론트(준혁) 핸드오프 문서. 이 백엔드에 화면을 붙일 때 필요한 전부:
> 접속 주소 · 로그인 방식 · 공통 규칙 · **화면별 API + 실제 요청/응답 예시**.
> 참조 구현은 `web/`(팀 확정 화면 다섯 장 — 서버가 `/`·`/learn.html`… 로 내보낸다) + 공용 런타임 `web/assets/oos/runtime.js`
> — 이 문서의 규칙(쿠키·CSRF·갱신·추가 인증·SSE·CSP)을 실제로 지키는 코드다. 팀 화면을 고칠 때 지킬 것은 `web/README.md`.
> 함께 주는 파일: `types.ts`(응답 타입 + 그대로 쓸 수 있는 `api()`·`login()`·`stepUp()` — 갱신·추가 인증 재시도 포함,
> 2026-10-22 실제 브라우저 + Vite 프록시로 확인) · `outofstock.http`(요청 모음).

> ### ⚑ 2026-11-02 — 기기 결속 키(DPoP) · 대량접속제어 · 비상 차단 · 중요정보 가림
> - **기기 결속 키**: 공용 런타임(`assets/oos/runtime.js`)이 이 브라우저 전용 ECDSA 키(꺼낼 수 없는 키)로 로그인·갱신·변경 요청(POST·DELETE)·관리 요청에
>   `DPoP` 헤더(1회용 서명)를 자동으로 붙인다. 팀 화면은 **할 일 없음**(`OOS.api` 를 쓰면 된다). 직접 `fetch` 로 송금·구매를 부르는 코드를 만들면 안 된다 —
>   키로 로그인한 세션은 증명 없이는 돈 동작이 401 `dpop_required`. 다른 프레임워크로 옮길 때 규칙: **2.8**.
> - **503 두 종류**: `overloaded`(혼잡 — 공개 조회부터 잠시 거절, `Retry-After` 2~5초) · `service_paused`(그 기능만 비상 차단 — 조회·다른 기능은 그대로). `detail` 을 보여 주면 된다.
> - 채팅·송금 응답의 `masked`(예: `["rrn"]`) — 서버가 주민등록번호·카드번호처럼 보이는 숫자를 가려서 저장했다는 뜻 → '가려서 보냈어요' 안내.
>
> ### ⚑ 2026-10-31 — 양봉이 꾸미기(새 화면 `avatar.html`)
> - 새 API: `GET /avatar/catalog·me` · `POST /avatar/buy` · `POST /avatar/equip` — **4.3**. 아이템 그림·이름·가격은 `web/assets/avatar-items.js`
>   한 파일(서버 사본은 `tools/build_avatar_items.py` 로 — 가격을 바꾸면 꼭 다시). 사기 요청은 아이템 id + **화면이 보여 준 가격**(다르면 409).
> - `/me` 응답에 `avatar`(입은 옷·몸값·수저) — 마이페이지 프로필 그림. `/me/logs/wallet?filter=shop` = '양봉이 꾸미기' 지출.
>
> ### ⚑ 2026-10-27 — 팀 화면 연동
> - 팀 저장소의 화면 다섯 장을 **이 서버가 같은 출처로 서빙**한다(옛 한 장짜리 화면 `/app` 은 `/` 로 넘어감). 예시 데이터·localStorage →
>   실제 API. 화면 겉모습은 그대로(접근성 대비 두 곳만 — ADR-0021).
> - 새 API: `GET·POST /learn/quiz`(Daily Stock Quiz — **답만 보내면 서버가 같은 문제를 다시 만들어 채점**, 하루 1번) ·
>   `GET·POST /practice/records`(웹 실습 기록 — 범위 검증, 보상과 무관) · 로그인 `remember`(로그인 유지 — 기본은 세션 쿠키).
> - 화면 규칙: 인라인 `onclick` 대신 `data-on` + `OOS.actions({...})` 등록, 외부 스크립트 금지, 서버 호출은 `OOS.api()` — `web/README.md`.
>
> ### ⚑ 10-22 판에서 바뀐 것 — 먼저 읽기
> 1. **웹 로그인 = HttpOnly 쿠키 + CSRF 헤더.** 토큰을 JS 로 저장하지 않는다(XSS 가 생겨도 토큰을 못 훔친다) — 2.1.
> 2. **접근 토큰은 10분.** 401 `token_expired` 면 `/auth/refresh` 후 같은 요청을 다시 — 2.1.
>    탈퇴·큰 금액 송금처럼 위험한 동작은 **비밀번호 재확인(추가 인증)** 을 요구한다 — 2.2.
> 3. **CORS 는 기본 꺼짐(같은 출처만).** 개발 서버(Vite)는 프록시로 같은 출처를 만든다 — 1장.
> 4. 오류 응답에 **`code`(이유 코드)** 가 붙는다 — 401 을 셋으로 나눠 처리해야 한다 — 2.6.
> 5. **실시간 알림 `GET /events`(SSE)** — 잔액·채팅·친구 요청·세션 종료를 바뀐 순간 받는다(폴링 대체) — 2.4.
> 6. 화면에 없는 기능(커뮤니티·시장/분석·게임·단기 시뮬·아바타·레거시 모의투자 등)의 API 는 **제거**했다 — 9장.
> 7. **API 문서(`/docs`)는 운영 서버에서 404**(구조 노출 최소화). 로컬 서버에서 보거나 이 문서를 기준으로.

---

## 0. 30초 요약

- **주소**: 웹 화면(`/`)이 열리는 주소가 곧 API 주소. 권장 구성은 nginx 경계 뒤 `http://<VM_IP>`(TLS 면 `https://`),
  nginx 없이 띄운 VM 이면 `http://<VM_IP>:8000`. 주원이 알려 준 주소를 쓴다.
- **전부 JSON**. 금액=원(정수), 등락률·수익률=%(실수, 이미 ×100 — 그대로 표시). 예외: 백테스트 계열은 소수(0.021=2.1%) — 3.1.
- **색 규칙(한국식)**: `change_rate`·`return_rate`·`pnl` 양수=빨강(상승/이익), 음수=파랑(하락/손실).
- **로그인 없이**: 홈·국내/해외 종목·뉴스·학습(열람)·실습·투자 성향 분석·검색.
  **로그인 필요**: 마이페이지·관심종목·학습 진도·친구·채팅·가상머니(출석·송금·충전·장기 모의투자) — 2.3 표.
- **웹 요청 규칙 3줄**: ① `fetch` 기본값(`credentials: "same-origin"`)이면 쿠키가 자동으로 실린다
  ② POST·DELETE 에만 헤더 `X-CSRF-Token: <oos_csrf 쿠키 값>` ③ 401 은 `code` 로 갈라서 처리(갱신 / 비밀번호 확인 / 로그인 화면).
- **돈·매매 POST** 는 `Idempotency-Key` 헤더(클릭마다 새 값) — 더블클릭·재시도에도 1번만 — 2.7.

---

## 1. 붙는 법

### 1.1 개발 서버(Vite) — 프록시로 '같은 출처' 만들기 (권장)

쿠키는 `SameSite=Strict` 이고, 서버는 쿠키로 인증된 변경 요청의 `Origin` 이 자기 출처와 같은지 확인한다.
그래서 브라우저 입장에서 화면과 API 가 **같은 출처(`http://localhost:5173`)** 가 되도록 Vite 가 API 경로만 VM 으로 넘긴다.

```ts
// vite.config.ts
import { defineConfig } from "vite";

const API = "http://<VM_IP>";            // nginx 없이 띄운 VM 이면 "http://<VM_IP>:8000", 로컬 백엔드면 "http://localhost:8000"
const PATHS = "auth|me|watchlist|wallet|attendance|transfer|invest|iap|pay|users|friends|chat|events|" +
              "learn|practice|survey|reco|indices|overseas|market|stocks|news|api|search|health|static";

export default defineConfig({
  server: {
    proxy: { [`^/(${PATHS})(?:[/?]|$)`]: { target: API } },   // changeOrigin 은 켜지 말 것(기본값 false 그대로)
  },
});
```
그리고 코드에서는 **상대 경로**로 부른다: `fetch("/indices")`, `api("/me")` (`types.ts` 의 `BASE_URL = ""`).

- `/api` 를 붙였다 떼는 방식(`rewrite`)은 **쓰지 말 것** — 갱신 쿠키가 `Path=/auth` 라 브라우저가 `/api/auth/refresh` 에는 싣지 않는다
  (10분 뒤 갱신이 실패해 계속 로그아웃된다).
- `changeOrigin: true` 로 두면 서버가 보는 Host 가 VM 주소로 바뀌어 출처가 달라진다 → 로그인·변경 요청이 403 `csrf_failed`.
- VM 이 **HTTPS** 면(앞단이 `X-Forwarded-Proto: https`) `http://localhost:5173` 과 출처 스킴이 달라진다 → 주원에게
  **개발용 서버**의 `.env` 에 `TRUSTED_ORIGINS=http://localhost:5173` 을 넣어 달라고 하면 된다(운영 서버에는 넣지 않는다).
  이때 쿠키 이름은 `__Host-oos_csrf` 처럼 접두어가 붙는다(아래 `csrfToken()` 이 둘 다 찾는다).
- 배포는 빌드 결과를 **API 와 같은 출처**에서 내보내면 프록시가 필요 없다(우리 `/app` 이 그 방식).

### 1.2 다른 출처에서 직접 부르기 — Bearer 모드(모바일 앱·스크립트·다른 도메인)

- 서버 `.env` 의 `CORS_ORIGINS` 에 그 출처를 넣어야 브라우저가 응답을 읽을 수 있다(기본은 비어 있음).
  허용해도 **쿠키는 보내지 않는다**(`allow_credentials=false`) — `fetch` 에 `credentials: "include"` 를 넣지 말 것.
- 로그인 때 `X-Client` 헤더를 **빼면** 응답 본문에 `token`·`refresh_token` 이 온다 → 이후 `Authorization: Bearer <token>`.
  Bearer 요청에는 CSRF 헤더가 필요 없다.
- 토큰은 **메모리(변수)에만**. localStorage 에 두면 XSS 한 번에 털린다 — 새로고침 뒤에도 로그인을 유지하려면 1.1 쿠키 모드.
- SSE(`/events`)는 `EventSource` 가 헤더를 못 붙이므로 Bearer 모드에선 `fetch` 스트림으로 읽거나 `/me/badges` 를 1분 간격으로 폴링.

### 1.3 로컬에서 직접 띄우기(선택)

```bash
cd stock-backend && docker compose up -d db
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python run.py initdb && .venv/bin/python run.py demo     # 표본 데이터 + 데모 계정(demo@oos.io / demo1234, friend@oos.io)
.venv/bin/uvicorn app.api:app --reload                               # http://localhost:8000/app · Swagger http://localhost:8000/docs
```
> **백엔드 없이 UI부터** 만들 거면 이 문서의 응답 예시 JSON 을 목(mock)으로 써도 된다.

---

## 2. 공통 규칙

### 2.1 인증 — 웹(쿠키) 흐름

| 단계 | 요청 | 응답 |
|---|---|---|
| 가입 | `POST /auth/register` `{username, password}` | `{ "username": "jane" }` (로그인은 따로) |
| 로그인 | `POST /auth/login` `{username, password, otp?}` + 헤더 **`X-Client: web`** | `{ "username": "jane", "mode": "cookie", "expires_in": 600, "mfa": false }` + 쿠키 3개 |
| 요청 | 쿠키 자동 · POST/DELETE 에 `X-CSRF-Token` | — |
| 갱신 | `POST /auth/refresh` + 헤더 `X-Client: web`, `X-CSRF-Token` (본문 없음) | `{ "ok": true, "mode": "cookie", "expires_in": 600 }` + 새 쿠키 |
| 로그아웃 | `POST /auth/logout` (+`X-CSRF-Token`) | `{ "ok": true, "ended": true }` — 서버 세션 폐기 + 쿠키 삭제 |

로그인이 주는 쿠키(HTTPS 면 이름 앞에 `__Host-`/`__Secure-`):

| 쿠키 | 내용 | 속성 |
|---|---|---|
| `oos_at` | 접근 토큰(10분) | HttpOnly · SameSite=Strict · Path=/ — **JS 로 못 읽는다** |
| `oos_rt` | 갱신 토큰(1회용, 쓰면 새것으로 회전) | HttpOnly · SameSite=Strict · **Path=/auth** |
| `oos_csrf` | 세션별 CSRF 토큰 | JS 가 읽어 `X-CSRF-Token` 헤더로 보낸다(HttpOnly 아님) |

```ts
// CSRF 토큰 읽기(HTTP·HTTPS 이름 둘 다)
function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)(?:__Host-)?oos_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}
```

- **로그인 상태 판단**: 토큰을 JS 가 못 읽으므로 `GET /me/badges`(가벼움)가 200 이면 로그인, 401 이면 비로그인.
  참조 구현은 localStorage 에 **아이디 힌트만** 둔다(토큰 아님).
- **세션 수명**: 접근 토큰 10분(자동 갱신) · **30분 동안 아무 요청이 없으면 유휴 만료** · 로그인 12시간 뒤에는 무조건 만료 → 다시 로그인.
- **갱신 토큰은 1회용**. 이미 쓴 갱신 토큰이 다시 오면 탈취로 보고 그 세션을 통째로 끊는다(`refresh_reuse`).
  탭 두 개가 동시에 갱신하는 경우는 30초 유예로 허용 — 그래도 **갱신 요청은 한 번에 하나**만 보내는 게 좋다(참조 구현 `refreshSession()` 은 진행 중인 갱신을 공유).
- 같은 브라우저에서 한 번 로그인하면 다른 탭도 같은 쿠키를 쓴다. 다른 탭이 로그아웃하면 이 탭의 다음 요청이 401 → 로그인 화면.

**Bearer 흐름(1.2)**: `POST /auth/login`(`X-Client` 없음) →
`{ "username", "token": "oosa_…", "refresh_token": "oosr_…", "token_type": "Bearer", "expires_in": 600, "mfa": false }` →
갱신은 `POST /auth/refresh` 본문 `{ "refresh_token": "oosr_…" }` → `{ "ok", "token", "refresh_token", "token_type", "expires_in" }`(둘 다 새것).

**가입·비밀번호 규칙**(어기면 400, `detail` 을 그대로 보여 주면 된다 — 폼에서 미리 검사하면 왕복이 준다)
- 아이디 2~50자(글자·숫자·`_ @ . -`, 글자나 숫자 1개 이상). **대소문자만 다른 아이디는 같은 아이디**(`Kim` 이 있으면 `kim` 가입 불가,
  로그인은 대소문자 무관 — 응답 `username` 이 저장된 표기). `guest`·`guest-…` 는 예약어.
- 비밀번호 8~128자 · **영문+숫자 모두** · 같은 문자 4번 연속 금지 · 흔한 비밀번호 금지 · 아이디 포함 금지 · (변경 시) 현재와 같은 값 금지.
- 로그인을 같은 IP·아이디로 10분 안에 5번 틀리면 429(`Retry-After` 초), **계정도 연속 5회 실패면 15분 잠금**(403 `account_locked`,
  반복되면 두 배씩). 존재하지 않는 아이디와 틀린 비밀번호는 같은 메시지(계정 존재 여부를 알려 주지 않는다). 가입은 IP 당 1시간 20번.
- **비밀번호 변경**(`POST /auth/change-password` `{old_password, new_password}`) → `{ "ok": true }`. **지금 세션은 유지**,
  다른 기기의 세션은 모두 종료. 현재 비밀번호를 5번 틀리면 429.

### 2.2 추가 인증(비밀번호 재확인) · 2단계 인증(OTP)

**추가 인증** — 서버가 401 `step_up_required` 를 주면 비밀번호 확인 창 → `POST /auth/step-up` `{password, otp?}` →
`{ "ok": true, "valid_for": 300, "mfa": false }` → **원래 요청을 다시** 보낸다(5분 동안은 다시 묻지 않음).

| 언제 | 대상 |
|---|---|
| 항상 | 회원 탈퇴(`DELETE /me`) · 2단계 인증 등록·해제 |
| 금액 | 송금 **100만 원 이상** |
| 위험 신호(접속 IP 대역이 바뀜 등) | 송금 · 충전 |
| 이상거래탐지(FDS) 점수 40~69 | 송금(평소와 다른 패턴) — 70 이상이면 403 `fds_blocked`(차단, 메시지 그대로) |

- 추가 인증 뒤 **5분**은 다시 묻지 않는다. **로그인은 추가 인증으로 치지 않는다** — 방금 로그인했어도 탈퇴·100만 원 이상 송금은
  창이 뜬다(탈취된 세션이 로그인 직후를 노리는 것을 막고, 이체마다 추가 인증하는 금융권 관행과 맞춘다 — 2026-10-23).
- 비밀번호가 틀리면 401 `step_up_failed`, OTP 를 켠 계정은 `otp` 도 필요(틀리면 `otp_invalid`). 5번 틀리면 429.
- 사용자가 확인 창을 닫으면 원래 요청만 실패로 끝내고 **세션은 그대로** 둔다(로그인 화면으로 보내지 말 것).
- 참조 구현: 송금 창의 입력값을 유지한 채 확인 창을 띄우고, 끝나면 원래 창으로 돌아가 재시도(`api()` 의 `stepUp()`).

**2단계 인증(선택 — 관리 역할만 필수)** — 이것도 API 만 있다(참조 화면 없음).

| 메서드 경로 | 설명 / 응답 |
|---|---|
| `GET /auth/mfa` | `{ "enabled": false }` |
| `POST /auth/mfa/setup` | (추가 인증 필요) `{ "secret": "…", "otpauth_uri": "otpauth://totp/…", "period": 30, "digits": 6 }` → QR 로 보여 주기 |
| `POST /auth/mfa/confirm` `{code}` | 앱의 6자리로 확인 → `{ "enabled": true, "recovery_codes": [10개] }` (**지금 한 번만** 보여 준다) |
| `POST /auth/mfa/disable` `{code}` | 끄기 |

켠 계정은 로그인 때 401 `otp_required` → 같은 로그인 요청에 `otp`(6자리 또는 복구 코드)를 넣어 다시 보낸다.

### 2.3 로그인 필요 여부 · 세션(기기) 관리

| 로그인 없이 되는 것 | 로그인 필요 |
|---|---|
| 홈 전체 · `/indices` · `/overseas/*` · `/market/stocks` · `/stocks/*` · `/reco/*` · `/news*`·`/api/news/home` · `/learn/dictionary`·`/learn/today`·`POST /learn/log` · `/practice/*` · `/survey/*` · `/search/*` · `/iap/products` · `/pay/toss/config` · `/health` | `/me`·`/me/*`·`DELETE /me` · `/watchlist*` · `/learn/progress*` · `/users` · `/friends*` · `/chat/*` · `/wallet` · `/attendance*` · `/transfer` · `/invest*` · `POST /iap/purchase` · `POST /pay/toss/prepare·confirm` · `/events` · `/auth/sessions`·`/auth/logout-all`·`/auth/step-up`·`/auth/mfa*`·`/auth/change-password` |

- **실습(`/practice/*`)** 은 비로그인도 된다 — 서버가 첫 요청에 **게스트 쿠키(`oos_gid`, HttpOnly)** 를 발급하고 그걸로 진행 상태를 구분한다.
  예전의 `user_id` 파라미터는 **무시**된다(남의 이름을 고를 수 없게). 로그인했으면 그 계정 기준.
  남의 실습 세션 번호로 접근하면 403(계정 세션은 그 계정으로만, 게스트 세션은 그 브라우저로만).
- `POST /survey/submit`·`POST /learn/log` 는 비로그인도 되고, 로그인 상태면 계정 기록에 남는다.

**내 세션(로그인한 기기)** — 참조 화면(`/app`)에는 아직 없다(화면은 스크린샷 기능 그대로). 마이페이지에 '로그인한 기기' 카드로 붙이길 권장.

| 메서드 경로 | 응답 |
|---|---|
| `GET /auth/sessions` | `{ "items": [ { "session_id": "5d13…", "client": "web", "device": "Chrome · Windows", "ip_net": "203.0.113.0/24", "created_at": "…", "last_seen_at": "…", "expires_at": "…", "mfa": false, "current": true } ] }` |
| `DELETE /auth/sessions/{session_id}` | `{ "ok": true }` — 그 기기 로그아웃(모르는 기기면 바로 끊기) |
| `POST /auth/logout-all` | `{ "ok": true, "sessions": 3 }` — 이 기기 포함 전부 |

> IP 는 원문 대신 대역(`/24`)만 보여 준다. 기기 표시는 브라우저 계열·OS 정도.

### 2.4 실시간 알림 — `GET /events` (SSE, 로그인 필요)

```ts
const es = new EventSource("/events");                 // 같은 출처 + 쿠키 세션
es.addEventListener("wallet",  e => setBalance(JSON.parse(e.data).balance));
es.addEventListener("chat",    e => { const d = JSON.parse(e.data); /* d.from 과 대화 중이면 새 메시지 조회, 아니면 배지 갱신 */ });
es.addEventListener("friends", e => refreshBadges());   // {type:"request", from} | {type:"accepted", by}
es.addEventListener("session", e => refreshBadges());   // 내 세션 중 하나가 끝남 — 이 세션이면 다음 요청이 401
es.addEventListener("reconnect", () => { es.close(); setTimeout(start, 500); });
```

| 이벤트 | 데이터 | 언제 |
|---|---|---|
| `hello` | `{ "heartbeat": 20 }` | 연결 직후 |
| `wallet` | `{ "balance": 2000000 }` | 잔액이 바뀐 거래가 **커밋된 뒤**(출석·송금 받음·충전·매매) |
| `chat` | `{ "from": "jane", "id": 812 }` | 새 메시지(본문은 싣지 않는다 → `/chat/{peer}/messages?after_id=` 로 조회) |
| `friends` | `{ "type": "request", "from": "jane" }` · `{ "type": "accepted", "by": "jane" }` | 친구 요청 받음 · 내 요청 수락됨 |
| `session` | `{ "reason": "logout", "sid": "5d13aebc" }` | 내 세션 중 하나가 끝남(다른 기기 로그아웃·관리자 종료 등) |
| `reconnect` | `{ "reason": "lifetime" }` | 30분마다 서버가 닫는다 → 다시 연결(재인증 기회) |

- 20초마다 하트비트(`: ping`) — 그때마다 서버가 세션을 다시 확인해 로그아웃·폐기·유휴 만료면 스트림을 닫는다.
- 알림은 **힌트**다 — 놓쳐도 다음 조회 때 화면이 맞춰진다. 잔액·목록은 항상 API 응답으로 다시 그린다.
- 연결이 끊기면 `EventSource` 가 5초 뒤 스스로 다시 붙는다. 계속 실패하면 참조 구현처럼 지수 백오프(최대 60초) + `/me/badges` 로 세션 확인.

### 2.5 표시 규칙
- **색**: 양수 빨강, 음수 파랑(숫자 앞에 +/- 표기 권장).
- **`live` 필드**: `true` = 장중 KIS 실시간 체결가, `false` = 일별 종가 기준. 배지로 "실시간/종가" 구분.
- **`phase`(장 상태)**: 국내 `pre`(개장 전)·`open`(정규장)·`closed`(장 마감)·`weekend`·**`holiday`(평일 휴장일 — 설·추석 등)**,
  미국은 `after`(애프터마켓) 추가(NYSE 휴장일도 `holiday`). 우리 /app 은 `holiday` 를 "휴장일 · M/D 종가" 로 표시.
- **날짜**: `"2026-09-11"`(YYYY-MM-DD). 시각은 ISO `"2026-09-11T10:30:00"`.
- **`source: "gen"`·`status: "sample"`·`sample: true`** 는 **표본(실제 값 아님)** — 화면에 '표본' 배지를 붙일 것.

### 2.6 에러 — 전부 JSON, 정책 거부는 `code` 포함

- 일반 오류 **400·403·404·409·413·429·503**: `{"detail": "사람이 읽는 메시지"}` — 그대로 보여 주면 된다.
  `409` = 동시에 겹친 요청(더블클릭 등)이거나 같은 `Idempotency-Key` 로 다른 내용 → 새로고침·잠시 뒤 다시 ·
  `429` = 시도 과다(`Retry-After` 초 헤더) · `503` = DB 가 잠시 응답하지 않음(`Retry-After: 5`, `/health` 도 503) · `413` = 본문 1MB 초과.
- **정책 거부(인증·권한·CSRF)**: `{"detail": "…", "code": "이유"}` + 같은 값의 헤더 `X-Auth-Code`.

| 상태 · code | 뜻 | 화면이 할 일 |
|---|---|---|
| 401 `token_expired` | 접근 토큰 10분 지남 | `POST /auth/refresh` → 성공하면 **원래 요청 재시도**, 실패하면 로그인 화면 |
| 401 `step_up_required` | 추가 인증 필요(2.2) | 비밀번호 확인 창 → `/auth/step-up` → 원래 요청 재시도 |
| 401 `login_required` · `token_invalid` | 로그인 안 됨 | 로그인 화면 |
| 401 `session_idle` · `session_expired` · `session_revoked` · `device_mismatch` · `refresh_reuse` | 세션이 끝남(유휴 30분·12시간·다른 곳에서 종료·다른 기기에서 쓰임·탈취 의심) | 로그인 화면 + `detail` 을 한 번 알려 주기 |
| 401 `invalid_credentials` · `otp_required` · `otp_invalid` | 로그인 실패 · OTP 필요 · OTP 틀림 | 로그인 폼에 메시지 / OTP 칸 표시 |
| 403 `csrf_failed` | CSRF 헤더 없음·불일치, 다른 출처 | 새로고침 안내(개발 중이면 1.1 프록시 설정 확인) |
| 403 `account_locked` | 연속 실패로 잠금 | 메시지 그대로 |
| 403 `fds_blocked` | 이상 거래로 송금 차단 | 메시지 그대로 |
| 403 `forbidden` · `mfa_required` | 권한 없음(관리 기능) | 메시지 그대로 |
| 401 `dpop_required` · `dpop_mismatch` | 기기 결속 세션인데 이 브라우저의 키 증명이 없음 · 다른 키(쿠키 복사 의심 — 세션 종료) | 로그인 화면 + `detail` |
| 401 `dpop_invalid` · `dpop_replay` | 깨졌거나 이미 쓴 증명(시계 차이 등) | 새 증명으로 한 번 재시도(런타임이 서버 시각으로 맞춤) |
| 503 `overloaded` | 혼잡 단계 — 중요도가 낮은 요청부터 잠시 거절(`Retry-After`, `X-Load-Level`) | `detail` 표시 · `Retry-After` 뒤 다시 |
| 503 `service_paused` | 그 기능(송금·충전·꾸미기 구매·모의투자)만 비상 차단 중(`Retry-After: 60`) | `detail` 표시 — 다른 기능은 그대로 |

- **422**(입력 형식 오류 — 필드 누락·길이 초과·숫자 범위 밖·저장할 수 없는 문자): 요청 검증에서 걸리면 `detail` 이 **배열**
  `[{"type": "string_too_long", "loc": ["body","memo"], "msg": "...", "ctx": {...}}]`, DB 가 값 자체를 거부하면 **문자열**.
  → 문자열이면 그대로, 배열이면 `detail[0].msg`(`types.ts` 의 `errorText()`).
- **길이 상한**(넘으면 422): 송금 메모 200 · `Idempotency-Key` 200 · 검색어 100 · 학습 로그 kind 20/item_key 60 · `as_of` 는 `YYYY-MM-DD` 만.
  아이디·비밀번호 규칙 위반은 **400**(한국어 안내) — 터무니없이 긴 값(아이디 100자·비밀번호 1024자 초과)만 422. 채팅 1~1000자(넘으면 400).
- 목록 API 의 `limit`·`offset` 은 서버가 범위를 자른다(예: `/news` 100, `/stocks/{t}/news` 50, offset 최대 1,000,000).

### 2.7 멱등키(Idempotency-Key) — 더블클릭·재시도에도 1번만
돈이나 매매가 오가는 POST 는 헤더 `Idempotency-Key: <클릭마다 새 무작위 문자열>` 을 받는다 —
**같은 키로 다시 보내면 처리는 1번만 하고 처음 결과를 돌려준다.** 지원: `/transfer`(다시 보내면 `"duplicate": true`) ·
`/practice/{id}/trade`. 토스 승인(`/pay/toss/confirm`)은 주문번호로 자동(두 번 불려도 적립 1번, 두 번째는 `duplicate: true`).
버튼도 요청이 끝날 때까지 잠가 두는 게 좋다. 서버가 4xx 로 거절했으면 다음 시도는 새 키, 네트워크 오류면 같은 키로 재시도.
(2026-10-24) 서버는 같은 키의 처리 여부를 **FDS 판정보다 먼저** 본다 — 응답을 못 받은 송금을 같은 키로 다시 보내면 추가 인증·차단 없이
처음 결과(`duplicate: true`)가 온다. 그래서 위 규칙(4xx 면 새 키)을 그대로 써도 같은 돈이 두 번 나가지 않는다.

---

### 2.8 기기 결속 키(DPoP) — 다른 클라이언트를 만들 때(2026-11)

- 키: WebCrypto `generateKey({name:'ECDSA', namedCurve:'P-256'}, false, ['sign','verify'])` — **extractable=false**, IndexedDB 에 객체로 보관.
- 증명(JWS compact): header `{"typ":"dpop+jwt","alg":"ES256","jwk":{"kty":"EC","crv":"P-256","x","y"}}` ·
  payload `{"htm":"POST","htu":"<origin><경로>","iat":<초 — 서버 Date 헤더로 보정>,"jti":"<16~64자 무작위>"}` · 서명 = WebCrypto 그대로(r‖s 64바이트).
- 붙이는 곳: `POST /auth/login`(키를 세션에 묶음) · `POST /auth/refresh` · 모든 POST·DELETE · `/admin/*`. 요청마다 새 `jti`(재사용하면 401 `dpop_replay`).
- 서버는 경로만 비교(`htu` 의 출처·쿼리는 보지 않음), 발급 시각 ±5분. 앱(Bearer)은 붙이지 않아도 된다(결속 안 된 세션 — 기존 통제).
- 참조 구현: `web/assets/oos/runtime.js` 의 `deviceKey()`·`dpopProof()` · 서버 `app/security/dpop.py` · 표준 RFC 9449.

## 3. 화면별 API

화면 순서: 홈 · 학습 · 실습 · 국내/해외 종목 · 뉴스 · 로그인/마이페이지 · 투자 성향 분석 · 친구·채팅. 각 항목: `메서드 경로` — 설명 → **응답 예시**.

### 3.1 홈

**지수** — `GET /indices` (항목마다 출처·기준일·상태 + 미니차트 이력)
```json
{ "phase": "open", "source": "krx", "index_source": "kis", "items": [
  { "code": "KOSPI", "name": "코스피", "value": 6724.34, "change": 6.37, "change_rate": 0.09,
    "source": "kis", "as_of": "2026-10-01", "ts": "2026-10-01T10:31:05+09:00",
    "status": "live", "stale": false, "history": [6627.26, 6717.97, 6724.34] },
  { "code": "KOSDAQ", "name": "코스닥", "value": 821.67, "change": 5.69, "change_rate": 0.70,
    "source": "yahoo", "as_of": "2026-09-30", "status": "close", "stale": false, "history": [812.41, 815.98, 821.67] },
  { "code": "USDKRW", "name": "원/달러 환율", "value": 1348.5, "change": 2.0, "change_rate": 0.15, "source": "gen", "status": "sample" }
] }
```
> `status` 로 배지: `live`(● 실시간) · `delayed`(장중) · `close`(`as_of` 종가) · `sample`(**표본**) · `unknown`(출처 기록 없는 예전 값).
> `source` = `kis`(한국투자증권 실시간) · `yahoo` · `krx`(KRX OpenAPI, 전 영업일) · `fdr` · `gen`(표본). `stale:true` 면 기준일이 오래됨(수집 중단).
> 국내(`/indices`)와 해외(`/overseas/indices`)는 **따로 호출해 따로 그릴 것** — 한쪽이 느려도 코스피·코스닥은 바로 보이게.

**종목 순위(탭)** — `GET /market/stocks?sort=value&limit=10`
`sort` = `value`(거래대금) | `volume`(거래량) | `marcap`(시총) | `rise`(상승률) | `fall`(하락률)
```json
{ "sort": "value", "live": false, "as_of": "2026-09-11", "items": [
  { "rank": 1, "ticker": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체",
    "price": 71000, "change_rate": 1.28, "volume": 15200000,
    "value": 1079200000000, "marcap": 423900000000000, "live": false } ] }
```

**주요 뉴스** — `GET /news?limit=8` → `{ "items": [ { "title", "url", "source", "published_at", "published_ts" } ] }`

**성향별 추천 상품** — `GET /reco/products?profile=안정형`  (`profile` = 안정형 | 중립형 | 공격형)
팀 문서 `OUTOFSTOCK_추천상품_선정.docx` 그대로(12개 중 성향별 3개). 조합 규칙: 안정형 저위험2+중위험1 · 중립형 중위험2+저위험1 · 공격형 중위험1+고위험2.
```json
{ "profile": "안정형", "grade": "안정형 · 저위험", "rule": "저위험 2 + 중위험 1", "subtitle": "…", "items": [
  { "code": "453010", "name": "PLUS KOFR금리", "manager": "한화자산운용", "grade": "6", "risk": "저위험",
    "kind": "초단기 금리(레포)", "type": "ETF", "desc": "나라 채권을 담보로 하루짜리 이자를 받는 상품이에요. 거의 움직이지 않아요.",
    "source": { "text": "한화자산운용, 「…」 투자설명서, 작성기준일 2026.01.28, …", "dart_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260213000014" },
    "price": null, "change_rate": null, "status": null } ],
  "excluded": "레버리지·인버스·커버드콜 등 파생형 상품은 제외해요…", "notice": "상품 위험 라벨은 금융위원회 …(저작권 표기 문구)", "checked_at": "2026-09-22" }
```
> `risk` 배지 색: 저위험 `#e0e7ff/#3730a3` · 중위험 `#fef08a/#854d0e` · 고위험 `#fecdd3/#9f1239`. `price` 는 우리 DB 에 시세가 있는 개별 주식만, ETF 는 `null`(지어내지 않음).
> **화면 표기(팀 요청)**: 카드에 `grade`(공식 1~6등급 숫자)는 보여 주지 않고 `risk` 배지만. 출처·저작권 문구는 `source`·`notice` 로 표기.

**오늘의 추천 종목 Top 5(멀티팩터 모델 mf-2)** — `GET /reco/top5?limit=5[&profile=안정형|중립형|공격형]`
7개 팩터(모멘텀·기술적 신호·가치·재무 건전성·뉴스 심리·수급·안정성) 점수를 시장 국면별 가중치로 합산(교육용, 투자 권유 아님).
`profile` 을 주면 **적합성 필터**(성향 대비 변동성·부실 위험 종목 제외). 순위는 마지막 '확정' 일봉 기준, `price`·`change_rate` 는 실시간.
```json
{ "as_of": "2026-09-30", "source": "krx", "profile": null, "note": "멀티팩터 점수 기반 교육용 예시 · 투자 권유 아님",
  "model": { "version": "mf-2.0", "regime": "bull", "regime_label": "상승 국면", "breadth": 0.62,
             "weights": { "momentum": 0.25, "technical": 0.2, "value": 0.1, "quality": 0.15, "sentiment": 0.1, "flow": 0.1, "lowrisk": 0.1 },
             "universe": 150, "eligible": 141, "excluded": { "단기 과열(…)": 2, "유동성 부족(…)": 7 } },
  "items": [
  { "rank": 1, "ticker": "000270", "name": "기아", "market": "KOSPI", "sector": "자동차",
    "score": 97.1, "grade": "S", "composite": 0.92, "tag": "재무 우량", "brief": "PER 4.9배 · 3개월 +4%",
    "tags": ["재무 우량", "상승 모멘텀", "주요 이평선 지지"],
    "reasons": ["재무 건전성: 영업이익률 11.4% · ROE 14.3% · 부채비율 45% · Altman Z'' 6.89(안전)", "…"],
    "cautions": ["최근 뉴스는 악재가 우세"],
    "factors": { "momentum": 1.2, "technical": 0.8, "value": 1.9, "quality": 1.6, "sentiment": -0.9, "flow": 1.5, "lowrisk": 0.6 },
    "contrib": { "momentum": 0.3, "technical": 0.16, "value": 0.19, "quality": 0.24, "sentiment": -0.09, "flow": 0.15, "lowrisk": 0.06 },
    "metrics": { "ret20": 0.009, "volatility": 0.27, "mdd": -0.12, "per": 4.9, "pbr": 0.7, "roe": 14.3, "altman_z": 6.89, "news": 3 },
    "risk": { "level": "보통", "volatility": 0.27, "mdd": -0.12 },
    "entry_date": "2026-09-30", "entry_price": 110000, "price": 110500, "change_rate": 0.45, "status": "live" } ] }
```
> 목록 한 줄: `name` + `grade` 배지 + `tag · brief` + 오른쪽 `score`점. 누르면 `contrib`(팩터 기여도 막대, 0 기준 좌우) + `reasons`(✓) + `cautions`(!).
> **`items` 가 비었거나 5개보다 적을 때** — 이유가 셋이라 문구를 나눠야 한다:
> 1. `as_of` 가 `null` → 국내 일봉이 아직 없음(첫 수집 전): "아직 국내 시세가 없어요"
> 2. `profile` 이 있고 `model.diversified_out["투자성향 대비 위험 높음"] > 0` → **성향 기준에서 빠짐**:
>    "오늘은 안정형 기준을 통과한 종목이 없어요" + 같은 API 를 `profile` 없이 불러 '성향 무관 Top 5' 를 보여 줄 수 있게.
> 3. `model.eligible == 0` → 적격 종목 없음: `model.excluded`(사유 → 종목 수)의 큰 것 1~2개를 그대로.
> 참조 구현: `web/index.html` 의 `fillTop5()`(팀 확정 화면). 서버에서 숫자로 보려면 `python tools/reco_check.py`.

**모델 설명서(추천 기준)** — `GET /reco/model` → 팩터 정의·국면별 가중치·적격성 필터·분산 제약·성향별 기준·데이터 시점 원칙·한계 + `current`(지금 적용 중인 국면/가중치).

**모형 모니터링(PSI·IC)** — `GET /reco/model/monitor?ref_days=60` (10분 캐시 · `ref_days` 는 20/40/60/120/250 중 가장 가까운 값)
```json
{ "available": true, "ref_date": "2026-07-09", "cur_date": "2026-10-01", "status": "주의",
  "psi": [ { "var": "vol", "label": "연변동성", "kind": "market", "psi": 0.18, "status": "주의", "n_ref": 142, "n_cur": 139,
             "ref_median": 0.31, "cur_median": 0.38 } ],
  "thresholds": { "warn": 0.1, "alert": 0.25, "rule": "PSI < 0.10 안정 · 0.10~0.25 주의 · ≥ 0.25 재검토" },
  "ic": { "series": [ { "date": "2026-06-02", "ic": 0.08 } ], "recent_mean": 0.03, "overall_mean": 0.05, "status": "양호" },
  "actions": [ "입력 분포가 크게 바뀐 변수(...) — 윈저라이즈 구간·가중치 재추정 검토" ] }
```
> 변수별 20종목 미만이면 `psi:null`·`status:"데이터 부족"`. 시장 변수(`kind:"market"`)는 국면 변화로 원래 크게 움직이므로 최대 `주의`.

**벤치마크 비교(Champion–Challenger)** — `GET /reco/model/challengers?days=180&horizon=20` (10분 캐시)
같은 기간·규칙에서 가중치만 바꾼 단순 모형(동일 가중·모멘텀 단일·저변동 단일)과 비교 —
`models[]: {key, label, periods, avg_excess, win_rate, ic_mean, cum_excess, vs_champion:{n, mean_diff, t_stat, champion_better_share, verdict}}`.

**모델 검증(백테스트)** — `GET /reco/top5/backtest?days=180&horizon=20`
과거 기준일마다 그날까지의 데이터로만 Top 5 를 뽑아 `horizon` 거래일 뒤 성과 측정(워크포워드, 거래비용 0.25% 차감).
```json
{ "available": true, "model_version": "mf-2.0", "params": { "days": 180, "horizon": 20, "step": 20, "cost_bps": 25 },
  "summary": { "periods": 7, "avg_top": 0.021, "avg_bench": 0.012, "avg_excess": 0.009, "win_rate": 0.57,
               "hit_rate": 0.54, "ic_mean": 0.06, "icir": 0.4, "cum_top": 0.15, "cum_bench": 0.09, "max_drawdown": -0.04 },
  "verdict": "…", "curve": [ { "date": "2026-05-14", "top": 0.012, "bench": 0.004 } ],
  "caveats": ["과거 성과는 미래 수익을 보장하지 않아요.", "…생존 편향…"] }
```
> 수익률·비율은 **소수(0.021 = 2.1%)**(다른 API 의 % 값과 다름). 값은 허용 값 중 가장 가까운 값으로 맞춘다 — `days` 60/90/120/180/250/400 ·
> `horizon`·`step` 5/10/20/40/60 · `limit` 3/5/10(실제 쓴 값은 `params`). 캐시에 없는 조합을 새로 계산시키는 요청은 IP 당 1분 12번(넘으면 429).

**한 달 전 추천 종목 투자 결과** — `GET /reco/top5/review?days=30`
30일 전 마지막 거래일에 **실제로 보여 준 Top 5(스냅샷, 없으면 그날까지의 데이터로 재계산)** 를 그날 종가에 샀다면 지금 몇 % 인지 + 같은 기간 전체 평균.
```json
{ "available": true, "days": 30, "as_of": "2026-08-25", "avg_return": 1.8, "wins": 4, "count": 5,
  "bench_return": 1.2, "excess_return": 0.6, "from_snapshot": true, "model_version": "mf-2.0",
  "top": { "rank": 1, "ticker": "035720", "name": "카카오", "tag": "골든크로스 유력", "entry_date": "2026-08-25",
           "entry_price": 44160, "price": 45200, "price_date": "2026-09-24", "return_pct": 2.36 },
  "items": [ /* top 과 같은 형태 × 5 */ ] }
```
> `top.return_pct ≥ 0` 이면 웃는 양봉이(`/static/assets/happy.png`), 음수면 우는 양봉이(`sad.png`). 데이터 부족 시 `{ "available": false }`.

**오늘의 용어** — `GET /learn/today` (한국 날짜 기준, 같은 날엔 모두 같은 용어 — 홈 '1분 용어 학습'과 학습하기 '오늘의 용어'가 함께 씀)
```json
{ "kind": "주식 용어", "name": "골든크로스", "en": "Golden Cross", "term": "골든크로스",
  "desc": "단기 이동평균선이 장기 이동평균선을 위로 뚫고…", "analogy": "…", "box": "비유",
  "level": "mid", "cat": "보조지표·기법", "tab": "terms" }
```

**진행 중 실습 배너** — `GET /practice/home` (로그인 계정 또는 게스트 쿠키 기준)
```json
{ "active": null }
// 진행 중이면 '요약'(3.3 의 전체 상태가 아님 — series·account 없음):
{ "active": { "session_id": 12, "scenario_id": "samsung-golden", "name": "삼성전자 골든크로스 구간",
              "cursor_date": "2020-05-29", "progress": { "current": 41, "total": 86, "pct": 48 },
              "total_asset": 10231500, "return_rate": 2.32 } }
```
> 이어서 하려면 `GET /practice/{session_id}` 로 전체 상태를 받는다.

### 3.2 학습

> **지금 서빙하는 `learn.html` 은 팀 `assets/terms.js`(169개)를 그대로 쓴다.** 서버 사전(`GET /learn/dictionary`)은 같은 카드에 docx·지표 상세를 얹은
> 확장판 — 다른 클라이언트(앱 등)용. '이해했어요' 저장은 서버 사전 이름으로 검증한다(화면 용어 ⊂ 서버 사전 — 시험이 확인).

**용어 사전(학습하기 화면 전체)** — `GET /learn/dictionary`
```json
{ "count": 141, "version": "2026-09-29",
  "categories": ["차트 기초","거래 기초","자금·계좌","시장·지수","기업·재무","투자 상품","제도·이벤트","위험 관리","보조지표·기법"],
  "levels": { "basic": "초급", "mid": "중급" },
  "practice": { "golden": { "label": "골든크로스 실습하러 가기", "scenario": "samsung-golden" },
                "rsi": { "label": "RSI 실습하러 가기", "scenario": "naver-rsi" }, "any": { "label": "실습하러 가기", "scenario": null } },
  "items": [ {
    "name": "RSI", "en": "Relative Strength Index", "level": "mid", "cat": "보조지표·기법",
    "analogy": "…양봉이의 한 줄 비유…", "explain": "…쉬운 개념 풀이…", "box": "비유",
    "alias": ["상대강도지수"], "related": ["과매수 / 과매도", "낙폭과대"], "practice": "rsi",
    "notes": [ { "name": "RSI", "define": "…", "howto": "…읽는 법…", "caution": "…오해 주의…" } ],
    "detail": { "kind": "indicator", "title": "RSI 14", "desc": "…",
                "rows": [["계산","상승폭 합 ÷ (상승폭+하락폭) 합"],["기간","기본 14일"],["보는 것","70 위 과열 · 30 아래 침체"],["주의","…"]] },
    "src": "team" } ] }
```
- 출처: `src` = `team`(팀 terms.js — 문구 그대로) · `doc`(팀 「주식 기초 용어 사전」 docx 에만 있던 용어, `box:"정의"` → 비유 칸 제목을 '한 줄 정의'로) · `ours`(백엔드 용어).
- `notes` = docx 의 한 줄 정의·읽는 법·오해 주의(카드에 '더 알아보기'). `detail` = 보조지표·매매기법의 계산·기간·보는 것·주의 / 신호·위험.
- `related` 는 실제 있는 카드 이름으로만 정리돼 있다. `practice` 키 → `practice` 표의 `scenario`(실습 시나리오 id).
- 재생성: 팀이 terms.js 나 docx 를 고치면 `python tools/build_learn_terms.py <팀 저장소 경로>` → `app/data/learn_terms.json`.

**'이해했어요'(학습 완료) 계정 저장** — 로그인 필요. 비로그인은 learn.html 처럼 localStorage 로 두고, 로그인하면 아래로 옮긴다.

| 메서드 경로 | body | 응답 |
|---|---|---|
| `GET /learn/progress` | – | `{ "done": ["PER","골든크로스"], "count": 2, "total": 141 }` |
| `POST /learn/progress` | `{ "term": "골든크로스", "done": true }` (false=해제) | 같은 형태(갱신된 목록). 없는 용어면 404 |
| `POST /learn/progress/sync` | `{ "terms": ["공매도", …] }` — 로그인 전 브라우저에 표시한 것 합치기 | 같은 형태 |
| `POST /learn/progress/reset` | – | 같은 형태(빈 목록) — '학습 기록 지우기' |

**열람 기록(선택, 로그인 시 저장)** — `POST /learn/log` `{ "kind": "term", "item_key": "RSI", "item_name": "RSI" }` → `{ "ok": true }` (마이페이지 '학습 로그')

**Daily Stock Quiz(로그인 시 계정 기록)** — 화면은 `web/assets/daily-quiz.js` 의 `buildDailyQuiz(날짜)` 로 그날 5문제를 만든다.
다 풀면 **고른 보기 번호만** 보내고 서버가 같은 문제은행(`app/data/daily_quiz.json` — `tools/build_daily_quiz.py`)·같은 공식으로 다시 만들어 채점한다.

| 메서드 경로 | body | 응답 |
|---|---|---|
| `GET /learn/quiz[?date=YYYY-MM-DD]` | – | `{ "date", "done": false, "answers": null, "score": null, "total": 5, "reward": 0, "total_days": 3, "reward_per_correct": 100000 }` |
| `POST /learn/quiz` | `{ "date": "2026-10-27", "answers": [2,0,3,1,1] }` (화면에 보인 순서의 보기 번호) | `{ "date", "done": true, "answers", "score": 3, "total": 5, "reward": 300000, "balance": 2300000, "duplicate": false, "total_days": 4, "reward_per_correct": 100000 }` |

같은 날 두 번째 제출은 바꾸지 않고 처음 결과(`duplicate: true`, 보상 다시 없음). 날짜는 한국 날짜 기준 하루 앞뒤까지만(400).
**정답 보상(2026-10-31)** — 점수 × `reward_per_correct`(기본 10만 원, `.env` `QUIZ_REWARD_PER_CORRECT`)를 기록과 같은 트랜잭션에서
지갑에 넣는다(원장 사유 `quiz_reward` → 마이페이지 '내 활동'에 '퀴즈 보상'). **한국 날짜 하루 1번** — 그날 이미 받았으면
기록만 남고 `reward: 0` + `reward_skipped`(안내 문구). 화면은 `reward` 를 그대로 보여 주고, 잔액은 `balance`(또는 SSE `wallet`)로 갱신.

### 3.3 실습

**웹 실습 기록(지금 서빙하는 `practice.html`)** — 매매는 화면이 차트를 그리며 계산하고, 로그인했으면 한 판씩 보낸다(로그인 전엔 브라우저에만).

| 메서드 경로 | body | 응답 |
|---|---|---|
| `POST /practice/records` | `{ "tech": "golden"\|"rsi"\|"pullback"\|"support", "mode": "practice"\|"real", "traded": true, "ret": 0.052, "entry_date": "2019-05-21"(실전만) }` | `{ "ok": true, "summary": { "golden": { "tries", "best", "real_tries", "real_best", "traded" } } }` |
| `GET /practice/records?limit=20&offset=0` | – | `{ "summary", "items": [{ "id","tech","mode","traded","ret","real_entry","created_at" }], "total", "has_more" }` |

`ret` 은 비율(0.052 = +5.2%). 서버는 다시 계산할 수 없는 값이라 **범위(−100%~+1000%)·종류만 확인하고 가상머니와 잇지 않는다**.
계정당 1시간 `PRACTICE_RECORD_MAX_PER_HOUR`(기본 120)건. 아래 시나리오 API(서버가 차트를 주는 옛 실습)는 다른 클라이언트용으로 남아 있다.

**시나리오 목록 + 내 상태** — `GET /practice/scenarios`
```json
{ "stats": { "done": 1, "total": 6, "by_difficulty": { "초급": 1, "중급": 0, "고급": 0 } },
  "active_scenario": null,
  "items": [
    { "scenario_id": "samsung-golden", "name": "삼성전자 골든크로스 구간", "stock": "삼성전자",
      "ticker": "005930", "difficulty": "초급", "start_date": "2026-01-05", "end_date": "2026-04-30",
      "objective": "SMA 5가 SMA 20을 위로 통과하는 시점을…", "indicators": ["SMA5","SMA20"],
      "status": "none", "progress": null, "best_return": null, "attempts": 0, "locked": false, "session_id": null } ] }
```
`status` = `none`(미시작) | `active`(진행 중) | `done`(완료). 진행 중 실습이 있으면 다른 미시작 실습은 `locked:true`.
시나리오 6개: `samsung-golden`(초급) · `naver-rsi`(중급) · `kakao-ma-fail`(고급) · `lgchem-sideways`(중급) · `skhynix-divergence`(고급) · `hyundai-volume`(초급).

**시작** — `POST /practice/start` `{ "scenario_id": "samsung-golden" }` → **실습 상태(아래)**.

**진행 상태** — `GET /practice/{session_id}` ← 실습 화면의 핵심 응답
```json
{ "session_id": 3, "scenario_id": "samsung-golden", "name": "삼성전자 골든크로스 구간",
  "stock": "삼성전자", "ticker": "005930", "difficulty": "초급", "objective": "…", "indicators": ["SMA5","SMA20"],
  "status": "active", "cursor_date": "2026-02-03", "start_date": "2026-01-05", "end_date": "2026-04-30",
  "progress": { "current": 21, "total": 78, "pct": 27 }, "has_next": true, "price": 70200, "day_change": 0.86,
  "series": [ { "trade_date": "2026-01-05", "open": 68000, "high": 69000, "low": 67500, "close": 68500, "volume": 12000000 } ],
  "overlays": { "SMA5": [null, null, 68200.0], "SMA20": [null] },
  "account": { "cash": 3000000, "holding_qty": 100, "holding_avg": 69000.0, "holding_value": 7020000,
               "pnl": 120000, "pnl_pct": 1.74, "total_asset": 10020000, "return_rate": 0.2, "start_cash": 10000000, "buyable": 42 },
  "final_return": null, "best_return": null }
```
- **차트**: `series`(캔들) + `overlays`(지표 배열, series 와 같은 길이, 워밍업 구간은 `null`). **미래 구간은 오지 않는다**(cursor 까지만).
- **하루/5일 진행** — `POST /practice/{id}/step` `{ "days": 1 }`(또는 5) → 갱신된 상태. 끝에 닿으면 자동 종료(`status:"done"`, `final_return`).
- **매수/매도** — `POST /practice/{id}/trade` `{ "side": "BUY", "quantity": 30 }` + `Idempotency-Key` → 갱신된 상태.
  25/50/75/최대 버튼은 `account.buyable` 로 계산(예: 50% = `Math.floor(buyable*0.5)`).
- **지금 종료** — `POST /practice/{id}/end` → 정산된 상태(`status:"done"`).

### 3.4 종목 — 국내 · 해외

**순위표** — 국내 `GET /market/stocks?sort=…`(3.1) · 해외 `GET /overseas/list?sort=value&limit=50`(`sort` 에 `price` 추가)
```json
{ "sort": "value", "live": false, "as_of": "2026-10-09", "source": "gen", "usd_krw": 1348.2,
  "status": { "live": false, "sim": false, "phase": "pre", "source": "gen", "session_date": "2026-10-09" },
  "items": [ { "rank": 1, "symbol": "META", "ticker": "META", "name": "Meta Platforms Inc.", "name_ko": "메타",
               "exchange": "NASDAQ", "sector": "커뮤니케이션", "currency": "USD", "price": 645.06, "change": 1.68,
               "change_rate": 0.26, "volume": 69528473, "value": 44850036793, "market_cap": 1625551199999,
               "price_krw": 869669, "live": false, "status": "close", "session_date": "2026-10-09" } ] }
```
**해외 지수** — `GET /overseas/indices` → `items[]`: S&P 500(`SPX`)·나스닥·다우존스·원/달러.

**검색**
- **자동완성(타이핑 중)** — `GET /search/suggest?q=삼서&limit=8&kind=all|kr|us&prefer=kr|us` · **기록 안 함**
  이름·코드·초성(`ㅅㅅㅈㅈ`)·**한글 조합 중 글자**(`삼서`→삼성)·대소문자/공백 무시(`sk`, `SK 하이`)·별칭(`네이버`→NAVER, `애플`)·
  **영문 약자의 한글 읽기**(`엘지`→LG화학, `에스케이`→SK하이닉스)·국내+해외. 정확도 → 시총 순.
  - `kind=all`(기본): 국내·해외를 시장별로 따로 찾아 합친다. `prefer` 쪽(기본 `kr`)이 주 시장, 다른 시장은 3칸까지 보장.
  - **모든 검색창은 `kind=all`**(해외 종목 화면은 `kind=all&prefer=us`). 국내 전용은 장기 모의투자 매수창(`kind=kr`)뿐.
```json
{ "query": "삼서", "took_ms": 2.1, "items": [
  { "type": "kr", "ticker": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체",
    "match": "typing", "price": 71000, "change_rate": -0.18, "live": false } ] }
```
- **실제로 고른 검색 기록(엔터·클릭)** — `POST /search/log` `{ "keyword": "삼성", "ticker": "005930" }` → 인기 검색 집계(IP 당 분당 30건)
- **인기 검색 종목** — `GET /search/popular?limit=8&days=7` → `{ items:[{type,ticker,name,cnt}], basis:"search"|"mixed"|"value" }`(기록이 적으면 거래대금 상위로 채움)
> **엔터 처리**: 하이라이트된 후보가 있으면 그것, 없으면 `/search/suggest` 를 즉시 호출해 1순위로 이동(디바운스 기다리지 말 것).
> 한글 IME 조합 중 엔터(`e.isComposing` / keyCode 229)는 `compositionend` 뒤에 처리. 늦게 온 응답이 최신 결과를 덮지 않게 AbortController/요청 번호.
> 참조 구현: `web/mypage.html` 의 관심 종목 검색(`watchSearch()` — 늦은 응답 무시). 팀 시안의 헤더 검색창은 아직 이동할 화면이 없어 연결 전.

**국내 종목 상세(펀더멘털)** — `GET /stocks/{ticker}/detail`
```json
{ "ticker": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체",
  "price": 71000, "change": 900, "change_rate": 1.28, "live": false,
  "status": "close", "session_date": "2026-10-01", "phase": "pre", "source": "krx",
  "fundamentals": { "open": 70200, "high": 71300, "low": 70000, "volume": 15200000,
    "value": 1079200000000, "market_cap": 423900000000000, "prev_close": 70100,
    "per": 14.2, "pbr": 1.4, "dividend_yield": null, "high_52w": 88000, "low_52w": 64500,
    "fundamentals_source": "dart" } }
```
> `per`·`pbr`·`dividend_yield` 는 **null 일 수 있다** → '-'. `fundamentals_source`: `dart`(실시세×DART 재무) · `sample`('표본' 표시) · `null`.
> 국내 유니버스가 비어 있으면(첫 수집 전) 404 + `"아직 시세 데이터가 없어요 — 첫 수집(run.py real) 뒤에 보여요"`.

**해외 종목 상세** — `GET /overseas/{symbol}/detail`(예: `AAPL`, 없으면 404)
```json
{ "symbol": "AAPL", "name": "Apple Inc.", "name_ko": "애플", "exchange": "NASDAQ", "currency": "USD",
  "price": 256.8, "change": -0.99, "change_rate": -0.38, "price_krw": 346217, "status": "close", "phase": "pre", "source": "gen",
  "fundamentals": { "open": 259.15, "high": 259.3, "low": 255.43, "prev_close": 257.79, "volume": 50982702, "value": 13092357873,
                    "market_cap": 3800640000000, "market_cap_krw": 5124022848000000, "per": 46.91, "pbr": 9.58,
                    "high_52w": 263.23, "low_52w": 219.26, "revenue": 491609786615, "net_income": 81012896085, "fiscal_year": 2025 },
  "financials_source": "gen", "forex": { "rate": 1348.2, "source": "fallback", "ts": "…" } }
```

**캔들 차트(기간별 + 지표)** — 국내 `GET /stocks/{ticker}/ohlc?period=6M&indicators=SMA5,SMA20,RSI14` · 해외 `GET /overseas/{symbol}/ohlc?…`(같은 형태)
`period` = `1M|3M|6M|1Y`. `indicators`(쉼표) = `SMA5,SMA20,SMA60,RSI14`.
```json
{ "ticker": "005930", "period": "6M",
  "series": [ { "trade_date": "2026-03-11", "open": 70000, "high": 71000, "low": 69500, "close": 70800, "volume": 13000000 } ],
  "overlays": { "SMA5": [69800.0], "SMA20": [69100.0], "RSI14": [58.3] } }
```

**종목 관련 뉴스** — `GET /stocks/{ticker}/news?limit=10&offset=0` (최신순, limit 최대 50)
```json
{ "ticker": "005930",
  "items": [ { "title": "삼성전자, HBM 공급 협상 순항", "url": "https://news.google.com/…", "source": "한국경제",
               "published_at": "2026-09-30", "published_ts": "2026-09-30T09:12:00+09:00", "type": "호재" } ],
  "summary": { "days": 7, "since": "2026-09-23", "pos": 4, "neg": 1, "neu": 3, "total": 8 } }
```
`type` = 호재|악재|중립(제목 표현으로 자동 분류한 참고 정보). `url` 은 http(s) 원문만(없으면 `""` — 제목만 표시).

**리스크·재무(종목 상세 심화 탭)** — 4.1.

### 3.5 뉴스

> **팀 `news.html` 은 `assets/news-api.js` 의 `NEWS_CONFIG.API_BASE` 에 서버 주소만 넣으면 이 API 로 채워진다**
> (`GET {API_BASE}/api/news/home` — 경로·응답 형태 모두 news-mock.js 맨 위 주석의 계약과 같다). 두 가지만:
> 1. 다른 출처에서 부른다면 `fetch(..., { credentials: 'include' })` 의 **`credentials` 줄을 지운다** — 뉴스는 로그인이 필요 없고,
>    교차 출처에서 credentials 를 켜면 브라우저가 응답을 막는다(서버가 자격 증명 CORS 를 허용하지 않음). 같은 출처면 상관없다.
> 2. **조회수(`views`)는 `null`** — 포털·언론사가 기사 조회수를 공개하지 않는다. 대신 `coverage: { articles, outlets }`(같은 이슈를 다룬
>    기사 수·언론사 수 = 보도량)를 주고 순위도 이 기준이다. `조회수 높은 순` → `보도량 순`, `조회 48,210` → `관련 3건 · 2곳`.

**뉴스 홈(뉴스 화면 전체)** — `GET /api/news/home` (같은 응답: `GET /news/home`)
```json
{ "status": "success", "message": "",
  "data": {
    "updatedAt": "2026-09-29T23:04:12+09:00", "todayLabel": "오늘 기준",
    "todayTop": [ { "id": "1523", "title": "삼성전자, HBM 수주 확대…", "summary": "", "source": "한국경제",
      "publishedAt": "2026-09-29T20:41:00+09:00", "url": "https://news.google.com/…", "views": null,
      "coverage": { "articles": 7, "outlets": 5 }, "sentiment": "호재",
      "stocks": [ { "ticker": "005930", "name": "삼성전자" } ],
      "related": [ { "id": "1519", "title": "…같은 이슈 다른 보도…", "source": "…", "publishedAt": "…", "url": "…" } ] } ],
    "themes": [ { "rank": 1, "name": "반도체", "summary": "(최신 관련 기사 제목)", "articleCount": 23,
                  "changeRate": 1.42, "keywords": ["메모리","HBM","장비"], "trend": [8,11,9,14,12,18,23],
                  "stocks": [ { "ticker": "000660", "name": "SK하이닉스", "changeRate": 2.1 } ], "sentiment": { "pos": 9, "neg": 2 } } ],
    "rankings": { "weekLabel": "9.23 ~ 9.29", "monthLabel": "9월", "basis": "보도량(관련 기사·언론사 수)",
                  "week": [ { "rank": 1, "rankChange": 2, "isNew": false, "id": "…", "title": "…", "source": "…",
                              "publishedAt": "…", "url": "…", "views": null, "coverage": { "articles": 31, "outlets": 12 } } ],
                  "month": [ "…같은 형태…" ] },
    "latest": [ { "id": "…", "title": "…", "summary": "", "source": "…", "publishedAt": "…", "url": "…",
                  "views": null, "sentiment": "악재", "stocks": [], "dateOnly": false } ],
    "aiPicks": { "recommend": { "ticker": "005930", "name": "삼성전자", "sentiment": 0.6, "pos_ratio": 0.7, "news_count": 12,
                                "evidence": [ { "title": "…", "type": "호재", "url": "…", "source": "…" } ] },
                 "avoid": null },
    "sample": false } }
```
- **이슈** = 같은 종목으로 묶인 기사들. 오늘의 이슈 TOP = 최근 24시간 이슈를 (언론사 수, 기사 수, 최신순)으로 — 5개가 안 되면 최근 3일(`todayLabel:"최근 3일"`).
- **테마**: 테마 사전에 걸린 오늘 기사 수 순. `changeRate` = 대표 종목 평균 등락률(%), `trend` = 최근 7일 일별 기사 수.
- **랭킹**: 주간 = 최근 7일, 월간 = 이번 달. `rankChange` = 직전 기간 순위 − 이번 순위(올랐으면 +), 새 이슈는 `isNew:true`.
- **AI 뉴스 픽**(`aiPicks`): 호재 우세 종목(`recommend`)·악재 우세 종목(`avoid`) + 근거 기사. 데이터가 없으면 `null`.
- `summary` 는 항상 `""`(본문 저장·요약 안 함) · `dateOnly:true` 면 시각 없이 날짜만 · `sample:true` 면 '샘플 데이터' 배지.
- 서버가 결과를 캐시한다(새 기사가 들어오면 바로 다시 계산, 아니면 최대 2분). 화면 자동 새로고침은 2~5분 간격이면 충분.

**뉴스 목록** — `GET /news?limit=20` (title/url/source/published_at/published_ts — 본문 없음, 링크만)

### 3.6 로그인 / 마이페이지

로그인·로그아웃·갱신·추가 인증·세션은 2.1~2.3. 그 밖:

| 메서드 경로 | body | 응답 |
|---|---|---|
| `POST /auth/change-password` | `{old_password, new_password}` | `{ "ok": true }` — 이 세션 유지, 다른 기기 종료 |
| `DELETE /me` | – (**추가 인증 필수**) | `{ "ok": true }` — 회원 탈퇴(계정 데이터 즉시 삭제, 쿠키 정리) |

**마이페이지** — `GET /me` — 각 기록은 **최근 5건(`preview`)** 만, 전체 건수는 `counts`
```json
{ "user": "jane", "profile": { "username": "jane", "created_at": "2026-09-01T…" },
  "survey": { "type": "중립형", "score": 17, "description": "…", "created_at": "…" },
  "practice": { "done": 1, "active": 0, "total": 3, "sessions": [ { "session_id": 3, "scenario_id": "samsung-golden",
     "name": "삼성전자 골든크로스 구간", "difficulty": "초급", "status": "done", "final_return": 3.2, "best_return": 3.2, "attempts": 1 } ] },
  "learning": { "count": 12, "items": [ { "kind": "term", "item_name": "RSI", "viewed_at": "…" } ] },
  "access":   { "count": 30, "items": [ { "id": 298, "event": "login", "detail": "Chrome · Windows", "at": "…" } ] },
  "watchlist":{ "count": 2, "items": [ /* 관심종목 item 형태(전체) */ ] },
  "wallet": { "username": "jane", "balance": 2000000, "ledger": [ /* 최근 5건 */ ] },
  "attendance": { "streak": 3, "total_days": 12, "checked_in_today": true, "daily_reward": 1000000 },
  "invest": { "open_count": 2, "closed_count": 1, "cost": 6000000, "value": 6390000, "pnl": 390000, "pnl_pct": 6.5, "realized_pnl": 12000 },
  "counts": { "wallet": 27, "practice": 3, "learning": 12, "access": 30, "survey": 1, "watchlist": 2 },
  "preview": 5 }
```

**로그 전용 페이지(더보기)** — `GET /me/logs/{kind}?limit=20&offset=0`
`kind` = `wallet`(지갑 거래) · `practice` · `learning` · `access`(접속 기록) · `survey`(성향 이력) · `watchlist`.
지갑만 `&filter=` = `all` | `reward`(출석·환영금) | `charge`(충전) | `transfer`(송금) | `invest`(장기투자).
```json
{ "kind": "wallet", "title": "지갑 거래 내역", "total": 27, "limit": 20, "offset": 0, "has_more": true,
  "filter": "all", "filters": [ { "key": "all", "label": "전체" }, { "key": "reward", "label": "출석·환영금" }, { "key": "charge", "label": "충전" },
                                { "key": "transfer", "label": "송금" }, { "key": "invest", "label": "장기투자" } ],
  "items": [ { "id": 41, "reason": "iap", "reason_ko": "충전(인앱결제)", "amount": 1000000, "balance_after": 15629450,
               "ref": "…", "memo": "가상머니 100만 충전", "created_at": "2026-09-24T10:57:01" } ] }
```
> 마이페이지: 섹션마다 5건 + **[더보기]** → 전용 페이지(20건씩 페이지 이동). 접속 기록의 `detail` 은 기기 계열(브라우저·OS)만.

**배지 / 내 성향(가벼운 조회)** — `GET /me/badges` → `{ "chat_unread": 2, "friend_requests": 1 }` · `GET /me/survey` → `{ "survey": {…} | null }`

**관심종목** — 목록 `GET /watchlist` · 추가 `POST /watchlist/{ticker}` · 해제 `DELETE /watchlist/{ticker}`
```json
// GET /watchlist
{ "count": 2, "items": [ { "ticker": "005930", "name": "삼성전자", "market": "KOSPI", "price": 71000, "change_rate": 1.28, "live": false } ] }
// POST/DELETE → { "ok": true, "ticker": "005930", "watched": true }   // 해제 시 watched:false
// 없는 종목코드 POST → 404 {"detail":"종목 없음: 999999"}  (별을 켜지 말 것)
```
> 시세가 아직 없는 종목도 목록에서 빠지지 않고 `price`·`change_rate` 가 `null`(`-` 로 표시).

### 3.7 투자 성향 분석(설문) — 팀 문서 `OUTOFSTOCK_투자성향설문.docx` 기준

5개 영역(A 투자 목적과 기대 수익 · B 손실 감내도 · C 투자 자금 성격과 투자 기간 · D 투자 경험과 지식 · E 종목 선택) × 4문항 = **20문항 은행**.
**영역별 2문항씩 무작위 → 10문항**, 보기는 순서대로 0·1·2·3점 → 총점 0~30. 등급: **0~10 안정형 · 11~20 중립형 · 21~30 공격형**.

**문항** — `GET /survey/questions` (매번 다른 10문항) · `?all=true` 면 20문항 전체
```json
{ "count": 10, "max_score": 30, "per_domain": 2,
  "questions": [ { "id": "Q3", "no": 3, "domain": "A", "domain_name": "투자 목적과 기대 수익",
    "q": "100만 원을 넣었을 때, 가장 마음에 드는 결과는?",
    "options": [ { "text": "매년 3만 원씩 꾸준히 벌고, 손해는 거의 없음", "score": 0 }, { "text": "…", "score": 1 },
                 { "text": "…", "score": 2 }, { "text": "잘되면 50만 원 벌고, 안되면 30만 원 손해", "score": 3 } ] } ],
  "domains": [ { "id": "A", "name": "투자 목적과 기대 수익", "measure": "…" } ],
  "grades": [ { "type": "안정형", "grade": "안정형 · 저위험", "range": "0~10점", "summary": "원금 변동을 줄이는 쪽이에요" } ] }
```

**제출** — `POST /survey/submit` (로그인 상태면 내 계정 성향 이력에 저장)
body 권장: `{ "answers": [ { "id": "Q3", "answer": 2 }, … 출제된 10문항 ] }` · 호환: `{ "answers": [2,0,3,…] }`(보기 번호 10개 = 점수)
```json
{ "type": "중립형", "key": "neutral", "risk": "중위험", "grade": "중립형 · 중위험", "score": 17, "max_score": 30,
  "summary": "수익과 안정 사이에서 균형을 잡는 쪽이에요",
  "description": "수익과 안정 사이에서 균형을 잡는 쪽이에요. 중위험 자산을 중심으로 조금씩 경험을 넓혀가요.",
  "breakdown": [ { "domain": "A", "name": "투자 목적과 기대 수익", "score": 4, "max": 6 } ],
  "products": [ /* /reco/products 의 items 3개 */ ], "product_rule": "중위험 2 + 저위험 1", "notice": "…" }
```
> 영역별 2문항이 아니거나, 없는 문항·중복·보기 범위(0~3) 밖이면 `400`. 결과 이미지: `/static/assets/risk-{key}.png`(key = stable | neutral | aggressive).

### 3.8 친구 · 채팅 — 요청을 **상대가 수락해야** 친구

| 메서드 경로 | 설명 | 응답 |
|---|---|---|
| `GET /users?q=` | 다른 사용자 + 나와의 관계 | `items[]: {username, created_at, relation, is_friend}` — `relation` = `friend` \| `sent` \| `received` \| `none` |
| `POST /friends/{아이디}` | 친구 요청 보내기 | `{ "ok": true, "friend": "jane", "status": "pending" }` — 상대가 먼저 요청해 뒀으면 바로 `"friends"` |
| `POST /friends/{아이디}/accept` | 받은 요청 수락 → 서로 친구 | `status: "friends"` |
| `POST /friends/{아이디}/decline` | 받은 요청 거절 | `status: "declined"` |
| `DELETE /friends/{아이디}/request` | 보낸 요청 취소 | `status: "canceled"` |
| `DELETE /friends/{아이디}` | 친구 끊기(양쪽 모두) | `status: "none"` |
| `GET /friends` | 내 친구(서로 수락한 사이만) | `items[]: {username, since}` |
| `GET /friends/requests` | 대기 중 요청 | `{ "received": [{id, username, created_at}], "sent": […] }` |
| `GET /friends/{아이디}/profile` | 공개 프로필(민감정보 제외) | `{username, created_at, survey_type, practice_done, invest:{open_count, pnl_pct}, relation, is_friend, is_me}` |
| `GET /chat/rooms` | 대화방 목록(최신순) | `{ items:[{peer, last_id, last_body, last_at, last_mine, unread, is_friend}], unread_total }` |
| `GET /chat/{상대}/messages?after_id=0&limit=50` | 대화 내용(조회 시 읽음 처리). `after_id` = 새 메시지만, `before_id` = 이전 메시지(위로 스크롤) | `{ peer, items:[{id, sender, receiver, body, created_at, read, mine}], peer_last_read_id, can_send, has_more }` |
| `POST /chat/{상대}/messages` | `{ "body": "안녕" }` — 1~1000자 | 보낸 메시지(위 `items` 한 줄과 같은 형태) |

> 채팅은 **서로 친구일 때만** 전송 가능(수락 전·친구 끊은 뒤엔 400, 지난 대화는 조회 가능). 새 메시지·친구 요청은 SSE 로 바로 온다(2.4).
> 채팅 본문은 서버에 암호화 저장되고, 화면에 넣을 때는 **항상 이스케이프**(참조 구현 `esc()`).

---

## 4. 부가 화면 — 리스크 · 가상머니 지갑

### 4.1 리스크 · 재무(종목 상세 심화 탭)
- `GET /stocks/{ticker}/risk` — 변동성·VaR(95/99%)·**ES 97.5%(`es_975`)**·최대낙폭
- `GET /stocks/{ticker}/var-backtest?conf=0.95` — VaR 모형 검증(`conf` 0.8~0.999). **롤링 아웃오브샘플** + Kupiec·Christoffersen + **바젤 신호등**.
  데이터가 짧으면 `method:"in_sample"`(참고용).
```json
{ "ok": true, "method": "rolling_oos", "window": 250, "observations": 250, "confidence": 0.99,
  "var": 0.031, "breaches": 6, "expected_breaches": 2.5, "kupiec_lr": 3.55, "kupiec_pvalue": 0.06, "reject_model_at_95": false,
  "christoffersen": { "lr_ind": 0.3, "p_ind": 0.58, "reject_ind": false, "clustered_breaches": 0, "lr_cc": 3.85, "p_cc": 0.15, "reject_cc": false },
  "traffic_light": { "zone": "yellow", "label": "황색(주의)", "green_max": 4, "yellow_max": 9 },
  "recent_breaches": [ "2026-08-04" ], "verdict": "주의(초과가 많거나 몰려서 발생)" }
```
- `GET /stocks/{ticker}/soundness` — 재무 건전성 점수·등급 + 회계 기반 부실예측 `distress`(Altman Z'') + **시장 기반 `merton`**
```json
"merton": { "ok": true, "dd": 5.42, "pd": 0.00003, "pd_band": "매우 낮음", "asset_value": 512000000000000, "asset_vol": 0.26, "converged": true,
            "inputs": { "equity": 420000000000000, "equity_vol": 0.31, "default_point": 61000000000000,
                        "default_point_rule": "유동부채 + ½×비유동부채", "risk_free": 0.03, "horizon_years": 1, "days": 250, "as_of": "2026-09-30" } }
```
> Merton(1974)·KMV: 시가총액·주가변동성·부도점으로 자산가치를 역산 → 부도거리 DD, PD = N(−DD). 금융업 제외. Top5 항목의 `metrics.merton_dd·merton_pd`.
- `GET /stocks/{ticker}/peers` — **비교 기업**: 재무 구조가 가까운 3곳과 주요 비율을 나란히(재무가 없으면 `{ "available": false, "reason" }` → 카드 숨김)
```json
{ "ticker": "035420", "available": true, "source": "dart",
  "base":  { "ticker": "035420", "name": "NAVER", "sector": "IT", "fiscal_year": 2025, "revenue": 10000000000000,
             "op_margin": 15.0, "net_margin": 12.0, "debt_ratio": 45.0, "fs_basis": "CFS" },
  "peers": [ { "ticker": "035720", "name": "카카오", "rank": 1, "distance": 0.0721, "op_margin": 13.0, "...": "…" } ],
  "method": "매출 규모(로그)·영업이익률·순이익률·부채비율을 0~1로 맞춘 뒤 거리가 가까운 순(업종 무관)", "note": "교육용 · …" }
```
- `GET /invest/risk`(로그인) — **내 포트폴리오 리스크·스트레스 테스트**(장기 모의투자 보유분)
```json
{ "ok": true, "stats_ok": true, "total_value": 6390000, "days": 250,
  "var_95": 0.021, "var_95_krw": 134190, "es_975": 0.034, "es_975_krw": 217260,
  "beta": 1.12, "beta_complete": true, "beta_source": "kospi", "annual_volatility": 0.27,
  "scenarios": [ { "name": "코스피 −10% (단기 폭락)", "market_shock": -0.1, "loss": -715680, "loss_pct": -0.112 } ],
  "historical": [ { "name": "과거 최악의 하루 (2026-04-07)", "loss": -301200, "loss_pct": -0.047 } ],
  "concentration": { "hhi": 0.52, "effective_n": 1.92, "max_weight": 0.66, "limit": 0.4, "limit_breach": true },
  "items": [ { "ticker": "005930", "name": "삼성전자", "value": 4200000, "weight": 0.66, "beta": 1.21 } ] }
```
> 코스피 이력이 없으면 베타 1 가정(`beta_source:"assumed"`). 공통 거래일 20일 미만이면 `stats_ok:false`(집중도만).

### 4.2 가상머니 지갑(로그인 필요) — 마이페이지의 지갑·출석·송금·충전·장기 모의투자

| 메서드 경로 | 설명 |
|---|---|
| `GET /wallet` | `{ "username", "balance": 2000000, "ledger": [ { "ledger_id", "reason", "reason_ko", "amount", "balance_after", "ref", "memo", "created_at" } ] }` |
| `GET /attendance` · `POST /attendance/check-in` | 출석 현황 · 오늘 출석(하루 1번, 동시에 눌러도 1번 지급) → `{ today, checked_in_today, streak, total_days, daily_reward, recent[], claimed, reward_given, balance }` |
| `POST /transfer` `{to, amount, memo}` | 송금(메모 200자) — **`Idempotency-Key`** · 100만 원 이상·위험 신호면 추가 인증 · FDS 차단 가능(2.2) → `{ ok, amount, from, to, balance, memo, duplicate }` |
| `GET /invest` | 장기 모의투자 `{ summary:{open_count, closed_count, cost, value, pnl, pnl_pct, realized_pnl}, open:[…], closed:[…] }` |
| `POST /invest/buy` `{ticker, quantity}` · `POST /invest/{position_id}/sell` `{quantity?}` | 현재가 매수 · 매도(`quantity` 생략=전량, 0 이하는 400). `realized_pnl` 은 나눠 판 것까지 누적 |
| `GET /iap/products` · `POST /iap/purchase` `{store, product_id, receipt}` | 인앱 충전 상품 · 영수증 검증 후 적립(영수증은 해시로만 저장 — 같은 영수증 재사용 불가) |
| `GET /pay/toss/config` · `POST /pay/toss/prepare` `{product_id}` · `POST /pay/toss/confirm` `{paymentKey, orderId, amount}` | 토스 결제(`enabled:false` 면 버튼 숨김). 위험 신호가 있으면 충전도 추가 인증 |

> 잔액 부족 등 규칙 위반은 400, 동시에 겹친 요청은 409 — 둘 다 `detail` 을 보여 주면 된다. 잔액이 바뀌면 SSE `wallet` 이벤트가 온다.
> 돈의 정합성은 서버 원장(복식부기, 합계 0 제약)이 지킨다 — 화면은 응답의 `balance` 를 그대로 쓰고 직접 계산하지 않는다.

### 4.3 양봉이 꾸미기(`avatar.html`) — 가상머니로 아이템 사서 입히기

아이템 30개 = 부위 6(`hat` 모자 · `top` 옷 · `bottom` 바지 · `shoes` 신발 · `glasses` 안경 · `pet` 펫) × 등급 5(서민 1만 · 보통 10만 · 고급 100만 ·
명품 1,000만 · 전설 1억). id 는 `부위-등급`(예: `hat-5` 다이아 왕관). 그림·이름·가격은 `web/assets/avatar-items.js` 의 `AVATAR`
(`AVATAR.svg(입은옷)` 꾸민 양봉이 · `AVATAR.itemSvg(id)` 아이템만 · `AVATAR.dataUrl(입은옷)` `<img>` 용 · `AVATAR.fmt(원)` '1억 1,000만원').

| 메서드 경로 | 보냄 | 받음 |
|---|---|---|
| `GET /avatar/catalog` (로그인 불필요) | – | `{ slots:[{id,name}], tiers:[{tier,name,color,bg}], classes:[{min,name,color}], items:[{id,slot,tier,name,price,desc}] }` |
| `GET /avatar/me` | – | `{ owned:["hat-2",…], equipped:{"hat":"hat-2"}, worth:100000, class:{name:"흙수저", level:1}, balance }` |
| `POST /avatar/buy` | `{ "item_id": "hat-2", "price": 100000, "equip": true }` | 위 `me` 와 같은 모양 + `bought` · `balance`(차감 뒤) |
| `POST /avatar/equip` | `{ "items": { "hat": "hat-3", "pet": null } }` — 바꿀 부위만(`null` = 벗기) | 위 `me` 와 같은 모양(잔액 없음) |

- **가격은 서버가 정한다** — `price` 는 화면이 사용자에게 보여 준 값(확인용). 서버 가격과 다르면 **409**(아무것도 안 바뀜 — 새로고침 안내).
- 이미 가진 아이템 다시 사기 · 잔액 부족 · 안 산 아이템 입기 · 다른 부위 아이템 · 없는 부위 → **400**(`detail` 그대로 보여 주기).
  갈아입기는 한 트랜잭션 — 하나라도 틀리면 전부 그대로. 두 탭에서 동시에 눌러도 돈은 1번만 빠진다.
- 위험 신호(세션 IP 대역 변경 등)가 있으면 사기에 추가 인증(2.2 — 런타임이 창을 띄우고 다시 보낸다).
- 몸값 = 입은 아이템 가격 합 → `class`: 흙수저(0) · 동수저(30만) · 은수저(300만) · 금수저(3,000만) · 다이아수저(3억). 서버가 계산해 주지만
  화면도 같은 기준(`AVATAR.classOf(AVATAR.worth(입은옷))`)으로 '입어 보기' 상태를 그린다.
- 원장 사유 `avatar_buy`('양봉이 꾸미기') — 마이페이지 '내 활동' 필터 `shop`. 되팔기·선물 없음.

---

## 5. 화면 → API 매핑 요약

| 화면 | 핵심 API |
|---|---|
| **홈** | `/indices` · `/overseas/indices` · `/reco/top5`(`profile`) · `/reco/top5/review` · `/reco/model` · `/reco/model/monitor` · `/reco/model/challengers` · `/reco/top5/backtest` · `/market/stocks` · `/news` · `/reco/products` · `/learn/today` · `/practice/home` |
| **투자 성향 분석** | `/survey/questions` · `/survey/submit` · `/reco/products` · `/me/survey` |
| **학습하기** | **`/learn/dictionary`** · `/learn/today` · `/learn/progress`(+`/sync`·`/reset`, 로그인) · `/learn/log` |
| **실습하기** | `/practice/scenarios` · `/practice/start` · `/practice/{id}` · `/step` · `/trade` · `/end` |
| **국내 종목** | `/market/stocks` · `/search/suggest` · `/search/log` · `/search/popular` · `/stocks/{t}/detail` · `/stocks/{t}/ohlc` · `/stocks/{t}/news` · `/stocks/{t}/risk` · `/stocks/{t}/var-backtest` · `/stocks/{t}/soundness` · `/stocks/{t}/peers` · `/watchlist/{t}` |
| **해외 종목** | `/overseas/list` · `/overseas/indices` · `/overseas/{s}/detail` · `/overseas/{s}/ohlc` · `/search/suggest?kind=all&prefer=us` |
| **뉴스** | **`/api/news/home`**(이슈 TOP·테마·랭킹·최신·AI 뉴스 픽) · `/news` · `/stocks/{t}/news` |
| **장기 모의투자** | `/invest` · `/invest/buy` · `/invest/{id}/sell` · `/invest/risk` · `/search/suggest?kind=kr` |
| **양봉이 꾸미기** | `/avatar/catalog` · `/avatar/me` · `/avatar/buy` · `/avatar/equip` · `/me`(`avatar` 요약 — 프로필 그림) |
| **친구·채팅** | `/users` · `/friends/*` · `/friends/requests` · `/friends/{u}/profile` · `/chat/rooms` · `/chat/{u}/messages` · `/events` |
| **마이페이지** | `/me` · `/me/logs/{kind}` · `/me/badges` · `/watchlist` · `/wallet` · `/attendance*` · `/transfer` · `/iap/*` · `/pay/toss/*` · `/auth/change-password` · `DELETE /me` (+ 화면 미구현 API: `/auth/sessions` · `/auth/logout-all` · `/auth/mfa*`) |
| **로그인** | `/auth/register` · `/auth/login` · `/auth/refresh` · `/auth/step-up` · `/auth/logout` |

응답 형태가 더 궁금하면 로컬 서버의 `/docs`(Swagger)에서 눌러 보는 게 제일 빠르다. 필드가 더 필요하면 주원에게.

---

## 6. 문제 해결

**로그인·쿠키**
- 로그인은 되는데 다음 요청이 401 → 쿠키가 안 실림: ① 교차 출처로 부르고 있다(1.1 프록시로 같은 출처) ② `/api` rewrite 를 쓴다(갱신 실패) ③ HTTPS VM 인데 `http://` 로 열었다.
- 로그인·POST 가 403 `csrf_failed` → ① POST·DELETE 에 `X-CSRF-Token` 을 안 붙였다 ② Vite `changeOrigin: true` ③ HTTPS VM + http 개발 서버(1.1 `TRUSTED_ORIGINS`).
- 10분마다 로그아웃된다 → 401 `token_expired` 에서 `/auth/refresh` 를 안 부르고 있다(2.1).

**지수가 안 보이거나 이상할 때**
1. 서버에서 `python run.py fetch-index` → 코스피·코스닥 값과 **출처·기준일**, 실패한 소스와 이유가 출력된다.
2. `KRX ... 401/403` → KRX OpenAPI 는 **API 마다 이용신청**이 필요하다(openapi.krx.co.kr → 「KOSPI/KOSDAQ 시리즈 일별시세정보」).
   승인 전에도 KIS·야후로 대체되어 화면엔 나온다.
3. `GET /health` 의 `issues`(`index_sample`·`index_stale`·`krx_index_denied`·`stale_data` …)로 상태 확인. 세부(출처별 오류)는 관리자 헬스(`/admin/health`, 권한 필요).
4. 장중 실시간 지수: `python run.py realtime`(KIS 키) 이 30초마다 갱신. 수집기를 안 켜도 장중 `/indices` 요청 때 60초마다 백그라운드 갱신.

## 7. 돈이 오가는 요청 — 더블클릭·재시도 대비

- **송금** `POST /transfer` — `Idempotency-Key: <송금 1건마다 새 랜덤 문자열>`. 같은 키로 다시 보내면 송금은 1번만, 응답 `duplicate: true`.
  같은 키로 받는 사람·금액을 바꾸면 **409**. 버튼은 요청 중 비활성화. 401 `step_up_required` 로 확인 창을 거친 재시도는 **같은 키**를 쓴다(아직 처리 전이므로).
- **토스 승인** `POST /pay/toss/confirm` — 결제 완료 화면에서 두 번 불려도(새로고침·React StrictMode 의 effect 2회) 적립은 1번, 두 번째는 `duplicate: true`.
- **실습 매매**도 같은 헤더를 받는다. 출석은 서버가 하루 1번만 지급(동시 클릭도 1번).

## 8. 웹 보안·접근성 — React 로 옮길 때 지킬 것

**보안(서버 CSP 와 맞추기)** — 서버가 주는 화면에는 엄격한 CSP(스크립트 nonce · Trusted Types · 인라인 이벤트 핸들러 금지)가 걸려 있다.
1. 사용자·외부 데이터(뉴스 제목·채팅·아이디)를 **`innerHTML`/`dangerouslySetInnerHTML` 로 넣지 말 것** — React 의 기본 텍스트 렌더링을 쓴다.
2. 링크는 `http(s)` 만(서버가 이미 거른다) + `rel="noopener noreferrer"`.
3. 토큰·비밀번호를 `localStorage`·URL·로그에 남기지 말 것(쿠키 모드면 애초에 JS 가 토큰을 모른다).
4. 같은 출처에서 빌드 결과를 내보낼 때는 인라인 `<script>`·`onclick=` 없이 번들 파일만(서버 CSP 를 그대로 쓰려면).

**접근성(KWCAG 2.2 / WCAG 2.1 AA)** — 참조 구현은 axe-core 기준 화면 위반 0건.
1. **글자 대비 4.5:1 이상** — 보조 글자색은 `#5b6b80`(흰 배경 5.4:1). `#94a3b8`(2.6:1)·`opacity` 로 흐리게 한 숫자는 미달.
2. **필터 버튼 묶음** — `<div role="group" aria-label="정렬 기준">` + 버튼마다 `aria-pressed`. `role="tablist"` 는 탭패널·방향키까지 구현할 때만.
3. **클릭되는 건 `<button>`/`<a href>`** — `div onClick` 은 키보드로 못 쓴다.
4. **입력창 레이블** — placeholder 는 레이블이 아니다. `<label htmlFor>` 또는 `aria-label`.
5. 추가 인증·확인 창은 `role="dialog" aria-modal="true"` + 열릴 때 첫 입력에 포커스 + Esc 로 닫기.
6. 본문 속 링크는 색만으로 구분하지 말고 밑줄. 확인은 axe DevTools 또는 Lighthouse 접근성 점수.

## 9. 제거된 API (2026-10-20) — 화면에 없는 기능 정리

화면(스크린샷 기준 기능)에 쓰이지 않는 API 는 공격 표면을 줄이려고 지웠다(175 → 120개 경로. 모든 경로는 정책 동작을 선언해야 하고, 선언이 없으면 기본 거부 403).
옛 프론트 코드가 아래를 부르면 404 다.

| 제거 | 대신 |
|---|---|
| 커뮤니티(`/community/*`) · 시장지표/수급/증시자금/가상자산/투자유의/일정(`/market/*` 중 `/market/stocks` 외) · 분석(`/analytics/*`·`/models/*`·`/calendar`) | 없음(기능 기각) |
| 60초 챌린지(`/game/*`) · 단기 시뮬(`/moneysim/*`) · 아바타(`/avatar*`) · 레거시 모의투자(`/sim/*`·`/backtest`) | 장기 모의투자(`/invest*`) |
| `/realtime` · `/rankings/{type}` | `/market/stocks?sort=` |
| `/learn/terms` · `/learn/indicators` · `/learn/techniques` · `/learn/recent` | `/learn/dictionary` · `/learn/today` |
| `/picks/today` · `/themes` · `/news/tagged` | `/api/news/home` 의 `aiPicks`·`themes`·`latest` |
| `/search?q=`(하위호환) | `/search/suggest` |
| `/reco/catalog` · `/reco/top5/history` · `/whatif/{t}` · `/portfolio/analyze` · `/wallet/reconcile` | 화면 미사용(대사는 서버 원장 대사 작업 · 관리자 `/admin/ledger`) |
| 실습·게임의 `user_id` 파라미터 | 서버 발급 게스트 쿠키(2.3) |
